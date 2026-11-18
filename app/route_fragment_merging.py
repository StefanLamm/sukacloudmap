"""Optional directed open-route fragment proposals, without a fleet solver.

Fragments are orders, not occupied physical vehicles. Even prefixes dearer
than common delivery enter the host pool. Physical-car assignment, common
delivery and the number of routes remain joint-selection decisions. Every
published plan still needs the host's normal full-road materialization/proof.
"""
from __future__ import annotations

from collections import Counter,defaultdict
from dataclasses import dataclass
from datetime import datetime,timedelta
from decimal import Decimal
from heapq import heappop,heappush,nsmallest
from math import cos,isfinite,radians
import random
import time

from app.route_metrics import evaluate_candidate
from app.routing_engine import RoadError,StopSearch,line_fee


class _PhaseBudget(Exception):pass


@dataclass(slots=True)
class _Fragment:
    number:int
    order:tuple
    pieces:int
    meters:int|None=None
    fee:Decimal|None=None
    leave:datetime|None=None
    priced:bool=False


class _Metrics:
    """Bounded-by-time directed lookup cache; unknown values stay unknown."""
    force_refresh=False
    def __init__(self,search,control,counts):
        self.search=search;self.control=control;self.counts=counts;self.cache={};self.failed=set()
        self.points={**search.data['warehouses'],**search.tasks}
        self.search_diagnostics=getattr(search.road,'search_diagnostics',None)
        trace=getattr(search.road,'route_stage_trace',None)
        if trace is not None:self.route_stage_trace=trace
    @staticmethod
    def valid(value):
        return (isinstance(value,dict) and all(isinstance(value.get(k),int) and not isinstance(value[k],bool)
                and value[k]>=0 for k in ('meters','seconds')))
    def get(self,a,b):
        key=a,b
        if key not in self.cache:
            self.control();self.counts['directed_metric_lookups']+=1
            try:value=self.search.road.known_segment(self.points[a],self.points[b])
            except RoadError as exc:
                if exc.code in {'CANCELLED','WALL_BUDGET'}:raise StopSearch(exc.code) from None
                value=None
            except (KeyError,ValueError,TypeError):value=None
            self.cache[key]={k:value[k] for k in ('meters','seconds')} if self.valid(value) else None
        return self.cache[key]
    def known_segment(self,a,b):return self.get(a['id'],b['id'])
    def collect(self,a,b,car,departure):
        value=self.get(a,b)
        if value is not None:return value
        if (a,b) in self.failed:
            self.counts['failed_direction_reuses']+=1;return None
        road=self.search.road
        if not (getattr(road,'production_planning',False) and
                bool(getattr(getattr(road,'settings',None),'road_calls_enabled',False))):
            self.counts['frozen_missing_edge_rejections']+=1;return None
        self.control()
        try:
            # No API is called from a native/host observation callback. The
            # shared adapter owns QPS, cancellation and the absolute deadline.
            value=road.segment(car,self.points[a],self.points[b],departure.isoformat())
        except RoadError as exc:
            if exc.code in {'CANCELLED','WALL_BUDGET'}:raise StopSearch(exc.code) from None
            self.failed.add((a,b));self.counts['road_failure_'+exc.code]+=1;return None
        if not self.valid(value):
            self.failed.add((a,b));self.counts['invalid_collected_metrics']+=1;return None
        self.cache[a,b]={k:value[k] for k in ('meters','seconds')}
        self.counts['collected_directed_metrics']+=1
        return self.cache[a,b]


