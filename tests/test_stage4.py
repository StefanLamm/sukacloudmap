from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
import json
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
import pytest
from sqlalchemy import select

from app.database import make_engine, migrate, session_factory
from app.main import create_app
from app.models import Account, Base, DailyDemand, DemandDay, DemandImport, InputSnapshot, QuoteVersion, Store, SystemState, Vehicle, Warehouse
from app.planning_inputs import content_hash
from conftest import ADMIN_PASSWORD, USER_PASSWORD, create_member, login
from test_stage3 import warehouse, quote, post, store_payload, vehicle_payload

DAY='2026-11-11'


def workbook(rows, headers=None):
    wb=Workbook();ws=wb.active;ws.title='数据';ws.append(headers or ['门店名称','件数','门店编号'])
    for row in rows:ws.append(row)
    stream=BytesIO();wb.save(stream);wb.close();return stream.getvalue()


def uploaded(c, rows, day=DAY, headers=None):
    r=c.post('/api/planning/demand-imports/upload?delivery_date='+day+'&filename=demands.xlsx',content=workbook(rows,headers))
    assert r.status_code==200,r.text;return r.json()


def previewed(c, rows, day=DAY, headers=None, mapping=None):
    u=uploaded(c,rows,day,headers)
    r=c.post('/api/planning/demand-imports/'+u['id']+'/preview',json=mapping or {'store_name':0,'pieces':1,'store_code':2})
    assert r.status_code==200,r.text;return r.json()


def commit(c,p,**changes):
    return c.post('/api/planning/demand-imports/'+p['id']+'/commit',json={'delivery_date':p['delivery_date'],'preview_revision':p['preview_revision'],**changes})


def day_state(c,day=DAY):
    r=c.get('/api/planning/days/'+day);assert r.status_code==200,r.text;return r.json()


def detail(c,id):
    r=c.get('/api/planning/snapshots/'+id);assert r.status_code==200,r.text;return r.json()


@pytest.fixture
def basis(client):
    login(client);w=warehouse(client);q=quote(client);s=post(client,'stores',store_payload(w,q,name='一店'));s2=post(client,'stores',store_payload(w,q,'S02',name='二店'))
    v=post(client,'vehicles',vehicle_payload(w))
    return client,w,q,s,s2,v


@pytest.mark.parametrize('path',['/api/planning/demand-template.xlsx','/api/planning/days/'+DAY,'/api/planning/days/'+DAY+'/snapshots','/api/planning/snapshots/missing','/api/planning/demand-imports/missing'])
def test_stage4_requires_authentication(client,path):
    assert client.get(path).status_code==401


def test_business_accounts_share_daily_demands_and_snapshots(basis,app):
    c,w,q,s,s2,v=basis;create_member(c)
    with TestClient(app) as business:
        login(business,'member',USER_PASSWORD)
        p=previewed(business,[[s['name'],2,s['code']]])
        result=commit(business,p);assert result.status_code==200,result.text
        assert day_state(c)['totals']['total_pieces']==2
        assert detail(c,result.json()['snapshot_id'])['input']['tasks'][0]['store_id']==s['id']
        business.headers.pop('x-csrf-token')
        assert business.post('/api/planning/demand-imports/'+p['id']+'/preview',json={'store_name':0,'pieces':1}).status_code==403


def test_template_has_only_daily_fields_and_no_sample_demand(client):
    login(client);r=client.get('/api/planning/demand-template.xlsx');assert r.status_code==200
    wb=load_workbook(BytesIO(r.content));ws=wb['数据'];assert [c.value for c in ws[1]]==['门店名称','件数','门店编号'];assert ws.max_row==1
    wb.close()


