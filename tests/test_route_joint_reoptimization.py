"""Whole-line reoptimization versus independent small-instance enumeration."""
from copy import deepcopy
from decimal import Decimal
from itertools import combinations, permutations

import pytest

from app.route_joint_reoptimization import reoptimize_joint_routes
from app.route_search import JointSearch
from app.routing_engine import StopSearch, check_result, normalize
from test_route_columns import plan, problem


def prepared(body, road, routes):
    initial = plan(body, road, routes)
    data = normalize(body)
    assert check_result(data, initial)['valid']
    search = JointSearch(data, road, 15, 0, lambda: False, initial)
    search.setup_fleet()
    assert search.consider([search.compact(row) for row in initial['routes']], force=True)
    return search, initial


def independent_fee(body, matrix):
    """Tiny exact allocation oracle; no production evaluate/select code."""
    tasks = {row['id']: row for row in body['tasks']}
    cars = [row for row in body['vehicles'] if row['available']]
    ids = set(tasks)
    best = None
    def recurse(index, remaining, fee):
        nonlocal best
        if index == len(cars):
            if any(tasks[item]['common_unit_price'] is None for item in remaining):
                return
            total = fee + sum((Decimal(tasks[item]['common_unit_price']) * tasks[item]['pieces'] for item in remaining), Decimal(0))
            best = total if best is None or total < best else best
            return
        recurse(index + 1, remaining, fee)
        car = cars[index]
        for count in range(1, len(remaining) + 1):
            for subset in combinations(sorted(remaining), count):
                if sum(tasks[item]['pieces'] for item in subset) > car['max_pieces']:
                    continue
                if any(tasks[item]['warehouse_id'] != car['warehouse_id'] for item in subset):
                    continue
                if len({tasks[item]['quote_id'] for item in subset}) != 1:
                    continue
                quote = body['quotes'][tasks[subset[0]]['quote_id']]
                for order in permutations(subset):
                    edges = list(zip([car['warehouse_id'], *order[:-1]], order))
                    if any(edge not in matrix for edge in edges):
                        continue
                    meters = sum(matrix[edge]['meters'] for edge in edges)
                    cost = ((Decimal(meters) * Decimal(quote['per_km']) / 1000).quantize(Decimal('.01'))
                            if quote['mode'] == 'KM' else Decimal(quote['base_price']) +
                            max(0, count - quote['included_stops']) * Decimal(quote['extra_stop_price']))
                    recurse(index + 1, remaining - set(subset), fee + cost)
    recurse(0, ids, Decimal(0))
    return format(best, '.2f')


def test_two_mandatory_lines_repartition_together_without_losing_required_tickets():
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    search, initial = prepared(body, road, [('V0', ['0', '2']), ('V1', ['1', '3'])])
    original = deepcopy(initial)
    details = reoptimize_joint_routes(search, max_neighborhoods=2, seconds=3)
    assert initial['total_fee'] == '220.00'
    assert search.best['total_fee'] == independent_fee(body, road.matrix) == '22.00'
    assert details['complete_actual_improvements'] > 0
    assert details['neighborhoods'][0]['thawed_routes'] == 2
    assert not search.best['common'] and not search.best['unassigned']
    assert check_result(search.data, search.best)['valid'] and initial == original


def test_joint_neighborhood_can_add_idle_car_when_three_routes_are_cheaper():
    body, road = problem(n=4, cars=3, common=None, capacity=2)
    # Only the pair 0->1 shares a trunk cheaply; the others should each depart.
    road.matrix['2', '3']['meters'] = 100000
    search, _ = prepared(body, road, [('V0', ['0', '2']), ('V1', ['1', '3'])])
    details = reoptimize_joint_routes(search, max_neighborhoods=2, seconds=3)
    assert search.best['total_fee'] == independent_fee(body, road.matrix) == '31.00'
    assert len(search.best['routes']) == 3
    assert len({row['vehicle_id'] for row in search.best['routes']}) == 3
    assert details['neighborhoods'][0]['eligible_physical_cars'] == 3
    assert check_result(search.data, search.best)['valid']


def test_joint_neighborhood_can_merge_cities_and_absorb_related_common():
    body, road = problem(n=5, cars=3, common='100', capacity=5)
    for n, task in enumerate(body['tasks']):
        task.update(city='城市' + str(n), province='同一省')
    for origin, destination in road.matrix:
        road.matrix[origin, destination]['meters'] = 1000 if origin != 'W' else 10000
    search, _ = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    details = reoptimize_joint_routes(search, max_neighborhoods=2, seconds=4)
    assert search.best['total_fee'] == independent_fee(body, road.matrix) == '14.00'
    assert len(search.best['routes']) == 1 and not search.best['common']
    assert len({search.tasks[item]['city'] for item in search.best['routes'][0]['task_ids']}) == 5
    assert details['neighborhoods'][0]['related_common_tasks'] == 1
    assert details['city_partition_imposed'] is False
    assert check_result(search.data, search.best)['valid']


