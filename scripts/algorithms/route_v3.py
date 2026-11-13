"""Multi-start road-priced route generation and physical-fleet set selection.

Geometry selects which possibilities to query; every accepted route has measured
road evidence. Candidate rows omit repeated polylines, which are materialized
only when a complete allocation improves the incumbent.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import random
import time

from app.routing_engine import (StopSearch, assemble, check_result, common_fee,
                                evaluate_route, normalize)


class JointSearch:
    def __init__(self, data, road, seconds, seed, cancelled, initial_result=None):
        self.data,self.road=data,road
        self.tasks=data['tasks'];self.all_ids=set(self.tasks)
        self.indices={key:1<<n for n,key in enumerate(sorted(self.tasks))}
        self.common={key for key,row in self.tasks.items() if row['common_unit_price'] is not None}
        self.mandatory=self.all_ids-self.common
        self.common_cost={key:Decimal(common_fee(row['pieces'],row['common_unit_price']) or '0') for key,row in self.tasks.items()}
        self.baseline=sum(self.common_cost.values(),Decimal(0))
        self.started=time.monotonic();self.seconds=max(0,seconds)
        self.search_seconds=max(0,seconds-min(.25,seconds*.05))
        self.cancelled=cancelled;self.seed=seed;self.rng=random.Random(seed)
        self.evaluations=self.hits=self.failures=self.network_failures=0
        self.max_evaluations=min(250000,max(20000,int(seconds*250)))
        self.pool={};self.memo={};self.raw=OrderedDict();self.pack_diagnostics=[]
        self.reasons={};self.phase='baseline';self.seeds_examined=0;self.pack_count=0;self.cached_prefixes=0
        self.best=assemble(data,[],self.common,self.mandatory)
        self.best_routes=[];self.best_rank=self.rank([])
        self.cars=[];self.initial_accepted=False
        if initial_result is not None:self.adopt_initial(initial_result)

    def control(self):
        if self.cancelled():raise StopSearch('CANCELLED')
        if time.monotonic()-self.started>=self.search_seconds:raise StopSearch('TIME_BUDGET')
        if self.evaluations>=self.max_evaluations:raise StopSearch('CANDIDATE_BUDGET')

    def remaining(self):return max(0,self.search_seconds-(time.monotonic()-self.started))

    def rank(self, routes):
        # Allocations passed here are disjoint. Store each column's common
        # opportunity cost once rather than scanning every ticket per beam state.
        hard=0;net=Decimal(0);meters=0
        for route in routes:
            self.decorate(route)
            hard+=route['_hard'];net+=route['_net'];meters+=route['meters']
        return (len(self.mandatory)-hard,self.baseline+net,len(routes),meters)

    def decorate(self, candidate):
        if '_net' not in candidate:
            ids=candidate['task_ids']
            candidate['mask']=sum(self.indices[i] for i in ids)
            candidate['_hard']=sum(i in self.mandatory for i in ids)
            candidate['_net']=Decimal(candidate['fee'])-sum((self.common_cost[i] for i in ids),Decimal(0))
        return candidate

    def compact(self, route):
        row={key:deepcopy(route[key]) for key in ['vehicle_id','task_ids','fee','meters','seconds','pieces']}
        if 'arrival_end_at' in route:row['_leave']=route['arrival_end_at']
        return self.decorate(row)

    def route_ok(self, route):
        # Ordinary DRIVING references have no evaluator errors; their independent
        # proof still marks REFERENCE_ONLY and cannot be confirmed for execution.
        return route['fee'] is not None and not route['errors']

    def remember(self, route):
        candidate=self.compact(route);key=(candidate['vehicle_id'],candidate['mask'])
        prior=self.pool.get(key)
        if prior is None or (Decimal(candidate['fee']),candidate['meters'],candidate['seconds'],candidate['task_ids'])<(Decimal(prior['fee']),prior['meters'],prior['seconds'],prior['task_ids']):
            self.pool[key]=candidate
        cache_key=(candidate['vehicle_id'],tuple(candidate['task_ids']))
        self.memo[cache_key]=candidate
        self.raw[cache_key]=route;self.raw.move_to_end(cache_key)
        while len(self.raw)>24:self.raw.popitem(last=False)
        return candidate

    def evaluate(self, vid, ids):
        self.control();key=(vid,tuple(ids))
        if key in self.memo:
            self.hits+=1;return self.memo[key]
        self.evaluations+=1
        route=evaluate_route(self.data,vid,list(ids),self.road,self.control)
        if not self.route_ok(route):
            self.failures+=1
            if any(e['code'].startswith(('ROAD_','DRIVING_','TRUCK_','COORDINATES_')) for e in route['errors']):self.network_failures+=1
            reason='；'.join(dict.fromkeys(e['message'] for e in route['errors']))
            for item in ids:self.reasons[item]=reason
            self.memo[key]=None;return None
        return self.remember(route)

    def materialize(self, candidate):
        key=(candidate['vehicle_id'],tuple(candidate['task_ids']))
        if key in self.raw:return self.raw[key]
        self.control()
        route=evaluate_route(self.data,candidate['vehicle_id'],candidate['task_ids'],self.road,self.control)
        if not self.route_ok(route):return None
        # An adapter must bind every physical vehicle and departure timestamp.
        # Never synthesize evidence by relabeling a different car's result.
        self.remember(route)
        return route

    def consider(self, routes, *, force=False):
        used=0;cars=set()
        for route in routes:
            self.decorate(route)
            if route['vehicle_id'] in cars or used&route['mask']:return False
            used|=route['mask'];cars.add(route['vehicle_id'])
        rank=self.rank(routes)
        if not force and rank>=self.best_rank:return False
        full=[]
        for route in routes:
            value=self.materialize(route)
            if value is None:return False
            full.append(value)
        covered={i for r in full for i in r['task_ids']}
        result=assemble(self.data,full,self.common-covered,self.mandatory-covered,self.reasons)
        # Only reference-verification warnings may remain in otherwise feasible
        # road-priced plans. Monetary/allocation/time errors are never accepted.
        blocking=[e for e in result['check']['issues'] if e['code'] not in {'DRIVING_REFERENCE','UNASSIGNED'}]
        if blocking:return False
        actual=self.rank([self.compact(r) for r in full])
        if not force and actual>=self.best_rank:return False
        self.best=result;self.best_routes=[self.compact(r) for r in full];self.best_rank=actual
        return True

    def adopt_initial(self, result):
        try:
            proof=check_result(self.data,result)
            if any(e['code'] not in {'DRIVING_REFERENCE','UNASSIGNED'} for e in proof['issues']):return
            if any(not self.route_ok(r) for r in result['routes']):return
            full=deepcopy(result['routes'])
            rows=[self.remember(r) for r in full]
            rank=self.rank(rows)
            if rank<self.best_rank:
                covered={i for r in full for i in r['task_ids']}
                self.best=assemble(self.data,full,self.common-covered,self.mandatory-covered,self.reasons)
                self.best_routes=rows;self.best_rank=rank
                self.initial_accepted=True
        except (KeyError,ValueError,TypeError):
            return

    def setup_fleet(self):
        self.phase='vehicle_preflight'
        for vehicle in sorted(self.data['vehicles'].values(),key=lambda v:(v['warehouse_id'],-v['max_pieces'],v['id'])):
            self.control()
            if not vehicle['available']:continue
            if hasattr(self.road,'preflight'):
                try:self.road.preflight(vehicle,self.data['warehouses'][vehicle['warehouse_id']])
                except Exception as exc:
                    from app.routing_engine import RoadError
                    if not isinstance(exc,RoadError):raise
                    for key,row in self.tasks.items():
                        if row['warehouse_id']==vehicle['warehouse_id']:self.reasons[key]=exc.message
                    continue
            self.cars.append(vehicle)

    @staticmethod
    def proximity(a,b):
        try:
            # This is only a dimensionless ordering signal, never route distance.
            return (float(a['longitude'])-float(b['longitude']))**2+(float(a['latitude'])-float(b['latitude']))**2
        except (KeyError,TypeError,ValueError):return 0

    def gain(self, route):
        self.decorate(route)
        return route['_hard'],-route['_net']

    def seed_cached_paths(self):
        """Turn prior measured paths into columns without hard-coding any city.

        Original physical-car/departure bindings are useful route *hints*. Each
        prefix is still fully evaluated through the road adapter; samples never
        become monetary evidence just because they mention a store identifier.
        """
        samples=getattr(self.road,'route_seed_samples',{})
        if isinstance(samples,dict):samples=samples.values()
        adjacent={};topology={};vehicles={v['id']:v for v in self.cars}
        for sample in samples:
            try:
                request=sample['request'];vid=request['vehicle_id']
                if vid not in vehicles:continue
                key=(vid,request['from_id'],request['departure_at'])
                if request['to_id'] in self.tasks:
                    item=(request['to_id'],request['departure_at'],sample.get('seconds'))
                    adjacent.setdefault(key,{})[request['to_id']]=item
                    topology.setdefault((vid,request['from_id']),[]).append(item)
            except (KeyError,TypeError):continue
        if not adjacent:return
        self.phase='cached_path_generation'
        until=time.monotonic()+min(30,self.remaining()*.35)
        node_limit=min(20000,max(1000,int(self.seconds*100)))
        stack=[]
        for vid,vehicle in vehicles.items():
            wh=self.data['warehouses'][vehicle['warehouse_id']]
            stack.append((vid,wh['id'],wh['earliest_departure_at'],[],0))
        while stack and self.cached_prefixes<node_limit and time.monotonic()<until:
            self.control();vid,current,leave,path,pieces=stack.pop();vehicle=vehicles[vid]
            if len(path)>=40:continue
            # Follow the historic timestamp chain when possible. A changed live
            # travel duration must not erase its useful stop-order hint. If the
            # current snapshot differs, fall back to same-car/from topology;
            # old durations are used only to find the next historical hint.
            options=adjacent.get((vid,current,leave))
            if not options:
                options={}
                for item,departure,seconds in sorted(topology.get((vid,current),[]),key=lambda row:(row[1]!=leave,row[1])):
                    options.setdefault(item,(item,departure,seconds))
            ordered=sorted(set(options)-set(path),key=lambda i:(i not in self.mandatory,-self.common_cost[i],i))
            for item in ordered:
                task=self.tasks[item]
                if task['warehouse_id']!=vehicle['warehouse_id'] or pieces+task['pieces']>vehicle['max_pieces']:continue
                if path and self.tasks[path[0]]['signature']!=task['signature']:continue
                self.cached_prefixes+=1
                candidate=self.evaluate(vid,path+[item])
                if candidate is None:continue
                self.quick_exchange(candidate)
                next_leave=candidate.get('_leave')
                _,historic_departure,historic_seconds=options[item]
                try:
                    historic_arrival=datetime.fromisoformat(historic_departure)+timedelta(seconds=historic_seconds)
                    next_leave=(max(historic_arrival,datetime.fromisoformat(task['open_at']))+timedelta(minutes=task['unload_minutes'])).isoformat()
                except (TypeError,ValueError,OverflowError):pass
                if next_leave is not None:stack.append((vid,item,next_leave,path+[item],pieces+task['pieces']))
                if self.cached_prefixes>=node_limit or time.monotonic()>=until:break
        # Simultaneous replacement is essential: a useful fifth car may free
        # enough stores to shorten two existing lines, with no single-line win.
        self.pack()

    def quick_exchange(self, candidate):
        removed=[r for r in self.best_routes if r['vehicle_id']==candidate['vehicle_id'] or r['mask']&candidate['mask']]
        kept=[r for r in self.best_routes if r not in removed]
        self.consider(kept+[candidate])

    def grow(self, vehicle, seed, *, limit=30, policy='savings'):
        first=self.evaluate(vehicle['id'],[seed])
        if first is None:return
        self.quick_exchange(first);chain=first
        for _ in range(min(limit,len(self.tasks))-1):
            self.control()
            used=set(chain['task_ids']);last=self.tasks[chain['task_ids'][-1]]
            fitting=[i for i,t in self.tasks.items() if i not in used and t['warehouse_id']==vehicle['warehouse_id'] and t['signature']==self.tasks[seed]['signature'] and t['pieces']+chain['pieces']<=vehicle['max_pieces']]
            if not fitting:break
            near=sorted(fitting,key=lambda i:(self.proximity(last,self.tasks[i]),i))
            expensive=sorted(fitting,key=lambda i:(i not in self.mandatory,-self.common_cost[i],i))
            early=sorted(fitting,key=lambda i:(self.tasks[i].get('latest_arrival_at') or self.tasks[i]['close_at'],i))
            choices=list(dict.fromkeys(near[:3]+expensive[:2]+early[:1]))
            winner=None;score=None
            for item in choices:
                positions=[len(chain['task_ids'])]
                if policy=='insertion':
                    # Probe different orderings, not just nearest-next routes.
                    positions=list(dict.fromkeys([len(chain['task_ids']),0,len(chain['task_ids'])//2]))
                for position in positions:
                    ids=chain['task_ids'][:position]+[item]+chain['task_ids'][position:]
                    trial=self.evaluate(vehicle['id'],ids)
                    if trial is None:continue
                    self.quick_exchange(trial)
                    delta=Decimal(trial['fee'])-Decimal(chain['fee'])
                    value=(item in self.mandatory,self.common_cost[item]-delta,-trial['meters'],-trial['seconds'])
                    if score is None or value>score:winner=trial;score=value
            if winner is None:break
            # Negative immediate savings may lead to a profitable shared trunk.
            # Every prefix remains in the candidate pool for global selection.
            chain=winner

    def generate(self):
        self.phase='diverse_route_generation'
        eligible=[i for i,t in self.tasks.items() if t['signature'] and any(v['warehouse_id']==t['warehouse_id'] and v['max_pieces']>=t['pieces'] for v in self.cars)]
        covered={i for r in self.best_routes for i in r['task_ids']}
        priority=sorted(eligible,key=lambda i:(i not in self.mandatory,i in covered,-self.common_cost[i],i))
        # Every eligible store is a possible start; there is no first-four-nearby
        # truncation. Spread starts among physical cars before revisiting seeds.
        schedules=[priority,sorted(eligible,key=lambda i:(self.tasks[i]['warehouse_id'],str(self.tasks[i].get('city','')),i)),list(reversed(priority))]
        shuffled=list(priority);self.rng.shuffle(shuffled);schedules.append(shuffled)
        for pass_no,order in enumerate(schedules):
            for index,seed in enumerate(order):
                self.control()
                cars=[v for v in self.cars if v['warehouse_id']==self.tasks[seed]['warehouse_id'] and v['max_pieces']>=self.tasks[seed]['pieces']]
                if not cars:continue
                # Later passes give the same seed another physical vehicle and
                # insertion policy, allowing whole-fleet cost comparisons.
                vehicle=cars[(index+pass_no)%len(cars)]
                self.seeds_examined+=1
                self.grow(vehicle,seed,limit=30,policy='savings' if pass_no%2==0 else 'insertion')
                if index%max(1,min(len(cars),8))==0:self.pack()
            self.pack()

    def beam_pack(self):
        # Set-packing beam over the actual physical fleet; using more vehicles
        # is accepted whenever it reduces total delivery cost.
        by_car={v['id']:[] for v in self.cars}
        for row in self.pool.values():
            if row['vehicle_id'] in by_car:by_car[row['vehicle_id']].append(row)
        states={0:[]}
        for vehicle in self.cars:
            self.control();vid=vehicle['id']
            choices=sorted(by_car[vid],key=lambda r:(-self.gain(r)[0],-self.gain(r)[1],r['meters'],r['task_ids']))[:80]
            following=dict(states)
            for index,(mask,rows) in enumerate(states.items()):
                if index%16==0:self.control()
                for choice in choices:
                    if mask&choice['mask']:continue
                    target=mask|choice['mask'];proposal=rows+[choice]
                    old=following.get(target)
                    if old is None or self.rank(proposal)<self.rank(old):following[target]=proposal
            states=dict(sorted(following.items(),key=lambda item:self.rank(item[1]))[:128])
        for rows in sorted(states.values(),key=self.rank)[:8]:self.consider(rows)

    def pack(self):
        self.control();self.pack_count+=1;self.phase='joint_fleet_selection'
        selected=False
        try:
            from app.route_selection import select_candidate_routes
        except ImportError:
            select_candidate_routes=None
        if select_candidate_routes and self.pool:
            allowance=min(10,max(.05,self.remaining()*.15))
            answer=select_candidate_routes(self.data,list(self.pool.values()),self.best_routes,allowance,self.cancelled)
            if isinstance(answer,tuple):rows,diagnostics=answer
            else:rows=answer.get('routes',[]);diagnostics=answer.get('diagnostics',{})
            self.pack_diagnostics.append(diagnostics)
            selected=self.consider(rows)
        if not selected:self.beam_pack()
        self.phase='diverse_route_generation'

    def split_and_relocate(self):
        self.phase='split_merge_relocation'
        # Explicitly examine an additional car taking part of an existing route.
        # This escapes the old single-route exchange's "more common is cheaper"
        # trap when two shorter dedicated lines beat one long detour.
        for baseline in list(self.best_routes):
            ids=baseline['task_ids']
            if len(ids)<2:continue
            used_cars={r['vehicle_id'] for r in self.best_routes}
            available=[v for v in self.cars if v['warehouse_id']==self.tasks[ids[0]]['warehouse_id'] and v['id'] not in used_cars]
            for cut in sorted(set([1,len(ids)//2,len(ids)-1])):
                for vehicle in available:
                    left=self.evaluate(baseline['vehicle_id'],ids[:cut]);right=self.evaluate(vehicle['id'],ids[cut:])
                    if left and right:
                        self.consider([r for r in self.best_routes if r['vehicle_id']!=baseline['vehicle_id']]+[left,right])
        # One-stop relocation can improve two jointly affected lines even when
        # neither replacement alone would improve a complete allocation.
        snapshot=list(self.best_routes)
        for source in snapshot:
            for item in source['task_ids']:
                for target in snapshot:
                    if source['vehicle_id']==target['vehicle_id']:continue
                    if self.tasks[item]['warehouse_id']!=self.tasks[target['task_ids'][0]]['warehouse_id'] or self.tasks[item]['signature']!=self.tasks[target['task_ids'][0]]['signature']:continue
                    left_ids=[i for i in source['task_ids'] if i!=item]
                    left=self.evaluate(source['vehicle_id'],left_ids) if left_ids else None
                    if left_ids and left is None:continue
                    for position in sorted(set([0,len(target['task_ids'])//2,len(target['task_ids'])])):
                        right=self.evaluate(target['vehicle_id'],target['task_ids'][:position]+[item]+target['task_ids'][position:])
                        if not right:continue
                        kept=[r for r in self.best_routes if r['vehicle_id'] not in {source['vehicle_id'],target['vehicle_id']}]
                        self.consider(kept+([left] if left else [])+[right])
        self.pack()


def optimize_joint(body,road,budget_seconds=15,seed=0,cancelled=lambda:False,*,legacy=None,initial_result=None):
    data=normalize(body)
    if len(data['tasks'])<=8 and len(data['vehicles'])<=8 and legacy is not None:
        result=legacy(body,road,budget_seconds,seed,cancelled)
        if initial_result is not None:
            keeper=JointSearch(data,road,budget_seconds,seed,cancelled,initial_result)
            rank=(len(result['unassigned']),Decimal(result['total_fee']),len(result['routes']),sum(r['meters'] for r in result['routes']))
            if keeper.initial_accepted and keeper.best_rank<rank:
                kept=keeper.best
                kept['algorithm']={**result['algorithm'],'initial_plan_retained_or_improved':True,'optimality_proven':False}
                return kept
            result['algorithm']['initial_plan_retained_or_improved']=keeper.initial_accepted
        return result
    search=JointSearch(data,road,budget_seconds,seed,cancelled,initial_result)
    stop='SEARCH_COMPLETED'
    try:
        search.setup_fleet()
        if search.cars:
            search.seed_cached_paths()
            search.generate()
            search.split_and_relocate()
    except StopSearch as exc:stop=exc.reason
    result=search.best
    result['algorithm']={'mode':'MULTI_START_JOINT_FLEET','seed':seed,'stop_reason':stop,
        'candidate_count':search.evaluations,'distinct_route_count':len(search.pool),'evaluation_cache_hits':search.hits,
        'route_seed_count':search.seeds_examined,'cached_path_prefixes':search.cached_prefixes,'joint_selection_runs':search.pack_count,'phase':search.phase,
        'elapsed_seconds':round(time.monotonic()-search.started,4),'budget_seconds':budget_seconds,
        'search_budget_seconds':search.search_seconds,'all_common_baseline':str(search.baseline.quantize(Decimal('.01'))) if not search.mandatory else None,
        'optimality_proven':False,'infeasibility_proven':False,'unknown_road_candidates':search.network_failures,
        'selection_diagnostics':search.pack_diagnostics[-8:],'road_stats':road.stats(),
        'cost_objective':'min_unassigned_then_total_fee_then_vehicles','initial_plan_retained_or_improved':search.initial_accepted}
    return result