def test_column_mapping_custom_names_and_duplicate_columns(basis):
    c,w,q,s,s2,v=basis
    u=uploaded(c,[[2,'ignore',s['name'],s['code']]],headers=['货量','备注','客户','店编码'])
    assert u['suggested_mapping']=={'store_name':None,'pieces':None,'store_code':None}
    r=c.post('/api/planning/demand-imports/'+u['id']+'/preview',json={'store_name':2,'pieces':0,'store_code':3})
    assert r.status_code==200 and r.json()['result']['after']['total_pieces']==2
    for mapping in [{'store_name':0,'pieces':0},{'store_name':10,'pieces':1},{'store_name':True,'pieces':1},{'pieces':1}]:
        assert c.post('/api/planning/demand-imports/'+u['id']+'/preview',json=mapping).status_code==422
    u=uploaded(c,[[s['name'],s['name'],3]],headers=['门店名称','门店名称','件数'])
    assert u['suggested_mapping']['store_name'] is None


def test_duplicate_aggregation_zero_skip_blank_rows_and_source_trace(basis):
    c,w,q,s,s2,v=basis
    p=previewed(c,[[s['name'],2,s['code']],[None,None,None],[s['name'],3,s['code']],[s2['name'],0,s2['code']]])
    r=p['result'];assert r['can_commit'] and r['after']['task_count']==1 and r['after']['total_pieces']==5
    assert r['merged_row_count']==1 and r['zero_row_count']==1 and r['blank_rows']==1
    assert [x['row'] for x in r['items'][0]['source_rows']]==[2,4]
    assert r['zero_rows'][0]['row']==5
    assert day_state(c)['revision']==0
    result=commit(c,p);assert result.status_code==200,result.text
    d=day_state(c);assert len(d['items'])==1 and d['items'][0]['pieces']==5
    body=detail(c,result.json()['snapshot_id'])['input']
    assert body['demand_batch']['file_hash']==p['file_hash']
    assert [r['row'] for r in body['tasks'][0]['source_rows']]==[2,4]


@pytest.mark.parametrize('quantity',[-1,1.5,True,None,'','1.5','1e3','NaN',1_000_000_001])
def test_invalid_pieces_rejected_without_partial_replacement(basis,quantity):
    c,w,q,s,s2,v=basis
    initial=commit(c,previewed(c,[[s['name'],7,s['code']]]));assert initial.status_code==200
    p=previewed(c,[[s['name'],2,s['code']],[s2['name'],quantity,s2['code']]])
    assert p['result']['error_count'] and p['result']['errors'][0]['row']==3
    assert commit(c,p).status_code==422
    assert day_state(c)['totals']['total_pieces']==7 and day_state(c)['revision']==1
    assert day_state(c)['snapshot_count']==1


@pytest.mark.parametrize('row,field',[[['未知店',1,None],'门店名称'],[['一店',1,'NOPE'],'门店编号'],[['错误名称',1,'S01'],'门店名称'],[['一店',1,1],'门店编号'],[['未知店',0,None],'门店名称']])
def test_unknown_mismatch_and_numeric_codes_never_guess(basis,row,field):
    c,w,q,s,s2,v=basis;p=previewed(c,[row]);assert not p['result']['can_commit'];assert p['result']['errors'][0]['field']==field
    assert commit(c,p).status_code==422 and day_state(c)['revision']==0


def test_duplicate_names_require_code_and_codes_preserve_zeroes(basis):
    c,w,q,s,s2,v=basis
    third=post(c,'stores',store_payload(w,q,'0001',name=s['name']))
    ambiguous=previewed(c,[[s['name'],2,None]]);assert not ambiguous['result']['can_commit']
    good=previewed(c,[[s['name'],2,' 0001 '],[s2['name'],1,'  '],[None,3,'0001']])
    assert good['result']['can_commit'];assert commit(c,good).status_code==200
    rows=day_state(c)['items'];assert rows[0]['store_id']==third['id'] and rows[0]['pieces']==5
    assert rows[1]['store_id']==s2['id']


