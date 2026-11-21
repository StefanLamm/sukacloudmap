"""Daily workflow integration: progress, group isolation and publication guards."""
from copy import deepcopy
import json
import threading
import time

import pytest
from sqlalchemy import select

from app.database import start_write
from app.models import Account, PlanningJob, PlanVersion
from app.routing_engine import check_result, normalize
from test_stage4 import basis
from test_plan_api import frozen, new_job, plan, waited


def install_workflow(monkeypatch, *, block=False, invalid=False):
    import app.plan_service as service
    entered=threading.Event();release=threading.Event();seen=[]
    def workflow(body,road,budget,seed,cancelled,*,initial_result=None,progress=None,wall_budget_seconds=None):
        seen.append({'body':deepcopy(body),'incumbent':deepcopy(initial_result),'budget':budget,'wall_budget_seconds':wall_budget_seconds})
        if progress:
            progress({'stage':'MULTI_START','message':'多轮搜索','round_index':1,'round_total':4,
                      'seed':seed,'best_fee':'15.75','elapsed_seconds':.1,'private_solver':object()})
        entered.set()
        if block:assert release.wait(4),'test did not release its private worker'
        result=service.optimize(body,road,min(1,budget),seed,lambda:False,initial_result=initial_result)
        if invalid:
            dropped=result['common'].pop()
            result['unassigned'].append({'task_id':dropped['task_id'],'pieces':dropped['pieces'],'reason':'no legal road'})
            result['total_fee']=str(sum(__import__('decimal').Decimal(row['fee']) for row in result['common']))
            result['fees_complete']=False;result['check']=check_result(normalize(body),result)
        result['algorithm'].update(mode='DAILY_DEEP_PORTFOLIO',stop_reason='CANCELLED' if cancelled() else 'SEARCH_COMPLETED',
            final_validation={'mode':'COMMON_ONLY_VALIDATED','fresh':False,'selected_segments':0})
        return result
    monkeypatch.setattr(service,'optimize_deep',workflow)
    return entered,release,seen


def test_new_jobs_default_to_deep_workflow_and_five_minute_budget(basis,monkeypatch):
    _,_,seen=install_workflow(monkeypatch)
    c,*_=basis;caps=c.get('/api/planning/capabilities').json()
    assert caps['automatic_workflow']=='DEEP_PORTFOLIO' and caps['automatic_road_verification']
    assert caps['default_budget_seconds']==300
    job=waited(c,new_job(c,frozen(basis)))
    assert job['status']=='COMPLETED' and seen[0]['budget']==300
    assert 0 < seen[0]['wall_budget_seconds'] <= 900
    assert job['summary']['algorithm']['mode']=='DAILY_DEEP_PORTFOLIO'
    assert job['progress']['stage']=='COMPLETE'


def test_progress_is_durable_small_and_does_not_hold_business_writer(basis,app,monkeypatch):
    entered,release,_=install_workflow(monkeypatch,block=True)
    c,*_=basis;snapshot=frozen(basis);job=new_job(c,snapshot)
    try:
        assert entered.wait(2)
        observed=c.get('/api/planning/jobs/'+job['id']).json()
        assert observed['status']=='RUNNING' and observed['summary'] is None and observed['plan_id'] is None
        assert observed['progress']['stage']=='MULTI_START' and observed['progress']['round_total']==4
        assert 'private_solver' not in observed['progress']
        with app.state.sessions() as db:
            start_write(db,app.state.engine)
            saved=db.get(PlanningJob,job['id'])
            assert json.loads(saved.result)['_progress']==observed['progress']
            assert db.scalar(select(PlanVersion).where(PlanVersion.job_id==job['id'])) is None
            db.rollback()
    finally:release.set()
    assert waited(c,job)['status']=='COMPLETED'


