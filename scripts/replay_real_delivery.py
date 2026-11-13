"""Repeat real workbook workflows against an explicitly isolated TEST_GRID server.

Does not write source files, business data, or trial-current.json. Vehicle xlsx
fixtures are supplied by the caller; no private historical plates go to APIs.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from openpyxl import load_workbook
from app.config import ROOT
from scripts.logistics_source_data import extract
from scripts.stress_test_local import good, wait_job, validate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:8765')
    parser.add_argument('--vehicles',type=Path,required=True)
    parser.add_argument('--invalid-vehicles',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'docs/retest-20261112/historical-replay.json')
    parser.add_argument('--budget',type=int,default=4)
    parser.add_argument('--include-unassigned-as-hf',action='store_true',help='User-approved test rule: all originally unassigned source rows use HF')
    args=parser.parse_args()
    assert args.base.startswith('http://127.0.0.1:8765'), 'QA loopback endpoint only'
    report={'status':'RUNNING','road_source':'TEST_GRID','real_road_calls':0,'business_database_modified_by_replay':False}
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    save()
    with httpx.Client(base_url=args.base,trust_env=False,timeout=45) as c:
        assert good(c.get('/api/runtime'))['test_fixture'] is True
        auth=good(c.post('/api/auth/login',json={'username':'hardening','password':'Qa-Only-Hardening-2026!'}))
        c.headers['x-csrf-token']=auth['csrf_token']
        records,source=extract();days=defaultdict(list)
        if args.include_unassigned_as_hf:
            count=sum(r['warehouse_code'] is None for r in records)
            records=[dict(r,warehouse_code=r['warehouse_code'] or 'HF') for r in records]
            report['user_approved_unassigned_rows_to_HF']=count
        for row in records:
            if row['warehouse_code']:days[row['delivery_date']].append(row)
        original_hashes={s['path']:s['sha256'] for s in source['sources']}
        catalog=good(c.get('/api/master-data/management'))
        # Test bulk replacement in QA while immutable older snapshots retain
        # their original vehicle rows. No current business vehicle is touched.
        old=catalog['vehicles']
        good(c.post('/api/master-data/vehicles/bulk-delete',json={'items':[{'id':v['id'],'expected_revision':v['revision']} for v in old]}))
        def preview_file(path):
            return good(c.post('/api/master-data/imports/vehicles/preview',params={'filename':path.name},content=path.read_bytes()))
        invalid=preview_file(args.invalid_vehicles)
        assert not invalid['can_commit'] and invalid['errors']
        assert c.post('/api/master-data/imports/'+invalid['preview_id']+'/commit').status_code==422
        assert not good(c.get('/api/master-data/management'))['vehicles']
        valid=preview_file(args.vehicles);assert valid['can_commit'],valid.get('errors')
        receipt=good(c.post('/api/master-data/imports/'+valid['preview_id']+'/commit'))
        cars=good(c.get('/api/master-data/management'))['vehicles']
        assert len(cars)==50 and all(v['max_pieces']==400 and v['size']==2 for v in cars)
        assert {v['cartype'] for v in cars}=={0,1,2}
        same=preview_file(args.vehicles);assert same['can_commit']
        repeated=good(c.post('/api/master-data/imports/'+same['preview_id']+'/commit'))
        assert repeated['receipt']['unchanged']==50 and repeated['receipt']['created']==0
        report['vehicle_import']={'rows':50,'max_pieces':400,'chinese_type_choices':3,'invalid_batch_rolled_back':True,'repeat_unchanged':50,'receipt':receipt['receipt']}
        save();print(json.dumps({'vehicle_import':'PASS','rows':50}),flush=True)
        # Read and upload each unchanged original monthly workbook, explicitly
        # selecting its real header and business date rather than a generated CSV.
        all_days=defaultdict(list)
        for row in records:all_days[row['delivery_date']].append(row)
        raw=[]
        for src in source['sources']:
            path=Path(src['path']); eligible=[d for d,rs in all_days.items() if Path(rs[0]['source_file']).name==path.name and all(r['warehouse_code'] for r in rs)]
            day=max(eligible,key=lambda d:len(days[d]));content=path.read_bytes()
            info=good(c.post('/api/planning/demand-imports/inspect',params={'filename':path.name},content=content))
            uploaded=good(c.post('/api/planning/demand-imports/upload',params={'delivery_date':day,'filename':path.name,**info['suggested']},content=content))
            mapping={k:v for k,v in uploaded['suggested_mapping'].items() if k in ['store_name','pieces','store_code','delivery_date']}
            viewed=good(c.post('/api/planning/demand-imports/'+uploaded['id']+'/preview',json=mapping))
            assert viewed['result']['can_commit'],viewed['result']['errors'][:2]
            receipt=good(c.post('/api/planning/demand-imports/'+uploaded['id']+'/commit',json={'delivery_date':day,'preview_revision':1}))
            assert receipt['totals']['total_pieces']==sum(r['pieces'] for r in days[day])
            raw.append({'source':path.name,'sha256':src['sha256'],'date':day,'rows':viewed['result']['selected_date_row_count'],'pieces':receipt['totals']['total_pieces'],'header_row':info['suggested']['header_row'],'status':'PASS'})
        report['original_workbooks']=raw;save()
        replay=[];selected=None
        for index,(day,rows) in enumerate(sorted(days.items()),1):
            state=good(c.get('/api/planning/days/'+day))
            if state['latest_snapshot']['input_changed']:
                snapshot=good(c.post('/api/planning/days/'+day+'/snapshots',json={'expected_revision':state['revision'],'expected_fingerprint':state['input_fingerprint']}))
            else:snapshot=state['latest_snapshot']
            head=good(c.get('/api/planning/days/'+day+'/plans'))['head_version'];started=time.monotonic()
            job=good(c.post('/api/planning/jobs',json={'snapshot_id':snapshot['id'],'expected_head':head,'budget_seconds':args.budget}),202)
            done=wait_job(c,job);p=good(c.get('/api/planning/plans/'+done['plan_id']))
            validate(p,sum(r['pieces'] for r in rows),len({r['store_name'] for r in rows if r['pieces']}))
            exported=c.get('/api/planning/plans/'+p['id']+'/export.xlsx');assert exported.status_code==200
            book=load_workbook(BytesIO(exported.content),read_only=True);allocation=list(book['逐店分配'].values)[1:]
            assert len(allocation)==len(p['input']['tasks']) and sum(r[3] for r in allocation)==p['result']['check']['total_pieces'];book.close()
            replay.append({'date':day,'tasks':len(p['input']['tasks']),'pieces':p['result']['check']['total_pieces'],'used_vehicles':len(p['result']['routes']),'seconds':round(time.monotonic()-started,4),'stop_reason':p['result']['algorithm']['stop_reason'],'status':'PASS'})
            if selected is None or len(p['input']['tasks'])>len(selected['input']['tasks']):selected=p
            if index%10==0:
                report['replay_progress']={'completed':index,'total':len(days)};save();print(json.dumps(report['replay_progress']),flush=True)
        p=selected;draft={'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':list(reversed(r['task_ids']))} for r in p['result']['routes']],'common_ids':[r['task_id'] for r in p['result']['common']],'unassigned_ids':[]}
        head=good(c.get('/api/planning/days/'+p['delivery_date']+'/plans'))['head_version']
        j=good(c.post('/api/planning/jobs',json={'snapshot_id':p['snapshot_id'],'parent_id':p['id'],'expected_head':head,'kind':'MANUAL','draft':draft,'budget_seconds':10}),202)
        manual=good(c.get('/api/planning/plans/'+wait_job(c,j)['plan_id']));validate(manual,p['result']['check']['total_pieces'],len(p['input']['tasks']))
        assert [r['task_ids'] for r in manual['result']['routes']]==[r['task_ids'] for r in draft['routes']]
        assert c.post('/api/planning/plans/'+manual['id']+'/confirm',json={'expected_head':head+1}).status_code==422
        restored=good(c.post('/api/planning/plans/'+p['id']+'/restore',json={'expected_head':head+1}),201)
        assert restored['result']==p['result'] and restored['status']=='DRAFT'
        def reader(_):
            with httpx.Client(base_url=args.base,trust_env=False,timeout=30,cookies=c.cookies) as reader:
                out=[]
                for n in range(40):
                    route=['/api/master-data/management','/api/planning/days/'+p['delivery_date'],'/api/planning/days/'+p['delivery_date']+'/plans','/api/planning/plans/'+p['id']][n%4]
                    started=time.monotonic();r=reader.get(route);assert r.status_code==200;out.append(time.monotonic()-started)
                return out
        with ThreadPoolExecutor(max_workers=8) as pool:latencies=[v for part in pool.map(reader,range(8)) for v in part]
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in original_hashes.items())
        report.update(status='PASS',replay={'days':len(replay),'tasks':sum(r['tasks'] for r in replay),'pieces':sum(r['pieces'] for r in replay),'all_exports_checked':True,'samples':replay},manual_reverse_order=True,fixture_confirmation_blocked=True,history_restore=True,source_files_unchanged=True,concurrent_reads={'users':8,'operations':len(latencies),'p95_seconds':round(sorted(latencies)[math.ceil(.95*len(latencies))-1],4),'http_5xx':0})
        save();print(json.dumps({k:v for k,v in report.items() if k not in ['replay','original_workbooks']}),flush=True)


if __name__=='__main__':main()