def test_name_only_matching_is_valid(basis):
    c,w,q,s,s2,v=basis
    u=uploaded(c,[[s['name'],2.0]],headers=['门店','数量'])
    assert u['suggested_mapping']=={'store_name':0,'pieces':1,'store_code':None}
    r=c.post('/api/planning/demand-imports/'+u['id']+'/preview',json={'store_name':0,'pieces':1});assert r.status_code==200
    assert commit(c,r.json()).status_code==200


def test_missing_header_does_not_silently_drop_the_first_demand(basis):
    c,w,q,s,s2,v=basis
    # An xlsx without a text header must not consume the first demand as labels.
    r=c.post('/api/planning/demand-imports/upload?delivery_date='+DAY+'&filename=d.xlsx',
             content=workbook([[s2['name'],3,s2['code']]],headers=[s['name'],2,s['code']]))
    assert r.status_code==422 and r.json()['code']=='DEMAND_HEADERS'
    assert day_state(c)['revision']==0


def test_reimport_replaces_the_day_instead_of_accumulating_and_retains_history(basis):
    c,w,q,s,s2,v=basis
    p=previewed(c,[[s['name'],2,s['code']]]);first=commit(c,p).json()
    old=detail(c,first['snapshot_id'])['input']
    second=commit(c,previewed(c,[[s['name'],2,s['code']]])).json();assert second['revision']==2
    p3=previewed(c,[[s['name'],4,s['code']],[s2['name'],1,s2['code']]])
    assert p3['result']['before']['task_count']==1 and p3['result']['before']['total_pieces']==2
    assert p3['result']['after']['task_count']==2 and p3['result']['after']['total_pieces']==5
    assert commit(c,p3).status_code==200
    assert day_state(c)['totals']['total_pieces']==5 and day_state(c)['snapshot_count']==3
    assert commit(c,p).json()==first
    assert day_state(c)['revision']==3 and day_state(c)['totals']['total_pieces']==5
    assert detail(c,first['snapshot_id'])['input']==old and detail(c,first['snapshot_id'])['input_changed']


def test_wrong_date_preview_revision_and_competing_imports_are_rejected(basis):
    c,w,q,s,s2,v=basis;p=previewed(c,[[s['name'],2,s['code']]])
    assert commit(c,p,delivery_date='2026-11-12').status_code==409
    assert commit(c,p,preview_revision=2).status_code==409
    competing=previewed(c,[[s2['name'],3,s2['code']]])
    assert commit(c,p).status_code==200
    assert commit(c,competing).status_code==409
    assert day_state(c)['totals']['total_pieces']==2 and day_state(c,'2026-11-12')['revision']==0
    assert c.post('/api/planning/demand-imports/'+p['id']+'/preview',json={'store_name':0,'pieces':1}).status_code==409


@pytest.mark.parametrize('change',['master','warehouse_day','vehicle_day','store_day'])
def test_preview_bound_to_master_and_selected_day_revisions(basis,change):
    c,w,q,s,s2,v=basis;p=previewed(c,[[s['name'],2,s['code']]])
    if change=='master':r=c.patch('/api/stores/'+s['id'],json={'expected_revision':1,'address':'变更地址'})
    elif change=='warehouse_day':r=c.put('/api/warehouses/'+w['id']+'/days/'+DAY,json={'expected_revision':0,'departure_time':'09:00'})
    elif change=='vehicle_day':r=c.put('/api/vehicles/'+v['id']+'/days/'+DAY,json={'expected_revision':0,'warehouse_id':w['id'],'available':False})
    else:r=c.put('/api/planning/days/'+DAY+'/stores/'+s['id'],json={'expected_revision':0,'latest_arrival_at':DAY+'T11:00:00+08:00'})
    assert r.status_code==200,r.text;assert commit(c,p).status_code==409
    assert day_state(c)['revision']==0


