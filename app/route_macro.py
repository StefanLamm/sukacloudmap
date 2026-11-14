"""Measured long-route proposals that may cross an unprofitable trunk prefix.

Cities provide diverse seeds and soft focuses, never delivery boundaries.
Only current tasks, complete quotes and known directed roads price proposals.
The caller owns the phase deadline and global column selection; this module
never starts a native selector or performs an HTTP request itself.
"""
from collections import Counter
from heapq import nsmallest
from datetime import datetime
from decimal import Decimal
import time

from app.road_service import DrivingRoad
from app.route_metrics import evaluate_candidate
from app.route_small_groups import _city_identity
from app.routing_engine import RoadError, StopSearch, check_result


class _EvaluationLimit(Exception):
    pass


def seed_long_routes(search, scope='common', max_groups=1, max_seeds=36,
                     max_stops=40, max_evaluations=14000):
    """Remember measured prefixes and accept only cheaper complete allocations.

    ``common`` uses initially common tickets and initially unused cars, keeping
    every original dedicated line fixed. ``compatible`` generates long columns
    for selected complete warehouse/quote groups, keeping outside lines and all
    routes containing mandatory tickets fixed. Even losing prefixes remain in
    the pool for the caller's later joint selection. No selection runs here.

    Phase/time/cancellation exceptions propagate with the incumbent intact.
    A live DrivingRoad is temporarily capped at its current call count, so
    materialization cannot silently request missing roads. Its prior cap is
    restored even if metric lookup, the observer or proof checking raises.
    """
    if scope not in {'common', 'compatible'}:
        raise ValueError('scope must be common or compatible')
    limits = ((max_groups, 16), (max_seeds, 128), (max_stops, 40), (max_evaluations, 100000))
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper for value, upper in limits):
        raise ValueError('Invalid bounded long-route limits')
    road = search.road
    capped = isinstance(road, DrivingRoad)
    previous_limit = getattr(road, 'call_limit', None)
    if capped:
        road.call_limit = road.calls if previous_limit is None else min(road.calls, previous_limit)
    try:
        return _generate(search, scope, max_groups, max_seeds, max_stops, max_evaluations)
    finally:
        if capped:
            road.call_limit = previous_limit


