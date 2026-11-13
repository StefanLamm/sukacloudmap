from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
import time

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest
from sqlalchemy import select

from app.database import make_engine, migrate, session_factory, start_write
from app.main import create_app
from app.models import Base, InputSnapshot, PlanningJob, PlanVersion, now
from app.plan_service import PlanningRunner, append_version, detail
from app.planning_inputs import encode
from app.road_service import FixtureRoad, load_fixture
from app.routing_engine import optimize
from conftest import login
from test_stage4 import basis, commit, day_state, previewed

DAY='2026-11-11'


def frozen(basis):
    c,w,q,s,s2,v=basis
    r=commit(c,previewed(c,[[s['name'],3,s['code']],[s2['name'],4,s2['code']]]));assert r.status_code==200
    return r.json()['snapshot_id']


def new_job(c,snapshot,expected=0,**kw):
    r=c.post('/api/planning/jobs',json={'snapshot_id':snapshot,'expected_head':expected,**kw})
    assert r.status_code==202,r.text;return r.json()


def waited(c,job):
    end=time.monotonic()+8
    while time.monotonic()<end:
        r=c.get('/api/planning/jobs/'+job['id']);assert r.status_code==200,r.text
        if r.json()['status'] not in ['RUNNING','QUEUED']:return r.json()
        time.sleep(.03)
    pytest.fail('job did not finish')


def plan(c,id):
    r=c.get('/api/planning/plans/'+id);assert r.status_code==200,r.text;return r.json()


def test_automatic_reoptimization_receives_incumbent_and_compares_fees(basis,monkeypatch):
    import app.plan_service as service
    original=service.optimize_deep;observed=[]
    def recording(body,road,budget,seed,cancelled,*,initial_result=None,progress=None):
        observed.append(deepcopy(initial_result))
        return original(body,road,budget,seed,cancelled,initial_result=initial_result,progress=progress)
    monkeypatch.setattr(service,'optimize_deep',recording)
    c,*_=basis;snapshot=frozen(basis)
    first=waited(c,new_job(c,snapshot));assert first['status']=='COMPLETED'
    before=plan(c,first['plan_id'])
    second=waited(c,new_job(c,snapshot,expected=before['version']));assert second['status']=='COMPLETED'
    after=plan(c,second['plan_id'])
    assert observed[0] is None and observed[1]==before['result']
    assert after['result']['comparison']['previous_total']==before['total_fee']
    assert after['result']['comparison']['delta']=='0.00'
    assert plan(c,first['plan_id'])['content_hash']==before['content_hash']


@pytest.mark.parametrize('path',['/api/planning/capabilities','/api/planning/examples/MIXED_56','/api/planning/days/'+DAY+'/plans','/api/planning/days/'+DAY+'/jobs','/api/planning/jobs/missing','/api/planning/plans/missing/export.xlsx'])
def test_routes_require_login(client,path):assert client.get(path).status_code==401


def test_async_whole_flow_common_fallback_confirm_export_restore_and_input_stale(basis):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis)
    job=waited(c,new_job(c,snapshot));assert job['status']=='COMPLETED',job
    p=plan(c,job['plan_id']);assert p['result']['total_fee']=='15.75' and p['result']['check']['valid'] and p['result']['confirmable']
    assert len(p['result']['common'])==2 and not p['result']['routes'] and p['result']['algorithm']['road_stats']['calls']==0
    confirmed=c.post('/api/planning/plans/'+p['id']+'/confirm',json={'expected_head':1});assert confirmed.status_code==201,confirmed.text
    cp=confirmed.json();assert cp['status']=='CONFIRMED' and cp['version']==2
    assert plan(c,p['id'])['status']=='DRAFT'
    x=c.get('/api/planning/plans/'+cp['id']+'/export.xlsx');assert x.status_code==200
    wb=load_workbook(BytesIO(x.content));assert len(wb.sheetnames)==6
    summary=dict(wb['版本与汇总'].values);assert summary['总费用（元）']=='15.75' and summary['总件数']==7
    assert len(list(wb['逐店分配'].values))==3
    restored=c.post('/api/planning/plans/'+p['id']+'/restore',json={'expected_head':2});assert restored.status_code==201
    assert restored.json()['version']==3 and restored.json()['result']==p['result']
    changed=c.patch('/api/stores/'+s['id'],json={'expected_revision':s['revision'],'common_unit_price':'10'});assert changed.status_code==200
    old=plan(c,cp['id']);assert old['input_changed'] and old['result']['total_fee']=='15.75'
    assert c.post('/api/planning/plans/'+restored.json()['id']+'/confirm',json={'expected_head':3}).status_code==409
    assert c.post('/api/planning/jobs',json={'snapshot_id':snapshot,'expected_head':3}).status_code==409