def test_unrelated_day_and_account_changes_do_not_stale_preview(basis):
    c,w,q,s,s2,v=basis;p=previewed(c,[[s['name'],2,s['code']]])
    assert c.put('/api/vehicles/'+v['id']+'/days/2026-11-12',json={'expected_revision':0,'warehouse_id':w['id'],'available':False}).status_code==200
    create_member(c)
    assert commit(c,p).status_code==200


def test_all_zero_file_can_clear_a_day_but_empty_file_cannot(basis):
    c,w,q,s,s2,v=basis;assert commit(c,previewed(c,[[s['name'],2,s['code']]])).status_code==200
    blank=previewed(c,[]);assert not blank['result']['can_commit'] and commit(c,blank).status_code==422
    zero=previewed(c,[[s['name'],0,s['code']]]);assert zero['result']['can_commit'] and zero['result']['after']['task_count']==0
    assert zero['result']['changes']['removed']==1 and commit(c,zero).status_code==200
    assert day_state(c)['totals']['total_pieces']==0 and day_state(c)['revision']==2 and day_state(c)['snapshot_count']==2


def test_missing_prices_coordinates_and_profiles_are_warnings_not_import_errors(basis):
    c,w,q,s,s2,v=basis
    assert c.patch('/api/stores/'+s['id'],json={'expected_revision':1,'longitude':None,'latitude':None,'dedicated_quote_id':None,'common_unit_price':None}).status_code==200
    p=previewed(c,[[s['name'],2,s['code']]]);assert p['result']['can_commit'] and p['result']['configuration_warning_count']==1
    assert '未配置共配单价' in p['result']['items'][0]['configuration_warnings']
    receipt=commit(c,p).json();body=detail(c,receipt['snapshot_id'])['input']
    assert body['stores'][0]['common_unit_price'] is None and body['quotes']==[] and body['road_status']=='NOT_VERIFIED'


def test_snapshot_freezes_prices_time_windows_vehicles_and_provenance(basis):
    c,w,q,s,s2,v=basis
    assert c.patch('/api/stores/'+s['id'],json={'expected_revision':1,'business_open':'20:00','business_close':'02:00','close_day_offset':1,'common_unit_price':'0'}).status_code==200
    assert c.put('/api/warehouses/'+w['id']+'/days/'+DAY,json={'departure_time':'19:00','expected_revision':0}).status_code==200
    assert c.put('/api/planning/days/'+DAY+'/stores/'+s['id'],json={'latest_arrival_at':'2026-11-12T01:30:00+08:00','expected_revision':0}).status_code==200
    receipt=commit(c,previewed(c,[[s['name'],2,s['code']]])).json();saved=detail(c,receipt['snapshot_id']);body=saved['input']
    assert body['stores'][0]['business_open_at']==DAY+'T20:00:00+08:00'
    assert body['stores'][0]['business_close_at']=='2026-11-12T02:00:00+08:00'
    assert body['stores'][0]['latest_arrival_at']=='2026-11-12T01:30:00+08:00'
    assert body['warehouses'][0]['earliest_departure_at']==DAY+'T19:00:00+08:00'
    assert body['stores'][0]['common_unit_price']=='0' and body['quotes'][0]['per_km']=='2'
    assert body['vehicles'][0]['cartype']==0 and body['vehicles'][0]['day_available'] is True
    assert content_hash(body)==saved['content_hash'] and body['demand_batch']['id']==receipt['import_id']
    assert not any(key in json.dumps(body) for key in ['password_hash','csrf_token','amap_web_service_key'])