def _generate(search, scope, max_groups, max_seeds, max_stops, max_evaluations):
    started = time.monotonic()
    compute_started = search.compute_elapsed()
    before_fee = search.best['total_fee']
    counts, accepted_samples = Counter(), []
    road, tasks = search.road, search.tasks
    search.control()
    if not callable(getattr(road, 'known_segment', None)):
        return {'method': 'LONG_MEASURED_ROUTE_SEEDS', 'scope': scope, 'before_fee': before_fee,
                'after_fee': before_fee, 'stop_reason': 'MATRIX_UNAVAILABLE', 'candidate_count': 0,
                'new_network_calls': 0, 'native_selection_runs': 0}
    proof = check_result(search.data, search.best)
    if (search.best['unassigned'] or not search.best['fees_complete']
            or any(issue['code'] != 'DRIVING_REFERENCE' for issue in proof['issues'])):
        return {'method': 'LONG_MEASURED_ROUTE_SEEDS', 'scope': scope, 'before_fee': before_fee,
                'after_fee': before_fee, 'stop_reason': 'NO_COMPLETE_INCUMBENT', 'candidate_count': 0,
                'new_network_calls': 0, 'native_selection_runs': 0}
    original_routes = list(search.best_routes)
    original_common = {row['task_id'] for row in search.best['common']}
    originally_used = {row['vehicle_id'] for row in original_routes}
    occupied = {row['vehicle_id']: row for row in original_routes}
    mandatory_cars = {row['vehicle_id'] for row in original_routes if any(item in search.mandatory for item in row['task_ids'])}
    mandatory_locked = {item for row in original_routes if any(i in search.mandatory for i in row['task_ids'])
                        for item in row['task_ids']}
    initial_allowed = original_common if scope == 'common' else set(tasks) - mandatory_locked
    groups = {}
    for item in sorted(initial_allowed):
        search.control()
        task = tasks[item]
        if task['signature'] and task['common_unit_price'] is not None:
            groups.setdefault((task['warehouse_id'], task['signature']), []).append(item)
    eligible_groups = []
    for group, ids in groups.items():
        search.control()
        eligible = False
        for car in search.cars:
            if (not car['available'] or car['warehouse_id'] != group[0]
                    or not any(tasks[item]['pieces'] <= car['max_pieces'] for item in ids)):
                continue
            previous = occupied.get(car['id'])
            if previous is None:
                eligible = True
            elif scope == 'compatible' and car['id'] not in mandatory_cars:
                first = tasks[previous['task_ids'][0]]
                eligible = (first['warehouse_id'], first['signature']) == group
            if eligible:
                break
        if eligible:
            eligible_groups.append(group)
        else:
            counts['groups_without_available_car'] += 1
    ordered_groups = sorted(eligible_groups, key=lambda group: (
        -sum((search.common_cost[item] for item in groups[group]), Decimal(0)), -len(groups[group]), group))[:max_groups]
    selected_groups = set(ordered_groups)
    if scope == 'common':
        fixed = original_routes
    else:
        fixed = [row for row in original_routes if any(item in search.mandatory for item in row['task_ids'])
                 or (tasks[row['task_ids'][0]]['warehouse_id'], tasks[row['task_ids'][0]]['signature']) not in selected_groups]
    fixed_cars = {row['vehicle_id'] for row in fixed}
    fixed_ids = {item for row in fixed for item in row['task_ids']}
    allowed = {item for group in ordered_groups for item in groups[group]} - fixed_ids
    cars = {row['id']: row for row in search.cars if row['available'] and row['id'] not in fixed_cars
            and (scope != 'common' or row['id'] not in originally_used)}
    points = {**search.data['warehouses'], **tasks}
    city_of = {item: _city_identity(tasks[item]) for item in allowed}
    deadline_of = {item: datetime.fromisoformat(tasks[item].get('latest_arrival_at') or tasks[item]['close_at']).timestamp()
                   for item in allowed}
    edges, memo, peer_cache = {}, {}, {}
    calls_before = road.stats().get('calls', 0)

    def metric(origin, destination):
        key = origin, destination
        if key not in edges:
            search.control()
            try:
                value = road.known_segment(points[origin], points[destination])
                if (not isinstance(value, dict) or any(isinstance(value.get(name), bool)
                        or not isinstance(value.get(name), int) or value[name] < 0 for name in ('meters', 'seconds'))):
                    value = None
            except RoadError as exc:
                if exc.code == 'CANCELLED':
                    raise StopSearch('CANCELLED') from None
                value = None
            except (KeyError, ValueError, TypeError):
                value = None
            edges[key] = value
            counts['directed_metric_lookups'] += 1
        return edges[key]

    def consider(rows):
        mutable = []
        for row in rows:
            if row['vehicle_id'] in fixed_cars:
                continue
            if not set(row['task_ids']) <= allowed:
                return False
            mutable.append(row)
        complete_rows = [*fixed, *mutable]
        rank = search.rank(complete_rows)
        if rank[0] or rank[1] >= Decimal(search.best['total_fee']):
            return False
        previous = search.best, search.best_routes, search.best_rank
        before = search.best['total_fee']
        accepted = search.consider(complete_rows)
        if accepted and Decimal(search.best['total_fee']) >= Decimal(before):
            # Materialized evidence remains authoritative even if an adapter's
            # metrics changed. Restore the complete incumbent on a tie/dearer
            # materialization instead of publishing a metric-only improvement.
            search.best, search.best_routes, search.best_rank = previous
            counts['materialized_nonimprovements_rejected'] += 1
            return False
        if accepted:
            counts['complete_actual_improvements'] += 1
            if len(accepted_samples) >= 32:
                del accepted_samples[8]
                counts['accepted_samples_truncated'] += 1
            accepted_samples.append({'elapsed_seconds': round(time.monotonic() - started, 5),
                                     'before_fee': before, 'after_fee': search.best['total_fee'],
                                     'mutable_routes': [{'vehicle_id': row['vehicle_id'], 'stores': len(row['task_ids']),
                                                         'pieces': row['pieces'], 'fee': row['fee']}
                                                        for row in search.best_routes if row['vehicle_id'] not in fixed_cars]})
        return accepted

    def start_cars(group, load):
        key = group[0], load
        if key not in peer_cache:
            options = [row for row in cars.values() if row['warehouse_id'] == group[0] and row['max_pieces'] >= load]
            peer_cache[key] = sorted(options, key=lambda row: (-row['max_pieces'], row['id']))[:4]
        return peer_cache[key]

    def evaluate(vehicle_id, order):
        search.control()
        key = vehicle_id, tuple(order)
        if key in memo:
            counts['proposal_memo_hits'] += 1
            return memo[key]
        if counts['candidate_count'] >= max_evaluations:
            raise _EvaluationLimit()
        counts['candidate_count'] += 1
        search.evaluations += 1
        value = evaluate_candidate(search.data, vehicle_id, list(order), road, search.control)
        if value is None:
            counts['missing_metric_orders'] += 1
            memo[key] = None
            return None
        if value['errors'] or value['fee'] is None:
            counts['infeasible_orders'] += 1
            memo[key] = None
            return None
        counts['feasible_prefix_columns'] += 1
        counts['maximum_prefix_stores'] = max(counts['maximum_prefix_stores'], len(order))
        # Only this chosen physical car is evaluated. The outer portfolio can
        # later reprice the identity/order for all compatible physical choices.
        row = search.remember(value)
        memo[key] = row
        return row

    def seeds(group):
        ids = [item for item in groups[group] if item in allowed and start_cars(group, tasks[item]['pieces'])
               and metric(group[0], item) is not None]
        cities = {}
        for item in ids:
            if city_of[item] is not None:
                cities.setdefault(city_of[item], []).append(item)
        ordered_cities = sorted(cities, key=lambda city: (
            -max(metric(group[0], item)['meters'] for item in cities[city]), city))
        # A cheap measured first leg keeps the focus instead of letting an
        # unfocused global entry drift to a different city. Focus is advisory.
        gateways = []
        for city in ordered_cities:
            closest = min(cities[city], key=lambda item: (
                metric(group[0], item)['meters'], deadline_of[item], item))
            valuable = min(cities[city], key=lambda item: (
                -search.common_cost[item], deadline_of[item], item))
            gateways.append((closest, valuable, city))
        global_orders = (sorted(ids, key=lambda item: (-search.common_cost[item], item)),
                         sorted(ids, key=lambda item: (-metric(group[0], item)['meters'], item)),
                         sorted(ids, key=lambda item: (metric(group[0], item)['meters'], item)),
                         sorted(ids, key=lambda item: (deadline_of[item], item)))
        # Rotate ranking families by depth so early global slots can include
        # high-common, far, near and early-window entries before their runners-up.
        global_entries = [(order[rank], None) for rank in range(3)
                          for order in global_orders if rank < len(order)]
        focused_rows = []
        # Every city gets the cheap-gateway append policy before any city gets
        # a second policy. Then use insertion at those gateways, followed by
        # the high-opportunity gateway family and existing nearest policy.
        for entry, policy in ((0, 'marginal'), (0, 'insertion'),
                              (1, 'marginal'), (1, 'insertion'),
                              (0, 'nearest'), (1, 'nearest')):
            focused_rows.extend((row[entry], row[2], policy) for row in gateways)
        focused_rows = list(dict.fromkeys(focused_rows))
        global_rows = list(dict.fromkeys((item, focus, policy)
            for policy in ('marginal', 'nearest') for item, focus in global_entries))
        result = []
        global_index = 0
        # Preserve global escape routes even with many city focuses. Dedupe
        # before interleaving so a repeated entry cannot consume its 1/4 share.
        for start in range(0, len(focused_rows), 3):
            result.extend(focused_rows[start:start + 3])
            if global_index < len(global_rows):
                result.append(global_rows[global_index])
                global_index += 1
        result.extend(global_rows[global_index:])
        return result

    stop_reason = 'BOUNDED_SEEDS_EXHAUSTED'
    try:
        for group in ordered_groups:
            for seed, focus, policy in seeds(group):
                search.control()
                if counts['seeds_started'] >= max_seeds:
                    stop_reason = 'SEED_LIMIT'
                    break
                choices = start_cars(group, tasks[seed]['pieces'])
                if not choices:
                    counts['seeds_without_available_physical_car'] += 1
                    continue
                vehicle = choices[counts['seeds_started'] % len(choices)]
                counts['seeds_started'] += 1
                counts['global_seed_chains' if focus is None else 'focused_seed_chains'] += 1
                chain = evaluate(vehicle['id'], [seed])
                while chain is not None:
                    search.control()
                    ids = chain['task_ids']
                    saving = sum((search.common_cost[item] for item in ids), Decimal(0)) - Decimal(chain['fee'])
                    counts['selected_growth_prefixes'] += 1
                    counts['negative_growth_prefixes'] += saving < 0
                    counts['positive_growth_prefixes'] += saving > 0
                    kept = [row for row in search.best_routes if row['vehicle_id'] != chain['vehicle_id']
                            and not row['mask'] & chain['mask']]
                    consider([*kept, chain])
                    if len(ids) >= max_stops:
                        break
                    used = set(ids)
                    if policy == 'insertion':
                        # All positions use their true directed bridges; a
                        # missing leg cannot serve as a speculative shortcut.
                        # This is only a cheap ranking of proposals. The three
                        # shortlisted whole routes are still exactly checked.
                        per_km = Decimal(tasks[ids[0]]['quote'].get('per_km') or '0')
                        def insertion_options():
                            for item in groups[group]:
                                search.control()
                                if (item not in allowed or item in used or
                                        chain['pieces'] + tasks[item]['pieces'] > vehicle['max_pieces']):
                                    continue
                                focus_penalty = int(focus is not None and city_of[item] != focus)
                                for position in range(len(ids) + 1):
                                    search.control()
                                    origin = group[0] if position == 0 else ids[position - 1]
                                    incoming = metric(origin, item)
                                    if incoming is None:
                                        continue
                                    delta_meters = incoming['meters']
                                    if position < len(ids):
                                        onward = metric(item, ids[position])
                                        replaced = metric(origin, ids[position])
                                        if onward is None or replaced is None:
                                            continue
                                        delta_meters += onward['meters'] - replaced['meters']
                                    counts['insertion_bridge_positions'] += 1
                                    proxy_net = Decimal(delta_meters) / 1000 * per_km - search.common_cost[item]
                                    yield (focus_penalty, proxy_net, delta_meters,
                                           deadline_of[item], item, position)
                        choices = nsmallest(3, insertion_options())
                        winner, winning_score = None, None
                        for focus_penalty, _, _, _, item, position in choices:
                            counts['insertion_exact_trials'] += 1
                            trial = evaluate(vehicle['id'], [*ids[:position], item, *ids[position:]])
                            if trial is None:
                                continue
                            delta = Decimal(trial['fee']) - Decimal(chain['fee']) - search.common_cost[item]
                            score = (focus_penalty, delta, trial['meters'], trial['seconds'], item, position)
                            if winning_score is None or score < winning_score:
                                winner, winning_score = trial, score
                        if winner is not None:
                            counts['selected_insertion_growths'] += 1
                        chain = winner
                        continue
                    outgoing = {}
                    for item in groups[group]:
                        search.control()
                        if (item not in allowed or item in used
                                or chain['pieces'] + tasks[item]['pieces'] > vehicle['max_pieces']):
                            continue
                        edge = metric(ids[-1], item)
                        if edge is not None:
                            outgoing[item] = edge
                    if not outgoing:
                        break
                    near = sorted(outgoing, key=lambda item: (outgoing[item]['meters'], item))
                    valuable = sorted(outgoing, key=lambda item: (-search.common_cost[item], item))
                    early = sorted(outgoing, key=lambda item: (deadline_of[item], item))
                    focused = [item for item in near if focus is not None and city_of[item] == focus]
                    options = list(dict.fromkeys(focused[:5] + near[:5] + valuable[:3] + early[:2]))
                    winner, winning_score = None, None
                    for item in options:
                        trial = evaluate(vehicle['id'], [*ids, item])
                        if trial is None:
                            continue
                        delta = Decimal(trial['fee']) - Decimal(chain['fee']) - search.common_cost[item]
                        soft_focus = int(focus is not None and city_of[item] != focus)
                        score = ((soft_focus, delta, trial['meters'], trial['seconds'], item) if policy == 'marginal' else
                                 (soft_focus, trial['meters'] - chain['meters'], delta, trial['seconds'], item))
                        if winning_score is None or score < winning_score:
                            winner, winning_score = trial, score
                    chain = winner  # Negative prefixes and cross-city extensions remain allowed.
            if counts['seeds_started'] >= max_seeds:
                break
    except _EvaluationLimit:
        stop_reason = 'CURRENT_ORDER_LIMIT'
    if stop_reason == 'BOUNDED_SEEDS_EXHAUSTED' and counts['seeds_started'] >= max_seeds:
        stop_reason = 'SEED_LIMIT'
    fixed_after = {row['vehicle_id']: tuple(row['task_ids']) for row in search.best_routes}
    if any(fixed_after.get(row['vehicle_id']) != tuple(row['task_ids']) for row in fixed):
        raise RuntimeError('A fixed original or mandatory route changed')
    return {'method': 'LONG_MEASURED_ROUTE_SEEDS', 'scope': scope, 'before_fee': before_fee,
            'after_fee': search.best['total_fee'], 'stop_reason': stop_reason,
            'elapsed_seconds': round(time.monotonic() - started, 4),
            'compute_seconds': round(search.compute_elapsed() - compute_started, 4),
            'candidate_count': counts['candidate_count'], 'selected_group_count': len(ordered_groups),
            'allowed_task_count': len(allowed), 'fixed_original_routes': len(fixed),
            'new_network_calls': road.stats().get('calls', 0) - calls_before,
            'native_selection_runs': 0, 'city_is_candidate_focus_only': True,
            'negative_prefix_growth_allowed': True, 'global_optimality_proven': False,
            **dict(counts), 'accepted_samples': accepted_samples}