def test_inputs_changed_during_deep_search_keep_result_but_do_not_append(basis,app,monkeypatch):
    entered,release,_=install_workflow(monkeypatch,block=True)
    c,w,q,s,*_=basis;job=new_job(c,frozen(basis))
    try:
        assert entered.wait(2)
        changed=c.patch('/api/stores/'+s['id'],json={'expected_revision':s['revision'],'common_unit_price':'20'})
        assert changed.status_code==200
    finally:release.set()
    done=waited(c,job)
    assert done['status']=='FAILED' and done['error_code']=='INPUT_CHANGED' and done['plan_id'] is None
    assert done['summary']['total_fee']=='15.75'
    assert done['progress']['stage']=='FAILED' and '未保存' in done['progress']['message']
    with app.state.sessions() as db:
        assert db.scalar(select(PlanVersion).where(PlanVersion.job_id==job['id'])) is None
        assert 'total_fee' in json.loads(db.get(PlanningJob,job['id']).result)


def test_incomplete_automatic_result_is_rejected_before_new_plan(basis,monkeypatch):
    install_workflow(monkeypatch,invalid=True)
    c,*_=basis;done=waited(c,new_job(c,frozen(basis)))
    assert done['status']=='FAILED' and done['error_code']=='DEEP_NO_FEASIBLE_PLAN' and done['plan_id'] is None


def test_disabled_creator_cannot_publish_after_long_search(basis,app,monkeypatch):
    entered,release,_=install_workflow(monkeypatch,block=True)
    c,*_=basis;job=new_job(c,frozen(basis))
    try:
        assert entered.wait(2)
        with app.state.sessions() as db:
            start_write(db,app.state.engine);row=db.get(PlanningJob,job['id']);db.get(Account,row.created_by).active=False;db.commit()
    finally:release.set()
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        with app.state.sessions() as db:
            row=db.get(PlanningJob,job['id'])
            if row.status not in ('QUEUED','RUNNING'):
                assert row.status=='FAILED' and row.error_code=='ACCOUNT_DISABLED'
                assert db.scalar(select(PlanVersion).where(PlanVersion.job_id==row.id)) is None
                break
        time.sleep(.02)
    else:raise AssertionError('disabled creator job did not stop')


def test_new_group_does_not_inherit_another_groups_saved_incumbent(basis,monkeypatch):
    from test_demand_groups import create_group,manual
    _,_,seen=install_workflow(monkeypatch)
    c,w,q,s,*_=basis;a=create_group(c,'daily-a');b=create_group(c,'daily-b')
    first=manual(c,a,[{'store_id':s['id'],'pieces':3}])
    second=manual(c,b,[{'store_id':s['id'],'pieces':8}])
    ja=waited(c,new_job(c,first['snapshot_id'],group_id=a['id']))
    jb=waited(c,new_job(c,second['snapshot_id'],group_id=b['id']))
    assert ja['status']==jb['status']=='COMPLETED'
    assert seen[0]['incumbent'] is None and seen[1]['incumbent'] is None
    saved=plan(c,ja['plan_id'])
    again=waited(c,new_job(c,first['snapshot_id'],saved['version'],group_id=a['id']))
    assert again['status']=='COMPLETED' and seen[2]['incumbent']==saved['result']


@pytest.mark.parametrize('expired_after', ['verify_deep_result', 'append_version'])
def test_wall_cutoff_retains_verified_complete_result_without_publishing(basis,monkeypatch,app,expired_after):
    import app.plan_service as service
    from types import SimpleNamespace
    install_workflow(monkeypatch)
    real_clock=time.monotonic
    clock=SimpleNamespace(offset=0.)
    monkeypatch.setattr(service,'time',SimpleNamespace(monotonic=lambda:real_clock()+clock.offset))
    original=getattr(service, expired_after)
    def completed_then_expired(*args,**kwargs):
        result=original(*args,**kwargs)
        clock.offset=10000.
        return result
    monkeypatch.setattr(service,expired_after,completed_then_expired)
    client,*_=basis
    job=waited(client,new_job(client,frozen(basis),budget_seconds=2,wall_budget_seconds=20))
    assert job['status']=='FAILED' and job['plan_id'] is None
    with app.state.sessions() as db:
        stored=db.get(PlanningJob,job['id'])
        assert stored.error_code=='WALL_BUDGET'
        result=json.loads(stored.result)
        assert not result['unassigned'] and result['fees_complete']
        assert result['algorithm']['publication']=='NOT_PUBLISHED_WALL_DEADLINE'
        assert result['algorithm']['source_provenance']['app_source_hash']
        assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(PlanVersion))==0
