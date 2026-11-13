"""Real HTTP load and 150 historical-day replay against isolated QA only."""
from collections import Counter,defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal,ROUND_HALF_UP
from io import BytesIO
import json,math,time
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from openpyxl import load_workbook
from app.config import ROOT
from app.demand_imports import demand_template
from app.planning_inputs import content_hash
from scripts.logistics_source_data import extract,DEFAULT_SOURCE

BASE='http://127.0.0.1:8765'
PASSWORD='Qa-Only-Hardening-2026!'
TARGETS={'read_p95_seconds':2.0,'write_p95_seconds':3.0,'unexpected_http_5xx':0,'independent_coverage_failures':0}
OUT=ROOT/'docs/hardening'


def login(name):
    c=httpx.Client(base_url=BASE,trust_env=False,timeout=45)
    r=c.post('/api/auth/login',json={'username':name,'password':PASSWORD});assert r.status_code==200,r.text
    c.headers['X-CSRF-Token']=r.json()['csrf_token'];return c


def good(r,status=200):
    assert r.status_code==status,(r.status_code,r.text[:500]);return r.json()


def wait_job(c,j):
    until=time.monotonic()+45
    while time.monotonic()<until:
        value=good(c.get('/api/planning/jobs/'+j['id']))
        if value['status'] not in ['RUNNING','QUEUED']:
            assert value['status']=='COMPLETED',value
            return value
        time.sleep(.025)
    raise AssertionError('job_timeout')


