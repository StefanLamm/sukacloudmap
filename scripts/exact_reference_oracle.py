"""Independent exhaustive oracle for complete small frozen-reference days.

Every ordered feasible route is considered within each warehouse/quote group;
then a physical-vehicle mask DP considers all dedicated/common allocations.
This is deliberately a benchmark oracle, not an online planning algorithm.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import time

from app.routing_engine import (RoadError, StopSearch, assemble, common_fee,
                                evaluate_route, line_fee, normalize)
from scripts.benchmark_algorithms import independent_audit


def exact_reference_oracle(body, road, budget_seconds=120, cancelled=lambda: False,
                           *, task_limit=12, group_limit=9, initial_result=None):
    started = time.monotonic()
    data = normalize(body)
    ids = sorted(data['tasks'])
    if len(ids) > task_limit:raise ValueError('Exact oracle task limit exceeded')
    indices = {item: 1 << n for n, item in enumerate(ids)}
    groups = defaultdict(list)
    for item, task in data['tasks'].items():
        if task['signature']:groups[task['warehouse_id'], task['signature']].append(item)
    if any(len(group) > group_limit for group in groups.values()):
        raise ValueError('Exact oracle quote-group limit exceeded')
    lookup = getattr(road, 'known_segment', None)
    if not callable(lookup):raise ValueError('Exact oracle requires a fixed measured graph')
    common_cost = {item: (int(Decimal(common_fee(task['pieces'], task['common_unit_price'])) * 100)
                         if task['common_unit_price'] is not None else None)
                   for item, task in data['tasks'].items()}
    common_ids = {item for item in ids if common_cost[item] is not None}
    mandatory = set(ids) - common_ids
    mandatory_mask = sum(indices[item] for item in mandatory)
    baseline = sum(value for value in common_cost.values() if value is not None)
    deadline = started + max(0, budget_seconds)
    enumerated = 0
    unknown = set()
    memo = {}
    per_vehicle = []
    vehicles = sorted([v for v in data['vehicles'].values() if v['available']], key=lambda v: v['id'])
    stop = 'EXHAUSTED_ALL_FEASIBLE_ORDERS_AND_ALLOCATIONS'
    enumeration_complete = dp_complete = True
    def control():
        if cancelled():raise StopSearch('CANCELLED')
        if time.monotonic() >= deadline:raise StopSearch('TIME_BUDGET')
    def cents(value):return int(Decimal(value) * 100)
    try:
        for vehicle in vehicles:
            control()
            # Ordinary-driving metrics are independent of a physical plate.
            # Equal capacity/start warehouses can share route enumeration;
            # the allocation DP still processes every real vehicle separately.
            key = vehicle['warehouse_id'], vehicle['max_pieces']
            if key not in memo:
                candidates = {}
                warehouse = data['warehouses'][vehicle['warehouse_id']]
                for (wid, _), group in sorted(groups.items()):
                    if wid != vehicle['warehouse_id']:continue
                    group = sorted(group)
                    optional_group = all(common_cost[item] is not None for item in group)
                    group_common = sum(common_cost[item] for item in group) if optional_group else None
                    def visit(path, origin, leave, mask, pieces, meters, seconds):
                        nonlocal enumerated
                        control()
                        for item in group:
                            bit = indices[item]
                            if mask & bit:continue
                            task = data['tasks'][item]
                            quantity = pieces + task['pieces']
                            if quantity > vehicle['max_pieces']:continue
                            metric = lookup(origin, task)
                            if metric is None:
                                unknown.add((origin['id'], item))
                                continue
                            distance, duration = metric.get('meters'), metric.get('seconds')
                            if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (distance, duration)):
                                unknown.add((origin['id'], item))
                                continue
                            arrival = leave + timedelta(seconds=duration)
                            unload = max(arrival, datetime.fromisoformat(task['open_at']))
                            if unload > datetime.fromisoformat(task['close_at']):continue
                            if task.get('latest_arrival_at') and arrival > datetime.fromisoformat(task['latest_arrival_at']):continue
                            new_path = path + [item]
                            total_meters, total_seconds = meters + distance, seconds + duration
                            new_mask = mask | bit
                            enumerated += 1
                            selected = [data['tasks'][i] for i in new_path]
                            fee = None
                            try:fee = cents(line_fee(selected[0]['quote'], total_meters, len(new_path), selected))
                            except RoadError:
                                # ONCE regional pricing can become certain when
                                # a later known target is added; keep exploring.
                                pass
                            if fee is not None:
                                opportunity = sum(common_cost[i] or 0 for i in new_path)
                                if any(i in mandatory for i in new_path) or fee < opportunity:
                                    row = {'task_ids': new_path, 'mask': new_mask, 'fee_cents': fee,
                                           'net_cents': fee - opportunity, 'meters': total_meters,
                                           'seconds': total_seconds, 'pieces': quantity}
                                    old = candidates.get(new_mask)
                                    score = fee, total_meters, total_seconds, tuple(new_path)
                                    if old is None or score < (old['fee_cents'], old['meters'], old['seconds'], tuple(old['task_ids'])):
                                        candidates[new_mask] = row
                            # Fees are nondecreasing with extra nonnegative road
                            # distance/stops/regions. This whole group cannot
                            # save money after its fee exceeds all common costs.
                            if fee is not None and optional_group and fee >= group_common:continue
                            visit(new_path, task, unload + timedelta(minutes=task['unload_minutes']),
                                  new_mask, quantity, total_meters, total_seconds)
                    visit([], warehouse, datetime.fromisoformat(warehouse['earliest_departure_at']), 0, 0, 0, 0)
                memo[key] = candidates
            per_vehicle.append((vehicle['id'], list(memo[key].values())))
    except StopSearch as exc:
        stop = exc.reason
        enumeration_complete = False
    states = {0: (0, 0, 0, ())}
    try:
        for vid, candidates in per_vehicle:
            control()
            following = dict(states)
            for index, (mask, previous) in enumerate(states.items()):
                if index % 64 == 0:control()
                for candidate in candidates:
                    if mask & candidate['mask']:continue
                    target = mask | candidate['mask']
                    value = (previous[0] + candidate['net_cents'], previous[1] + candidate['meters'],
                             previous[2] + 1, previous[3] + ((vid, candidate),))
                    old = following.get(target)
                    if old is None or value[:3] < old[:3]:following[target] = value
            states = following
    except StopSearch as exc:
        stop = exc.reason
        dp_complete = False
    mask, best = min(states.items(), key=lambda pair: ((mandatory_mask & ~pair[0]).bit_count(), baseline + pair[1][0], pair[1][1], pair[1][2]))
    routes = [evaluate_route(data, vid, row['task_ids'], road) for vid, row in best[3]]
    covered = {item for route in routes for item in route['task_ids']}
    result = assemble(data, routes, common_ids - covered, mandatory - covered)
    audit = independent_audit(data, result, road)
    if initial_result is not None:
        initial_audit = independent_audit(data, initial_result, road)
        if initial_audit['valid']:
            initial_rank = (Decimal(initial_result['total_fee']), initial_audit['road_meters'], initial_audit['used_vehicles'])
            result_rank = (Decimal(result['total_fee']), audit['road_meters'], audit['used_vehicles'])
            if not audit['valid'] or initial_rank < result_rank:
                result = deepcopy(initial_result)
                audit = initial_audit
                enumeration_complete = False
    exact = enumeration_complete and dp_complete and not unknown and audit['valid']
    report = {'scope': 'EXACT_SMALL_FROZEN_DRIVING_REFERENCE_MODEL', 'stop_reason': stop,
              'all_historical_tasks_retained': len(ids), 'quote_group_sizes': sorted(len(g) for g in groups.values()),
              'enumerated_order_prefixes': enumerated, 'unique_routes': sum(len(c) for c in memo.values()),
              'physical_vehicle_columns': sum(len(c) for _, c in per_vehicle), 'allocation_masks': len(states),
              'unknown_relevant_edges': len(unknown), 'enumeration_complete': enumeration_complete,
              'allocation_complete': dp_complete, 'frozen_model_optimality_proven': exact,
              'frozen_model_minimum_unassigned_proven': enumeration_complete and dp_complete and not unknown,
              'frozen_model_delivery_infeasible_proven': enumeration_complete and dp_complete and not unknown and bool(result['unassigned']),
              'real_world_global_optimality_proven': False, 'objective_order': ['unassigned', 'fee_cents', 'meters', 'vehicles'],
              'graph_hash': getattr(road, 'graph_hash', None), 'external_calls': 0,
              'elapsed_seconds': round(time.monotonic() - started, 6), 'audit': audit,
              'assumptions': ['OPEN_ROUTES_WITH_NO_RETURN_LEG', 'ONE_ROUTE_PER_PHYSICAL_VEHICLE',
                              'ORDINARY_DRIVING_METRICS_ARE_VEHICLE_INDEPENDENT',
                              'CURRENT_FROZEN_QUOTES_TIME_WINDOWS_CAPACITIES', 'UNSPLIT_STORE_TICKETS']}
    result['algorithm'] = {'mode': 'INDEPENDENT_EXACT_REFERENCE_ORACLE', 'optimality_proven': False,
                           'frozen_model_optimality_proven': exact, 'stop_reason': stop,
                           'candidate_count': enumerated, 'road_stats': road.stats()}
    return result, report


def optimize_exact_reference(body, road, budget_seconds=120, seed=0, cancelled=lambda: False, *, initial_result=None):
    return exact_reference_oracle(body, road, budget_seconds, cancelled, initial_result=initial_result)[0]