class _Builder:
    def __init__(self,search,seconds,neighbours,seed):
        self.search=search;self.data=search.data;self.tasks=search.tasks
        self.started=time.monotonic();self.compute_started=search.compute_elapsed()
        remaining=max(0.,search.remaining())
        self.seconds=min(remaining,remaining if seconds is None else max(0.,float(seconds)))
        self.neighbours=neighbours;self.seed=search.seed if seed is None else seed
        self.counts=Counter();self.metrics=_Metrics(search,self.control,self.counts)
        self.next_fragment=0;self.serial=0;self.seen_orders=set();self.valid_orders=set();self.seen_bindings=set();self.specs=[]
        self.coords={}
        for item,t in self.tasks.items():
            try:
                xy=float(t['longitude']),float(t['latitude'])
                if all(isfinite(v) for v in xy):self.coords[item]=xy
            except (KeyError,ValueError,TypeError):pass
        self.cars=defaultdict(list)
        # A caller may not yet have a complete incumbent or setup_fleet().
        # Every current available physical car remains eligible for repricing.
        for car in sorted(self.data['vehicles'].values(),key=lambda c:(c['warehouse_id'],c['max_pieces'],c['id'])):
            if car['available']:self.cars[car['warehouse_id']].append(car)
        self.before_pool=len(search.pool)
        self.calls_before=search.road.stats().get('calls',0)
    def control(self):
        self.search.control()
        if self.search.compute_elapsed()-self.compute_started>=self.seconds:raise _PhaseBudget()
    def fragment(self,order):
        self.next_fragment+=1
        return _Fragment(self.next_fragment,tuple(order),sum(self.tasks[i]['pieces'] for i in order))
    def hydrate(self,fragment,spec,*,live=False):
        if fragment.priced:return True
        current=spec['warehouse'];leave=datetime.fromisoformat(self.data['warehouses'][current]['earliest_departure_at']);meters=0
        for item in fragment.order:
            metric=(self.metrics.collect(current,item,spec['car'],leave) if live else self.metrics.get(current,item))
            if metric is None:return False
            meters+=metric['meters'];task=self.tasks[item]
            leave=max(leave+timedelta(seconds=metric['seconds']),datetime.fromisoformat(task['open_at']))+timedelta(minutes=task['unload_minutes'])
            current=item
        try:fee=Decimal(line_fee(spec['quote'],meters,len(fragment.order),[self.tasks[i] for i in fragment.order]))
        except RoadError as exc:
            self.counts['price_failure_'+exc.code]+=1;return False
        fragment.meters,fragment.fee,fragment.leave,fragment.priced=meters,fee,leave,True
        return True
    def remember(self,fragment,evaluated=None):
        order=fragment.order
        if order not in self.seen_orders:self.seen_orders.add(order);self.counts['unique_orders']+=1
        warehouse=self.tasks[order[0]]['warehouse_id'];valid=False
        def retain(route):
            nonlocal valid
            key=route['vehicle_id'],order
            if key in self.seen_bindings:return
            if not self.search.route_ok(route):
                self.seen_bindings.add(key);self.counts['constraint_rejections']+=1;return
            self.search.remember(route);self.seen_bindings.add(key);valid=True;self.counts['columns_submitted']+=1
            if order not in self.valid_orders:
                self.valid_orders.add(order);self.counts['valid_orders']+=1
            if Decimal(route['fee'])>sum((self.search.common_cost[i] for i in order),Decimal(0)):
                self.counts['dearer_than_common_columns_retained']+=1
        if evaluated is not None:retain(evaluated)
        for car in self.cars[warehouse]:
            if car['max_pieces']<fragment.pieces:continue
            key=car['id'],order
            if key in self.seen_bindings:
                self.counts['duplicate_physical_orders']+=1;continue
            self.control()
            # Reprice current physical cars. Compact metric proposals have no
            # copied/relabelled geometry or road-evidence vehicle bindings.
            route=self.evaluate(car['id'],order)
            self.counts['physical_car_pricings']+=1
            if route is None:
                self.counts['unmeasured_orders']+=1;continue
            retain(route)
        return valid
    def evaluate(self,vehicle_id,order):
        self.control();self.search.evaluations+=1;self.counts['candidate_evaluations']+=1
        diagnostics=getattr(self.search.road,'search_diagnostics',None)
        if diagnostics is not None:diagnostics.counts['evaluated_candidates']+=1
        route=evaluate_candidate(self.data,vehicle_id,list(order),self.metrics,self.control)
        if route is not None and not self.search.route_ok(route):self.search.failed_route(route,list(order))
        return route
    def relation(self,spec,fragment,*,incoming=False):
        endpoint=fragment.order[0] if incoming else fragment.order[-1]
        candidates=[f for f in spec['active'].values() if f.number!=fragment.number and f.pieces+fragment.pieces<=spec['capacity']]
        if not candidates:return []
        xy=self.coords.get(endpoint)
        other=lambda f:f.order[-1] if incoming else f.order[0]
        if xy is not None:
            scale=cos(radians(xy[1]))
            def distance(f):
                point=self.coords.get(other(f))
                return (((point[0]-xy[0])*scale)**2+(point[1]-xy[1])**2, spec['ranks'][other(f)],f.number) if point is not None else (float('inf'),spec['ranks'][other(f)],f.number)
            known=[f for f in candidates if other(f) in self.coords]
            if known:return nsmallest(self.neighbours,known,key=distance)
        # Missing coordinates do not impose a store-count cutoff. Use measured
        # relationships within a deterministic seed neighbourhood; no geometry
        # or missing distance ever becomes a chargeable road metric.
        size=len(spec['ranks']);rank=spec['ranks'][endpoint]
        sample=nsmallest(self.neighbours*4,candidates,key=lambda f:((spec['ranks'][other(f)]-rank)%size,f.number))
        def measured(f):
            key=(other(f),endpoint) if incoming else (endpoint,other(f))
            value=self.metrics.get(*key)
            return (value is None,value['meters'] if value is not None else (spec['ranks'][other(f)]-rank)%size,f.number)
        return nsmallest(self.neighbours,sample,key=measured)
    def price_pair(self,spec,a,b,*,live=False):
        if a.pieces+b.pieces>spec['capacity']:return None
        if not self.hydrate(a,spec,live=live) or not self.hydrate(b,spec,live=live):return None
        bridge=(self.metrics.collect(a.order[-1],b.order[0],spec['car'],a.leave) if live else self.metrics.get(a.order[-1],b.order[0]))
        origin=self.metrics.get(spec['warehouse'],b.order[0])
        if bridge is None or origin is None:return None
        meters=a.meters+b.meters+bridge['meters']-origin['meters']
        order=a.order+b.order
        try:fee=Decimal(line_fee(spec['quote'],meters,len(order),[self.tasks[i] for i in order]))
        except RoadError as exc:
            self.counts['price_failure_'+exc.code]+=1;return None
        return a.fee+b.fee-fee,meters,fee
    def offer(self,spec,a,b):
        if a.number==b.number or a.pieces+b.pieces>spec['capacity']:return
        key=a.number,b.number
        if key in spec['offered']:return
        spec['offered'].add(key);self.serial+=1;self.counts['pair_proposals']+=1
        priced=self.price_pair(spec,a,b)
        entry=(a.number,b.number)
        if priced is not None:
            saving=priced[0]
            heappush(spec['priced'],(-saving,-len(a.order)-len(b.order),self.serial,entry))
        else:
            # This separate exploration queue has no assumed monetary value.
            # It gets turns even when cached priced proposals keep arriving.
            heappush(spec['unmeasured'],(self.serial,entry))
    def prepare_spec(self,warehouse,signature,ids,capacity,cars):
        shuffled=sorted(ids);random.Random(str(self.seed)+'|'+warehouse+'|'+signature).shuffle(shuffled)
        spec={'warehouse':warehouse,'signature':signature,'capacity':capacity,'car':cars[0],
              'car_count':len(cars),'quote':self.tasks[ids[0]]['quote'],'ranks':{i:j for j,i in enumerate(shuffled)},
              'active':{},'fragments':{},'priced':[],'unmeasured':[],'offered':set()}
        self.specs.append(spec)
        for item in sorted(ids):
            self.control()
            if self.tasks[item]['pieces']>capacity:continue
            f=self.fragment((item,));spec['active'][f.number]=f;spec['fragments'][f.number]=f
            self.hydrate(f,spec);self.remember(f)
        for f in list(spec['active'].values()):
            self.control()
            for other in self.relation(spec,f):self.offer(spec,f,other);self.offer(spec,other,f)
        return spec
    def merge_spec(self,spec):
        pop_count=0
        while spec['priced'] or spec['unmeasured']:
            self.control();pop_count+=1
            unknown=bool(spec['unmeasured'] and (not spec['priced'] or pop_count%(self.neighbours+1)==0))
            if unknown:_,pair=heappop(spec['unmeasured'])
            else:_,_,_,pair=heappop(spec['priced'])
            a=spec['active'].get(pair[0]);b=spec['active'].get(pair[1])
            if a is None or b is None:self.counts['stale_heap_entries']+=1;continue
            priced=self.price_pair(spec,a,b,live=unknown)
            if priced is None:self.counts['unpriced_pair_rejections']+=1;continue
            if unknown:
                # Fresh receipts are priced before entering the monetary heap;
                # never use a zero-distance placeholder to select a merge.
                self.remember(a);self.remember(b)
                self.serial+=1;heappush(spec['priced'],(-priced[0],-len(a.order)-len(b.order),self.serial,pair));continue
            order=a.order+b.order
            route=self.evaluate(spec['car']['id'],order)
            if route is None or not self.search.route_ok(route):
                self.counts['merge_constraint_rejections']+=1;continue
            merged=self.fragment(order)
            merged.meters=route['meters'];merged.fee=Decimal(route['fee']);merged.leave=datetime.fromisoformat(route['arrival_end_at']);merged.priced=True
            del spec['active'][a.number];del spec['active'][b.number]
            spec['active'][merged.number]=merged;spec['fragments'][merged.number]=merged
            self.counts['accepted_fragment_merges']+=1
            self.counts['negative_saving_merges']+=int(priced[0]<0)
            self.remember(merged,evaluated=route)
            for other in self.relation(spec,merged):self.offer(spec,merged,other)
            for other in self.relation(spec,merged,incoming=True):self.offer(spec,other,merged)
    def run(self):
        groups=defaultdict(list)
        for item,task in sorted(self.tasks.items()):
            if task['signature'] is not None and self.cars[task['warehouse_id']]:groups[task['warehouse_id'],task['signature']].append(item)
        priorities={group:(sum(item in self.search.mandatory for item in ids),
            sum((self.search.common_cost[item] for item in ids),Decimal(0))) for group,ids in groups.items()}
        # This is the current common bill that could at most be avoided, not a
        # predicted saving. Fleet/store counts must not outrank the money goal.
        ordered_groups=sorted(groups,key=lambda group:(-priorities[group][0],-priorities[group][1],group))
        specifications=[]
        for warehouse,signature in ordered_groups:
            ids=groups[warehouse,signature]
            tiers=defaultdict(list)
            for car in self.cars[warehouse]:tiers[car['max_pieces']].append(car)
            for capacity,cars in sorted(tiers.items(),key=lambda pair:(-len(pair[1]),pair[0])):
                specifications.append((capacity,warehouse,signature,ids,cars))
        self.counts['planned_capacity_group_passes']=len(specifications)
        reason='PROPOSALS_EXHAUSTED';passes=[]
        try:
            for capacity,warehouse,signature,ids,cars in specifications:
                self.control();self.counts['started_capacity_group_passes']+=1
                mandatory,opportunity=priorities[warehouse,signature]
                status={'pass_id':warehouse+'|'+signature+'|'+str(capacity),'warehouse_id':warehouse,
                    'quote_signature':signature,'mandatory_task_count':mandatory,'common_opportunity_fee':format(opportunity,'.2f'),
                    'capacity':capacity,'available_vehicle_count':len(cars),'task_count':len(ids),'status':'STARTED'}
                passes.append(status)
                spec=self.prepare_spec(warehouse,signature,ids,capacity,cars);self.merge_spec(spec)
                self.counts['completed_capacity_group_passes']+=1;status['status']='COMPLETED'
        except _PhaseBudget:reason='PHASE_BUDGET'
        except StopSearch as exc:
            if exc.reason!='PHASE_BUDGET':raise
            reason=exc.reason
        if reason=='PHASE_BUDGET' and passes and passes[-1]['status']=='STARTED':passes[-1]['status']='PHASE_BUDGET'
        partitions=[{'warehouse_id':s['warehouse'],'quote_signature':s['signature'],'capacity':s['capacity'],
            'available_vehicle_count':s['car_count'],'orders':[list(f.order) for f in sorted(s['active'].values(),key=lambda f:f.number)]} for s in self.specs]
        return {'method':'DIRECTED_OPEN_CLARKE_WRIGHT_FRAGMENTS','enabled':True,'stop_reason':reason,
            'elapsed_seconds':round(time.monotonic()-self.started,6),
            'compute_seconds':round(self.search.compute_elapsed()-self.compute_started,6),
            'budget_seconds':self.seconds,'counts':dict(self.counts),'candidate_count':self.counts['valid_orders'],
            'pool_before':self.before_pool,'pool_after':len(self.search.pool),'partitions':partitions,
            'capacity_passes':passes,'group_priority':'MANDATORY_COVERAGE_THEN_CURRENT_COMMON_FEE_UPPER_BOUND',
            'common_opportunity_is_realized_saving':False,
            'new_network_calls':max(0,self.search.road.stats().get('calls',0)-self.calls_before),
            'native_selection_runs':0,'fee_reduction':'0.00','complete_plan_published':False,
            'fragments_are_physical_vehicle_assignments':False,'geometry_used_for_cost_or_time':False,
            'total_call_limit':None if getattr(self.search.road,'production_planning',False) else 0,
            'clock_policy':'PHASE_COMPUTE_DELTA_EXCLUDES_PROVIDER_IO; HOST_AND_ROAD_GLOBAL_WALL_REMAIN_AUTHORITATIVE'}