def validate(plan,expected_pieces,expected_tasks):
    result=plan['result'];assert result['check']['valid'] and not result['confirmable'] and not result['unassigned']
    assert result['execution_scope']=='TEST_ONLY' and result['road_source']=='FIXTURE'
    assert result['algorithm']['road_stats']['calls']==0
    seen=Counter();cars=Counter();total=Decimal(0);pieces=0
    stores={s['id']:s for s in plan['input']['stores']};tasks={t['store_id']:t for t in plan['input']['tasks']}
    vehicles={v['id']:v for v in plan['input']['vehicles']}
    for route in result['routes']:
        cars[route['vehicle_id']]+=1;seen.update(route['task_ids']);v=vehicles[route['vehicle_id']]
        amount=sum(tasks[i]['pieces'] for i in route['task_ids']);assert amount<=400 and route['pieces']==amount
        assert all(tasks[i]['warehouse_id']==v['day_warehouse_id'] for i in route['task_ids'])
        fee=(Decimal(route['meters'])/1000*Decimal('5.8')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        assert fee==Decimal(route['fee']);total+=fee;pieces+=amount
    for common in result['common']:
        seen[common['task_id']]+=1;pieces+=common['pieces'];total+=Decimal(common['fee'])
    assert set(seen)==set(tasks) and all(count==1 for count in seen.values())
    assert all(count==1 for count in cars.values()) and len(cars)<=50
    assert len(tasks)==expected_tasks and pieces==expected_pieces and total==Decimal(result['total_fee'])
    assert plan['input_hash']==content_hash(plan['input'])


def main():
    OUT.mkdir(exist_ok=True);(OUT/'load-protocol.json').write_text(json.dumps({'frozen_before_run':True,'targets':TARGETS,'concurrency':12,'operations_per_user':200,'roads':'TEST_GRID only','real_api_calls':0},indent=2))
    c=login('hardening');assert good(c.get('/api/runtime'))['test_fixture']
    records,source=extract();eligible=[r for r in records if r['warehouse_code']]
    days=defaultdict(list)
    for r in eligible:days[r['delivery_date']].append(r)
    all_days=defaultdict(list)
    for r in records:all_days[r['delivery_date']].append(r)
    raw_checks=[]
    # Import original raw monthly files (not generated templates) for each month.
    for file in source['sources']:
        path=Path(file['path']);valid_days=[d for d,rs in all_days.items() if Path(rs[0]['source_file']).name==path.name and all(r['warehouse_code'] for r in rs)]
        day=max(valid_days,key=lambda d:len(days[d]));data=path.read_bytes();started=time.perf_counter()
        info=good(c.post('/api/planning/demand-imports/inspect',params={'filename':path.name},content=data))
        selected=info['suggested'];u=good(c.post('/api/planning/demand-imports/upload',params={'delivery_date':day,'filename':path.name,**selected},content=data))
        mapping={k:v for k,v in u['suggested_mapping'].items() if k in ['store_name','pieces','store_code','delivery_date']}
        preview=good(c.post('/api/planning/demand-imports/'+u['id']+'/preview',json=mapping))
        assert preview['result']['can_commit'],preview['result']['errors'][:5]
        receipt=good(c.post('/api/planning/demand-imports/'+u['id']+'/commit',json={'delivery_date':day,'preview_revision':1}))
        assert receipt['totals']['total_pieces']==sum(r['pieces'] for r in days[day])
        raw_checks.append({'file':path.name,'date':day,'source_sha256':file['sha256'],'rows':preview['result']['selected_date_row_count'],'pieces':receipt['totals']['total_pieces'],'other_dates':preview['result']['other_date_row_count'],'ignored_template_rows':preview['result']['ignored_nonbusiness_row_count'],'seconds':round(time.perf_counter()-started,4),'status':'PASS'})
    replay=[];selected_plan=None
    for number,(day,rows) in enumerate(sorted(days.items()),1):
        before=good(c.get('/api/planning/days/'+day));snap=before['latest_snapshot'];assert not snap['input_changed']
        versions=good(c.get('/api/planning/days/'+day+'/plans'));started=time.perf_counter()
        job=good(c.post('/api/planning/jobs',json={'snapshot_id':snap['id'],'expected_head':versions['head_version'],'budget_seconds':5}),202)
        done=wait_job(c,job);p=good(c.get('/api/planning/plans/'+done['plan_id']))
        validate(p,sum(r['pieces'] for r in rows),len({r['store_name'] for r in rows if r['pieces']}))
        excel=c.get('/api/planning/plans/'+p['id']+'/export.xlsx');assert excel.status_code==200
        wb=load_workbook(BytesIO(excel.content),read_only=True);allocations=list(wb['逐店分配'].values)[1:]
        assert sum(r[3] for r in allocations)==sum(r['pieces'] for r in rows) and len(allocations)==len(p['input']['tasks'])
        assert dict(wb['版本与汇总'].values)['总费用（元）']==p['result']['total_fee'];wb.close()
        replay.append({'date':day,'tasks':len(p['input']['tasks']),'pieces':sum(r['pieces'] for r in rows),'seconds':round(time.perf_counter()-started,4),'used_vehicles':p['result']['check']['used_vehicles'],'status':'PASS','stop_reason':p['result']['algorithm']['stop_reason']})
        if selected_plan is None or len(p['input']['tasks'])>len(selected_plan['input']['tasks']):selected_plan=p
        if number%10==0:print(json.dumps({'replayed_days':number,'of':len(days),'last_seconds':replay[-1]['seconds']}),flush=True)
    p=selected_plan;head=good(c.get('/api/planning/days/'+p['delivery_date']+'/plans'))['head_version']
    draft={'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':list(reversed(r['task_ids']))} for r in p['result']['routes']],'common_ids':[],'unassigned_ids':[]}
    job=good(c.post('/api/planning/jobs',json={'snapshot_id':p['snapshot_id'],'expected_head':head,'parent_id':p['id'],'kind':'MANUAL','draft':draft,'budget_seconds':10}),202)
    manual=good(c.get('/api/planning/plans/'+wait_job(c,job)['plan_id']));validate(manual,p['result']['check']['total_pieces'],len(p['input']['tasks']))
    assert [r['task_ids'] for r in manual['result']['routes']]==[r['task_ids'] for r in draft['routes']]
    # Maximum permitted batch: 10,000 rows, repeated stores aggregate into whole tickets.
    stores=good(c.get('/api/master-data/management'))['stores'];wb=load_workbook(BytesIO(demand_template()));ws=wb['数据']
    for i in range(10000):s=stores[i%len(stores)];ws.append([s['name'],1,s['code']])
    stream=BytesIO();wb.save(stream);wb.close();started=time.perf_counter()
    u=good(c.post('/api/planning/demand-imports/upload?delivery_date=2026-11-11&filename=max-10000.xlsx',content=stream.getvalue()))
    pr=good(c.post('/api/planning/demand-imports/'+u['id']+'/preview',json={'store_name':0,'pieces':1,'store_code':2}));assert pr['result']['can_commit']
    receipt=good(c.post('/api/planning/demand-imports/'+u['id']+'/commit',json={'delivery_date':'2026-11-11','preview_revision':1}));assert receipt['totals']['total_pieces']==10000
    max_batch_seconds=round(time.perf_counter()-started,4)
    shared=good(c.post('/api/workspace/records',json={'title':'pressure-shared','content':'并发修订检查'}),201)
    clients=[login(f'stress{i:02}') for i in range(1,13)]
    def worker(index):
        client=clients[index];samples=[]
        for n in range(200):
            kind=n%5;started=time.perf_counter();method='read'
            if kind==0:r=client.get('/api/master-data/catalog?delivery_date='+p['delivery_date'])
            elif kind==1:r=client.get('/api/planning/days/'+p['delivery_date'])
            elif kind==2:r=client.get('/api/planning/days/'+p['delivery_date']+'/plans')
            elif kind==3:
                method='write';r=client.post('/api/workspace/records',json={'title':f'压力新增 {index}-{n}','content':'独立临时库记录'})
            else:
                current=good(client.get('/api/workspace/records?q=pressure-shared'))['items'][0]
                method='write';r=client.patch('/api/workspace/records/'+shared['id'],json={'title':'pressure-shared','content':f'压力更新{index}-{n}','expected_revision':current['revision']})
            samples.append({'method':method,'seconds':round(time.perf_counter()-started,6),'status':r.status_code,'operation':kind})
        client.close();return samples
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=12) as pool:samples=[sample for part in pool.map(worker,range(12)) for sample in part]
    elapsed=time.perf_counter()-started
    statuses=Counter(s['status'] for s in samples);assert all(code in [200,201,409] for code in statuses),statuses
    applied=sum(s['operation']==4 and s['status']==200 for s in samples)
    final=good(c.get('/api/workspace/records?q=pressure-shared'))['items'][0];assert final['revision']==1+applied
    def p95(values):return sorted(values)[math.ceil(.95*len(values))-1]
    latency={method:{'count':sum(s['method']==method for s in samples),'p95_seconds':p95([s['seconds'] for s in samples if s['method']==method]),'max_seconds':max(s['seconds'] for s in samples if s['method']==method)} for method in ['read','write']}
    passed=latency['read']['p95_seconds']<=TARGETS['read_p95_seconds'] and latency['write']['p95_seconds']<=TARGETS['write_p95_seconds']
    report={'status':'PASS' if passed else 'PERFORMANCE_TARGET_FAILED','roads':'NON_GEOGRAPHIC_TEST_GRID','real_api_calls':0,'real_business_database_modified':False,
     'raw_workbook_checks':raw_checks,'historical_replay':{'days':len(replay),'tasks':sum(r['tasks'] for r in replay),'pieces':sum(r['pieces'] for r in replay),'end_to_end_p95_seconds':p95([r['seconds'] for r in replay]),'all_exports_checked':True,'samples':replay},
     'manual_reverse_order_preserved':True,'max_batch':{'rows':10000,'tasks':receipt['totals']['task_count'],'pieces':10000,'seconds':max_batch_seconds,'status':'PASS'},
     'concurrency':{'users':12,'measured_operations':len(samples),'extra_read_modify_reads':480,'elapsed_seconds':round(elapsed,4),'statuses':dict(statuses),'latency':latency,'shared_revision_updates':applied,'lost_updates':0,'targets':TARGETS,'samples':samples}}
    (OUT/'stress-test.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in report.items() if k not in ['historical_replay','concurrency']},ensure_ascii=False),flush=True);print(json.dumps({'latency':latency,'status':report['status']}),flush=True)
    c.close()


if __name__=='__main__':main()
