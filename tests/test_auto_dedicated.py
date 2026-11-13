from copy import deepcopy
from decimal import Decimal
from itertools import permutations
import json
import threading

import pytest
from pydantic import ValidationError

from app.auto_dedicated import allocate_dedicated
from app.models import InputSnapshot, PlanningJob
from app.plan_api import NewJob
from app.plan_service import PlanningRunner
from app.planning_inputs import encode
from app.road_service import FixtureRoad, load_fixture
from app.routing_engine import StopSearch, normalize, recalculate
from test_plan_api import frozen, new_job, plan, waited
from test_stage4 import basis


def fixture(two_cars=False, capacity=10):
    body=load_fixture('MIXED_56')
    body['vehicles'][0]['max_pieces']=capacity
    if two_cars:body['vehicles'].append({**body['vehicles'][0],'id':'V20'})
    return body


def draft(auto, fixed=None, common=None, unassigned=None):
    return {'routes':fixed or [],'common_ids':common or [],'unassigned_ids':unassigned or [],'auto_dedicated_ids':auto}


def test_auto_dedicated_uses_the_cheapest_route_and_never_common_even_when_free():
    body=fixture()
    for task in body['tasks']:task['common_unit_price']='0'
    result=allocate_dedicated(body,draft(['A','B'],common=['C','D']),FixtureRoad(body))
    oracle=[recalculate(body,draft([],fixed=[{'vehicle_id':'V10','task_ids':list(order)}],common=['C','D']),FixtureRoad(body)) for order in permutations(['A','B'])]
    assert Decimal(result['total_fee'])==min(Decimal(item['total_fee']) for item in oracle if item['check']['valid'])
    assert set(result['routes'][0]['task_ids'])=={'A','B'}
    assert {r['task_id'] for r in result['common']}=={'C','D'}
    assert result['check']['valid'] and result['algorithm']['optimality_proven']


def test_auto_dedicated_splits_by_volume_and_uses_multiple_real_vehicles():
    body=fixture(True,6)
    result=allocate_dedicated(body,draft(['A','B'],common=['C','D']),FixtureRoad(body))
    assert len(result['routes'])==2 and result['check']['valid']
    assert {r['vehicle_id'] for r in result['routes']}=={'V10','V20'}
    assert all(r['pieces']<=6 for r in result['routes'])
    assert not result['unassigned']


def test_auto_dedicated_insufficient_capacity_remains_an_explicit_conflict():
    body=fixture(False,6)
    result=allocate_dedicated(body,draft(['A','B'],common=['C','D']),FixtureRoad(body))
    assert result['unassigned'] and not result['check']['valid'] and not result['fees_complete']
    assert {r['task_id'] for r in result['common']}=={'C','D'}
    assert all('专车' in r['reason'] for r in result['unassigned'])
    assert any(e['code']=='AUTO_DEDICATED_UNASSIGNED' for e in result['check']['issues'])


def test_fixed_vehicle_and_order_are_preserved_while_automatic_tasks_are_inserted():
    body=fixture(True,20)
    for row in body['tasks']:row['quote_id']='KM2'
    fixed=[{'vehicle_id':'V10','task_ids':['D','B']}]
    result=allocate_dedicated(body,draft(['A'],fixed=fixed,common=['C']),FixtureRoad(body))
    original=next(r for r in result['routes'] if 'D' in r['task_ids'])
    assert original['vehicle_id']=='V10'
    assert [i for i in original['task_ids'] if i in {'D','B'}]==['D','B']
    assert result['check']['valid']
    assert result['algorithm']['fixed_order_preserved']
    assert {r['task_id'] for r in result['common']}=={'C'}


def test_auto_dedicated_respects_quote_compatibility_and_warehouse():
    body=fixture(True,20)
    result=allocate_dedicated(body,draft(['A','C'],common=['B','D']),FixtureRoad(body))
    assert len(result['routes'])==2 and result['check']['valid']
    body['vehicles'][1]['warehouse_id']='OTHER'
    result=allocate_dedicated(body,draft(['A','C'],common=['B','D']),FixtureRoad(body))
    assert result['unassigned'] and all(r['vehicle_id']=='V10' for r in result['routes'])


def test_bad_auto_draft_is_reported_without_silently_repairing_coverage():
    body=fixture()
    result=allocate_dedicated(body,draft(['A','A','unknown'],common=['A','B','C','D']),FixtureRoad(body))
    assert result['algorithm']['stop_reason']=='INVALID_DRAFT'
    assert not result['check']['valid']
    assert any(e['code']=='AUTO_DEDICATED_INPUT' for e in result['check']['issues'])


def test_auto_dedicated_cancellation_and_budget_have_finite_results():
    body=fixture();count=[0]
    def limited():
        count[0]+=1
        if count[0]>2:raise StopSearch('TIME_BUDGET')
    result=allocate_dedicated(body,draft(['A','B'],common=['C','D']),FixtureRoad(body),limited)
    assert result['algorithm']['stop_reason']=='TIME_BUDGET'
    assert not result['algorithm']['optimality_proven']
    def cancelled():raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch,match='CANCELLED'):
        allocate_dedicated(body,draft(['A','B'],common=['C','D']),FixtureRoad(body),cancelled)


