"""Exhaustive independent oracle for randomized small joint allocations."""
from datetime import datetime,timedelta
from decimal import Decimal,ROUND_HALF_UP
from itertools import permutations
import random

import pytest
from app.road_service import evidence,road_request
from app.routing_engine import optimize


def scenario(seed):
    rng=random.Random(seed);n=rng.randint(1,5);cars=rng.randint(0,3)
    quotes={'K':{'mode':'KM','per_km':str(rng.choice([0,1,5.8]))},'B':{'mode':'BASE_STOPS','base_price':str(rng.randint(0,12)),'included_stops':rng.randint(0,3),'extra_stop_price':str(rng.randint(0,5))}}
    tasks=[]
    for i in range(n):
        tasks.append({'id':str(i),'store_id':str(i),'warehouse_id':rng.choice(['W','X']),'pieces':rng.randint(1,9),'quote_id':rng.choice(['K','B',None]),
         'common_unit_price':rng.choice([None,'0','0.005','1.17','7.25']),'open_at':'2026-11-11T08:00:00+08:00','close_at':'2026-11-11T18:00:00+08:00','unload_minutes':rng.randint(0,10),
         'latest_arrival_at':rng.choice([None,'2026-11-11T08:20:00+08:00','2026-11-11T18:00:00+08:00'])})
    vs=[{'id':'V'+str(i),'warehouse_id':rng.choice(['W','X']),'max_pieces':rng.randint(3,20),'available':rng.random()>.1} for i in range(cars)]
    if seed>=200:
        for i,t in enumerate(tasks):t.update(warehouse_id='W',pieces=rng.randint(1,5),quote_id='K' if seed%2 or i%2 else 'B',latest_arrival_at=None)
        for v in vs:v.update(warehouse_id='W',available=True,max_pieces=rng.randint(5,20))
    labels=['W','X']+[t['id'] for t in tasks]
    roads={(v['id'],a,b):(rng.randint(0,12000),rng.randint(0,1200)) for v in vs for a in labels for b in labels if a!=b}
    body={'source':'FIXTURE','delivery_date':'2026-11-11','warehouses':[{'id':w,'earliest_departure_at':'2026-11-11T08:00:00+08:00'} for w in ['W','X']], 'vehicles':vs,'quotes':quotes,'tasks':tasks}
    class Road:
        def segment(self,v,a,b,at):
            m,s=roads[v['id'],a['id'],b['id']];return evidence(road_request(v,a,b,at,'FIXTURE'),m,s,'FIXTURE')
        def stats(self):return {'calls':0,'cache_hits':0}
    return body,Road(),roads


def oracle(body,roads):
    # Enumerate allocations per physical car and every stop ordering independently.
    def cents(value):return int((Decimal(value)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    tasks=body['tasks'];states={0:0}
    for v in body['vehicles']:
        choices={}
        if v['available']:
            for length in range(1,len(tasks)+1):
                for seq in permutations(range(len(tasks)),length):
                    rows=[tasks[i] for i in seq]
                    if sum(t['pieces'] for t in rows)>v['max_pieces'] or any(t['warehouse_id']!=v['warehouse_id'] for t in rows):continue
                    if rows[0]['quote_id'] is None or len({t['quote_id'] for t in rows})!=1:continue
                    previous=v['warehouse_id'];leave=datetime.fromisoformat('2026-11-11T08:00:00+08:00');meters=0;valid=True
                    for t in rows:
                        m,s=roads[v['id'],previous,t['id']];meters+=m;arrival=leave+timedelta(seconds=s)
                        start=max(arrival,datetime.fromisoformat(t['open_at']))
                        if start>datetime.fromisoformat(t['close_at']) or t['latest_arrival_at'] and arrival>datetime.fromisoformat(t['latest_arrival_at']):valid=False;break
                        leave=start+timedelta(minutes=t['unload_minutes']);previous=t['id']
                    if not valid:continue
                    q=body['quotes'][rows[0]['quote_id']]
                    cost=cents(Decimal(meters)/1000*Decimal(q['per_km'])) if q['mode']=='KM' else cents(Decimal(q['base_price'])+max(len(rows)-q['included_stops'],0)*Decimal(q['extra_stop_price']))
                    mask=sum(1<<i for i in seq);choices[mask]=min(choices.get(mask,cost),cost)
        next_states=dict(states)
        for used,cost in states.items():
            for mask,price in choices.items():
                if used&mask:continue
                next_states[used|mask]=min(next_states.get(used|mask,cost+price),cost+price)
        states=next_states
    outcomes=[]
    for mask,cost in states.items():
        missing=0
        for i,t in enumerate(tasks):
            if mask&(1<<i):continue
            if t['common_unit_price'] is None:missing+=1
            else:cost+=cents(Decimal(t['pieces'])*Decimal(t['common_unit_price']))
        outcomes.append((missing,cost))
    return min(outcomes)


@pytest.mark.parametrize('seed',range(1000))
def test_randomized_joint_allocation_matches_independent_oracle(seed):
    body,road,roads=scenario(seed);expected=oracle(body,roads);actual=optimize(body,road,budget_seconds=5)
    assert (len(actual['unassigned']),int(Decimal(actual['total_fee'])*100))==expected,(seed,actual,expected)
    assert actual['algorithm']['stop_reason']=='SEARCH_COMPLETED'
    assert actual['check']['valid']==(expected[0]==0)
    assert not actual['confirmable']
