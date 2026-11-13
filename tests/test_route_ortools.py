"""Independent, finite-network checks for measured OR-Tools candidates."""
from copy import deepcopy
from decimal import Decimal
from itertools import permutations

import pytest

from app.road_service import evidence, road_request
from app.route_ortools import generate_ortools_candidates, optimize_ortools
from app.routing_engine import assemble, check_result, evaluate_route, normalize


def problem(n=3, cars=2, quote=None, common='100', capacity=10):
    quote = quote or {'mode':'KM', 'per_km':'1'}
    tasks = [{'id':str(i), 'store_id':str(i), 'warehouse_id':'W', 'pieces':1,
              'quote_id':'Q', 'common_unit_price':common,
              'open_at':'2026-11-13T06:00:00+08:00',
              'close_at':'2026-11-13T23:00:00+08:00',
              'unload_minutes':0, 'latest_arrival_at':None} for i in range(n)]
    body = {'source':'FIXTURE', 'delivery_date':'2026-11-13', 'tasks':tasks,
            'quotes':{'Q':quote}, 'vehicles':[{'id':f'V{i}', 'warehouse_id':'W',
                        'max_pieces':capacity, 'available':True} for i in range(cars)],
            'warehouses':[{'id':'W', 'earliest_departure_at':'2026-11-13T06:00:00+08:00'}]}
    pairs = {(a,b):(1000,60) for a in ['W', *map(str,range(n))] for b in map(str,range(n)) if a != b}
    class Road:
        def __init__(self):self.evaluated=0
        def known_segment(self,a,b):
            value=pairs.get((a['id'],b['id']))
            return {'meters':value[0], 'seconds':value[1]} if value else None
        def segment(self,v,a,b,at):
            from app.routing_engine import RoadError
            value=self.known_segment(a,b)
            if value is None:raise RoadError('ROAD_UNAVAILABLE','Unknown measured fixture edge')
            self.evaluated+=1
            return evidence(road_request(v,a,b,at,'FIXTURE'),value['meters'],value['seconds'],'FIXTURE')
        def stats(self):return {'calls':0,'fixture_segments':self.evaluated}
    return body,Road(),pairs


def oracle(body,pairs):
    """Enumerate every stop order and physical-car partition, independently."""
    data=normalize(body);ids=list(data['tasks']);states={0:Decimal(0)}
    for car in body['vehicles']:
        choices={}
        for count in range(1,len(ids)+1):
            for ordering in permutations(range(len(ids)),count):
                rows=[data['tasks'][ids[i]] for i in ordering]
                if sum(r['pieces'] for r in rows)>car['max_pieces'] or len({r['quote_id'] for r in rows})!=1:continue
                old=car['warehouse_id'];meters=0
                for row in rows:
                    if (old,row['id']) not in pairs:break
                    meters+=pairs[old,row['id']][0];old=row['id']
                else:
                    q=rows[0]['quote'];mask=sum(1<<i for i in ordering)
                    cost=(Decimal(meters)/1000*Decimal(q['per_km']) if q['mode']=='KM'
                          else Decimal(q['base_price'])+max(count-q['included_stops'],0)*Decimal(q['extra_stop_price']))
                    choices[mask]=min(choices.get(mask,cost),cost)
        following=dict(states)
        for used,cost in states.items():
            for mask,fee in choices.items():
                if not used&mask:following[used|mask]=min(following.get(used|mask,cost+fee),cost+fee)
        states=following
    outcomes=[]
    for mask,cost in states.items():
        missing=0
        for i,key in enumerate(ids):
            if mask&(1<<i):continue
            row=data['tasks'][key]
            if row['common_unit_price'] is None:missing+=1
            else:cost+=Decimal(row['common_unit_price'])*row['pieces']
        outcomes.append((missing,cost.quantize(Decimal('.01'))))
    return min(outcomes)


