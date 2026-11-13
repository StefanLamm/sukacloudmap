from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import MetaData, Table, select, text

from app.database import make_engine, migrate, migrations_config
from app.models import (Base, DailyDemand, DemandGroup, DemandImport, GroupArrangement, InputSnapshot, PlanningJob, PlanVersion)
from app.planning_inputs import content_hash, encode
from test_stage4 import DAY, basis, commit, previewed, workbook
from test_plan_api import new_job, plan, waited


def create_group(c,name='首组',day=DAY):
    response=c.post('/api/planning/days/'+day+'/groups',json={'name':name})
    assert response.status_code==201,response.text
    return response.json()


def state(c,g):
    response=c.get('/api/planning/days/'+g['delivery_date'],params={'group_id':g['id']})
    assert response.status_code==200,response.text
    return response.json()


def manual(c,g,items,revision=None):
    response=c.put('/api/planning/groups/'+g['id']+'/demands',json={'expected_revision':state(c,g)['revision'] if revision is None else revision,'items':items})
    assert response.status_code==200,response.text
    return response.json()


def group_import(c,g,rows):
    response=c.post('/api/planning/demand-imports/upload',params={'delivery_date':g['delivery_date'],'filename':'group.xlsx','group_id':g['id']},content=workbook(rows))
    assert response.status_code==200,response.text
    batch=response.json()
    response=c.post('/api/planning/demand-imports/'+batch['id']+'/preview',json={'store_name':0,'pieces':1,'store_code':2})
    assert response.status_code==200,response.text
    return response.json()


def plans(c,g):
    response=c.get('/api/planning/days/'+g['delivery_date']+'/plans',params={'group_id':g['id']})
    assert response.status_code==200,response.text
    return response.json()


def test_same_day_groups_same_store_manual_edit_empty_and_validation(basis):
    c,w,q,s,s2,v=basis
    a=create_group(c,'上午批次');b=create_group(c,'下午批次')
    assert state(c,a)['revision']==0 and state(c,a)['items']==[]
    assert c.post('/api/planning/days/'+DAY+'/groups',json={'name':' 上午批次 '}).status_code==409
    assert c.post('/api/planning/days/'+DAY+'/groups',json={'name':'  '}).status_code==422
    first=manual(c,a,[{'store_id':s['id'],'pieces':3}]);second=manual(c,b,[{'store_id':s['id'],'pieces':9}])
    assert first['totals']['total_pieces']==3 and second['totals']['total_pieces']==9
    assert state(c,a)['items'][0]['pieces']==3 and state(c,b)['items'][0]['pieces']==9
    invalid=c.put('/api/planning/groups/'+a['id']+'/demands',json={'expected_revision':1,'items':[{'store_id':s['id'],'pieces':3},{'store_id':s['id'],'pieces':4}]})
    assert invalid.status_code==422 and invalid.json()['code']=='DUPLICATE_STORE'
    assert c.put('/api/planning/groups/'+a['id']+'/demands',json={'expected_revision':0,'items':[]}).status_code==409
    manual(c,a,[{'store_id':s['id'],'pieces':7},{'store_id':s2['id'],'pieces':2}])
    empty=manual(c,a,[]);assert empty['totals']['task_count']==0
    assert state(c,a)['snapshot_count']==3 and state(c,b)['snapshot_count']==1
    assert state(c,b)['totals']['total_pieces']==9
    assert c.get('/api/planning/days/2026-11-12',params={'group_id':a['id']}).status_code==404


def test_import_only_replaces_selected_group_and_manual_keeps_unchanged_sources(basis):
    c,w,q,s,s2,v=basis;a=create_group(c,'文件一');b=create_group(c,'文件二')
    manual(c,b,[{'store_id':s['id'],'pieces':25}])
    p=group_import(c,a,[[s['name'],4,s['code']],[s2['name'],2,s2['code']]])
    assert commit(c,p).status_code==200
    old={row['store_id']:row for row in state(c,a)['items']}
    manual(c,a,[{'store_id':s['id'],'pieces':4},{'store_id':s2['id'],'pieces':5}])
    updated={row['store_id']:row for row in state(c,a)['items']}
    for field in ['source_rows','import_id','import_warehouse_id']:
        assert updated[s['id']][field]==old[s['id']][field]
    assert updated[s2['id']]['import_id']!=old[s2['id']]['import_id']
    assert updated[s2['id']]['source_rows'][0]['sheet_name']=='人工创建'
    again=group_import(c,a,[[s2['name'],6,s2['code']]]);assert commit(c,again).status_code==200
    assert state(c,a)['totals']['total_pieces']==6
    assert state(c,b)['totals']['total_pieces']==25 and state(c,b)['snapshot_count']==1


