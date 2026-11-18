"""Bounded whole-line neighborhoods for joint dedicated/common reoptimization.

The caller explicitly enables this phase and owns its time allowance. Two to
four current lines are completely thawed together with related common tickets
and idle physical cars. Other lines and their cars remain fixed. Candidates
    use current road measurements, and only the host's full independent proof may
publish a strictly cheaper complete allocation. City labels impose no boundary.
"""
from copy import deepcopy
from decimal import Decimal
from itertools import chain
import math
import time

from app.road_service import DrivingRoad
from app.route_metrics import evaluate_candidate
from app.route_search import JointSearch
from app.route_small_groups import _measured_orders
from app.routing_engine import RoadError, StopSearch, assemble, check_result, evaluate_route


def _complete(data, result):
    proof = check_result(data, result)
    return (not result['unassigned'] and result['fees_complete'] and
            all(issue['code'] == 'DRIVING_REFERENCE' for issue in proof['issues']))


def _known(road, a, b):
    try:
        value = road.known_segment(a, b)
        if (isinstance(value, dict) and all(isinstance(value.get(name), int) and
                not isinstance(value[name], bool) and value[name] >= 0 for name in ('meters', 'seconds'))):
            return value
        diagnostics = getattr(road, 'search_diagnostics', None)
        if diagnostics is not None:
            diagnostics.missing(a, b)
    except RoadError as exc:
        if exc.code == 'CANCELLED':
            raise StopSearch('CANCELLED') from None
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _relation(search, left, right):
    """Measured directed proximity orders work; it never prices a proposal."""
    best = math.inf
    for a in left:
        for b in right:
            search.control()
            for origin, destination in ((a, b), (b, a)):
                edge = _known(search.road, search.tasks[origin], search.tasks[destination])
                if edge is not None:
                    best = min(best, edge['meters'])
    return best


def _scope(search, anchor_id, count, max_common):
    routes = list(search.best_routes)
    anchor = next((row for row in routes if row['vehicle_id'] == anchor_id), None)
    if anchor is None:
        return None
    warehouse = search.data['vehicles'][anchor_id]['warehouse_id']
    peers = [row for row in routes if row is not anchor and
             search.data['vehicles'][row['vehicle_id']]['warehouse_id'] == warehouse]
    if not peers:
        return None
    peers.sort(key=lambda row: (_relation(search, anchor['task_ids'], row['task_ids']),
                                -float(search.decorate(row)['_net']), row['vehicle_id']))
    selected = [anchor, *peers[:count - 1]]
    thawed_cars = {row['vehicle_id'] for row in selected}
    fixed = [row for row in routes if row['vehicle_id'] not in thawed_cars]
    frozen_cars = {row['vehicle_id'] for row in fixed}
    ids = set(chain.from_iterable(row['task_ids'] for row in selected))
    signatures = {search.tasks[item]['signature'] for item in ids}
    common = [row['task_id'] for row in search.best['common']
              if search.tasks[row['task_id']]['warehouse_id'] == warehouse and
              search.tasks[row['task_id']]['signature'] in signatures]
    # Preserve high-cost common opportunities as well as nearby tickets. Both
    # priorities are interleaved; neither is a constraint on accepted routing.
    near = sorted(common, key=lambda item: (_relation(search, ids, [item]),
                                            -search.common_cost[item], item))
    costly = sorted(common, key=lambda item: (-search.common_cost[item], item))
    added = list(dict.fromkeys(chain.from_iterable(zip(costly, near))))[:max_common]
    ids.update(added)
    cars = {vid: vehicle for vid, vehicle in search.data['vehicles'].items()
            if vehicle['available'] and vehicle['warehouse_id'] == warehouse and vid not in frozen_cars}
    data = {**search.data, 'tasks': {i: search.tasks[i] for i in sorted(ids)}, 'vehicles': cars}
    full_by_car = {row['vehicle_id']: row for row in search.best['routes']}
    initial = assemble(data, [deepcopy(full_by_car[row['vehicle_id']]) for row in selected], set(added), set())
    return data, initial, fixed, thawed_cars, added