def test_manual_invalid_choices_retained_not_silently_fixed_or_confirmed(basis):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis)
    draft={'routes':[{'vehicle_id':v['id'],'task_ids':[s['id'],s2['id']]}],'common_ids':[s['id']],'unassigned_ids':[]}
    job=waited(c,new_job(c,snapshot,kind='MANUAL',draft=draft));assert job['status']=='COMPLETED',job
    p=plan(c,job['plan_id']);assert p['result']['routes'][0]['task_ids']==[s['id'],s2['id']]
    assert not p['result']['check']['valid'] and not p['result']['confirmable']
    assert any(e['code']=='TASK_COVERAGE' for e in p['result']['check']['issues'])
    assert c.post('/api/planning/plans/'+p['id']+'/confirm',json={'expected_head':1}).status_code==422
    fixed={'routes':[],'common_ids':[s['id'],s2['id']],'unassigned_ids':[]}
    job=waited(c,new_job(c,snapshot,1,kind='MANUAL',draft=fixed,parent_id=p['id']))
    assert plan(c,job['plan_id'])['result']['check']['valid']


def test_null_common_is_unassigned_and_zero_price_is_available(basis):
    c,w,q,s,s2,v=basis
    for obj,price in [(s,None),(s2,'0')]:assert c.patch('/api/stores/'+obj['id'],json={'expected_revision':1,'common_unit_price':price}).status_code==200
    snapshot=frozen(basis);j=waited(c,new_job(c,snapshot))
    assert j['status']=='FAILED' and j['error_code']=='DEEP_NO_FEASIBLE_PLAN' and j['plan_id'] is None
    assert c.get('/api/planning/days/'+DAY+'/plans').json()['total']==0


def test_only_latest_head_can_be_modified_and_strict_requests(basis):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis)
    j=waited(c,new_job(c,snapshot));p=plan(c,j['plan_id'])
    assert c.post('/api/planning/jobs',json={'snapshot_id':snapshot,'expected_head':0}).status_code==409
    for kw in [{'expected_head':True},{'call_limit':1},{'kind':'MANUAL'},{'seed':-1},{'source':'FIXTURE'}]:
        payload={'snapshot_id':snapshot,'expected_head':1,**kw}
        assert c.post('/api/planning/jobs',json=payload).status_code==422
    c.headers.pop('x-csrf-token')
    assert c.post('/api/planning/plans/'+p['id']+'/restore',json={'expected_head':1}).status_code==403


def test_fixtures_are_isolated_and_immutable_business_data(basis,app):
    c,w,q,s,s2,v=basis
    for fixture,total in [('MIXED_56','56.00'),('SHARED_40','40.00')]:
        r=c.get('/api/planning/examples/'+fixture);assert r.status_code==200 and r.json()['result']['total_fee']==total
        assert not r.json()['result']['confirmable']
    assert c.get('/api/planning/days/'+DAY+'/plans').json()['total']==0
    assert day_state(c)['revision']==0
    r=c.post('/api/planning/examples/MIXED_56/recalculate',json={'routes':[{'vehicle_id':'V10','task_ids':['A','B','C']}],'common_ids':['D'],'unassigned_ids':[]})
    assert r.status_code==200 and not r.json()['result']['check']['valid']
    assert r.json()['result']['routes'][0]['task_ids']==['A','B','C']


def test_formula_like_names_export_as_literal_text(basis):
    c,w,q,s,s2,v=basis
    for obj,name in [(s,'=1+1'),(s2,'@SUM(1,2)')]:assert c.patch('/api/stores/'+obj['id'],json={'expected_revision':1,'name':name}).status_code==200
    p=commit(c,previewed(c,[[None,3,s['code']],[None,4,s2['code']]]));assert p.status_code==200,p.text
    j=waited(c,new_job(c,p.json()['snapshot_id']));p=plan(c,j['plan_id'])
    wb=load_workbook(BytesIO(c.get('/api/planning/plans/'+p['id']+'/export.xlsx').content))
    names=[row[2] for row in list(wb['逐店分配'].rows)[1:]]
    assert {cell.value for cell in names}=={'=1+1','@SUM(1,2)'} and all(cell.data_type=='s' for cell in names)


def test_restart_preserves_plan_costs_and_queued_jobs(settings,basis,app):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis)
    j=waited(c,new_job(c,snapshot));saved=plan(c,j['plan_id'])
    with TestClient(create_app(settings)) as restarted:
        login(restarted)
        assert plan(restarted,saved['id'])['result']==saved['result']
        assert restarted.get('/api/planning/plans/'+saved['id']+'/export.json').json()['content_hash']==saved['content_hash']