def generate_fragment_routes(search,*,enabled=False,seconds=None,neighbours=8,seed=None):
    """Submit all valid singleton/merged prefixes; never call pack/consider.

    The caller supplies the generation slice and retains time for a cheap
    complete seed, actual materialization and its normal global selector.
    Global cancellation/wall/time stops propagate with the incumbent intact.
    """
    if not enabled:return {'method':'DIRECTED_OPEN_CLARKE_WRIGHT_FRAGMENTS','enabled':False,
        'stop_reason':'DISABLED','candidate_count':0,'native_selection_runs':0,'partitions':[]}
    if isinstance(neighbours,bool) or not isinstance(neighbours,int) or neighbours<1:raise ValueError('neighbours must be a positive integer')
    if seconds is not None and (isinstance(seconds,bool) or not isfinite(float(seconds)) or seconds<0):raise ValueError('seconds must be finite and nonnegative')
    if not callable(getattr(search.road,'known_segment',None)):
        return {'method':'DIRECTED_OPEN_CLARKE_WRIGHT_FRAGMENTS','enabled':True,'stop_reason':'MATRIX_UNAVAILABLE',
                'candidate_count':0,'native_selection_runs':0,'partitions':[]}
    return _Builder(search,seconds,neighbours,seed).run()


def greedy_fragment_pack(search,*,seconds=None):
    """Cheap disjoint-order selection plus augmenting physical-car matching.

    This is still a metric proposal. The caller must materialize the entire
    returned routes through search.consider before retaining a complete plan.
    Optional routes require negative current net cost; mandatory tickets never
    become common or disappear merely because this heuristic cannot place them.
    """
    if seconds is not None and (isinstance(seconds,bool) or not isfinite(float(seconds)) or seconds<0):raise ValueError('seconds must be finite and nonnegative')
    started=time.monotonic();compute_started=search.compute_elapsed()
    allowance=max(0.,search.remaining()) if seconds is None else min(max(0.,float(seconds)),max(0.,search.remaining()))
    def control():
        search.control()
        if search.compute_elapsed()-compute_started>=allowance:raise _PhaseBudget()
    recipes={};facts={};matched={};occupied={};used=0;counts=Counter();reason='GREEDY_PACKED';current_net=Decimal(0)
    try:
        for n,row in enumerate(list(search.pool.values())):
            if n%64==0:control()
            car=search.data['vehicles'].get(row['vehicle_id']);order=tuple(row['task_ids'])
            if not car or not car['available'] or row['fee'] is None or not order:continue
            if order not in facts:
                if any(i not in search.tasks for i in order) or len(set(order))!=len(order):
                    facts[order]=None
                else:
                    tasks=[search.tasks[i] for i in order]
                    if len({t['warehouse_id'] for t in tasks})!=1 or len({t['signature'] for t in tasks})!=1 or tasks[0]['signature'] is None:
                        facts[order]=None
                    else:
                        facts[order]=(sum(t['pieces'] for t in tasks),tasks[0]['warehouse_id'],
                            sum(i in search.mandatory for i in order),
                            sum((search.common_cost[i] for i in order),Decimal(0)),sum(search.indices[i] for i in order))
            if facts[order] is None:continue
            pieces,warehouse,hard,opportunity,mask=facts[order]
            if pieces>car['max_pieces'] or warehouse!=car['warehouse_id']:continue
            net=Decimal(row['fee'])-opportunity
            if not hard and net>=0:continue
            entry=recipes.setdefault(order,{'rows':{},'hard':hard,'mask':mask,'net':net,'common':opportunity})
            entry['rows'][car['id']]=row;entry['net']=min(entry['net'],net)
        ranked=sorted(recipes,key=lambda key:(not recipes[key]['hard'],-recipes[key]['hard'],recipes[key]['net'],-len(key),key))
        def augment(order,seen_orders,seen_cars):
            control()
            if order in seen_orders:return False
            seen_orders.add(order)
            cars=sorted(recipes[order]['rows'],key=lambda vid:(Decimal(recipes[order]['rows'][vid]['fee']),search.data['vehicles'][vid]['max_pieces'],vid))
            for vid in cars:
                if vid in seen_cars:continue
                seen_cars.add(vid);previous=occupied.get(vid)
                if previous is None or augment(previous,seen_orders,seen_cars):
                    occupied[vid]=order;matched[order]=vid;return True
            return False
        for order in ranked:
            control();entry=recipes[order]
            if entry['mask']&used:continue
            previous_matched,previous_occupied=dict(matched),dict(occupied)
            if augment(order,set(),set()):
                actual_net=sum((Decimal(recipes[key]['rows'][vid]['fee'])-recipes[key]['common'] for key,vid in matched.items()),Decimal(0))
                if not entry['hard'] and actual_net>=current_net:
                    matched,occupied=previous_matched,previous_occupied
                    counts['nonimproving_reassignment_rejections']+=1;continue
                current_net=actual_net;used|=entry['mask'];counts['accepted_orders']+=1
            else:counts['physical_matching_rejections']+=1
    except _PhaseBudget:reason='PHASE_BUDGET'
    except StopSearch as exc:
        if exc.reason!='PHASE_BUDGET':raise
        reason=exc.reason
    rows=[recipes[order]['rows'][vid] for order,vid in sorted(matched.items(),key=lambda p:p[1])]
    covered={i for row in rows for i in row['task_ids']};unassigned=sorted(search.mandatory-covered)
    return {'routes':rows,'common_ids':sorted(search.common-covered),'unassigned_ids':unassigned,
        'complete_proposal':not unassigned,'physical_vehicles_unique':len({r['vehicle_id'] for r in rows})==len(rows),
        'stop_reason':reason,'counts':dict(counts),'elapsed_seconds':round(time.monotonic()-started,6),
        'compute_seconds':round(search.compute_elapsed()-compute_started,6),'native_selection_runs':0,
        'metric_net_cost':str(current_net),'complete_plan_published':False}