def test_group_overrides_are_isolated_from_other_groups_and_legacy_day(basis):
    c,w,q,s,s2,v=basis;a=create_group(c,'早班');b=create_group(c,'晚班')
    for g in [a,b]:manual(c,g,[{'store_id':s['id'],'pieces':3}])
    before_a=state(c,a);before_b=state(c,b)
    assert c.put('/api/warehouses/'+w['id']+'/days/'+DAY,json={'expected_revision':0,'departure_time':'10:30'}).status_code==200
    assert c.put('/api/vehicles/'+v['id']+'/days/'+DAY,json={'expected_revision':0,'warehouse_id':w['id'],'available':False}).status_code==200
    assert c.put('/api/planning/groups/'+a['id']+'/warehouses/'+w['id'],json={'expected_revision':0,'departure_time':'09:00'}).status_code==200
    assert c.put('/api/planning/groups/'+a['id']+'/vehicles/'+v['id'],json={'expected_revision':0,'warehouse_id':w['id'],'available':False}).status_code==200
    assert c.put('/api/planning/days/'+DAY+'/stores/'+s['id'],params={'group_id':a['id']},json={'expected_revision':0,'latest_arrival_at':DAY+'T12:00:00+08:00'}).status_code==200
    catalogs=[c.get('/api/planning/groups/'+g['id']+'/catalog').json() for g in [a,b]]
    assert catalogs[0]['warehouses'][0]['day_departure_time']=='09:00'
    assert catalogs[1]['warehouses'][0]['day_departure_time']==w['departure_time']
    assert catalogs[0]['vehicles'][0]['day_available'] is False and catalogs[1]['vehicles'][0]['day_available'] is True
    assert state(c,a)['items'][0]['latest_arrival_at']==DAY+'T12:00:00+08:00'
    assert state(c,b)['items'][0]['latest_arrival_at']==before_b['items'][0]['latest_arrival_at']
    assert state(c,b)['input_fingerprint']==before_b['input_fingerprint']
    assert state(c,a)['input_fingerprint']!=before_a['input_fingerprint']


def test_group_snapshot_jobs_heads_and_versions_do_not_cross_groups(basis):
    c,w,q,s,s2,v=basis;a=create_group(c,'甲组');b=create_group(c,'乙组')
    receipts=[manual(c,g,[{'store_id':s['id'],'pieces':pieces}]) for g,pieces in [(a,3),(b,8)]]
    saved=[]
    for g,receipt in zip([a,b],receipts):
        job=waited(c,new_job(c,receipt['snapshot_id'],group_id=g['id']))
        assert job['status']=='COMPLETED',job
        p=plan(c,job['plan_id']);assert p['group_id']==g['id'];saved.append(p)
    assert saved[0]['input']['totals']['total_pieces']==3 and saved[1]['input']['totals']['total_pieces']==8
    assert plans(c,a)['total']==plans(c,b)['total']==1
    for g,p in zip([a,b],saved):
        assert plans(c,g)['items'][0]['id']==p['id']
        snapshots=c.get('/api/planning/days/'+DAY+'/snapshots',params={'group_id':g['id']}).json()
        assert snapshots['total']==1 and snapshots['items'][0]['group_id']==g['id']
    mismatch=c.post('/api/planning/jobs',json={'snapshot_id':receipts[0]['snapshot_id'],'group_id':b['id'],'expected_head':saved[1]['version']})
    assert mismatch.status_code==422 and mismatch.json()['code']=='GROUP_MISMATCH'
    restored=c.post('/api/planning/plans/'+saved[0]['id']+'/restore',json={'expected_head':saved[0]['version']})
    assert restored.status_code==201,restored.text
    assert plans(c,a)['total']==2 and plans(c,b)['head_version']==saved[1]['version']


