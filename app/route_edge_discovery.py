"""Collect promising directed connections outside native callbacks.

Coordinates order proposals only. Every usable metric comes from the normal
road adapter, including its cache, shared QPS, cancellation and wall deadline.
The finite neighbourhood is a search operator, never an API call allowance.
"""
from collections import defaultdict
from decimal import Decimal
from math import cos, radians
from time import monotonic

from app.routing_engine import RoadError, StopSearch, common_fee


def compatible_edge_proposals(data, *, neighbours=6, incumbent=None, control=lambda:None):
    groups, fleet = defaultdict(list), defaultdict(list)
    for car in data['vehicles'].values():
        if car['available']:
            fleet[car['warehouse_id']].append(car)
    for item, task in data['tasks'].items():
        if task['signature'] is not None and any(
                c['max_pieces'] >= task['pieces'] for c in fleet[task['warehouse_id']]):
            groups[task['warehouse_id'], task['signature']].append(item)
    proposed = {}
    def add(origin, destination, priority=1):
        t = data['tasks'][destination]
        a = data['warehouses'].get(origin) or data['tasks'][origin]
        if origin == destination:
            return
        load = t['pieces'] + (a['pieces'] if origin in data['tasks'] else 0)
        cars = [c for c in fleet[t['warehouse_id']] if c['max_pieces'] >= load]
        if not cars:
            return
        opportunity = Decimal(common_fee(t['pieces'], t['common_unit_price']) or '0')
        mandatory = t['common_unit_price'] is None
        proposed[origin, destination] = (not mandatory, priority, -opportunity,
            min(cars, key=lambda c: (c['max_pieces'], c['id'])))
    def distance(a, b):
        try:
            latitude = (float(a['latitude']) + float(b['latitude'])) / 2
            return ((float(a['longitude']) - float(b['longitude'])) * cos(radians(latitude))) ** 2 + (
                float(a['latitude']) - float(b['latitude'])) ** 2
        except (KeyError, TypeError, ValueError):
            return float('inf')
    for (warehouse, _), ids in sorted(groups.items()):
        for item in sorted(ids):
            control()
            add(warehouse, item, 0)
            nearest = sorted((i for i in ids if i != item), key=lambda i: (
                distance(data['tasks'][item], data['tasks'][i]), i))[:neighbours]
            for other in nearest:
                add(item, other)
                add(other, item)
    # Entire current lines remain intact. Extra measured bridges allow later
    # operators to move whole tails or activate common tickets/idle vehicles.
    if incumbent:
        common = {row['task_id'] for row in incumbent['common']}
        for route in incumbent['routes']:
            if not route['task_ids']:
                continue
            end = route['task_ids'][-1]
            task = data['tasks'][end]
            eligible = [i for i in common if data['tasks'][i]['warehouse_id'] == task['warehouse_id']
                        and data['tasks'][i]['signature'] == task['signature']]
            for other in sorted(eligible, key=lambda i: (distance(task, data['tasks'][i]), i))[:neighbours]:
                add(end, other, 0)
                add(other, end, 0)
    return [(a, b, row[3]) for (a, b), row in sorted(proposed.items(), key=lambda pair: (
        (pair[1][0], pair[1][2], pair[1][1]), pair[0]))]


def collect_promising_edges(data, road, control, *, incumbent=None, phase_wall_seconds=None, diagnostics=None):
    started = monotonic()
    result = {'method':'COMPATIBLE_DIRECTED_NEIGHBOUR_DISCOVERY', 'new_network_calls':0,
              'proposed_directions':0, 'already_known':0, 'measured_directions':0,
              'failures':{}, 'candidate_count':0, 'fee_reduction':'0.00',
              'geometry_used_for_cost_or_time':False, 'total_call_limit':None}
    if diagnostics is not None:
        diagnostics.update(result)
        result = diagnostics
    result['phase_wall_seconds'] = phase_wall_seconds
    def tick():
        control()
        if phase_wall_seconds is not None and monotonic()-started >= phase_wall_seconds:
            raise StopSearch('EDGE_DISCOVERY_TIME_SHARE')
    if not getattr(road, 'production_planning', False):
        return {**result, 'skipped':True, 'stop_reason':'FROZEN_ROAD_COMPARISON_NO_HTTP', 'elapsed_seconds':0.}
    if not getattr(road.settings, 'road_calls_enabled', False):
        return {**result, 'skipped':True, 'stop_reason':'LIVE_CALLS_DISABLED', 'elapsed_seconds':0.}
    lookup = getattr(road, 'known_segment', None)
    if not callable(lookup):
        # Truck receipts bind the actual vehicle profile and departure time.
        # A missing shared matrix is an unsupported discovery capability, not
        # an infeasible task; normal per-vehicle road evaluation still applies.
        return {**result, 'skipped':True,
                'stop_reason':'VEHICLE_DEPENDENT_ADAPTER_NO_MATRIX', 'elapsed_seconds':0.}
    before = road.stats().get('calls', 0)
    points = {**data['warehouses'], **data['tasks']}
    try:
        proposals = compatible_edge_proposals(data, incumbent=incumbent, control=tick)
        result['proposed_directions'] = len(proposals)
        for a, b, car in proposals:
            tick()
            if lookup(points[a], points[b]) is not None:
                result['already_known'] += 1
                continue
            try:
                road.segment(car, points[a], points[b],
                             data['warehouses'][car['warehouse_id']]['earliest_departure_at'])
                result['measured_directions'] += 1
            except RoadError as exc:
                if exc.code in {'CANCELLED', 'WALL_BUDGET'}:
                    raise StopSearch(exc.code) from None
                result['failures'][exc.code] = result['failures'].get(exc.code, 0) + 1
        result['stop_reason'] = 'PROPOSALS_EXHAUSTED'
    except StopSearch as exc:
        result['stop_reason'] = exc.reason
        if exc.reason != 'EDGE_DISCOVERY_TIME_SHARE':
            raise
    finally:
        result.update(new_network_calls=max(0, road.stats().get('calls', 0)-before),
                      elapsed_seconds=round(monotonic()-started,6), skipped=False)
    return result
