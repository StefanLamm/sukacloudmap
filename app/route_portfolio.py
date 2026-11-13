"""Anytime cost-first portfolio over exactly priced, measured-road candidates.

The incumbent is always a completely checked delivery allocation. Native routing
and metric proposals merely expand a shared pool; no solver surrogate, loading
score, or unverified missing edge may replace that incumbent's real quotation.
"""
from decimal import Decimal
import time
from app.routing_engine import StopSearch, normalize, evaluate_route
from app.route_search import JointSearch, optimize_joint


def _phase(search,name,seconds,action):
    search.control()
    before=time.monotonic();deadline=search.compute_elapsed()+min(max(0,seconds),search.remaining())
    original=search.control;original_remaining=search.remaining;search.phase=name
    def controlled():
        original()
        if search.compute_elapsed()>=deadline:raise StopSearch('PHASE_BUDGET')
    search.control=controlled;stop='PHASE_COMPLETED';details=None
    search.remaining=lambda:max(0,min(original_remaining(),deadline-search.compute_elapsed()))
    try:
        details=action()
    except StopSearch as exc:
        stop=exc.reason
        if stop!='PHASE_BUDGET':raise
    finally:
        search.control=original
        search.remaining=original_remaining
    return {'method':name,'elapsed_seconds':round(time.monotonic()-before,4),'stop_reason':stop,
            'best_fee':search.best['total_fee'],'dedicated_vehicles':len(search.best_routes),
            'unassigned':len(search.best['unassigned']),'candidate_count':len(search.pool),
            **({'details':details} if isinstance(details,dict) else {})}


def _native(search,seconds,strategy,first,seed,penalty_multiplier=1):
    from app.route_ortools import generate_ortools_candidates
    lookup=getattr(search.road,'known_segment',None)
    if not callable(lookup):return {'stop_reason':'MATRIX_UNAVAILABLE'}
    def batch(specs):
        before=len(search.pool)
        for spec in specs:
            search.control()
            value=search.evaluate(spec['vehicle_id'],spec['task_ids'])
            if value is not None:search.quick_exchange(value)
        if len(search.pool)!=before:search.pack()
    specs,diagnostics=generate_ortools_candidates(search.data,
        lambda vehicle,a,b:lookup(a,b),max(0,seconds),seed,search.cancelled,
        initial_routes=search.best_routes,vehicle_independent=True,strategy=strategy,
        first_strategy=first,common_penalty_multiplier=penalty_multiplier,on_batch=batch)
    # Include any final trace not emitted because its native slice timed out.
    if search.remaining()>.05:batch(specs)
    return diagnostics


def _has_readonly_start(search):
    """A zero-HTTP adapter cannot invent a missing first road leg.

    Probe its actual evidence interface once per eligible first store/car,
    rather than spinning thousands of neighborhoods in an empty cache. An
    external exact-bound cache may hold edges absent from its metric index.
    """
    if getattr(search.road,'call_limit',None)!=0:return True
    for vehicle in search.cars:
        for task_id,task in search.tasks.items():
            if not task['signature'] or task['warehouse_id']!=vehicle['warehouse_id'] or task['pieces']>vehicle['max_pieces']:continue
            search.control()
            route=evaluate_route(search.data,vehicle['id'],[task_id],search.road,search.control)
            if search.route_ok(route):
                search.quick_exchange(search.remember(route))
                return True
    return False