def test_frozen_route_and_car_are_untouched_and_incompatible_quotes_stay_separate():
    body, road = problem(n=6, cars=3, common=None, capacity=4)
    body['quotes']['R'] = {'mode': 'BASE_STOPS', 'base_price': '3', 'included_stops': 4, 'extra_stop_price': '100'}
    for task in body['tasks'][4:]:
        task['quote_id'] = 'R'
    search, initial = prepared(body, road, [('V0', ['0', '2']), ('V1', ['1', '3']), ('V2', ['4', '5'])])
    frozen = deepcopy(initial['routes'][2])
    details = reoptimize_joint_routes(search, max_routes=2, max_neighborhoods=1, max_common=0, seconds=3)
    assert details['neighborhoods'][0]['thawed_vehicle_ids'] == ['V0', 'V1']
    assert next(row for row in search.best['routes'] if row['vehicle_id'] == 'V2') == frozen
    assert len(search.best['routes']) == len({row['vehicle_id'] for row in search.best['routes']})
    assert all(len({search.tasks[item]['signature'] for item in row['task_ids']}) == 1 for row in search.best['routes'])
    assert check_result(search.data, search.best)['valid']


def test_two_quote_groups_jointly_compete_for_one_free_car_without_mixing_prices():
    body, road = problem(n=6, cars=3, common='100', capacity=3)
    body['quotes']['R'] = {'mode': 'BASE_STOPS', 'base_price': '1', 'included_stops': 3, 'extra_stop_price': '100'}
    for task in body['tasks'][2:4]:
        task['quote_id'] = 'R'
    for (origin, destination), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, _ = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    details = reoptimize_joint_routes(search, max_neighborhoods=1, max_common=3, seconds=4)
    assert search.best['total_fee'] == independent_fee(body, road.matrix) == '23.00'
    assert len(search.best['routes']) == 3
    assert check_result(search.data, search.best)['valid']


def test_missing_measured_bridge_is_rejected_without_querying_unknown_roads():
    body, road = problem(n=4, cars=2, common=None, capacity=4)
    road.matrix = {pair: metric for pair, metric in road.matrix.items() if pair in {
        ('W', '0'), ('0', '1'), ('W', '2'), ('2', '3')}}
    search, initial = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    original_segment = road.segment
    def guarded(vehicle, a, b, at):
        assert (a['id'], b['id']) in road.matrix
        return original_segment(vehicle, a, b, at)
    road.segment = guarded
    details = reoptimize_joint_routes(search, max_neighborhoods=2, seconds=2)
    assert search.best['total_fee'] == initial['total_fee'] == independent_fee(body, road.matrix)
    assert details['neighborhoods'][0]['pruned_missing_prefixes'] > 0
    assert details['new_network_calls'] == 0
    assert check_result(search.data, search.best)['valid']


def test_related_common_different_warehouse_or_quote_cannot_enter_neighborhood():
    body, road = problem(n=6, cars=3, common='100', capacity=6)
    body['warehouses'].append({**body['warehouses'][0], 'id': 'OTHER'})
    body['tasks'][4]['warehouse_id'] = 'OTHER'
    body['quotes']['R'] = {'mode': 'BASE_STOPS', 'base_price': '1', 'included_stops': 6, 'extra_stop_price': '1'}
    body['tasks'][5]['quote_id'] = 'R'
    for (origin, _), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, _ = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    details = reoptimize_joint_routes(search, max_neighborhoods=1, seconds=2)
    assert details['neighborhoods'][0]['task_count'] == 4
    assert {row['task_id'] for row in search.best['common']} == {'4', '5'}
    assert check_result(search.data, search.best)['valid']


def test_zero_allowance_and_cancel_preserve_initial_complete_allocation():
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    search, initial = prepared(body, road, [('V0', ['0', '2']), ('V1', ['1', '3'])])
    original = deepcopy(search.best)
    details = reoptimize_joint_routes(search, seconds=0)
    assert details['stop_reason'] == 'NO_UNUSED_JOINT_BUDGET'
    assert search.best == original
    search.cancelled = lambda: True
    with pytest.raises(StopSearch, match='CANCELLED'):
        reoptimize_joint_routes(search, seconds=1)
    assert search.best == original


