"""Independent monetary/coverage oracle for small manual automatic-dedicated cases.

Only the production entry point and evidence envelope are reused. Allocation,
order feasibility, monetary formulas and county deduplication are enumerated here
without calling the production evaluator, normalizer or optimizer helpers.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import permutations, product
import random

import pytest

from app.auto_dedicated import allocate_dedicated
from app.road_service import evidence, road_request
from app.routing_engine import RoadError


class MatrixRoad:
    def __init__(self, roads, unavailable=()):
        self.roads=roads
        self.unavailable=set(unavailable)

    def segment(self, vehicle, origin, destination, departure):
        key=(vehicle['id'],origin['id'],destination['id'])
        if key in self.unavailable:
            raise RoadError('ROAD_UNAVAILABLE','独立测试缺失路段')
        meters,seconds=self.roads[key]
        return evidence(road_request(vehicle,origin,destination,departure,'FIXTURE'),meters,seconds,'FIXTURE')

    def stats(self):
        return {'calls':0,'source':'FIXTURE'}


def scenario(seed):
    rng=random.Random(seed)
    count=rng.randint(2,4)
    fixed_count=min(seed%3,count-1)
    rules={'amount':'7.25','billing_mode':'PER_REGION' if seed%2 else 'ONCE',
           'targets':[{'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市',
                       'district_code':'340104','district_name':'蜀山区'}]}
    if seed%7==0:
        rules['targets'].append({'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'','district_name':''})
    quotes={'K':{'mode':'KM','per_km':'1.17'},
            'B':{'mode':'BASE_STOPS','base_price':'9.25','included_stops':1,'extra_stop_price':'3.15'},
            'R':{'mode':'KM_REGION','per_km':'0.9','region_surcharge':rules}}
    tasks=[]
    for i in range(count):
        fixed=i<fixed_count
        code=rng.choice(['340104','340103'])
        tasks.append({'id':'T'+str(i),'store_id':'T'+str(i),'warehouse_id':'W' if fixed else rng.choice(['W','W','X']),
            'pieces':rng.randint(1,6),'quote_id':'K' if fixed else rng.choice(['K','B','R']),
            'common_unit_price':'0', # Cheaper common service must never replace an automatic dedicated ticket.
            'open_at':'2026-11-12T08:00:00+08:00','close_at':'2026-11-12T18:00:00+08:00',
            'latest_arrival_at':None,'unload_minutes':rng.randint(0,5),
            'province':'安徽省','city':'合肥市','district':'蜀山区' if code=='340104' else '庐阳区','region_code':code})
    fixed_ids=[t['id'] for t in tasks[:fixed_count]]
    rng.shuffle(fixed_ids)
    automatic=[t['id'] for t in tasks[fixed_count:]]
    cars=[{'id':'V'+str(i),'plate':'TEST'+str(i),'warehouse_id':'W' if i==0 else rng.choice(['W','X']),
           'max_pieces':rng.randint(3,13),'available':True if i==0 else rng.random()>.15} for i in range(2+seed%2)]
    cars[0]['max_pieces']=max(cars[0]['max_pieces'],sum(t['pieces'] for t in tasks[:fixed_count]))
    common=[];unassigned=[]
    for kind,enabled in [('C',seed%4==0),('U',seed%5==0)]:
        if enabled:
            tasks.append({**tasks[0],'id':kind,'store_id':kind,'pieces':2,'common_unit_price':'1.17'})
            (common if kind=='C' else unassigned).append(kind)
    labels=['W','X']+[t['id'] for t in tasks]
    roads={(v['id'],a,b):(rng.randint(1,12000),rng.randint(1,600))
           for v in cars for a in labels for b in labels if a!=b}
    body={'source':'FIXTURE','delivery_date':'2026-11-12','warehouses':[{'id':w,'earliest_departure_at':'2026-11-12T08:00:00+08:00'} for w in ['W','X']],
          'vehicles':cars,'quotes':quotes,'tasks':tasks}
    draft={'routes':[{'vehicle_id':'V0','task_ids':fixed_ids}] if fixed_ids else [],
           'common_ids':common,'unassigned_ids':unassigned,'auto_dedicated_ids':automatic}
    return body,draft,roads


def rounded_cents(value):
    return int((Decimal(value)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def route_cost(body, vehicle, order, roads, unavailable):
    tasks={t['id']:t for t in body['tasks']}
    rows=[tasks[i] for i in order]
    if not rows:return (0,0)
    if not vehicle['available'] or sum(t['pieces'] for t in rows)>vehicle['max_pieces']:
        return None
    if any(t['warehouse_id']!=vehicle['warehouse_id'] for t in rows):return None
    quote=body['quotes'][rows[0]['quote_id']]
    if any(body['quotes'][t['quote_id']]!=quote for t in rows):return None
    current=vehicle['warehouse_id'];meters=0
    departure=datetime.fromisoformat(next(w['earliest_departure_at'] for w in body['warehouses'] if w['id']==current))
    for task in rows:
        key=(vehicle['id'],current,task['id'])
        if key in unavailable:return None
        distance,duration=roads[key];meters+=distance
        arrival=departure+timedelta(seconds=duration)
        unloading=max(arrival,datetime.fromisoformat(task['open_at']))
        if unloading>datetime.fromisoformat(task['close_at']) or task.get('latest_arrival_at') and arrival>datetime.fromisoformat(task['latest_arrival_at']):return None
        departure=unloading+timedelta(minutes=task['unload_minutes']);current=task['id']
    if quote['mode'].startswith('KM'):
        fee=Decimal(meters)*Decimal(quote['per_km'])/1000
    else:
        fee=Decimal(quote['base_price'])+max(0,len(rows)-quote['included_stops'])*Decimal(quote['extra_stop_price'])
    rule=quote.get('region_surcharge')
    if rule:
        counties={task['region_code'] for task in rows if any(
            task['region_code']==target['district_code'] if target.get('district_code') else task['region_code'][:4]==target['city_code'][:4]
            for target in rule['targets'])}
        fee+=Decimal(rule['amount'])*(len(counties) if rule.get('billing_mode')=='PER_REGION' else int(bool(counties)))
    return rounded_cents(fee),meters


def independently_best(body, draft, roads, optimize_routes=False, unavailable=()):
    """Enumerate every ticket-to-car assignment, then every allowed full order."""
    cars=body['vehicles'];automatic=draft['auto_dedicated_ids']
    fixed={r['vehicle_id']:r['task_ids'] for r in draft['routes']}
    tasks={t['id']:t for t in body['tasks']}
    common_cost=sum(rounded_cents(Decimal(tasks[i]['pieces'])*Decimal(tasks[i]['common_unit_price'])) for i in draft['common_ids'])
    best=None
    for destinations in product([None]+[v['id'] for v in cars],repeat=len(automatic)):
        assigned={v['id']:list(fixed.get(v['id'],[])) for v in cars}
        for ticket,vehicle_id in zip(automatic,destinations):
            if vehicle_id is not None:assigned[vehicle_id].append(ticket)
        route_options=[];impossible=False
        for vehicle in cars:
            originals=fixed.get(vehicle['id'],[])
            valid=[]
            for order in permutations(assigned[vehicle['id']]):
                if not optimize_routes and [i for i in order if i in originals]!=originals:continue
                cost=route_cost(body,vehicle,order,roads,set(unavailable))
                if cost is not None:valid.append((cost[0],int(bool(order)),cost[1]))
            if not valid:impossible=True;break
            route_options.append(min(valid))
        if impossible:continue
        candidate=(destinations.count(None),common_cost+sum(r[0] for r in route_options),
                   sum(r[1] for r in route_options),sum(r[2] for r in route_options))
        if best is None or candidate<best:best=candidate
    assert best is not None
    return best


@pytest.mark.parametrize('seed',range(32))
def test_small_manual_allocation_matches_independent_exhaustive_oracle(seed):
    body,draft,roads=scenario(seed)
    expected=independently_best(body,draft,roads)
    result=allocate_dedicated(body,draft,MatrixRoad(roads))
    automatic=set(draft['auto_dedicated_ids'])
    actual=(sum(t['task_id'] in automatic for t in result['unassigned']),rounded_cents(result['total_fee']),
            len(result['routes']),sum(r['meters'] for r in result['routes']))
    assert actual==expected,(seed,actual,expected,result)
    assert {t['task_id'] for t in result['common']}==set(draft['common_ids'])
    assert set(draft['unassigned_ids']).issubset({t['task_id'] for t in result['unassigned']})
    assert not automatic.intersection(t['task_id'] for t in result['common'])
    fixed={t:r['vehicle_id'] for r in draft['routes'] for t in r['task_ids']}
    for route in result['routes']:
        vehicle=next(v for v in body['vehicles'] if v['id']==route['vehicle_id'])
        assert route_cost(body,vehicle,route['task_ids'],roads,set()) is not None
        assert all(route['vehicle_id']==fixed.get(t,route['vehicle_id']) for t in route['task_ids'])
        original=next((r['task_ids'] for r in draft['routes'] if r['vehicle_id']==route['vehicle_id']),[])
        assert [t for t in route['task_ids'] if t in original]==original
    assert result['check']['valid']==(expected[0]==0 and not draft['unassigned_ids'])
    if expected[0] or draft['unassigned_ids']:assert not result['algorithm']['optimality_proven']


@pytest.mark.parametrize('seed',range(8))
def test_optimized_manual_allocation_keeps_fixed_vehicles_and_matches_oracle(seed):
    body,draft,roads=scenario(seed+32)
    expected=independently_best(body,draft,roads,optimize_routes=True)
    result=allocate_dedicated(body,draft,MatrixRoad(roads),optimize_routes=True)
    automatic=set(draft['auto_dedicated_ids'])
    assert (sum(t['task_id'] in automatic for t in result['unassigned']),rounded_cents(result['total_fee']),len(result['routes']),sum(r['meters'] for r in result['routes']))==expected
    for fixed in draft['routes']:
        route=next(r for r in result['routes'] if r['vehicle_id']==fixed['vehicle_id'])
        assert set(fixed['task_ids']).issubset(route['task_ids'])


def test_missing_road_with_a_complete_alternative_does_not_claim_global_optimality():
    body,draft,roads=scenario(10)
    body['tasks']=body['tasks'][:2]
    for task in body['tasks']:task.update(warehouse_id='W',quote_id='K',pieces=1)
    body['vehicles']=[{**body['vehicles'][0],'max_pieces':10}]
    draft={'routes':[],'common_ids':[],'unassigned_ids':[],'auto_dedicated_ids':['T0','T1']}
    missing={(body['vehicles'][0]['id'],'W','T0')}
    expected=independently_best(body,draft,roads,unavailable=missing)
    result=allocate_dedicated(body,draft,MatrixRoad(roads,missing))
    assert not result['unassigned'] and result['check']['valid']
    assert rounded_cents(result['total_fee'])==expected[1]
    assert result['algorithm']['route_failure_count']>0
    assert not result['algorithm']['optimality_proven']