def _scalable_columns(local, diagnostics):
    """Generate complementary whole-ticket columns beyond tiny enumeration.

    Tail exchanges and block relocation contribute *both* affected orders to
    one physical-fleet selector. No move is accepted by a distance surrogate or
    by a one-line saving. Each family gets a time slice; a large Cartesian
    neighborhood cannot silently starve common refill and idle-car activation.
    """
    paths = [tuple(route['task_ids']) for route in local.best_routes]
    seen = set()
    methods = []
    diagnostics['scalable_methods'] = methods
    diagnostics['scalable_order_count'] = 0

    def edge(a, b):
        if b is None:
            return 0
        origin = local.data['warehouses'][a] if a in local.data['warehouses'] else local.tasks[a]
        value = _known(local.road, origin, local.tasks[b])
        return value['meters'] if value is not None else math.inf

    def positions(order, block):
        warehouse = local.tasks[block[0]]['warehouse_id']
        # Ranking selects work order only. Unknown useful bridges are still
        # emitted after known ones and may be collected outside the callback.
        def delta(index):
            left = order[index - 1] if index else warehouse
            right = order[index] if index < len(order) else None
            old = edge(left, right)
            added = edge(left, block[0]) + edge(block[-1], right)
            return added - old if math.isfinite(added) and math.isfinite(old) else math.inf
        return sorted(range(len(order) + 1), key=lambda index: (delta(index), index))

    def tails(tick):
        for left_index, left in enumerate(paths):
            for right in paths[left_index + 1:]:
                if local.tasks[left[0]]['signature'] != local.tasks[right[0]]['signature']:
                    continue
                # Shorter transferred tails first, then the complete grid.
                cuts = sorted(((a, b) for a in range(len(left) + 1) for b in range(len(right) + 1)),
                              key=lambda pair: (abs((len(left) - pair[0]) - (len(right) - pair[1])), pair))
                for a, b in cuts:
                    tick()
                    yield left[:a] + right[b:]
                    yield right[:b] + left[a:]

    def blocks(tick):
        for source in paths:
            sizes = sorted({1, len(source), *(2 ** power for power in range(len(source).bit_length())
                                             if 2 ** power <= len(source))}, reverse=True)
            for size in sizes:
                for start in range(len(source) - size + 1):
                    tick()
                    block = source[start:start + size]
                    remainder = source[:start] + source[start + size:]
                    yield remainder
                    # A detached block is also an idle-car activation option.
                    yield block
                    for target in paths:
                        if target == source or local.tasks[target[0]]['signature'] != local.tasks[block[0]]['signature']:
                            continue
                        for position in positions(target, block):
                            tick()
                            yield target[:position] + block + target[position:]

    def refill(tick):
        common = sorted((row['task_id'] for row in local.best['common']),
                        key=lambda item: (-local.common_cost[item], item))
        for original in paths:
            chain = original
            current = propose(chain, tick)
            if current is None:
                continue
            remaining = [item for item in common if local.tasks[item]['signature'] == local.tasks[chain[0]]['signature']]
            while remaining:
                tick()
                best = None
                for item in remaining:
                    for position in positions(chain, (item,)):
                        tick()
                        order = chain[:position] + (item,) + chain[position:]
                        candidate = propose(order, tick)
                        if candidate is None:
                            continue
                        # Continue through a temporarily unprofitable prefix;
                        # a long shared trunk can become economical later.
                        gain = local.common_cost[item] - (Decimal(candidate['fee']) - Decimal(current['fee']))
                        score = gain, -candidate['meters'], tuple(order)
                        if best is None or score > best[0]:
                            best = score, order, candidate, item
                if best is None:
                    break
                _, chain, current, item = best
                remaining.remove(item)
        return
        yield  # Keep all families as cooperative generators.

    def activate(tick):
        occupied = {route['vehicle_id'] for route in local.best_routes}
        idle = [car for car in local.cars if car['id'] not in occupied]
        if not idle:
            return
        common_ids = {row['task_id'] for row in local.best['common']}
        priority = sorted(local.tasks, key=lambda item: (item not in common_ids, -local.common_cost[item], item))
        for seed in priority:
            tick()
            fleet = [car for car in idle if car['warehouse_id'] == local.tasks[seed]['warehouse_id']]
            if not fleet:
                continue
            chain = (seed,)
            current = propose(chain, tick)
            if current is None:
                continue
            remaining = [item for item in priority if item != seed and
                         local.tasks[item]['warehouse_id'] == local.tasks[seed]['warehouse_id'] and
                         local.tasks[item]['signature'] == local.tasks[seed]['signature']]
            while remaining:
                tick()
                best = None
                for item in remaining:
                    # Append and measured best insertion both generate full
                    # orders; capacities/windows are decided by real pricing.
                    for position in positions(chain, (item,)):
                        tick()
                        order = chain[:position] + (item,) + chain[position:]
                        candidate = propose(order, tick)
                        if candidate is None:
                            continue
                        gain = local.common_cost[item] - (Decimal(candidate['fee']) - Decimal(current['fee']))
                        score = gain, item in local.mandatory, -candidate['meters'], tuple(order)
                        if best is None or score > best[0]:
                            best = score, order, candidate, item
                if best is None:
                    break
                _, chain, current, item = best
                remaining.remove(item)
        return
        yield

    def propose(order, tick):
        tick()
        if not order:
            return None
        order = tuple(order)
        options = [car for car in local.cars if car['warehouse_id'] == local.tasks[order[0]]['warehouse_id']
                   and car['max_pieces'] >= sum(local.tasks[item]['pieces'] for item in order)]
        if not options:
            return None
        if order in seen:
            return next((local.memo[(car['id'], order)] for car in options
                         if local.memo.get((car['id'], order)) is not None), None)
        seen.add(order)
        diagnostics['scalable_order_count'] += 1
        capacities = {}
        for car in options:
            capacities.setdefault(car['max_pieces'], []).append(car['id'])
        best = None
        for peers in capacities.values():
            tick()
            candidate = local.evaluate(peers[0], order)
            if candidate is None:
                continue
            if best is None or Decimal(candidate['fee']) < Decimal(best['fee']):
                best = candidate
            for vid in peers[1:]:
                tick()
                local.remember({**candidate, 'vehicle_id': vid, 'errors': []})
                diagnostics['shared_metric_proposals'] += 1
        return best

    families = [('tail_exchange', tails), ('block_relocate_and_split', blocks),
                ('common_refill', refill), ('idle_car_growth', activate)]
    for index, (name, generate) in enumerate(families):
        local.control()
        started = local.compute_elapsed()
        deadline = started + local.remaining() * .8 / (len(families) - index)
        before = diagnostics['scalable_order_count']
        method = {'method': name, 'generated_orders': 0, 'stop_reason': 'COMPLETED'}
        methods.append(method)
        def tick():
            local.control()
            if local.compute_elapsed() >= deadline:
                raise StopSearch('SCALABLE_FAMILY_BUDGET')
        try:
            for order in generate(tick):
                propose(order, tick)
        except StopSearch as exc:
            if exc.reason != 'SCALABLE_FAMILY_BUDGET':
                raise
            method['stop_reason'] = exc.reason
        finally:
            method.update(generated_orders=diagnostics['scalable_order_count'] - before,
                          compute_seconds=round(local.compute_elapsed() - started, 6))


