"""Road-priced regressions for whole-fleet search, beyond the exact tiny solver."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.road_service import evidence, road_request
from app.route_search import JointSearch, optimize_joint
from app.routing_engine import RoadError, assemble, check_result, evaluate_route, normalize


def clustered_problem(cars=4, common='100', capacity=9):
    tasks=[{'id':str(i),'store_id':str(i),'warehouse_id':'W','pieces':1,
            'quote_id':'Q','common_unit_price':common,'longitude':float(i//3),
            'latitude':float(i%3),'open_at':'2026-11-12T06:00:00+08:00',
            'close_at':'2026-11-12T23:59:00+08:00','unload_minutes':0,
            'latest_arrival_at':None} for i in range(9)]
    body={'source':'FIXTURE','delivery_date':'2026-11-12',
          'warehouses':[{'id':'W','longitude':0.,'latitude':0.,'earliest_departure_at':'2026-11-12T06:00:00+08:00'}],
          'vehicles':[{'id':f'V{i}','warehouse_id':'W','max_pieces':capacity,'available':True} for i in range(cars)],
          'quotes':{'Q':{'mode':'KM','per_km':'1'}},'tasks':tasks}
    class Road:
        def __init__(self):self.calls=0;self.samples=[]
        def segment(self,v,a,b,at):
            self.calls+=1
            meters=10000 if a['id']=='W' else 2000 if int(a['id'])//3==int(b['id'])//3 else 100000
            row=evidence(road_request(v,a,b,at,'FIXTURE'),meters,meters//100,'FIXTURE')
            self.samples.append(row)
            return row
        def stats(self):return {'calls':0,'fixture_segments':self.calls}
    return body,Road()


def test_more_dedicated_vehicles_beat_one_long_detour():
    body,road=clustered_problem()
    data=normalize(body)
    initial=assemble(data,[evaluate_route(data,'V0',list(data['tasks']),road)],[],[])
    assert initial['total_fee']=='222.00' and len(initial['routes'])==1
    result=optimize_joint(body,road,8,initial_result=initial)
    # Independent bound: every cluster requires 10 km from the warehouse and
    # 2+2 km within it; crossing clusters costs 100 km, so three cars cost 42.
    assert result['total_fee']=='42.00'
    assert len(result['routes'])==3
    assert result['common']==[] and result['unassigned']==[]
    assert check_result(data,result)['valid']
    assert result['algorithm']['joint_selection_runs']>0
    assert not result['algorithm']['optimality_proven']
    assert result['algorithm']['cost_objective']=='min_unassigned_then_total_fee_then_distance_then_vehicles'


def test_coverage_precedes_price_and_never_exceeds_physical_capacity():
    body,road=clustered_problem(cars=2,common=None,capacity=3)
    result=optimize_joint(body,road,4)
    assert len(result['unassigned'])==3
    assert sum(r['pieces'] for r in result['routes'])==6
    assert len({r['vehicle_id'] for r in result['routes']})==len(result['routes'])
    assert all(r['pieces']<=3 for r in result['routes'])
    assert {i['code'] for i in result['check']['issues']}=={'UNASSIGNED'}


@pytest.mark.parametrize('options,reason', [({'budget_seconds':0},'TIME_BUDGET'),({'cancelled':lambda:True},'CANCELLED')])
def test_stopping_retains_an_independently_checked_incumbent(options,reason):
    body,road=clustered_problem(cars=30)
    data=normalize(body)
    # A zero search budget must still allow an already evaluated warm-start.
    initial=assemble(data,[evaluate_route(data,f'V{i}',[str(i)],road) for i in range(9)],[],[])
    before=road.calls
    result=optimize_joint(body,road,initial_result=initial,**options)
    assert result['total_fee']==initial['total_fee']=='90.00'
    assert road.calls==before
    assert result['algorithm']['stop_reason']==reason
    assert check_result(data,result)['valid']


def test_tiny_exact_solver_keeps_incumbent_when_interrupted():
    from app.routing_engine import optimize
    body,road=clustered_problem(cars=1)
    body['tasks']=body['tasks'][:3]
    data=normalize(body)
    initial=assemble(data,[evaluate_route(data,'V0',list(data['tasks']),road)],[],[])
    result=optimize(body,road,budget_seconds=0,initial_result=initial)
    assert result['total_fee']==initial['total_fee']=='14.00'
    assert result['algorithm']['stop_reason']=='TIME_BUDGET'
    assert result['algorithm']['initial_plan_retained_or_improved']
    assert not result['algorithm']['optimality_proven']


@pytest.mark.parametrize('options,reason', [({'budget_seconds':0},'TIME_BUDGET'),({'cancelled':lambda:True},'CANCELLED')])
def test_more_initial_routes_than_raw_cache_retains_need_no_road_calls(options,reason):
    body,road=clustered_problem(cars=30)
    template=body['tasks'][0]
    body['tasks']=[{**deepcopy(template),'id':str(i),'store_id':str(i)} for i in range(30)]
    data=normalize(body)
    initial=assemble(data,[evaluate_route(data,f'V{i}',[str(i)],road) for i in range(30)],[],[])
    assert len(initial['routes'])==30
    before=road.calls
    result=optimize_joint(body,road,initial_result=initial,**options)
    assert len(result['routes'])==30
    assert result['total_fee']==initial['total_fee']=='300.00'
    assert result['unassigned']==[] and result['common']==[]
    assert road.calls==before
    assert result['algorithm']['stop_reason']==reason
    assert result['algorithm']['initial_plan_retained_or_improved']
    assert check_result(data,result)['valid']


def test_missing_road_edges_preserve_common_and_do_not_claim_optimality():
    body,_=clustered_problem()
    class MissingRoad:
        def segment(self,*args):raise RoadError('ROAD_CALL_BUDGET','Unknown road edge')
        def stats(self):return {'calls':0}
    result=optimize_joint(body,MissingRoad(),2)
    assert result['total_fee']=='900.00' and len(result['common'])==9
    assert result['unassigned']==[]
    assert result['algorithm']['unknown_road_candidates']>0
    assert not result['algorithm']['optimality_proven']


def test_old_cached_paths_are_hints_and_are_rechecked_through_adapter():
    body,road=clustered_problem()
    data=normalize(body)
    for n,ids in enumerate([['0','1','2'],['3','4','5'],['6','7','8']]):
        evaluate_route(data,f'V{n}',ids,road)
    samples=deepcopy(road.samples)
    # The hint's apparent distance is deliberately false; final prices must use
    # fresh adapter evaluations, rather than this sample's money or geometry.
    for item in samples:item.update(meters=1,seconds=1)
    road.route_seed_samples=samples
    search=JointSearch(data,road,4,0,lambda:False)
    search.setup_fleet();search.seed_cached_paths()
    assert search.cached_prefixes==9
    assert search.best['total_fee']=='42.00'
    assert len(search.best['routes'])==3
    assert check_result(data,search.best)['valid']