def _optimize(body,road,budget_seconds=15,seed=0,cancelled=lambda:False,*,legacy=None,initial_result=None,native=True):
    data=normalize(body)
    if len(data['tasks'])<=8 and len(data['vehicles'])<=8 and legacy is not None:
        return optimize_joint(body,road,budget_seconds,seed,cancelled,legacy=legacy,initial_result=initial_result)
    search=JointSearch(data,road,budget_seconds,seed,cancelled,initial_result)
    phases=[];stop='SEARCH_COMPLETED';cycle=0
    try:
        search.setup_fleet()
        eligible=any(t['signature'] and any(v['warehouse_id']==t['warehouse_id'] and v['max_pieces']>=t['pieces'] for v in search.cars) for t in search.tasks.values())
        if not eligible:
            stop='NO_DEDICATED_CANDIDATES'
        elif not _has_readonly_start(search):
            stop='NO_AVAILABLE_ROAD_EVIDENCE'
        else:
            if search.initial_revalidation_required:
                phases.append(_phase(search,'revalidate_previous_route_hints',max(.1,budget_seconds*.25),search.revalidate_initial_routes))
            phases.append(_phase(search,'cached_path_seeds',min(3,budget_seconds*.1),search.seed_cached_paths))
            if native and callable(getattr(road,'known_segment',None)):
                duration=min(30,max(.1,budget_seconds*.28),search.remaining()*.45)
                phases.append(_phase(search,'guided_local_search',duration,
                    lambda:_native(search,duration,'GUIDED_LOCAL_SEARCH','PARALLEL_CHEAPEST_INSERTION',seed)))
            phases.append(_phase(search,'diverse_insertion',min(10,budget_seconds*.16),search.generate))
            from app.route_neighborhood import improve_neighborhood
            while search.remaining()>.02:
                search.control();cycle+=1
                duration=min(45,max(.1,search.remaining()*.72))
                phases.append(_phase(search,'adaptive_large_neighborhood',duration,
                    lambda:improve_neighborhood(search,seconds=duration,seed=seed+cycle*104729)))
                if search.remaining()>.1:
                    duration=min(2,max(.05,search.remaining()*.12))
                    phases.append(_phase(search,'joint_exact_cost_selection',duration,search.pack))
                if native and search.remaining()>2:
                    duration=min(20,search.remaining()*.3)
                    variant=('SIMULATED_ANNEALING','TABU_SEARCH','GUIDED_LOCAL_SEARCH')[(cycle-1)%3]
                    first=('LOCAL_CHEAPEST_INSERTION','SAVINGS','PARALLEL_CHEAPEST_INSERTION')[(cycle-1)%3]
                    multiplier=(.75,1.5,2,4,.5,1)[(cycle-1)%6]
                    phases.append(_phase(search,variant.lower(),duration,
                        lambda:_native(search,duration,variant,first,seed+cycle*31337,multiplier)))
                if len(phases)>64:phases=phases[:3]+phases[-61:]
    except StopSearch as exc:stop=exc.reason
    result=search.best
    result['algorithm']={'mode':'HYBRID_COST_PORTFOLIO' if native else 'ADAPTIVE_LARGE_NEIGHBORHOOD',
        'seed':seed,'stop_reason':stop,'candidate_count':search.evaluations,'distinct_route_count':len(search.pool),
        'evaluation_cache_hits':search.hits,'route_seed_count':search.seeds_examined,
        'cached_path_prefixes':search.cached_prefixes,'joint_selection_runs':search.pack_count,
        'elapsed_seconds':round(time.monotonic()-search.started,4),'budget_seconds':budget_seconds,
        'compute_seconds':round(search.compute_elapsed(),4),'wall_budget_seconds':search.wall_limit,
        'search_budget_seconds':search.search_seconds,'phase':search.phase,'methods':phases,
        'all_common_baseline':str(search.baseline.quantize(Decimal('.01'))) if not search.mandatory else None,
        'optimality_proven':False,'infeasibility_proven':False,'unknown_road_candidates':search.network_failures,
        'selection_diagnostics':search.pack_diagnostics[-8:],'road_stats':road.stats(),
        'cost_objective':'min_unassigned_then_total_fee_then_distance_then_vehicles',
        'initial_plan_retained_or_improved':search.initial_accepted,
        'initial_reference_revalidation_required':search.initial_revalidation_required,
        'candidate_metric_costs_are_proposals':True,'final_plan_independently_checked':True,
        'fleet_utilization':_fleet_utilization(search)}
    return result


def _fleet_utilization(search):
    dedicated_pieces=sum(r['pieces'] for r in search.best_routes)
    capacity=sum(search.data['vehicles'][r['vehicle_id']]['max_pieces'] for r in search.best_routes)
    return {'dedicated_pieces':dedicated_pieces,'dedicated_capacity':capacity,
        'loaded_percent':round(100*dedicated_pieces/capacity,2) if capacity else None,
        'dedicated_meters':sum(r['meters'] for r in search.best_routes),
        'per_vehicle':[{'vehicle_id':r['vehicle_id'],'pieces':r['pieces'],
            'capacity':search.data['vehicles'][r['vehicle_id']]['max_pieces'],
            'loaded_percent':round(100*r['pieces']/search.data['vehicles'][r['vehicle_id']]['max_pieces'],2)} for r in search.best_routes]}


def optimize_hybrid(body,road,budget_seconds=15,seed=0,cancelled=lambda:False,*,legacy=None,initial_result=None):
    return _optimize(body,road,budget_seconds,seed,cancelled,legacy=legacy,initial_result=initial_result,native=True)


def optimize_alns(body,road,budget_seconds=15,seed=0,cancelled=lambda:False,*,legacy=None,initial_result=None):
    return _optimize(body,road,budget_seconds,seed,cancelled,legacy=legacy,initial_result=initial_result,native=False)
