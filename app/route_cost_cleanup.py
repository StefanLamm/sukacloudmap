"""Deterministic, measured-road reductions of a complete delivery incumbent.

The ordinary searches need not generate every useful shortened route. This
bounded pass prices complete alternatives: a prefix, optional stop removal or
compatible route merge, with all other tickets still dedicated or common.
Unknown directed bridges are skipped. Only the host may materialize evidence
and accept an independently checked whole allocation.
"""
from collections import Counter
from decimal import Decimal
from heapq import nsmallest
from itertools import combinations
import time

from app.road_service import DrivingRoad
from app.route_metrics import evaluate_candidate
from app.routing_engine import StopSearch


class _CleanupLimit(Exception):
    def __init__(self, reason):
        self.reason = reason


def _identity(route):
    return route['vehicle_id'], tuple(route['task_ids'])


def _region(task):
    code = task.get('region_code')
    if isinstance(code, str) and len(code) == 6 and code.isdigit() and not code.endswith('00'):
        return ('code', code)
    names = tuple(task.get(key) for key in ('province', 'city', 'district'))
    return ('names', *names) if all(isinstance(name, str) and name for name in names) else None


def improve_cost_cleanup(search, *, max_rounds=2, max_candidates=512,
                         max_double_deletions=48, max_route_pairs=16):
    """Polish a full incumbent without new HTTP or sacrificing its proof.

    Limits apply to the entire invocation. No randomness is used. Prefixes and
    whole-route outsourcing are considered before optional single/double
    deletion, followed by a bounded set of compatible route concatenations.
    Every accepted step compares the complete actual quoted allocation, so
    fewer cars, shorter distance or lower marginal fees alone cannot win.

    Local candidate/time limits retain the host's last checked result. Host
    phase expiry, cancellation and global wall-budget expiry propagate, with
    the provider allowance restored even on unrelated materialization errors.
    """
    started = time.monotonic()
    before_fee = search.best['total_fee']
    counters = Counter()
    accepted_by_kind = Counter()
    diagnostics = {
        'before_fee': before_fee, 'after_fee': before_fee,
        'candidate_count': 0, 'metric_evaluations': 0, 'accepted': 0,
        'rounds_started': 0, 'rounds_completed': 0,
        'candidate_limit': max_candidates, 'round_limit': max_rounds,
        'double_deletion_limit': max_double_deletions,
        'route_pair_limit': max_route_pairs,
        'new_network_calls': 0, 'seed_independent': True,
        'global_optimality_proven': False, 'skipped': False,
    }

    def finish(reason):
        diagnostics.update(
            after_fee=search.best['total_fee'], stop_reason=reason,
            elapsed_seconds=round(time.monotonic() - started, 4),
            accepted_by_kind=dict(accepted_by_kind), **dict(counters))
        return diagnostics

    if search.best['unassigned'] or not search.best['fees_complete'] or any(
            issue['code'] != 'DRIVING_REFERENCE' for issue in search.best['check']['issues']):
        diagnostics['skipped'] = True
        return finish('INCOMPLETE_INCUMBENT')
    if not callable(getattr(search.road, 'known_segment', None)):
        diagnostics['skipped'] = True
        return finish('MATRIX_UNAVAILABLE')
    if not search.best_routes:
        diagnostics['skipped'] = True
        return finish('NO_DEDICATED_ROUTES')
    if max_rounds <= 0 or max_candidates <= 0:
        diagnostics['skipped'] = True
        return finish('NO_CLEANUP_ALLOWANCE')

    road = search.road
    capped = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    original_limit = getattr(road, 'call_limit', None)
    calls_before = road.calls if capped else 0
    if capped:
        # Candidate metrics are not evidence. Host materialization may read a
        # bound cache/graph, but must never request missing provider evidence.
        road.call_limit = min(road.calls, original_limit) if original_limit is not None else road.calls
    seen = set()

    def control():
        search.control()
        if search.remaining() <= 0:
            raise _CleanupLimit('TIME_BUDGET')

    def current(vehicle_id):
        return next((row for row in search.best_routes if row['vehicle_id'] == vehicle_id), None)

    def try_allocation(kept, vehicle_id, ids, kind):
        """An empty order means all removed stores use configured common fees."""
        control()
        identity = tuple(sorted([_identity(row) for row in kept] +
                                ([(vehicle_id, tuple(ids))] if ids else [])))
        if identity in seen:
            counters['duplicate_allocations_skipped'] += 1
            return
        seen.add(identity)
        if diagnostics['candidate_count'] >= max_candidates:
            raise _CleanupLimit('CANDIDATE_LIMIT')
        diagnostics['candidate_count'] += 1
        counters[kind + '_candidates'] += 1
        proposed = list(kept)
        if ids:
            search.evaluations += 1
            diagnostics['metric_evaluations'] += 1
            row = evaluate_candidate(search.data, vehicle_id, list(ids), road, control)
            if row is None:
                counters['missing_metric_candidates'] += 1
                return
            if row['fee'] is None or row['errors']:
                counters['infeasible_candidates'] += 1
                return
            proposed.append(search.remember(row))
        previous_fee = Decimal(search.best['total_fee'])
        if search.consider(proposed):
            diagnostics['accepted'] += 1
            accepted_by_kind[kind] += 1
            if Decimal(search.best['total_fee']) < previous_fee:
                counters['fee_improvements'] += 1

    def prefixes(vehicle_id):
        row = current(vehicle_id)
        if row is None:
            return
        ids = list(row['task_ids'])
        # Every dropped suffix ticket needs a common quote; a mandatory ticket
        # can never silently disappear when a profitable short prefix is kept.
        first_cut = 1 + max((i for i, item in enumerate(ids) if item not in search.common), default=-1)
        kept = [r for r in search.best_routes if r['vehicle_id'] != vehicle_id]
        for cut in range(first_cut, len(ids)):
            # Keep this context for every prefix, even if the whole-route
            # option wins first. A cheaper prefix may reintroduce a few stores.
            try_allocation(kept, vehicle_id, ids[:cut], 'prefix' if cut else 'whole_to_common')

    def deletions(vehicle_id, count):
        row = current(vehicle_id)
        if row is None:
            return
        ids = list(row['task_ids'])
        optional = [item for item in ids if item in search.common]
        if len(optional) < count:
            return
        kept = [r for r in search.best_routes if r['vehicle_id'] != vehicle_id]
        choices = [(item,) for item in sorted(optional)]
        if count == 2:
            # A shared nonlinear region charge may disappear only after both
            # optional stops leave. Explicit region labels only prioritize
            # probes; the complete fee evaluator remains authoritative.
            allowance = min(max(0, max_double_deletions - counters['double_deletions_examined']),
                            max(0, max_candidates - diagnostics['candidate_count']))
            if not allowance:
                counters['double_deletion_limit_reached'] = 1
                return
            def priority(pair):
                control()
                region = _region(search.tasks[pair[0]])
                return (not (region is not None and region == _region(search.tasks[pair[1]])),
                        sum((search.common_cost[item] for item in pair), Decimal(0)), pair)
            choices = nsmallest(allowance, combinations(sorted(optional), 2), key=priority)
        for removed in choices:
            control()
            if count == 2:
                if counters['double_deletions_examined'] >= max_double_deletions:
                    counters['double_deletion_limit_reached'] = 1
                    break
                counters['double_deletions_examined'] += 1
            removed_set = set(removed)
            try_allocation(kept, vehicle_id, [item for item in ids if item not in removed_set],
                           'single_delete' if count == 1 else 'double_delete')

    def merge_pairs():
        # Expensive complete lines are a useful deterministic probe order, not
        # a lower bound, compatibility rule, or reason to force fewer vehicles.
        rows = sorted(search.best_routes, key=lambda row: (-Decimal(row['fee']), row['vehicle_id']))
        for left, right in combinations(rows, 2):
            control()
            if counters['route_pairs_examined'] >= max_route_pairs:
                counters['route_pair_limit_reached'] = 1
                break
            a, b = current(left['vehicle_id']), current(right['vehicle_id'])
            if a is None or b is None or _identity(a) != _identity(left) or _identity(b) != _identity(right):
                continue
            first, second = search.tasks[a['task_ids'][0]], search.tasks[b['task_ids'][0]]
            if first['warehouse_id'] != second['warehouse_id'] or first['signature'] != second['signature']:
                continue
            used = {row['vehicle_id'] for row in search.best_routes}
            pair = {a['vehicle_id'], b['vehicle_id']}
            pieces = a['pieces'] + b['pieces']
            cars = [car for car in search.cars if car['available']
                    and car['warehouse_id'] == first['warehouse_id'] and car['max_pieces'] >= pieces
                    and (car['id'] in pair or car['id'] not in used)]
            if not cars:
                counters['route_pairs_without_capacity'] += 1
                continue
            counters['route_pairs_examined'] += 1
            kept = [row for row in search.best_routes if row['vehicle_id'] not in pair]
            for car in sorted(cars, key=lambda car: (car['id'] not in pair, car['max_pieces'], car['id'])):
                for ids in (list(a['task_ids']) + list(b['task_ids']), list(b['task_ids']) + list(a['task_ids'])):
                    try_allocation(kept, car['id'], ids, 'pair_merge')

    stopped = 'ROUND_LIMIT'
    try:
        for _ in range(max_rounds):
            control()
            diagnostics['rounds_started'] += 1
            accepted_before = diagnostics['accepted']
            vehicles = sorted(row['vehicle_id'] for row in search.best_routes)
            for vehicle_id in vehicles:
                prefixes(vehicle_id)
            for vehicle_id in vehicles:
                deletions(vehicle_id, 1)
            for vehicle_id in vehicles:
                deletions(vehicle_id, 2)
            merge_pairs()
            diagnostics['rounds_completed'] += 1
            if diagnostics['accepted'] == accepted_before:
                stopped = 'NO_IMPROVEMENT'
                break
    except _CleanupLimit as exc:
        stopped = exc.reason
    except StopSearch as exc:
        if exc.reason in {'CANCELLED', 'WALL_BUDGET', 'PHASE_BUDGET'}:
            raise
        stopped = exc.reason
    finally:
        if capped:
            road.call_limit = original_limit
            diagnostics['new_network_calls'] = max(0, road.calls - calls_before)
    return finish(stopped)