def test_planning_budget_accepts_24000_seconds_and_rejects_above_limit():
    payload={'snapshot_id':'S','expected_head':0,'budget_seconds':24000}
    assert NewJob.model_validate(payload).budget_seconds==24000
    with pytest.raises(ValidationError):NewJob.model_validate({**payload,'budget_seconds':24001})


def test_long_budget_lease_renews_and_expired_worker_does_not_hold_group_for_hours(basis,app,monkeypatch):
    import app.plan_service as service
    c,*_=basis;snapshot=frozen(basis);clock=[1000];monkeypatch.setattr(service,'now',lambda:clock[0])
    runner=PlanningRunner(app.state.engine,app.state.sessions,app.state.settings)
    with app.state.sessions() as db:
        source=db.get(InputSnapshot,snapshot)
        row=PlanningJob(delivery_date=source.delivery_date,snapshot_id=snapshot,kind='MANUAL',status='RUNNING',lease_until=1060,
            request=encode({'budget_seconds':24000}),created_by=source.created_by,created_by_name=source.created_by_name)
        db.add(row);db.commit();job_id=row.id
    clock[0]=1030;assert runner.renew_lease(job_id)
    with app.state.sessions() as db:
        row=db.get(PlanningJob,job_id);assert row.lease_until==1090;row.cancel_requested=True;db.commit()
    assert not runner.renew_lease(job_id)
    with app.state.sessions() as db:row=db.get(PlanningJob,job_id);row.cancel_requested=False;db.commit()
    clock[0]=1100;assert not runner.renew_lease(job_id)
    runner.claim()
    with app.state.sessions() as db:
        row=db.get(PlanningJob,job_id);assert row.status=='FAILED' and row.error_code=='WORKER_INTERRUPTED'


def test_manual_auto_job_saves_repriced_plan_and_comparison_without_live_calls(basis,app):
    c,w,q,s,s2,v=basis;app.state.settings.planning_road_source='TEST_GRID';snapshot=frozen(basis)
    previous=waited(c,new_job(c,snapshot));assert previous['status']=='COMPLETED'
    before=plan(c,previous['plan_id'])
    job=waited(c,new_job(c,snapshot,before['version'],kind='MANUAL',parent_id=before['id'],draft=draft([s['id'],s2['id']]),budget_seconds=24000))
    assert job['status']=='COMPLETED',job
    after=plan(c,job['plan_id']);result=after['result']
    assert len(result['routes'])==1 and result['routes'][0]['vehicle_id']==v['id']
    assert not result['common'] and not result['unassigned']
    assert result['comparison']['previous_total']==before['result']['total_fee']
    assert result['comparison']['current_total']==result['total_fee']
    assert result['algorithm']['road_stats']['calls']==0
    assert result['algorithm']['auto_dedicated_count']==2


def test_exact_auto_search_does_not_force_allocation_to_a_late_or_unpriced_route():
    body=fixture()
    body['tasks'][0]['latest_arrival_at']=body['warehouses'][0]['earliest_departure_at']
    result=allocate_dedicated(body,draft(['A'],common=['B','C','D']),FixtureRoad(body))
    assert not result['routes'] and result['unassigned'][0]['task_id']=='A'
    assert not result['algorithm']['optimality_proven']
    from app.routing_engine import RoadError
    class MissingRoad(FixtureRoad):
        def segment(self,*args,**kwargs):raise RoadError('ROAD_UNAVAILABLE','无法获得道路报价')
    body=fixture()
    result=allocate_dedicated(body,draft(['A'],common=['B','C','D']),MissingRoad(body))
    assert not result['routes'] and result['unassigned'][0]['task_id']=='A'
    assert not result['algorithm']['optimality_proven']


@pytest.mark.parametrize('count',[2,4])
def test_many_cars_consider_a_larger_single_vehicle_instead_of_two_cheaper_first_legs(count):
    from app.road_service import evidence,road_request
    body=fixture();body['vehicles']=[{**body['vehicles'][0],'id':'SMALL'+str(i),'max_pieces':6} for i in range(6)]
    body['vehicles'].append({**body['vehicles'][0],'id':'BIG','max_pieces':30})
    for task in body['tasks']:task.update(quote_id='KM2',pieces=4)
    class ByVehicleRoad(FixtureRoad):
        def segment(self,v,a,b,departure):
            meters=(30000 if v['id']=='BIG' else 25000) if a['id']=='W' else 10000
            return evidence(road_request(v,a,b,departure,'FIXTURE'),meters,60,'FIXTURE')
    chosen=[t['id'] for t in body['tasks'][:count]];others=[t['id'] for t in body['tasks'][count:]]
    result=allocate_dedicated(body,draft(chosen,common=others),ByVehicleRoad(body))
    assert result['check']['valid'],result['check']
    assert len(result['routes'])==1 and result['routes'][0]['vehicle_id']=='BIG'
    assert Decimal(result['total_fee'])==Decimal(60+20*(count-1))+sum(Decimal(row['fee']) for row in result['common'])
    if count==4:assert not result['algorithm']['optimality_proven']