@pytest.mark.parametrize('limit', [1, 5, True])
def test_invalid_thawed_line_count_is_rejected(limit):
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    search, _ = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    with pytest.raises(ValueError):
        reoptimize_joint_routes(search, max_routes=limit)


def test_four_line_scope_is_available_with_exact_whole_ticket_reassignment():
    body, road = problem(n=4, cars=4, common=None, capacity=4)
    for (origin, _), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, _ = prepared(body, road, [('V0', ['0']), ('V1', ['1']), ('V2', ['2']), ('V3', ['3'])])
    details = reoptimize_joint_routes(search, max_routes=4, max_neighborhoods=3, seconds=3)
    assert search.best['total_fee'] == independent_fee(body, road.matrix) == '13.00'
    assert len(search.best['routes']) == 1
    assert all(row['task_ids'] for row in search.best['routes'])
    assert check_result(search.data, search.best)['valid']


def test_short_windows_cannot_be_ignored_to_publish_lower_metric_fee():
    body, road = problem(n=4, cars=2, common=None, capacity=4)
    # All visits last a minute and only original two-stop routes fit. A four
    # stop route is cheaper in meters but must remain infeasible in time.
    for task in body['tasks']:
        task['close_at'] = '2026-11-13T06:02:00+08:00'
    for (origin, _), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, initial = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    details = reoptimize_joint_routes(search, seconds=2, max_neighborhoods=1)
    assert search.best['total_fee'] == initial['total_fee'] == '22.00'
    assert len(search.best['routes']) == 2
    assert check_result(search.data, search.best)['valid']


def test_road_materialization_drift_cannot_turn_an_estimate_into_an_actual_saving():
    body, road = problem(n=4, cars=2, common=None, capacity=4)
    for (origin, _), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, initial = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    # Search metrics imply 13 for a merge, while actual materialized evidence
    # costs 26. The current complete 22 incumbent must stay protected.
    road.drift = True
    reoptimize_joint_routes(search, seconds=2, max_neighborhoods=1)
    assert search.best['total_fee'] == initial['total_fee'] == '22.00'
    assert len(search.best['routes']) == 2
    assert check_result(search.data, search.best)['valid']


def test_guarded_live_adapter_has_zero_new_calls_and_restores_call_limit():
    from test_route_cost_cleanup import GuardedMeasuredRoad
    body, _ = problem(n=4, cars=2, common=None, capacity=4)
    road = GuardedMeasuredRoad()
    # Each original singleton pair has a 1km departure plus one 1km leg.
    search, initial = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    limit = road.call_limit
    road.require_read_only = True
    details = reoptimize_joint_routes(search, seconds=2, max_neighborhoods=1)
    assert road.seen_limits and all(value <= road.calls for value in road.seen_limits)
    assert road.call_limit == limit and details['new_network_calls'] == 0
    assert search.best['total_fee'] <= initial['total_fee']
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('deadline_limited', [False, True])
def test_large_mandatory_neighborhood_passes_complete_thawed_scope_to_native(monkeypatch, deadline_limited):
    body, road = problem(n=9, cars=3, common=None, capacity=9)
    if deadline_limited:
        road.deadline_limited_candidates = True
    for (origin, _), metric in road.matrix.items():
        metric['meters'] = 1000 if origin != 'W' else 10000
    search, initial = prepared(body, road, [('V0', ['0', '1', '2', '3']), ('V1', ['4', '5', '6', '7', '8'])])
    seen = []
    def native(data, lookup, seconds, seed, cancelled, *, initial_routes, vehicle_independent, on_batch,
               deadline_limited_candidates):
        seen.append((data, initial_routes, deadline_limited_candidates))
        # The caller owns actual pricing/selection. A native proposal carries
        # identities/order only and no authoritative amount.
        rows = [{'vehicle_id': 'V2', 'task_ids': [str(i) for i in range(9)]}]
        on_batch(rows)
        return rows, {'stop_reason': 'SEARCH_COMPLETED'}
    monkeypatch.setattr('app.route_ortools.generate_ortools_candidates', native)
    details = reoptimize_joint_routes(search, seconds=2, max_neighborhoods=1)
    assert seen and len(seen[0][0]['tasks']) == 9 and len(seen[0][1]) == 2
    assert seen[0][2] is deadline_limited
    assert search.best['total_fee'] == '18.00' < initial['total_fee'] == '27.00'
    assert len(search.best['routes']) == 1
    assert details['neighborhoods'][0]['native_diagnostics']['stop_reason'] == 'SEARCH_COMPLETED'
    assert check_result(search.data, search.best)['valid']