def polish_route_orders(search, vehicle_ids, *, max_passes=2, max_candidates=1200):
    """Lower complete quoted cost by reordering only explicitly selected lines.

    Each target keeps its physical car and exact store set. Other lines and
    common allocations remain fixed. Single-stop relocations and directed
    reversals are deterministic proposals, never geometric estimates. The host
    materializes an improving proposal before it becomes the retained result.

    This pass receives no extra allowance: control/remaining belong to its
    caller. Local expiry keeps the last proved result; cancellation, host phase
    expiry and global wall expiry propagate with the provider cap restored.
    """
    started = time.monotonic()
    before_fee = search.best['total_fee']
    counts = Counter()
    samples = []
    diagnostics = {'before_fee': before_fee, 'after_fee': before_fee,
                   'candidate_count': 0, 'accepted': 0, 'passes_started': 0,
                   'passes_completed': 0, 'candidate_limit': max_candidates,
                   'pass_limit': max_passes, 'new_network_calls': 0,
                   'seed_independent': True, 'additional_budget_allocated': 0,
                   'global_optimality_proven': False, 'skipped': False}

    def finish(reason):
        diagnostics.update(after_fee=search.best['total_fee'], stop_reason=reason,
                           elapsed_seconds=round(time.monotonic() - started, 4),
                           accepted_move_samples=samples, **dict(counts))
        return diagnostics

    if search.best['unassigned'] or not search.best['fees_complete'] or any(
            issue['code'] != 'DRIVING_REFERENCE' for issue in search.best['check']['issues']):
        diagnostics['skipped'] = True
        return finish('INCOMPLETE_INCUMBENT')
    if not callable(getattr(search.road, 'known_segment', None)):
        diagnostics['skipped'] = True
        return finish('MATRIX_UNAVAILABLE')
    requested = [vehicle_ids] if isinstance(vehicle_ids, str) else list(vehicle_ids)
    if any(not isinstance(vehicle_id, str) or not vehicle_id for vehicle_id in requested):
        raise ValueError('Explicit target vehicle identities must be nonempty strings')
    requested = sorted(set(requested))
    diagnostics['requested_vehicle_count'] = len(requested)
    originals = {row['vehicle_id']: row for row in search.best_routes}
    common_ids = frozenset(row['task_id'] for row in search.best['common'])
    original_sets = {vehicle_id: frozenset(row['task_ids']) for vehicle_id, row in originals.items()}

    def line_key(row):
        return (row['vehicle_id'], tuple(row['task_ids']), row['fee'],
                row['meters'], row['seconds'], row['pieces'])

    targets = []
    for vehicle_id in requested:
        row = originals.get(vehicle_id)
        if row is None:
            counts['unknown_target_vehicles_skipped'] += 1
            continue
        if len(row['task_ids']) < 2:
            counts['single_stop_targets_skipped'] += 1
            continue
        quote = search.tasks[row['task_ids'][0]]['quote']
        # Stop-count and region surcharges depend on the fixed physical store
        # set. With no positive kilometer rate, a permutation cannot strictly
        # reduce the current full quotation, regardless of its distance.
        if (quote['mode'] in {'BASE_STOPS', 'BASE_STOPS_REGION'}
                or quote['mode'] in {'KM', 'KM_REGION'} and Decimal(str(quote['per_km'])) == 0):
            counts['order_invariant_price_targets_skipped'] += 1
            continue
        targets.append(vehicle_id)
    diagnostics['eligible_vehicle_count'] = len(targets)
    fixed = {vehicle_id: line_key(row) for vehicle_id, row in originals.items() if vehicle_id not in targets}
    seen = {vehicle_id: {tuple(originals[vehicle_id]['task_ids'])} for vehicle_id in targets}
    road = search.road
    capped = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    original_limit = getattr(road, 'call_limit', None)
    calls_before = road.calls if capped else 0
    if capped:
        road.call_limit = min(road.calls, original_limit) if original_limit is not None else road.calls

    def control():
        search.control()
        if search.remaining() <= 0:
            raise _CleanupLimit('NO_REMAINING_BUDGET')

    def current(vehicle_id):
        return next((row for row in search.best_routes if row['vehicle_id'] == vehicle_id), None)

    def scope_matches():
        actual = {row['vehicle_id']: row for row in search.best_routes}
        if (len(search.best_routes) != len(originals) or set(actual) != set(originals)
                or frozenset(row['task_id'] for row in search.best['common']) != common_ids):
            return False
        for vehicle_id, row in actual.items():
            if (frozenset(row['task_ids']) != original_sets[vehicle_id]
                    or len(row['task_ids']) != len(original_sets[vehicle_id])
                    or row['pieces'] != originals[vehicle_id]['pieces']):
                return False
            if vehicle_id in fixed and line_key(row) != fixed[vehicle_id]:
                return False
        return True

    def trial(vehicle_id, ids, kind):
        control()
        order = tuple(ids)
        if order in seen[vehicle_id]:
            counts['duplicate_orders_skipped'] += 1
            return
        if diagnostics['candidate_count'] >= max_candidates:
            raise _CleanupLimit('CANDIDATE_LIMIT')
        seen[vehicle_id].add(order)
        diagnostics['candidate_count'] += 1
        counts[kind + '_candidates'] += 1
        search.evaluations += 1
        row = evaluate_candidate(search.data, vehicle_id, list(order), road, control)
        if row is None:
            counts['missing_metric_orders'] += 1
            return
        if row['fee'] is None or row['errors']:
            counts['infeasible_orders'] += 1
            return
        counts['feasible_orders'] += 1
        previous_route = current(vehicle_id)
        if previous_route is None or Decimal(row['fee']) >= Decimal(previous_route['fee']):
            counts['nonimproving_metric_orders'] += 1
            return
        previous = search.best, search.best_routes, search.best_rank
        previous_fee = search.best['total_fee']
        candidate = search.remember(row)
        # Preserve even the existing line-list positions, in addition to the
        # exact allocations and order on every nontarget physical vehicle.
        proposed = [candidate if old['vehicle_id'] == vehicle_id else old for old in search.best_routes]
        if not search.consider(proposed):
            counts['materialization_or_complete_cost_rejections'] += 1
            return
        if Decimal(search.best['total_fee']) >= Decimal(previous_fee) or not scope_matches():
            search.best, search.best_routes, search.best_rank = previous
            counts['materialized_nonimprovement_or_scope_rejections'] += 1
            return
        diagnostics['accepted'] += 1
        if len(samples) < 16:
            samples.append({'vehicle_id': vehicle_id, 'kind': kind,
                            'before_fee': previous_fee, 'after_fee': search.best['total_fee'],
                            'before_route_fee': previous_route['fee'], 'after_route_fee': current(vehicle_id)['fee'],
                            'before_meters': previous_route['meters'], 'after_meters': current(vehicle_id)['meters']})
        else:
            counts['accepted_move_samples_omitted'] += 1

    stopped = 'PASS_LIMIT'
    try:
        control()
        if max_passes <= 0 or max_candidates <= 0:
            diagnostics['skipped'] = True
            stopped = 'NO_POLISH_ALLOWANCE'
        elif not targets:
            diagnostics['skipped'] = True
            stopped = 'NO_PRICE_SENSITIVE_TARGETS'
        else:
            for _ in range(max_passes):
                control()
                diagnostics['passes_started'] += 1
                accepted_before = diagnostics['accepted']
                for vehicle_id in targets:
                    control()
                    ids = list(current(vehicle_id)['task_ids'])
                    for source in range(len(ids)):
                        control()
                        shortened = ids[:source] + ids[source + 1:]
                        for position in range(len(shortened) + 1):
                            trial(vehicle_id, shortened[:position] + [ids[source]] + shortened[position:], 'relocate')
                    for left in range(len(ids)):
                        control()
                        for right in range(left + 1, len(ids)):
                            trial(vehicle_id, ids[:left] + list(reversed(ids[left:right + 1])) + ids[right + 1:], 'two_opt')
                diagnostics['passes_completed'] += 1
                if diagnostics['accepted'] == accepted_before:
                    stopped = 'NO_IMPROVEMENT'
                    break
    except _CleanupLimit as exc:
        stopped = exc.reason
    except StopSearch as exc:
        if exc.reason in {'CANCELLED', 'WALL_BUDGET', 'PHASE_BUDGET'}:
            raise
        stopped = exc.reason
    finally:
        if capped:
            road.call_limit = original_limit
            diagnostics['new_network_calls'] = max(0, road.calls - calls_before)
    if not scope_matches():
        raise RuntimeError('Order polish changed a fixed allocation')
    diagnostics.update(fixed_allocations_unchanged=True, target_store_sets_and_physical_cars_unchanged=True)
    return finish(stopped)