def test_delete_group_cascades_derived_data_keeps_other_group_and_master(basis,app):
    c,w,q,s,s2,v=basis;a=create_group(c,'将删除');b=create_group(c,'保留')
    ra=manual(c,a,[{'store_id':s['id'],'pieces':3}]);rb=manual(c,b,[{'store_id':s['id'],'pieces':9}])
    job=waited(c,new_job(c,ra['snapshot_id'],group_id=a['id']));assert job['status']=='COMPLETED'
    old=plan(c,job['plan_id'])
    # Also save a child version; deletion must clear self-referencing plan chains.
    assert c.post('/api/planning/plans/'+old['id']+'/restore',json={'expected_head':old['version']}).status_code==201
    assert c.put('/api/planning/groups/'+a['id']+'/warehouses/'+w['id'],json={'expected_revision':0,'departure_time':'09:00'}).status_code==200
    deleted=c.delete('/api/planning/groups/'+a['id'],params={'expected_revision':state(c,a)['revision']})
    assert deleted.status_code==200,deleted.text
    assert c.get('/api/planning/plans/'+old['id']).status_code==404
    assert c.get('/api/planning/jobs/'+job['id']).status_code==404
    assert c.get('/api/planning/snapshots/'+ra['snapshot_id']).status_code==404
    assert c.get('/api/planning/demand-imports/'+ra['import_id']).status_code==404
    assert state(c,b)['totals']['total_pieces']==9
    assert c.get('/api/master-data/management').json()['stores'][0]['id']==s['id']
    with app.state.sessions() as db:
        for cls in [DailyDemand,DemandImport,InputSnapshot,PlanningJob,PlanVersion,GroupArrangement]:
            assert db.scalar(select(cls).where(cls.group_id==a['id'])) is None
        assert db.get(InputSnapshot,rb['snapshot_id']) is not None
        assert db.execute(text('PRAGMA foreign_key_check')).all()==[]


def test_delete_running_group_cancels_worker_without_reviving_results(basis,app,monkeypatch):
    import app.plan_service as service
    original=service.optimize_deep;entered=threading.Event();exited=threading.Event()
    def slow(body,road,budget,seed,cancelled,*,initial_result=None,progress=None):
        entered.set();end=time.monotonic()+3
        while time.monotonic()<end and not cancelled():time.sleep(.02)
        try:return original(body,road,budget,seed,cancelled,initial_result=initial_result,progress=progress)
        finally:exited.set()
    monkeypatch.setattr(service,'optimize_deep',slow)
    c,w,q,s,s2,v=basis;g=create_group(c);receipt=manual(c,g,[{'store_id':s['id'],'pieces':3}])
    job=new_job(c,receipt['snapshot_id'],group_id=g['id']);assert entered.wait(2)
    deleted=c.delete('/api/planning/groups/'+g['id'],params={'expected_revision':1})
    assert deleted.status_code==200,deleted.text
    assert exited.wait(3)
    time.sleep(.3)
    assert c.get('/api/planning/jobs/'+job['id']).status_code==404
    with app.state.sessions() as db:
        assert db.scalar(select(PlanVersion).where(PlanVersion.group_id==g['id'])) is None
        assert db.get(DemandGroup,g['id']) is None
        assert db.execute(text('PRAGMA foreign_key_check')).all()==[]


def test_delete_named_group_keeps_unscoped_legacy_compatibility_data(basis,app):
    c,w,q,s,s2,v=basis;g=create_group(c);manual(c,g,[{'store_id':s['id'],'pieces':3}])
    legacy=commit(c,previewed(c,[[s2['name'],7,s2['code']]]));assert legacy.status_code==200
    deleted=c.delete('/api/planning/groups/'+g['id'],params={'expected_revision':1})
    assert deleted.status_code==200,deleted.text
    unscoped=c.get('/api/planning/days/'+DAY).json();assert unscoped['totals']['total_pieces']==7
    assert c.get('/api/planning/snapshots/'+legacy.json()['snapshot_id']).status_code==200
    with app.state.sessions() as db:assert db.execute(text('PRAGMA foreign_key_check')).all()==[]