def test_old_snapshots_immutable_after_rewarehouse_reprice_and_new_freeze(basis):
    c,w,q,s,s2,v=basis;receipt=commit(c,previewed(c,[[s['name'],2,s['code']]])).json();old=detail(c,receipt['snapshot_id'])
    w2=warehouse(c,'W02')
    assert c.patch('/api/stores/'+s['id'],json={'warehouse_id':w2['id'],'expected_revision':1}).status_code==200
    assert c.patch('/api/quotes/'+q['id'],json={'per_km':'3','expected_revision':1,'effective_from':DAY}).status_code==200
    stale=detail(c,receipt['snapshot_id']);assert stale['input_changed'] and stale['input']==old['input']
    current=day_state(c);assert current['items'][0]['warehouse_id']==w2['id'] and current['latest_snapshot']['input_changed']
    r=c.post('/api/planning/days/'+DAY+'/snapshots',json={'expected_revision':current['revision'],'expected_fingerprint':current['input_fingerprint']})
    assert r.status_code==200,r.text;new=detail(c,r.json()['id'])
    assert new['version']==2 and not new['input_changed'] and new['input']['tasks'][0]['warehouse_id']==w2['id']
    assert new['input']['quotes'][0]['per_km']=='3'
    assert detail(c,receipt['snapshot_id'])['content_hash']==old['content_hash']
    again=c.post('/api/planning/days/'+DAY+'/snapshots',json={'expected_revision':current['revision'],'expected_fingerprint':current['input_fingerprint']})
    assert again.status_code==200 and again.json()['id']==new['id']
    history=c.get('/api/planning/days/'+DAY+'/snapshots?limit=1').json();assert history['total']==2 and history['items'][0]['id']==new['id']
    export=c.get('/api/planning/snapshots/'+receipt['snapshot_id']+'/export.json');assert export.status_code==200 and export.json()['input']==old['input']


def test_freeze_rejects_inputs_changed_after_read(basis):
    c,w,q,s,s2,v=basis;commit(c,previewed(c,[[s['name'],2,s['code']]]));d=day_state(c)
    assert c.put('/api/vehicles/'+v['id']+'/days/'+DAY,json={'expected_revision':0,'warehouse_id':w['id'],'available':False}).status_code==200
    assert c.post('/api/planning/days/'+DAY+'/snapshots',json={'expected_revision':d['revision'],'expected_fingerprint':d['input_fingerprint']}).status_code==409
    assert '车辆归仓或可用性已变化' in day_state(c)['latest_snapshot']['changed_reasons']


def test_per_day_deadline_validation_and_clearing_do_not_change_master(basis):
    c,w,q,s,s2,v=basis;path='/api/planning/days/'+DAY+'/stores/'+s['id']
    assert c.put(path,json={'expected_revision':0,'latest_arrival_at':DAY+'T08:00:00'}).status_code==422
    assert c.put(path,json={'expected_revision':0,'latest_arrival_at':'2026-11-13T08:00:00+08:00'}).status_code==422
    r=c.put(path,json={'expected_revision':0,'latest_arrival_at':DAY+'T02:00:00Z'});assert r.status_code==200 and r.json()['latest_arrival_at']==DAY+'T10:00:00+08:00'
    assert c.put(path,json={'expected_revision':0,'latest_arrival_at':None}).status_code==409
    assert c.put(path,json={'expected_revision':1,'latest_arrival_at':None}).status_code==200
    store=c.get('/api/master-data/catalog?delivery_date='+DAY).json()['stores'][0];assert store['latest_arrival_at']==DAY+'T18:00:00+08:00' and store['latest_arrival_source']=='BUSINESS_CLOSE' and store['day_revision']==2
    master=c.get('/api/master-data/management').json()['stores'][0];assert master['latest_arrival_at']==DAY+'T09:00:00+08:00'
    other=c.get('/api/master-data/catalog?delivery_date=2026-11-13').json()['stores'][0];assert other['latest_arrival_at']=='2026-11-13T18:00:00+08:00'


