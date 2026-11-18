"""Generic fragment proposals; no native selector, database or real network."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.road_service import evidence,road_request
from app.route_fragment_merging import generate_fragment_routes,greedy_fragment_pack
from app.route_search import JointSearch
from app.routing_engine import StopSearch,normalize


def problem(n=8,*,capacities=(8,),mandatory=False):
    return {'source':'FIXTURE','delivery_date':'2026-11-13',
        'warehouses':[{'id':'depot','longitude':'117','latitude':'31','crs':'GCJ02',
            'earliest_departure_at':'2026-11-13T08:00:00+08:00'}],
        'vehicles':[{'id':f'physical-{i}','warehouse_id':'depot','available':True,'max_pieces':cap} for i,cap in enumerate(capacities)],
        'quotes':{'q':{'mode':'KM','per_km':'1'}},
        'tasks':[{'id':f'task-{i:04}','store_id':f'task-{i:04}','warehouse_id':'depot','pieces':1,
            'quote_id':'q','common_unit_price':None if mandatory else '150','longitude':str(117+i*.001),
            'latitude':'31.1','crs':'GCJ02','city':'city-'+str(i%2),
            'open_at':'2026-11-13T08:00:00+08:00','close_at':'2026-11-13T23:00:00+08:00',
            'latest_arrival_at':None,'unload_minutes':0} for i in range(n)]}


class MatrixRoad:
    production_planning=False;call_limit=0;io_seconds=0.;force_refresh=False
    def __init__(self,body,*,missing=(),live=False,distances=None,durations=None):
        self.warehouses={w['id'] for w in body['warehouses']};self.points={t['id']:t for t in [*body['warehouses'],*body['tasks']]}
        self.missing=set(missing);self.lookups=Counter();self.network=[];self.fixture_reads=0
        self.production_planning=live;self.call_limit=None if live else 0
        self.settings=SimpleNamespace(road_calls_enabled=live)
        self.distances=distances or {};self.durations=durations or {}
    def value(self,a,b):
        if b['id'] in self.warehouses:return None
        return {'meters':self.distances.get((a['id'],b['id']),1000000 if a['id'] in self.warehouses else 1000),
                'seconds':self.durations.get((a['id'],b['id']),3600 if a['id'] in self.warehouses else 60)}
    def known_segment(self,a,b):
        key=a['id'],b['id'];self.lookups[key]+=1
        return None if key in self.missing else self.value(a,b)
    def segment(self,car,a,b,departure):
        key=a['id'],b['id']
        if key in self.missing:
            assert self.production_planning,'Frozen generation/materialization must not invent a missing edge'
            self.network.append(key);self.missing.remove(key)
        self.fixture_reads+=1;metric=self.value(a,b)
        assert metric is not None,'Open routes never require a return leg'
        return evidence(road_request(car,a,b,departure,'FIXTURE'),metric['meters'],metric['seconds'],'FIXTURE',False,
            restriction='VERIFIED_FIXTURE_ONLY')
    def stats(self):return {'calls':len(self.network),'call_limit':self.call_limit,'fixture_reads':self.fixture_reads}


def host(body,road=None,cancelled=lambda:False):
    return JointSearch(normalize(body),road or MatrixRoad(body),60,17,cancelled)


def test_default_disabled_has_no_host_side_effects():
    class Untouchable:
        def __getattr__(self,name):raise AssertionError('Disabled feature touched '+name)
    assert generate_fragment_routes(Untouchable())['stop_reason']=='DISABLED'


def test_opt_in_trace_reaches_central_metric_evaluator_through_wrapper():
    events=[]
    class Trace:
        def scope(self,name):return name
        def route(self,event,row,**kwargs):events.append((event,tuple(row['task_ids'])))
    body=problem(8,capacities=(8,));road=MatrixRoad(body);road.route_stage_trace=Trace();search=host(body,road)
    generate_fragment_routes(search,enabled=True,seconds=15)
    assert any(event=='CENTRAL_METRIC_REQUEST' and len(ids)>1 for event,ids in events)


def test_far_trunk_crosses_losing_prefix_and_matches_independent_one_car_oracle(monkeypatch):
    body=problem(8,capacities=(8,));search=host(body);before=deepcopy(search.best)
    original_consider=search.consider
    def no_solver(*a,**k):raise AssertionError('Builder must not call a global selector or publish an incumbent')
    monkeypatch.setattr(search,'pack',no_solver);monkeypatch.setattr(search,'consider',no_solver)
    details=generate_fragment_routes(search,enabled=True,seconds=15)
    packed=greedy_fragment_pack(search,seconds=5)
    assert details['candidate_count']>8 and details['counts']['dearer_than_common_columns_retained']>0
    assert max(len(row['task_ids']) for row in search.pool.values())==8
    assert search.best==before and packed['complete_proposal']
    assert len(packed['routes'])==1 and len(packed['routes'][0]['task_ids'])==8
    # Exactly one real car, uniform 1000km outbound + 1km per extra stop.
    # Every possible dedicated subset size is considered independently here.
    n=len(body['tasks']);common=Decimal('150')
    oracle=min([n*common,*[Decimal(1000+k-1)+(n-k)*common for k in range(1,n+1)]])
    assert sum(Decimal(row['fee']) for row in packed['routes'])==oracle
    assert original_consider(packed['routes'])
    assert not search.best['unassigned'] and Decimal(search.best['total_fee'])==oracle


def test_capacity_tiers_retain_small_car_orders_and_match_distinct_actual_cars():
    body=problem(6,capacities=(2,2,4),mandatory=True);search=host(body)
    result=generate_fragment_routes(search,enabled=True,seconds=15)
    assert [(p['capacity'],p['available_vehicle_count']) for p in result['partitions']]==[(2,2),(4,1)]
    assert any(len(row['task_ids'])==2 and search.data['vehicles'][row['vehicle_id']]['max_pieces']==2 for row in search.pool.values())
    assert any(len(row['task_ids'])==4 and search.data['vehicles'][row['vehicle_id']]['max_pieces']==4 for row in search.pool.values())
    packed=greedy_fragment_pack(search,seconds=5)
    assert packed['complete_proposal'] and packed['physical_vehicles_unique']
    assert len({i for row in packed['routes'] for i in row['task_ids']})==6
    assert search.consider(packed['routes']) and not search.best['unassigned']


def test_multiple_routes_are_not_all_bound_to_one_representative_car():
    body=problem(16,capacities=(8,8),mandatory=True);search=host(body)
    generate_fragment_routes(search,enabled=True,seconds=15)
    packed=greedy_fragment_pack(search,seconds=5)
    assert packed['complete_proposal'] and len(packed['routes'])==2
    assert len({r['vehicle_id'] for r in packed['routes']})==2
    assert sum(len(r['task_ids']) for r in packed['routes'])==16
    assert search.consider(packed['routes']) and not search.best['unassigned']


def test_cross_city_wait_unload_and_directional_time_windows_are_rechecked():
    body=problem(2,capacities=(2,),mandatory=True)
    a,b=[t['id'] for t in body['tasks']]
    body['warehouses'][0]['earliest_departure_at']='2026-11-13T23:30:00+08:00'
    body['tasks'][0].update(open_at='2026-11-14T00:00:00+08:00',close_at='2026-11-14T00:10:00+08:00',unload_minutes=10)
    body['tasks'][1].update(open_at='2026-11-13T23:00:00+08:00',close_at='2026-11-14T02:00:00+08:00',unload_minutes=20)
    road=MatrixRoad(body,distances={(b,a):100,(a,b):1000},
        durations={('depot',a):600,('depot',b):600,(a,b):300,(b,a):1200})
    search=host(body,road);result=generate_fragment_routes(search,enabled=True,seconds=15)
    orders={tuple(row['task_ids']) for row in search.pool.values()}
    assert (a,b) in orders and (b,a) not in orders
    assert result['counts']['merge_constraint_rejections']>=1
    packed=greedy_fragment_pack(search,seconds=5)
    assert packed['complete_proposal'] and search.consider(packed['routes'])
    route=search.best['routes'][0]
    assert {search.tasks[i]['city'] for i in route['task_ids']}=={'city-0','city-1'}
    assert route['stops'][0]['unload_start_at']=='2026-11-14T00:00:00+08:00'


def test_quote_and_warehouse_compatibility_are_not_relaxed():
    body=problem(4,capacities=(4,4))
    body['quotes']['other']={'mode':'BASE_STOPS','base_price':'2','included_stops':1,'extra_stop_price':'1'}
    body['tasks'][2]['quote_id']='other'
    body['warehouses'].append({**body['warehouses'][0],'id':'second-depot'})
    body['vehicles'][1]['warehouse_id']='second-depot';body['tasks'][3]['warehouse_id']='second-depot'
    search=host(body);generate_fragment_routes(search,enabled=True,seconds=15)
    for row in search.pool.values():
        assert len({search.tasks[i]['warehouse_id'] for i in row['task_ids']})==1
        assert len({search.tasks[i]['signature'] for i in row['task_ids']})==1


def test_unknown_edges_are_never_zero_cost_and_frozen_generation_never_collects():
    body=problem(5,capacities=(5,));ids=[t['id'] for t in body['tasks']]
    road=MatrixRoad(body,missing=[(a,b) for a in ids for b in ids if a!=b]);search=host(body,road)
    result=generate_fragment_routes(search,enabled=True,seconds=15)
    assert result['counts']['unpriced_pair_rejections']>0
    assert max(len(row['task_ids']) for row in search.pool.values())==1
    assert not road.network and road.fixture_reads==0
    # repricing every physical car reuses the same directed cache.
    assert max(road.lookups.values())==1


def test_production_collects_only_needed_missing_directions_outside_pool_observers():
    body=problem(5,capacities=(5,));ids=[t['id'] for t in body['tasks']]
    road=MatrixRoad(body,missing=[(a,b) for a in ids for b in ids if a!=b],live=True)
    search=host(body,road);in_callback=[False];original=road.segment
    def segment(*args,**kwargs):
        assert not in_callback[0]
        return original(*args,**kwargs)
    road.segment=segment
    def observer(recipe):
        in_callback[0]=True
        try:assert set(recipe)=={'vehicle_id','task_ids'}
        finally:in_callback[0]=False
    road.candidate_observer=observer
    result=generate_fragment_routes(search,enabled=True,seconds=15)
    assert road.network and result['counts']['collected_directed_metrics']==len(road.network)
    assert max(len(row['task_ids']) for row in search.pool.values())==5
    assert road.call_limit is None


def test_more_than_seven_stores_and_missing_coordinates_use_measured_relations():
    body=problem(12,capacities=(12,))
    for task in body['tasks']:task.update(longitude=None,latitude=None)
    search=host(body);result=generate_fragment_routes(search,enabled=True,seconds=15,seed=44)
    assert result['stop_reason']=='PROPOSALS_EXHAUSTED'
    assert max(len(row['task_ids']) for row in search.pool.values())==12
    assert not result['geometry_used_for_cost_or_time']


def test_optional_positive_net_falls_back_to_common_and_mandatory_never_disappears():
    body=problem(3,capacities=(1,));body['tasks'][0]['common_unit_price']=None
    search=host(body);generate_fragment_routes(search,enabled=True,seconds=15)
    packed=greedy_fragment_pack(search,seconds=5)
    assert packed['complete_proposal'] and len(packed['routes'])==1
    assert packed['routes'][0]['task_ids']==[body['tasks'][0]['id']]
    assert len(packed['common_ids'])==2 and not packed['unassigned_ids']
    body['tasks'][1]['common_unit_price']=None
    constrained=host(body);generate_fragment_routes(constrained,enabled=True,seconds=15)
    partial=greedy_fragment_pack(constrained,seconds=5)
    assert not partial['complete_proposal'] and len(partial['unassigned_ids'])==1
    assert not set(partial['unassigned_ids'])&set(partial['common_ids'])


def test_generation_phase_budget_and_global_cancel_preserve_incumbent(monkeypatch):
    body=problem(20,capacities=(20,));search=host(body);before=deepcopy(search.best)
    result=generate_fragment_routes(search,enabled=True,seconds=0)
    assert result['stop_reason']=='PHASE_BUDGET' and search.best==before
    cancelled=[False];search=host(body,cancelled=lambda:cancelled[0]);before=deepcopy(search.best)
    cancelled[0]=True
    with pytest.raises(StopSearch) as exc:generate_fragment_routes(search,enabled=True,seconds=15)
    assert exc.value.reason=='CANCELLED' and search.best==before
    class Clock:
        value=100.
        def __call__(self):self.value+=.0001;return self.value
    monkeypatch.setattr('app.route_fragment_merging.time.monotonic',Clock())
    ticking=host(body);before=deepcopy(ticking.best)
    details=generate_fragment_routes(ticking,enabled=True,seconds=.003)
    assert details['stop_reason']=='PHASE_BUDGET' and ticking.best==before


def test_outer_phase_cutoff_still_returns_current_pool_and_partition(monkeypatch):
    from app.route_portfolio import _phase
    class Clock:
        value=100.
        def __call__(self):self.value+=.0001;return self.value
    monkeypatch.setattr('app.route_fragment_merging.time.monotonic',Clock())
    search=host(problem(12,capacities=(12,)))
    result=_phase(search,'fragment-small-slice',.0005,
        lambda:generate_fragment_routes(search,enabled=True,seconds=20))
    assert result['details']['stop_reason']=='PHASE_BUDGET'
    assert 'partitions' in result['details'] and 'pool_after' in result['details']


def test_provider_wait_is_not_confused_with_generation_compute_slice(monkeypatch):
    class Clock:
        value=100.
        def __call__(self):return self.value
    clock=Clock();monkeypatch.setattr('app.route_fragment_merging.time.monotonic',clock)
    body=problem(5,capacities=(5,));ids=[t['id'] for t in body['tasks']]
    class WaitingRoad(MatrixRoad):
        def segment(self,*args,**kwargs):
            clock.value+=.25;self.io_seconds+=.25
            return super().segment(*args,**kwargs)
    road=WaitingRoad(body,missing=[(a,b) for a in ids for b in ids if a!=b],live=True)
    road.wall_deadline=clock.value+10
    search=host(body,road)
    result=generate_fragment_routes(search,enabled=True,seconds=.001)
    assert result['stop_reason']=='PROPOSALS_EXHAUSTED'
    assert result['elapsed_seconds']>=.25 and result['compute_seconds']==0
    assert result['new_network_calls']>0 and road.call_limit is None


def test_greedy_matching_does_not_lose_money_by_moving_a_mandatory_route_to_a_dearer_car():
    body=problem(2,capacities=(2,2));body['tasks'][0]['common_unit_price']=None
    search=host(body);a,b=[t['id'] for t in body['tasks']]
    # These compact columns represent different current vehicle-specific roads.
    rows=[('physical-0',a,'1.00'),('physical-1',a,'1000.00'),('physical-0',b,'2.00')]
    for vid,item,fee in rows:
        candidate={'vehicle_id':vid,'task_ids':[item],'fee':fee,'meters':1000,'seconds':1,'pieces':1,'errors':[],'_estimated':True}
        search.remember(candidate)
    packed=greedy_fragment_pack(search,seconds=5)
    assert packed['complete_proposal'] and len(packed['routes'])==1
    assert packed['routes'][0]['task_ids']==[a] and packed['routes'][0]['vehicle_id']=='physical-0'
    assert packed['common_ids']==[b]
    assert packed['counts']['nonimproving_reassignment_rejections']==1


def two_quote_warehouse_groups(*,high_cars,low_cars,mandatory_low=False):
    body=problem(2,capacities=(8,)*high_cars)
    high=body['warehouses'][0]['id'];low='another-warehouse'
    for task in body['tasks']:task['common_unit_price']='100'
    body['warehouses'].append({**body['warehouses'][0],'id':low})
    body['vehicles'].extend({'id':f'another-physical-{i}','warehouse_id':low,'available':True,'max_pieces':8} for i in range(low_cars))
    # The lower common bill deliberately has more stores; neither this count
    # nor an equal/larger fleet may put it ahead of the higher amount.
    body['tasks'].extend({**body['tasks'][0],'id':f'another-task-{i}','store_id':f'another-task-{i}',
        'warehouse_id':low,'common_unit_price':None if mandatory_low else '1'} for i in range(4))
    return body,high,low


@pytest.mark.parametrize('high_cars,low_cars',[(2,2),(1,4)],ids=['equal-fleet-count','low-value-larger-fleet'])
def test_group_order_uses_current_common_money_before_store_or_fleet_count(high_cars,low_cars):
    body,high,low=two_quote_warehouse_groups(high_cars=high_cars,low_cars=low_cars)
    search=host(body);result=generate_fragment_routes(search,enabled=True,seconds=15)
    passes=result['capacity_passes']
    assert [row['warehouse_id'] for row in passes]==[high,low]
    assert [row['available_vehicle_count'] for row in passes]==[high_cars,low_cars]
    assert Decimal(passes[0]['common_opportunity_fee'])>Decimal(passes[1]['common_opportunity_fee'])
    assert passes[0]['task_count']<passes[1]['task_count']
    assert not result['common_opportunity_is_realized_saving']


def test_mandatory_coverage_precedes_optional_common_money_priority():
    body,high,low=two_quote_warehouse_groups(high_cars=4,low_cars=1,mandatory_low=True)
    search=host(body);result=generate_fragment_routes(search,enabled=True,seconds=15)
    passes=result['capacity_passes']
    assert [row['warehouse_id'] for row in passes]==[low,high]
    assert passes[0]['mandatory_task_count']==4 and passes[1]['mandatory_task_count']==0
    assert Decimal(passes[0]['common_opportunity_fee'])==0<Decimal(passes[1]['common_opportunity_fee'])