def test_cancel_and_writer_not_held_during_search(basis,app,monkeypatch):
    import app.plan_service as service
    original=service.optimize_deep
    entered=__import__('threading').Event()
    def slow(body,road,budget,seed,cancelled,*,initial_result=None,progress=None):
        entered.set()
        until=time.monotonic()+3
        while not cancelled() and time.monotonic()<until:time.sleep(.02)
        return original(body,road,budget,seed,cancelled,initial_result=initial_result,progress=progress)
    monkeypatch.setattr(service,'optimize_deep',slow)
    c,w,q,s,s2,v=basis;snapshot=frozen(basis);j=new_job(c,snapshot)
    assert entered.wait(2)
    started=time.monotonic();r=c.patch('/api/stores/'+s['id'],json={'expected_revision':1,'name':'搜索期间可维护'})
    assert r.status_code==200 and time.monotonic()-started<1.5
    assert c.post('/api/planning/jobs/'+j['id']+'/cancel').status_code==200
    done=waited(c,j);assert done['status']=='CANCELLED',done
    assert done['plan_id'] is None and done['error_code']=='INPUT_CHANGED'
    assert done['summary']['valid'] and done['progress']['stage']=='CANCELLED'
    assert '未保存' in done['progress']['message']


def test_expired_worker_lease_reports_interruption(basis,app):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis)
    with app.state.sessions() as db:
        start_write(db,app.state.engine)
        actor=db.get(InputSnapshot,snapshot)
        job=PlanningJob(delivery_date=DAY,snapshot_id=snapshot,kind='OPTIMIZE',status='RUNNING',request='{}',lease_until=now()-1,created_by=actor.created_by,created_by_name=actor.created_by_name)
        db.add(job);db.commit();id=job.id
    done=waited(c,{'id':id});assert done['status']=='FAILED' and done['error_code']=='WORKER_INTERRUPTED'


def test_append_versions_concurrent_save_cannot_overwrite(basis,app):
    c,w,q,s,s2,v=basis;snapshot=frozen(basis);j=waited(c,new_job(c,snapshot));p=plan(c,j['plan_id'])
    def restore(_):
        with TestClient(app) as other:
            login(other)
            return other.post('/api/planning/plans/'+p['id']+'/restore',json={'expected_head':1}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(restore,range(2)))
    assert sorted(results)==[201,409] and c.get('/api/planning/days/'+DAY+'/plans').json()['total']==2


def test_full_migration_matches_models_and_rollback_restores_schema(settings):
    engine=make_engine(settings);migrate(engine)
    with engine.connect() as connection:assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    from alembic import command
    from app.database import migrations_config
    with engine.begin() as connection:
        cfg=migrations_config();cfg.attributes['connection']=connection;command.downgrade(cfg,'0003_daily_inputs')
    migrate(engine)
    with engine.connect() as connection:assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    engine.dispose()


def test_sqlite_online_backup_restore_preserves_saved_version(basis,app,tmp_path):
    import sqlite3
    from scripts.backup_sqlite import backup
    c,w,q,s,s2,v=basis;snapshot=frozen(basis);j=waited(c,new_job(c,snapshot));p=plan(c,j['plan_id'])
    source=app.state.engine.url.database;destination=tmp_path/'private-backup.db';proof=backup(source,destination)
    assert proof['integrity']=='ok' and destination.stat().st_mode & 0o777==0o600
    restored=tmp_path/'restored.db'
    with sqlite3.connect(destination) as src,sqlite3.connect(restored) as dst:src.backup(dst)
    with sqlite3.connect(restored) as db:
        saved=db.execute('SELECT content_hash,content FROM plan_versions WHERE id=?',(p['id'],)).fetchone()
        assert saved[0]==p['content_hash'] and json.loads(saved[1])['result']==p['result']
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with pytest.raises(ValueError):backup(source,destination)


def test_scale_import_snapshot_async_fallback_and_six_sheet_export(client,app):
    from test_stage4 import scale_snapshot
    snapshot=scale_snapshot(client,app)
    j=waited(client,new_job(client,snapshot['id'],budget_seconds=2));assert j['status']=='COMPLETED',j
    p=plan(client,j['plan_id']);r=p['result']
    assert r['check']['valid'] and r['check']['task_count']==800 and r['check']['total_pieces']==24000
    assert len(r['common'])==800 and not r['routes'] and not r['unassigned']
    assert r['total_fee']=='77910.00' and r['algorithm']['road_stats']['calls']==0
    wb=load_workbook(BytesIO(client.get('/api/planning/plans/'+p['id']+'/export.xlsx').content))
    allocations=list(wb['逐店分配'].values)[1:]
    assert len(allocations)==800 and sum(row[3] for row in allocations)==24000
    assert dict(wb['版本与汇总'].values)['总费用（元）']=='77910.00'