@pytest.mark.parametrize('quote',[
    {'mode':'KM','per_km':'1'},
    {'mode':'BASE_STOPS','base_price':'10','included_stops':1,'extra_stop_price':'2'},
    {'mode':'BASE_STOPS','base_price':'10','included_stops':0,'extra_stop_price':'2'},
])
def test_common_penalties_and_route_pricing_match_small_independent_oracle(quote):
    body,road,pairs=problem(quote=quote)
    expected=oracle(body,pairs)
    result=optimize_ortools(body,road,1.1)
    assert (len(result['unassigned']),Decimal(result['total_fee']))==expected
    assert check_result(normalize(body),result)['valid']
    assert result['algorithm']['road_stats']['calls']==0
    assert result['algorithm']['routing_diagnostics']['quoted_fees_from_model'] is False
    assert not result['algorithm']['optimality_proven']


def test_shared_physical_vehicle_cannot_run_two_quote_groups_at_once():
    body,road,pairs=problem(n=4,cars=1)
    body['quotes']['B']={'mode':'BASE_STOPS','base_price':'10','included_stops':1,'extra_stop_price':'2'}
    for row in body['tasks'][2:]:row['quote_id']='B'
    expected=oracle(body,pairs)
    result=optimize_ortools(body,road,1.4)
    assert expected==(0,Decimal('202.00'))
    assert (len(result['unassigned']),Decimal(result['total_fee']))==expected
    assert len(result['routes'])==1 and len(result['common'])==2
    assert len({r['vehicle_id'] for r in result['routes']})==len(result['routes'])