def test_import_failure_after_deleting_old_rows_rolls_back(basis,app,monkeypatch):
    c,w,q,s,s2,v=basis;commit(c,previewed(c,[[s['name'],7,s['code']]]));p=previewed(c,[[s2['name'],3,s2['code']]])
    import app.demand_imports as service
    def fail(*args,**kwargs):raise RuntimeError('injected failure')
    monkeypatch.setattr(service,'freeze',fail)
    r=commit(c,p);assert r.status_code==500
    current=day_state(c);assert current['revision']==1 and current['totals']['total_pieces']==7 and current['snapshot_count']==1
    with app.state.sessions() as db:assert db.get(DemandImport,p['id']).status=='PREVIEW'


def test_repreview_version_expiry_and_snapshot_integrity(basis,app):
    c,w,q,s,s2,v=basis;p=previewed(c,[[s['name'],2,s['code']]])
    r=c.post('/api/planning/demand-imports/'+p['id']+'/preview',json={'store_name':0,'pieces':1,'store_code':2});assert r.status_code==200
    assert commit(c,p).status_code==409
    p=r.json()
    with app.state.sessions() as db:db.get(DemandImport,p['id']).previewed_at=1;db.commit()
    assert commit(c,p).status_code==409
    receipt=commit(c,previewed(c,[[s['name'],2,s['code']]])).json()
    with app.state.sessions() as db:
        snap=db.get(InputSnapshot,receipt['snapshot_id']);body=json.loads(snap.content);body['totals']['total_pieces']=999;snap.content=json.dumps(body);db.commit()
    assert c.get('/api/planning/snapshots/'+receipt['snapshot_id']).status_code==500


def test_formulas_links_bad_files_and_size_guard(basis,monkeypatch):
    c,w,q,s,s2,v=basis
    p=previewed(c,[[s['name'],'=1+1',s['code']]]);assert not p['result']['can_commit'] and commit(c,p).status_code==422
    assert c.post('/api/planning/demand-imports/upload?delivery_date='+DAY+'&filename=d.xlsx',content=b'not xlsx').status_code==422
    assert c.post('/api/planning/demand-imports/upload?delivery_date=bad&filename=d.xlsx',content=b'x').status_code==422
    import app.planning_api as routes
    monkeypatch.setattr(routes,'MAX_FILE',100)
    assert c.post('/api/planning/demand-imports/upload?delivery_date='+DAY+'&filename=d.xlsx',content=b'x'*101).status_code==413


def test_disabled_account_during_upload_cannot_save_batch(basis,app):
    c,w,q,s,s2,v=basis;member=create_member(c);content=workbook([[s['name'],2,s['code']]])
    with TestClient(app) as business:
        login(business,'member',USER_PASSWORD)
        def chunks():
            yield content[:100]
            assert c.patch('/api/accounts/'+member['id'],json={'active':False,'expected_revision':1}).status_code==200
            yield content[100:]
        assert business.post('/api/planning/demand-imports/upload?delivery_date='+DAY+'&filename=d.xlsx',content=chunks()).status_code==401
    with app.state.sessions() as db:assert list(db.scalars(select(DemandImport)))==[]