def reoptimize_joint_routes(search, *, seconds=None, max_routes=4, max_neighborhoods=6,
                            max_common=12, exact_stores=7, native=True, scalable=True):
    """Mutate ``search.best`` only after strict complete actual-fee improvement.

    Use as an independently switched phase in a JointSearch host. Returned
    diagnostics include each neighborhood's actual before/after fee and size.
    Exhaustive measured ordered subsets are used only for tiny compatible
    groups; larger groups receive full-line native routing and insertion seeds.
    A proven pool optimum is explicitly not a global routing optimum.
    """
    bounds = ((max_routes, 2, 4), (max_neighborhoods, 1, 64),
              (max_common, 0, 100), (exact_stores, 1, 8))
    if not isinstance(scalable,bool):
        raise ValueError('scalable must be a boolean')
    if any(isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
           for value, low, high in bounds):
        raise ValueError('Invalid bounded joint-neighborhood limits')
    started = time.monotonic()
    compute_started = search.compute_elapsed()
    allowance = search.remaining() if seconds is None else min(search.remaining(), max(0., float(seconds)))
    deadline = compute_started + allowance
    diagnostics = {'method': 'WHOLE_LINE_JOINT_REOPTIMIZATION', 'before_fee': search.best['total_fee'],
                   'after_fee': search.best['total_fee'], 'budget_seconds': allowance,
                   'max_thawed_routes': max_routes, 'neighborhoods': [], 'candidate_count': 0,
                   'complete_actual_improvements': 0, 'missing_metric_orders': 0,
                   'new_network_calls': 0, 'global_optimality_proven': False,
                   'scalable_search_enabled':scalable,
                   'stop_reason': 'SEARCH_COMPLETED', 'city_partition_imposed': False}
    search.control()
    if not callable(getattr(search.road, 'known_segment', None)):
        diagnostics['stop_reason'] = 'MATRIX_UNAVAILABLE'
        return diagnostics
    if not _complete(search.data, search.best):
        diagnostics['stop_reason'] = 'NO_COMPLETE_INCUMBENT'
        return diagnostics
    if allowance <= .005:
        diagnostics['stop_reason'] = 'NO_UNUSED_JOINT_BUDGET'
        return diagnostics
    original_control = search.control
    road = search.road
    previous_limit = getattr(road, 'call_limit', None)
    capped = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    calls_before = road.stats().get('calls', 0)
    if capped:
        road.call_limit = road.calls if previous_limit is None else min(road.calls, previous_limit)

    def control():
        original_control()
        if search.compute_elapsed() >= deadline:
            raise StopSearch('JOINT_NEIGHBORHOOD_BUDGET')

    search.control = control
    local = None
    try:
        anchors = sorted(search.best_routes, key=lambda row: (-search.decorate(row)['_net'], row['vehicle_id']))
        anchor_ids = [row['vehicle_id'] for row in anchors]
        seen = set()
        for index in range(max_neighborhoods):
            control()
            if not anchor_ids:
                diagnostics['stop_reason'] = 'NO_DEDICATED_ANCHOR_LINES'
                break
            count = 2 + index % (max_routes - 1)
            scope = _scope(search, anchor_ids[index % len(anchor_ids)], count, max_common)
            if scope is None:
                continue
            data, initial, fixed, thawed_cars, added = scope
            identity = tuple(sorted(thawed_cars)), tuple(sorted(data['tasks']))
            if identity in seen:
                continue
            seen.add(identity)
            remaining_neighborhoods = max(1, min(max_neighborhoods - index, len(anchor_ids)))
            seconds_left = max(0., deadline - search.compute_elapsed())
            local_seconds = seconds_left / remaining_neighborhoods
            if local_seconds < .02:
                break
            neighborhood_started = time.monotonic()
            neighborhood_compute_started = search.compute_elapsed()
            row = {'before_vehicles': len(search.best_routes), 'thawed_routes': len(thawed_cars), 'thawed_vehicle_ids': sorted(thawed_cars),
                   'task_count': len(data['tasks']), 'related_common_tasks': len(added),
                   'eligible_physical_cars': len(data['vehicles']),
                   'before_fee': search.best['total_fee'], 'after_fee': search.best['total_fee'],
                   'generated_orders': 0, 'missing_metric_orders': 0,
                   'live_connection_evaluations': 0,
                   'pruned_missing_prefixes': 0, 'pruned_capacity_prefixes': 0,
                   'shared_metric_proposals': 0, 'complete_actual_improvements': 0,
                   'native_selection_runs': 0, 'native_diagnostics': None,
                   'stop_reason': 'SEARCH_COMPLETED'}
            diagnostics['neighborhoods'].append(row)
            local = JointSearch(data, road, local_seconds, search.seed + index * 104729,
                                search.cancelled, initial)
            local.cars = sorted(data['vehicles'].values(), key=lambda car: (-car['max_pieces'], car['id']))
            local_control = local.control
            def tick():
                control()
                local_control()
            local.control = tick
            local_consider = local.consider
            selecting = False

            def publish():
                # Local proof is not an independently complete global plan.
                # Respect the outer job's compute/wall/cancellation controls
                # before starting any new global proof, even when every route
                # happens to remain in the raw evidence cache. A local scope's
                # own slice may have expired while this outer budget remains.
                original_control()
                if not _complete(data, local.best):
                    return False
                proposed = [*fixed, *(search.compact(route) for route in local.best_routes)]
                if search.rank(proposed)[0] or search.rank(proposed)[1] >= Decimal(search.best['total_fee']):
                    return False
                previous = search.best, search.best_routes, search.best_rank
                for route in local.best['routes']:
                    search.remember(route)
                if not search.consider(proposed):
                    return False
                if Decimal(search.best['total_fee']) >= Decimal(previous[0]['total_fee']):
                    search.best, search.best_routes, search.best_rank = previous
                    return False
                row['complete_actual_improvements'] += 1
                diagnostics['complete_actual_improvements'] += 1
                return True

            def consider(candidates, *, force=False):
                improved = local_consider(candidates, force=force)
                if improved and (selecting or not scalable):
                    publish()
                return improved
            local.consider = consider

            # Production proposals may collect necessary directed connections
            # outside native callbacks. Frozen/research matrices remain closed.
            def evaluate(vid, ids):
                local.control()
                key = vid, tuple(ids)
                if local.stage_trace is not None:
                    local.stage_trace.route('PRICE_REQUEST',{'vehicle_id':vid,'task_ids':ids},scope=local.trace_scope)
                telemetry = getattr(road, 'search_diagnostics', None)
                if key in local.memo:
                    local.hits += 1
                    if telemetry is not None:
                        telemetry.counts['evaluation_cache_hits'] += 1
                    if local.stage_trace is not None:
                        local.stage_trace.route('PRICE_CACHE',{'vehicle_id':vid,'task_ids':ids},
                            status='VALID' if local.memo[key] is not None else 'REJECTED',scope=local.trace_scope)
                    return local.memo[key]
                local.evaluations += 1
                if telemetry is not None:
                    telemetry.counts['evaluated_candidates'] += 1
                candidate = evaluate_candidate(data, vid, list(ids), road, local.control)
                if candidate is None:
                    row['missing_metric_orders'] += 1
                    if local.stage_trace is not None:
                        local.stage_trace.route('PRICE_METRIC_MISSING',{'vehicle_id':vid,'task_ids':ids},scope=local.trace_scope)
                    if not getattr(road, 'production_planning', False):
                        local.memo[key] = None
                        return None
                    # Full evaluation binds the actual car and evolving leave
                    # times. Adapter QPS/cancellation/wall deadline still apply.
                    candidate = evaluate_route(data, vid, list(ids), road, local.control)
                    local.control()
                    row['live_connection_evaluations'] += 1
                if not local.route_ok(candidate):
                    local.failed_route(candidate, ids)
                    local.memo[key] = None
                    return None
                return local.remember(candidate)
            local.evaluate = evaluate
            native_pack = local.pack
            def pack():
                nonlocal selecting
                if local.remaining() >= .075:
                    row['native_selection_runs'] += 1
                    selecting = True
                    try:
                        native_pack()
                    finally:
                        selecting = False
            local.pack = pack
            # Every proposal enters the same joint selector. A single-column
            # exchange cannot publish a local allocation before complements
            # (including required-ticket remnants) have been considered.
            if scalable:
                local.quick_exchange = lambda candidate: None
            try:
                publish()
                groups = {}
                for item, task in data['tasks'].items():
                    if task['signature']:
                        groups.setdefault((task['warehouse_id'], task['signature']), []).append(item)
                large = any(len(ids) > exact_stores for ids in groups.values())
                generation_deadline = local.compute_elapsed() + local.remaining() * (.18 if large and scalable else .65)
                def generation_control():
                    local.control()
                    if local.compute_elapsed() >= generation_deadline:
                        raise StopSearch('JOINT_GENERATION_BUDGET')
                try:
                    # Reuse this job's measured columns inside the thawed scope;
                    # frozen tickets/cars can never enter its local set selector.
                    for number, candidate in enumerate(list(search.pool.values())):
                        if number % 128 == 0:
                            generation_control()
                        if (candidate['vehicle_id'] in data['vehicles'] and
                                set(candidate['task_ids']) <= set(data['tasks'])):
                            local.evaluate(candidate['vehicle_id'], candidate['task_ids'])
                    for (warehouse, _), ids in groups.items():
                        generation_control()
                        cars = [car for car in local.cars if car['warehouse_id'] == warehouse]
                        if not cars:
                            continue
                        if len(ids) > exact_stores:
                            continue
                        points = {warehouse: data['warehouses'][warehouse], **{i: data['tasks'][i] for i in ids}}
                        neighbors = {origin: {item for item in ids if origin != item and
                                     _known(road, point, data['tasks'][item]) is not None}
                                     for origin, point in points.items()}
                        capacities = {}
                        for car in cars:
                            capacities.setdefault(car['max_pieces'], []).append(car['id'])
                        pieces = {i: data['tasks'][i]['pieces'] for i in ids}
                        for size in range(len(ids), 0, -1):
                            for order in _measured_orders(ids, size, warehouse, neighbors, pieces,
                                                          max(capacities), generation_control, row):
                                row['generated_orders'] += 1
                                load = sum(pieces[i] for i in order)
                                for capacity, peers in capacities.items():
                                    if load > capacity:
                                        continue
                                    candidate = local.evaluate(peers[0], order)
                                    if candidate is None:
                                        continue
                                    local.quick_exchange(candidate)
                                    for vid in peers[1:]:
                                        generation_control()
                                        # This compact metric calculation has no
                                        # vehicle-specific network/profile effects.
                                        # Final evidence still binds the real car.
                                        local.remember({**candidate, 'vehicle_id': vid, 'errors': []})
                                        row['shared_metric_proposals'] += 1
                except StopSearch as exc:
                    if exc.reason != 'JOINT_GENERATION_BUDGET':
                        raise
                    row['generation_stop_reason'] = exc.reason
                local.pack()
                if large and native and local.remaining() > .15:
                    from app.route_ortools import generate_ortools_candidates
                    def batch(specs):
                        for spec in specs:
                            candidate = local.evaluate(spec['vehicle_id'], spec['task_ids'])
                            if candidate is not None and not scalable:
                                local.quick_exchange(candidate)
                        local.pack()
                    native_options = {}
                    experiments = getattr(road, 'deep_experiments', None)
                    if hasattr(experiments, 'native_candidate_retention'):
                        native_options['candidate_retention'] = experiments.native_candidate_retention
                    if getattr(road, 'route_stage_trace', None) is not None:
                        native_options['stage_trace'] = road.route_stage_trace
                    if getattr(road, 'native_candidate_stream_observer', None) is not None:
                        native_options['candidate_stream_observer'] = road.native_candidate_stream_observer
                    if getattr(experiments,'complete_assignment_pricing',False):
                        from app.route_assignment_pricing import assignment_callback
                        native_options.update(complete_assignment_pricing=True,
                            on_assignment=assignment_callback(local,scope_ids=data['tasks'],after_accept=publish))
                    specs, row['native_diagnostics'] = generate_ortools_candidates(
                        data, lambda vehicle, a, b: _known(road, a, b), local.remaining() * (.45 if scalable else .55),
                        local.seed, search.cancelled, initial_routes=local.best_routes,
                        vehicle_independent=True, on_batch=batch, **native_options)
                    if local.remaining() > .075:
                        batch(specs)
                if large and local.remaining() > .1:
                    if scalable:
                        _scalable_columns(local, row)
                    else:
                        local.generate()
                local.pack()
            except StopSearch as exc:
                row['stop_reason'] = exc.reason
                if isinstance(getattr(exc,'native_diagnostics',None),dict):
                    row['native_diagnostics']=exc.native_diagnostics
                if exc.reason in {'CANCELLED', 'WALL_BUDGET', 'JOINT_NEIGHBORHOOD_BUDGET'}:
                    raise
            finally:
                row['after_fee'] = search.best['total_fee']
                row['after_vehicles'] = len(search.best_routes)
                row['elapsed_seconds'] = round(time.monotonic() - neighborhood_started, 5)
                row['compute_seconds'] = round(search.compute_elapsed() - neighborhood_compute_started, 5)
                row['candidate_count'] = local.evaluations
                row['retained_route_count'] = len(local.pool)
                row['local_pool_optimal'] = bool(local.pack_diagnostics and
                    local.pack_diagnostics[-1].get('cost_candidate_pool_optimal'))
                diagnostics['candidate_count'] += local.evaluations
                diagnostics['missing_metric_orders'] += row['missing_metric_orders']
                search.evaluations += local.evaluations
                local = None
    except StopSearch as exc:
        diagnostics['stop_reason'] = exc.reason
        if exc.reason in {'CANCELLED','WALL_BUDGET'}:
            exc.joint_reoptimization_diagnostics=diagnostics
            raise
    finally:
        search.control = original_control
        if capped:
            road.call_limit = previous_limit
        diagnostics.update(after_fee=search.best['total_fee'],
                           elapsed_seconds=round(time.monotonic() - started, 5),
                           compute_seconds=round(search.compute_elapsed() - compute_started, 5),
                           new_network_calls=max(0, road.stats().get('calls', 0) - calls_before))
    return diagnostics