def test_more_cars_are_used_when_they_reduce_total_spending():
    body,road,pairs=problem(n=9,cars=4)
    for a,b in pairs:
        pairs[a,b]=(10000 if a=='W' else 2000 if int(a)//3==int(b)//3 else 100000,60)
    result=optimize_ortools(body,road,2)
    assert result['total_fee']=='42.00'
    assert len(result['routes'])==3 and not result['common'] and not result['unassigned']
    assert check_result(normalize(body),result)['valid']


def test_same_fee_base_route_uses_measured_distance_as_secondary_objective():
    body,road,pairs=problem(n=3,cars=1,quote={'mode':'BASE_STOPS','base_price':'10','included_stops':3,'extra_stop_price':'2'})
    for key in pairs:pairs[key]=(10000,60)
    for key in [('W','0'),('0','1'),('1','2')]:pairs[key]=(1000,60)
    result=optimize_ortools(body,road,1)
    assert result['total_fee']=='10.00' and result['routes'][0]['meters']==3000
    assert result['routes'][0]['task_ids']==['0','1','2']
    assert result['algorithm']['routing_diagnostics']['distance_tie_break']


def test_missing_directed_road_is_forbidden_and_not_priced_as_zero():
    body,road,pairs=problem(n=2,cars=1)
    pairs.clear();pairs['W','0']=(1000,60)
    result=optimize_ortools(body,road,1)
    assert result['total_fee']=='101.00'
    assert result['routes'][0]['task_ids']==['0']
    assert len(result['common'])==1 and result['algorithm']['routing_diagnostics']['unknown_edges']>0
    assert check_result(normalize(body),result)['valid']


@pytest.mark.parametrize('mode,total',[('ONCE','13.00'),('PER_REGION','23.00'),('PER_STORE','33.00')])
def test_nonadditive_region_charges_are_recomputed_on_actual_route(mode,total):
    rule={'amount':'10','billing_mode':mode,'targets':[{'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'','district_name':''}]}
    body,road,pairs=problem(cars=1,quote={'mode':'KM_REGION','per_km':'1','region_surcharge':rule})
    for i,row in enumerate(body['tasks']):
        row.update(province='安徽省',city='合肥市',district='蜀山区' if i<2 else '包河区',region_code='340104' if i<2 else '340111')
    result=optimize_ortools(body,road,1)
    assert result['total_fee']==total
    assert len(result['routes'])==1 and not result['common']
    assert check_result(normalize(body),result)['valid']


def test_arrival_deadline_before_opening_still_allows_waiting_until_open():
    body,road,pairs=problem(n=1,cars=1)
    body['tasks'][0].update(open_at='2026-11-13T09:00:00+08:00',latest_arrival_at='2026-11-13T08:00:00+08:00')
    pairs['W','0']=(1000,3600)
    result=optimize_ortools(body,road,1)
    assert result['total_fee']=='1.00' and not result['common']
    assert result['routes'][0]['stops'][0]['unload_start_at']=='2026-11-13T09:00:00+08:00'
    assert check_result(normalize(body),result)['valid']


def test_actual_arrival_revalidation_rejects_a_model_relaxation():
    body,road,pairs=problem(n=1,cars=1)
    body['tasks'][0].update(open_at='2026-11-13T09:00:00+08:00',latest_arrival_at='2026-11-13T06:30:00+08:00')
    pairs['W','0']=(1000,7200)
    result=optimize_ortools(body,road,1)
    assert result['total_fee']=='100.00' and len(result['common'])==1
    assert not result['routes'] and result['algorithm']['rejected_candidates']>0
    assert check_result(normalize(body),result)['valid']


@pytest.mark.parametrize('options,reason',[({'budget_seconds':0},'TIME_BUDGET'),({'cancelled':lambda:True},'CANCELLED')])
def test_interruptions_keep_valid_incumbent_without_new_road_calls(options,reason):
    body,road,pairs=problem(n=3,cars=1)
    data=normalize(body);initial=assemble(data,[evaluate_route(data,'V0',list(data['tasks']),road)],[],[])
    before=road.evaluated
    result=optimize_ortools(body,road,initial_result=initial,**options)
    assert result['total_fee']=='3.00' and road.evaluated==before
    assert result['algorithm']['stop_reason']==reason
    assert check_result(data,result)['valid']


def test_solver_unavailable_falls_back_to_valid_incumbent(monkeypatch):
    import app.route_ortools as module
    body,road,pairs=problem(n=1,cars=1)
    data=normalize(body);initial=assemble(data,[evaluate_route(data,'V0',['0'],road)],[],[])
    def absent():raise ImportError('test missing optional solver')
    monkeypatch.setattr(module,'_ortools_api',absent)
    result=optimize_ortools(body,road,1,initial_result=initial)
    assert result['total_fee']=='1.00' and result['algorithm']['stop_reason']=='SOLVER_UNAVAILABLE'


def test_raw_candidate_specs_never_expose_model_money_as_a_quote():
    body,road,pairs=problem(n=2,cars=1)
    data=normalize(body)
    rows,diagnostics=generate_ortools_candidates(data,lambda v,a,b:road.known_segment(a,b),.15,
                                                vehicle_independent=True,strategy='GREEDY_DESCENT')
    assert rows and diagnostics['quoted_fees_from_model'] is False
    assert all(set(row)=={'vehicle_id','task_ids'} for row in rows)
    assert road.evaluated==0


def test_common_penalty_sampling_changes_specs_but_never_the_real_quote_or_input():
    body,road,pairs=problem(n=1,cars=1,common='10')
    pairs['W','0']=(15000,60)  # Dedicated15 vs actual common10.
    original=deepcopy(body);data=normalize(body)
    low,lo=generate_ortools_candidates(data,lambda v,a,b:road.known_segment(a,b),.2,
                vehicle_independent=True,strategy='GREEDY_DESCENT',common_penalty_multiplier=.5)
    high,hi=generate_ortools_candidates(data,lambda v,a,b:road.known_segment(a,b),.2,
                vehicle_independent=True,strategy='GREEDY_DESCENT',common_penalty_multiplier=2)
    assert not low and high and hi['surrogate_common_penalty_multiplier']=='2'
    assert lo['surrogate_common_penalty_multiplier']=='1/2'
    for factor in [.5,2]:
        result=optimize_ortools(body,road,.8,strategy='GREEDY_DESCENT',common_penalty_multiplier=factor)
        assert result['total_fee']=='10.00' and result['common'][0]['fee']=='10.00'
        assert not result['routes'] and not result['unassigned']
    assert body==original


@pytest.mark.parametrize('factor',[0,-1,.09,10.01,float('nan'),float('inf'),None,True])
def test_bad_common_penalty_factors_are_rejected_without_native_search(factor):
    body,road,pairs=problem(n=1,cars=1)
    with pytest.raises(ValueError):optimize_ortools(body,road,1,common_penalty_multiplier=factor)
    assert road.evaluated==0