def test_two_concurrent_commits_only_replace_once(basis,app):
    c,w,q,s,s2,v=basis;p=previewed(c,[[s['name'],2,s['code']]]);other=previewed(c,[[s2['name'],3,s2['code']]])
    with TestClient(app) as peer:
        login(peer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one=pool.submit(commit,c,p);two=pool.submit(commit,peer,other);statuses=sorted([one.result().status_code,two.result().status_code])
        assert statuses==[200,409]
        assert day_state(c)['revision']==1 and day_state(c)['snapshot_count']==1


def scale_snapshot(client,app):
    login(client)
    source=json.loads((Path(__file__).resolve().parents[1]/'产品交付/第一阶段需求与开发交接/规模固定数据集.json').read_text())
    assert source['api_use_prohibited'] is True
    from app.master_service import normalize,signature
    with app.state.sessions() as db:
        actor=db.scalar(select(Account).where(Account.username=='admin'))
        for w in source['warehouses']:
            db.add(Warehouse(id=w['id'],code=w['id'],name=w['name'],address='固定测试地址',longitude=None,latitude=None,crs='GCJ02',location_source='FIXTURE，无真实位置',departure_time='08:00'))
        for code,params in source['quotes'].items():
            values=normalize('quotes',{'code':code,'name':code,**params,'effective_from':DAY})
            from app.models import Quote
            db.add(Quote(id=code,code=code,name=code));db.flush()
            db.add(QuoteVersion(quote_id=code,version=1,signature=signature(values),created_by=actor.id,**{k:values[k] for k in ['name','mode','per_km','base_price','included_stops','extra_stop_price','effective_from']}))
        db.flush()
        for s in source['stores']:
            db.add(Store(id=s['id'],code=s['id'],name=s['name'],city=s['city'],address='固定测试地址',longitude=None,latitude=None,crs='GCJ02',location_source='FIXTURE，无真实位置',warehouse_id=s['warehouse_id'],business_open='08:00',business_close='10:00',close_day_offset=0,unload_minutes=s['unload_minutes'],latest_arrival_at=s['latest_arrival_at'],dedicated_quote_id=s['quote_id'],common_unit_price=s['common_unit_price']))
        for v in source['vehicles']:
            db.add(Vehicle(id=v['id'],plate=v['plate'],max_pieces=v['max_pieces'],warehouse_id=v['warehouse_id'],available=v['available']))
        db.get(SystemState,1).master_revision+=1;db.commit()
    names={s['id']:s['name'] for s in source['stores']}
    rows=[[names[d['store_id']],d['pieces'],d['store_id']] for d in source['demand']]
    p=previewed(client,rows);assert p['result']['can_commit'] and p['result']['after']['task_count']==800 and p['result']['after']['total_pieces']==24000
    assert all(w['task_count']==200 and w['total_pieces']==6000 for w in p['result']['after']['warehouses'])
    receipt=commit(client,p);assert receipt.status_code==200,receipt.text
    state=day_state(client);assert len(state['items'])==800 and state['totals']['total_pieces']==24000
    snapshot=detail(client,receipt.json()['snapshot_id']);body=snapshot['input']
    assert len(body['tasks'])==800 and len(body['vehicles'])==50 and len(body['warehouses'])==4
    assert sum(t['pieces'] for t in body['tasks'])==24000
    assert [s['source_rows'][0]['row'] for s in body['tasks']]==list(range(2,802))
    assert body['road_status']=='NOT_VERIFIED' and all(v['road_verified'] is False for v in body['vehicles'])
    return snapshot


def test_fixed_4_warehouse_800_store_24000_piece_50_vehicle_input(client,app):
    scale_snapshot(client,app)


def test_upgrade_from_stage3_preserves_master_data_and_schema(settings):
    from app.security import hash_password
    engine=make_engine(settings);migrate(engine,'0002_master_data')
    # Seed the historical schema itself: current ORM classes contain columns
    # introduced after this migration and cannot represent an old installation.
    from sqlalchemy import MetaData, Table
    with engine.begin() as connection:
        metadata=MetaData()
        accounts=Table('accounts',metadata,autoload_with=connection)
        warehouses=Table('warehouses',metadata,autoload_with=connection)
        instant=__import__('time').time_ns()//1000000000
        connection.execute(accounts.insert().values(id='A1',username='admin',display_name='原管理员',is_admin=True,active=True,
            password_hash=hash_password(ADMIN_PASSWORD),auth_revision=1,revision=1,created_at=instant,updated_at=instant))
        connection.execute(warehouses.insert().values(id='W1',code='W1',name='原仓库',address='原地址',crs='GCJ02',location_source='原资料',
            departure_time='08:00',revision=1,created_at=instant,updated_at=instant))
    migrate(engine);migrate(engine)
    with engine.connect() as connection:assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c);assert c.get('/api/master-data/management').json()['warehouses'][0]['name']=='原仓库'
        assert day_state(c)['revision']==0
