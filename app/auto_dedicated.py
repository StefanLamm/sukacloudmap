"""Constrained automatic dedicated allocation inside an otherwise manual draft."""
from copy import deepcopy
from decimal import Decimal
from itertools import permutations, product

from app.routing_engine import (StopSearch, assemble, evaluate_route, normalize,
                                optimize_manual, problem, recalculate)


def allocate_dedicated(body, draft, road, control=None, optimize_routes=False):
    """Keep explicit modes/vehicles and allocate auto tickets only to dedicated cars.

    Small inputs enumerate vehicle assignments and route insertions. Larger
    inputs use bounded insertion searches. Fixed-stop relative order is retained
    unless the user also enabled route optimization.
    """
    automatic=list(draft.get('auto_dedicated_ids', []))
    if not automatic:
        return (optimize_manual if optimize_routes else recalculate)(body,draft,road,control)
    data=normalize(body);tasks=data['tasks'];known=set(tasks)
    fixed=deepcopy(draft['routes']);common=list(draft['common_ids']);unassigned=list(draft['unassigned_ids'])
    all_ids=[i for route in fixed for i in route['task_ids']]+common+unassigned+automatic
    known_auto=[i for i in automatic if i in known]
    reasons={i:'已选择专车自动分配，但没有找到满足归仓、报价、容量、营业时间与道路条件的车辆线路；请增加车辆或调整选择。' for i in known_auto}
    baseline=recalculate(body,{'routes':fixed,'common_ids':common,'unassigned_ids':unassigned+known_auto},road,control)
    invalid=(len(all_ids)!=len(set(all_ids)) or set(all_ids)!=known
             or len({r['vehicle_id'] for r in fixed})!=len(fixed))
    if invalid:
        baseline['check']['issues'].append(problem('AUTO_DEDICATED_INPUT','自动分车草稿须完整覆盖任务，每票仅选择一种配送安排，且每辆车只能有一条固定线路。'))
        baseline['check']['valid']=baseline['confirmable']=False
        baseline['algorithm']={'mode':'MANUAL_AUTO_DEDICATED','stop_reason':'INVALID_DRAFT','optimality_proven':False,'automatic_dedicated_ids':automatic}
        return baseline
    fixed_by_car={r['vehicle_id']:list(r['task_ids']) for r in fixed}
    cars=sorted([v for v in data['vehicles'].values() if v['available']],key=lambda v:(v['max_pieces'],v['id']))
    best=baseline;best_rank=None;candidates=0;failures=0;stop_reason='SEARCH_COMPLETED'
    exact=(len(automatic)<=6 and len(cars)<=6 or len(automatic)<=2 and len(cars)<=50 or len(automatic)<=3 and len(cars)<=12) and sum(len(r['task_ids']) for r in fixed)+len(automatic)<=8
    def tick():
        if control:control()
        if candidates>=20000:raise StopSearch('CANDIDATE_BUDGET')
    def rank(result):
        errors=[e for r in result['routes'] for e in r['errors'] if e['code']!='ROAD_TIME_UNVERIFIED']
        return (sum(row['task_id'] in automatic for row in result['unassigned']),len(errors),
                sum(r['fee'] is None for r in result['routes']),Decimal(result['total_fee']),len(result['routes']),
                sum(r['meters'] for r in result['routes']))
    def keep(routes,missing):
        nonlocal best,best_rank
        result=assemble(data,routes,common,unassigned+list(missing),reasons)
        score=rank(result)
        if best_rank is None or score<best_rank:best=result;best_rank=score
    keep(baseline['routes'],known_auto)
    def fits(vehicle,ids,new_id):
        row=tasks[new_id]
        if row['warehouse_id']!=vehicle['warehouse_id'] or not row['signature']:return False
        if any(i not in tasks or tasks[i]['signature']!=row['signature'] or tasks[i]['warehouse_id']!=vehicle['warehouse_id'] for i in ids):return False
        return sum(tasks[i]['pieces'] for i in ids)+row['pieces']<=vehicle['max_pieces']
    def evaluated(vehicle_id,ids):
        nonlocal candidates,failures
        tick();candidates+=1
        route=evaluate_route(data,vehicle_id,list(ids),road,control)
        if route['fee'] is None or any(e['code']!='ROAD_TIME_UNVERIFIED' for e in route['errors']):failures+=1
        return route
    def usable(route):return route['fee'] is not None and not any(e['code']!='ROAD_TIME_UNVERIFIED' for e in route['errors'])
    try:
        if exact:
            # Permuting fixed stops is optional. Insertion enumerates all auto
            # orders and every position around the retained fixed sequence.
            starts=[(r['vehicle_id'],list(permutations(r['task_ids'])) if optimize_routes else [tuple(r['task_ids'])]) for r in fixed]
            options=product(*(orders for _,orders in starts)) if starts else [()]
            def visit(index,assignment,missing):
                tick()
                if index==len(automatic):
                    routes=[evaluated(vid,ids) for vid,ids in assignment.items() if ids]
                    # Auto tickets may only enter a feasible, priced route.
                    # Explicit invalid manual lines remain visible on their own.
                    if any(set(r['task_ids']).intersection(automatic) and not usable(r) for r in routes):return
                    keep(routes,missing);return
                item=automatic[index]
                for vehicle in cars:
                    ids=assignment.get(vehicle['id'],[])
                    if not fits(vehicle,ids,item):continue
                    for position in range(len(ids)+1):
                        changed={**assignment,vehicle['id']:ids[:position]+[item]+ids[position:]}
                        visit(index+1,changed,missing)
                visit(index+1,assignment,missing+[item])
            for values in options:
                initial={vid:list(order) for (vid,_),order in zip(starts,values)}
                visit(0,initial,[])
        else:
            # Large deliveries get several packing orders. Large pieces first
            # avoids spending the remaining capacity on small tickets too early.
            ordered=sorted(automatic,key=lambda i:(-tasks[i]['pieces'],tasks[i]['warehouse_id'],tasks[i]['signature'],i))
            orders=[('fee',ordered),('large',ordered),('fee',list(reversed(ordered))),('fee',sorted(automatic))]
            for policy,order in orders:
                assignment=deepcopy(fixed_by_car)
                routes={r['vehicle_id']:r for r in baseline['routes']};missing=[]
                for index,item in enumerate(order):
                    tick();chosen=None
                    for vehicle in cars:
                        ids=assignment.get(vehicle['id'],[])
                        if not fits(vehicle,ids,item):continue
                        previous=routes.get(vehicle['id']);previous_fee=Decimal(previous['fee']) if previous and previous['fee'] is not None else Decimal(0)
                        for position in range(len(ids)+1):
                            route=evaluated(vehicle['id'],ids[:position]+[item]+ids[position:])
                            if not usable(route):continue
                            delta=Decimal(route['fee'])-previous_fee
                            # A larger first vehicle can be cheaper once later
                            # stops share its initial trip. Compare this packing
                            # candidate with fee-first searches, never replace a
                            # lower-cost completed solution with it blindly.
                            score=((0 if ids else 1),-vehicle['max_pieces'],delta,route['meters'],vehicle['id']) if policy=='large' else (delta,vehicle['max_pieces']-route['pieces'],route['meters'],vehicle['id'])
                            if chosen is None or score<chosen[0]:chosen=(score,route)
                    if chosen:
                        route=chosen[1];assignment[route['vehicle_id']]=route['task_ids'];routes[route['vehicle_id']]=route
                    else:missing.append(item)
                    keep(list(routes.values()),missing+order[index+1:])
                keep(list(routes.values()),missing)
            if optimize_routes and not best['unassigned']:
                improved=optimize_manual(body,{'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':r['task_ids']} for r in best['routes']],
                    'common_ids':common,'unassigned_ids':unassigned},road,control)
                keep(improved['routes'],[])
    except StopSearch as exc:
        if exc.reason=='CANCELLED':raise
        stop_reason=exc.reason
    for row in best['unassigned']:
        if row['task_id'] in automatic:row['reason']=reasons[row['task_id']]
    if any(row['task_id'] in automatic for row in best['unassigned']):
        best['check']['issues'].append(problem('AUTO_DEDICATED_UNASSIGNED','部分已选专车的任务无法自动分配，已保留为异常；不会改成共配。'))
        best['check']['valid']=best['confirmable']=False
    best['algorithm']={'mode':'MANUAL_AUTO_DEDICATED','stop_reason':stop_reason,'candidate_count':candidates,
        'optimality_proven':exact and not failures and stop_reason=='SEARCH_COMPLETED' and best['check']['valid'],
        'automatic_dedicated_ids':automatic,'explicit_allocation_preserved':True,
        'fixed_order_preserved':not optimize_routes,'auto_dedicated_count':len(automatic),'route_failure_count':failures}
    return best