def test_upgrade_groups_real_legacy_history_without_rewriting_json_or_hashes(settings):
    engine=make_engine(settings);migrate(engine,'0005_location_tasks')
    instant=int(datetime(2026,11,11,18,0,tzinfo=timezone.utc).timestamp())
    frozen=encode({'schema_version':1,'historical':'原始快照','totals':{'task_count':1,'total_pieces':5}})
    planned=encode({'schema_version':1,'input':json.loads(frozen),'result':{'total_fee':'123.45','historical':'原始方案'}})
    hashes=[hashlib.sha256(value.encode()).hexdigest() for value in [frozen,planned]]
    with engine.begin() as connection:
        metadata=MetaData()
        def insert(table_name,**values):
            table=Table(table_name,metadata,autoload_with=connection,extend_existing=True)
            connection.execute(table.insert().values(**values))
        common={'revision':1,'created_at':instant,'updated_at':instant}
        insert('accounts',id='A',username='legacy',display_name='旧管理员',password_hash='historical-hash',is_admin=True,active=True,auth_revision=1,**common)
        insert('warehouses',id='W',code='W',name='旧仓库',address='旧仓地址',crs='GCJ02',location_source='历史',departure_time='08:00',**common)
        insert('stores',id='S',code='S',name='旧门店',city='合肥市',address='旧门店地址',crs='GCJ02',location_source='历史',warehouse_id='W',
               business_open='08:00',business_close='18:00',close_day_offset=0,unload_minutes=10,**common)
        insert('vehicles',id='V',plate='皖A12345',max_pieces=400,warehouse_id='W',available=True,**common)
        insert('demand_imports',id='I',delivery_date=DAY,file_name='legacy.xlsx',file_hash='0'*64,document='{}',preview_revision=1,status='COMMITTED',created_by='A',created_at=instant)
        insert('demand_days',delivery_date=DAY,revision=2,latest_import_id='I',updated_by='A',updated_at=instant)
        insert('daily_demands',id='D',delivery_date=DAY,warehouse_id='W',store_id='S',pieces=5,source_rows='[]',import_id='I')
        insert('input_snapshots',id='SN',delivery_date=DAY,version=1,demand_revision=2,input_fingerprint='1'*64,content_hash=hashes[0],content=frozen,created_by='A',created_by_name='旧管理员',created_at=instant)
        insert('planning_jobs',id='J',delivery_date=DAY,snapshot_id='SN',kind='MANUAL',status='COMPLETED',request='{"legacy":true}',result='{}',cancel_requested=False,
               created_by='A',created_by_name='旧管理员',created_at=instant)
        insert('plan_versions',id='P',delivery_date=DAY,version=1,snapshot_id='SN',job_id='J',status='DRAFT',content=planned,content_hash=hashes[1],note='历史',
               created_by='A',created_by_name='旧管理员',created_at=instant)
        insert('warehouse_days',warehouse_id='W',delivery_date=DAY,departure_time='09:30',revision=2)
        insert('vehicle_days',vehicle_id='V',delivery_date=DAY,warehouse_id='W',available=False,revision=3)
        insert('store_days',store_id='S',delivery_date=DAY,latest_arrival_at=DAY+'T10:00:00+08:00',revision=4)
        insert('shared_records',id='R',title='旧协作任务',content='原文',created_by='A',updated_by='A',**common)
    migrate(engine);migrate(engine)
    with engine.connect() as connection:
        gid=connection.execute(text('SELECT id FROM demand_groups WHERE delivery_date=:day'),{'day':DAY}).scalar_one()
        assert connection.execute(text('SELECT name,revision,latest_import_id FROM demand_groups')).one()==('历史需求',2,'I')
        for table in ['demand_imports','daily_demands','input_snapshots','planning_jobs','plan_versions']:
            assert connection.execute(text('SELECT group_id FROM '+table)).scalar_one()==gid
        assert connection.execute(text('SELECT content,content_hash FROM input_snapshots')).one()==(frozen,hashes[0])
        assert connection.execute(text('SELECT content,content_hash FROM plan_versions')).one()==(planned,hashes[1])
        assert connection.execute(text('SELECT request FROM planning_jobs')).scalar_one()=='{"legacy":true}'
        arrangements={kind:(json.loads(payload),revision) for kind,payload,revision in connection.execute(text('SELECT kind,payload,revision FROM group_arrangements'))}
        assert arrangements['warehouses']==({'departure_time':'09:30'},2)
        assert arrangements['vehicles']==({'warehouse_id':'W','available':False},3)
        assert arrangements['stores']==({'latest_arrival_at':DAY+'T10:00:00+08:00'},4)
        assert connection.execute(text('SELECT task_date FROM shared_records')).scalar_one()=='2026-11-12'
        assert connection.execute(text('PRAGMA foreign_key_check')).all()==[]
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    with pytest.raises(RuntimeError,match='不能无损降级'):
        with engine.begin() as connection:
            cfg=migrations_config();cfg.attributes['connection']=connection;command.downgrade(cfg,'0005_location_tasks')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT content_hash FROM plan_versions')).scalar_one()==hashes[1]
        assert connection.execute(text('SELECT count(*) FROM demand_groups')).scalar_one()==1
        assert connection.execute(text('PRAGMA foreign_key_check')).all()==[]
    engine.dispose()
