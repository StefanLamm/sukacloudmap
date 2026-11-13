"""Independent finite-pool oracles for joint physical-vehicle allocation."""
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
import random
from types import SimpleNamespace

import pytest

import app.route_selection as selection
from app.routing_engine import StopSearch, quote_signature


def problem(task_count=4, vehicle_count=2, common='100', capacity=10):
    quote = {'mode': 'KM', 'per_km': '1'}
    tasks = {str(index): {'id': str(index), 'warehouse_id': 'W', 'pieces': 1,
                          'quote': deepcopy(quote), 'signature': quote_signature(quote),
                          'common_unit_price': common}
             for index in range(task_count)}
    vehicles = {f'V{index}': {'id': f'V{index}', 'warehouse_id': 'W', 'available': True,
                             'max_pieces': capacity} for index in range(vehicle_count)}
    return {'tasks': tasks, 'vehicles': vehicles, 'warehouses': {'W': {'id': 'W'}}}


def route(vehicle, tasks, fee):
    return {'vehicle_id': vehicle, 'task_ids': list(tasks), 'fee': f'{Decimal(fee):.2f}',
            'meters': int(Decimal(fee) * 1000), 'seconds': 60, 'pieces': len(tasks)}


def oracle(data, candidates):
    # This deliberately does not use any selector helper or linear constraint.
    # Enumerate every subset, apply physical conflicts and price uncovered stores.
    best = None
    for count in range(len(candidates) + 1):
        for routes in combinations(candidates, count):
            cars, tasks = [], []
            for item in routes:
                cars.append(item['vehicle_id']); tasks.extend(item['task_ids'])
            if len(cars) != len(set(cars)) or len(tasks) != len(set(tasks)):
                continue
            missing, fee = 0, sum(Decimal(item['fee']) for item in routes)
            for task_id, task in data['tasks'].items():
                if task_id not in tasks:
                    if task['common_unit_price'] is None:
                        missing += 1
                    else:
                        fee += Decimal(task['common_unit_price']) * task['pieces']
            score = missing, fee, sum(item['meters'] for item in routes), len(routes)
            if best is None or score < best:
                best = score
    return best


def score(data, routes):
    covered = {task for item in routes for task in item['task_ids']}
    fee = sum(Decimal(item['fee']) for item in routes)
    missing = 0
    for task_id, task in data['tasks'].items():
        if task_id not in covered:
            if task['common_unit_price'] is None:
                missing += 1
            else:
                fee += Decimal(task['common_unit_price']) * task['pieces']
    return missing, fee, sum(item['meters'] for item in routes), len(routes)


def test_two_conflicting_routes_must_be_replaced_together():
    data = problem()
    incumbent = [route('V0', ['0', '1'], 100), route('V1', ['2', '3'], 100)]
    replacement = [route('V0', ['0', '2'], 60), route('V1', ['1', '3'], 60)]
    # Each individual replacement conflicts with the other incumbent route.
    for item in replacement:
        assert score(data, [item]) > score(data, incumbent)
    selected, diagnostics = selection.select_candidate_routes(data, replacement, incumbent, 4)
    assert {tuple(item['task_ids']) for item in selected} == {('0', '2'), ('1', '3')}
    assert score(data, selected) == (0, Decimal('120'), 120000, 2)
    assert diagnostics['candidate_pool_optimal']
    assert diagnostics['scope'] == 'CANDIDATE_POOL_ONLY'
    assert diagnostics['mip_gap'] == 0


def test_more_dedicated_vehicles_can_lower_total_cost():
    data = problem(task_count=4, vehicle_count=3)
    incumbent = [route('V0', ['0', '1', '2', '3'], 220)]
    candidates = [route('V0', ['0', '1'], 50), route('V1', ['2'], 30), route('V2', ['3'], 30)]
    selected, diagnostics = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert len(selected) == 3
    assert score(data, selected) == (0, Decimal('110'), 110000, 3)
    assert diagnostics['candidate_pool_optimal']


def test_coverage_has_priority_over_price_and_common_has_no_time_constraint():
    data = problem(task_count=3, vehicle_count=2, common=None)
    data['tasks']['2'].update(common_unit_price='10', open_at=None, close_at=None,
                              latest_arrival_at='1900-01-01T00:00:00', unload_minutes=100000)
    candidates = [route('V0', ['0'], 9000), route('V1', ['1'], 8000), route('V1', ['2'], 1)]
    selected, diagnostics = selection.select_candidate_routes(data, candidates, [], 4)
    assert score(data, selected) == (0, Decimal('17010'), 17000000, 2)
    assert diagnostics['candidate_pool_optimal']


def test_random_small_pools_match_independent_exhaustive_oracle():
    rng = random.Random(20261112)
    for _ in range(50):
        data = problem(task_count=5, vehicle_count=3, capacity=3)
        for task in data['tasks'].values():
            task['common_unit_price'] = None if rng.random() < .25 else str(rng.randint(1, 90))
        candidates = []
        for _ in range(9):
            task_ids = rng.sample(list(data['tasks']), rng.randint(1, 3))
            candidates.append(route(f'V{rng.randrange(3)}', task_ids, rng.randint(1, 160)))
        selected, diagnostics = selection.select_candidate_routes(data, candidates, [], 4)
        assert score(data, selected) == oracle(data, candidates)
        assert diagnostics['candidate_pool_optimal'], diagnostics


def test_invalid_capacity_fee_and_duplicate_candidates_are_not_selected():
    data = problem(capacity=2)
    valid = route('V0', ['0', '1'], 50)
    bad_capacity = route('V1', ['1', '2', '3'], 1)
    bad_fee = {**route('V1', ['2', '3'], 50), 'fee': '0.00'}
    bad_duplicate = route('V1', ['2', '2'], 1)
    bad_count = {**route('V1', ['2', '3'], 50), 'pieces': 0}
    selected, diagnostics = selection.select_candidate_routes(data, [valid, valid, bad_capacity, bad_fee, bad_duplicate, bad_count], [], 4)
    assert selected == [valid]
    assert diagnostics['invalid_candidates'] == 4
    assert diagnostics['candidate_count'] == 1


def test_different_quote_and_warehouse_cannot_share_route():
    data = problem(task_count=3)
    data['tasks']['1']['quote'] = {'mode': 'KM', 'per_km': '2'}
    data['tasks']['2']['warehouse_id'] = 'OTHER'
    selected, diagnostics = selection.select_candidate_routes(data, [route('V0', ['0', '1'], 1), route('V1', ['0', '2'], 1)], [], 4)
    assert selected == []
    assert diagnostics['invalid_candidates'] == 2


@pytest.mark.parametrize('behavior,reason', [('unavailable', 'SOLVER_UNAVAILABLE'), ('error', 'SOLVER_ERROR'), ('timeout', 'TIME_LIMIT'), ('fractional', 'INVALID_SOLVER_RESULT')])
def test_solver_failure_keeps_incumbent_and_reports_fallback(monkeypatch, behavior, reason):
    data = problem()
    incumbent = [route('V0', ['0', '1', '2', '3'], 200)]
    candidates = [route('V1', ['0', '1', '2', '3'], 50)]
    original_api = selection._solver_api()

    def api():
        if behavior == 'unavailable':
            raise ImportError('optional solver absent')
        def solve(*args, **kwargs):
            if behavior == 'error':
                raise RuntimeError('solver error')
            if behavior == 'timeout':
                return SimpleNamespace(status=1, x=None)
            return SimpleNamespace(status=0, x=[.5] * len(args[0]))
        return (*original_api[:3], solve, original_api[4])
    monkeypatch.setattr(selection, '_solver_api', api)
    selected, diagnostics = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert selected == incumbent
    assert diagnostics['fallback_reason'] == reason
    assert not diagnostics['candidate_pool_optimal']


def test_cancellation_propagates_before_or_after_solver(monkeypatch):
    data = problem()
    candidates = [route('V0', ['0', '1'], 10)]
    with pytest.raises(StopSearch, match='CANCELLED'):
        selection.select_candidate_routes(data, candidates, [], 4, lambda: True)
    calls = [False]
    api = selection._solver_api()
    def solve(*args, **kwargs):
        result = api[3](*args, **kwargs)
        calls[0] = True
        return result
    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    with pytest.raises(StopSearch, match='CANCELLED'):
        selection.select_candidate_routes(data, candidates, [], 4, lambda: calls[0])


def test_timeout_keeps_discretely_verified_improvement_across_bounded_slices(monkeypatch):
    data = problem()
    incumbent = [route('V0', ['0', '1', '2', '3'], 200)]
    candidates = [route('V1', ['0', '1', '2', '3'], 50)]
    api = selection._solver_api()
    now, calls, upper_bounds = [0.0], [], []
    monkeypatch.setattr(selection, 'monotonic', lambda: now[0])

    def solve(objective, **kwargs):
        duration = kwargs['options']['time_limit']
        calls.append(duration); now[0] += duration
        upper_bounds.append(kwargs['constraints'].ub[-1])
        if len(calls) == 1:
            values = [0] * len(objective)
            values[1] = 1  # Cheaper V1 route; no common or missing tickets.
            return SimpleNamespace(status=1, x=values, mip_gap=.5)
        return SimpleNamespace(status=1, x=None)

    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    selected, diagnostics = selection.select_candidate_routes(data, candidates, incumbent, 2.2)
    assert selected == candidates
    assert diagnostics['fallback_reason'] == 'TIME_LIMIT'
    assert diagnostics['improved_incumbent']
    assert not diagnostics['candidate_pool_optimal'] and diagnostics['mip_gap'] is None
    assert 2 <= len(calls) <= 3 and all(0 < duration <= 1 for duration in calls)
    assert diagnostics['elapsed_seconds'] <= 2.2
    assert upper_bounds[1] < upper_bounds[0]


def flat_problem(task_count=4, vehicle_count=2, base='1', included=1, extra='1'):
    data = problem(task_count=task_count, vehicle_count=vehicle_count)
    quote = {'mode': 'BASE_STOPS', 'base_price': base, 'included_stops': included,
             'extra_stop_price': extra}
    for task in data['tasks'].values():
        task['quote'] = deepcopy(quote)
        task['signature'] = quote_signature(quote)
    return data


def flat_route(vehicle, tasks, meters, fee=None):
    ids = list(tasks)
    return {'vehicle_id': vehicle, 'task_ids': ids, 'fee': money(fee if fee is not None else len(ids)),
            'meters': meters, 'seconds': 60, 'pieces': len(ids)}


def money(value):
    return f'{Decimal(value):.2f}'


def test_equal_cost_prefers_shorter_total_distance_even_with_more_vehicles():
    data = flat_problem()
    incumbent = [flat_route('V0', ['0', '1', '2', '3'], 80000)]
    candidates = [flat_route('V0', ['0', '1'], 10000), flat_route('V1', ['2', '3'], 10000)]
    selected, diagnostic = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert score(data, selected) == oracle(data, incumbent + candidates) == (0, Decimal('4'), 20000, 2)
    assert diagnostic['coverage_optimal'] and diagnostic['cost_optimal_for_coverage']
    assert diagnostic['distance_optimal_for_coverage_and_cost']
    assert diagnostic['vehicle_count_optimal_for_coverage_cost_distance']
    assert diagnostic['candidate_pool_optimal'] and diagnostic['total_meters'] == 20000


def test_equal_cost_and_distance_prefers_fewer_physical_cars():
    data = flat_problem()
    incumbent = [flat_route('V0', ['0', '1'], 10000), flat_route('V1', ['2', '3'], 10000)]
    candidates = [flat_route('V0', ['0', '1', '2', '3'], 20000)]
    selected, diagnostic = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert score(data, selected) == (0, Decimal('4'), 20000, 1)
    assert diagnostic['candidate_pool_optimal']
    assert diagnostic['vehicle_count_optimal_for_coverage_cost_distance']


def test_one_cent_more_cannot_buy_shorter_distance_or_higher_loading():
    data = problem(task_count=2, vehicle_count=2)
    incumbent = [{'vehicle_id': 'V0', 'task_ids': ['0', '1'], 'fee': '0.01',
                  'meters': 14, 'seconds': 60, 'pieces': 2}]
    candidates = [{'vehicle_id': f'V{i}', 'task_ids': [str(i)], 'fee': '0.01',
                   'meters': 6, 'seconds': 60, 'pieces': 1} for i in range(2)]
    selected, diagnostic = selection.select_candidate_routes(data, candidates, incumbent, 4)
    # 12 m with two cars costs .02, while 14 m with one car costs .01.
    assert selected == incumbent
    assert score(data, selected) == (0, Decimal('.01'), 14, 1)
    assert diagnostic['candidate_pool_optimal']


def test_random_fixed_per_stop_prices_match_full_cost_distance_fleet_oracle():
    rng = random.Random(20261113)
    for _ in range(50):
        data = flat_problem(task_count=5, vehicle_count=3)
        for task in data['tasks'].values():
            task['common_unit_price'] = None if rng.random() < .2 else str(rng.randint(1, 4))
        candidates = []
        for _ in range(9):
            ids = rng.sample(list(data['tasks']), rng.randint(1, 3))
            candidates.append(flat_route(f'V{rng.randrange(3)}', ids, rng.randint(1, 60)))
        selected, diagnostic = selection.select_candidate_routes(data, candidates, [], 4)
        assert score(data, selected) == oracle(data, candidates)
        assert diagnostic['candidate_pool_optimal'], diagnostic


def test_distance_proof_is_conditional_when_money_timed_out(monkeypatch):
    data = flat_problem()
    incumbent = [flat_route('V0', ['0', '1', '2', '3'], 80000)]
    candidates = [flat_route('V1', ['0', '1', '2', '3'], 20000)]
    api = selection._solver_api()
    now, durations = [0.0], []
    monkeypatch.setattr(selection, 'monotonic', lambda: now[0])
    def solve(objective, **kwargs):
        duration = kwargs['options']['time_limit']
        durations.append(duration); now[0] += duration
        if len(durations) == 1:
            return SimpleNamespace(status=1, x=None)
        return api[3](objective, **kwargs)
    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    selected, diagnostic = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert selected == candidates
    assert not diagnostic['cost_optimal_for_coverage']
    assert diagnostic['distance_optimal_for_coverage_and_cost']
    assert diagnostic['distance_proof_fee'] == '4.00'
    assert not diagnostic['candidate_pool_optimal'] and diagnostic['mip_gap'] is None
    assert all(0 < value <= 1 for value in durations)


def test_cancellation_after_distance_slice_does_not_publish_result(monkeypatch):
    data = flat_problem()
    incumbent = [flat_route('V0', ['0', '1', '2', '3'], 80000)]
    candidates = [flat_route('V1', ['0', '1', '2', '3'], 20000)]
    api = selection._solver_api()
    calls = [0]
    def solve(objective, **kwargs):
        result = api[3](objective, **kwargs)
        calls[0] += 1
        return result
    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    with pytest.raises(StopSearch, match='CANCELLED'):
        selection.select_candidate_routes(data, candidates, incumbent, 4, lambda: calls[0] >= 2)


def test_distance_timeout_keeps_shorter_verified_route_without_losing_cost_proof(monkeypatch):
    data = flat_problem()
    incumbent = [flat_route('V0', ['0', '1', '2', '3'], 80000)]
    candidates = [flat_route('V1', ['0', '1', '2', '3'], 20000)]
    api = selection._solver_api()
    now, durations = [0.0], []
    monkeypatch.setattr(selection, 'monotonic', lambda: now[0])
    def solve(objective, **kwargs):
        duration = kwargs['options']['time_limit']
        durations.append(duration); now[0] += duration
        if len(durations) == 1:
            return api[3](objective, **kwargs)  # proves no cheaper total fee
        if len(durations) == 2:
            values = [0] * len(objective)
            values[1] = 1  # short route with exactly the retained cost
            return SimpleNamespace(status=1, x=values)
        return SimpleNamespace(status=1, x=None)
    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    selected, diagnostic = selection.select_candidate_routes(data, candidates, incumbent, 2.2)
    assert selected == candidates
    assert diagnostic['cost_candidate_pool_optimal'] and diagnostic['cost_mip_gap'] == 0
    assert not diagnostic['distance_optimal_for_coverage_and_cost']
    assert not diagnostic['candidate_pool_optimal'] and diagnostic['mip_gap'] is None
    assert diagnostic['fallback_reason'] == 'TIME_LIMIT'
    assert all(0 < value <= 1 for value in durations)
    assert diagnostic['elapsed_seconds'] <= 2.2


def test_timeout_fractional_or_conflicting_solution_never_replaces_incumbent(monkeypatch):
    data = problem()
    incumbent = [route('V0', ['0', '1', '2', '3'], 200)]
    candidates = [route('V1', ['0', '1', '2', '3'], 50)]
    api = selection._solver_api()
    def solve(objective, **kwargs):
        values = [0] * len(objective)
        values[0] = values[1] = 1  # Double coverage is never a valid incumbent.
        return SimpleNamespace(status=1, x=values)
    monkeypatch.setattr(selection, '_solver_api', lambda: (*api[:3], solve, api[4]))
    selected, diagnostics = selection.select_candidate_routes(data, candidates, incumbent, 4)
    assert selected == incumbent and not diagnostics['improved_incumbent']


def test_empty_or_expired_pool_keeps_incumbent():
    data = problem()
    selected, diagnostics = selection.select_candidate_routes(data, [], [], 0)
    assert selected == [] and diagnostics['candidate_pool_optimal']
    incumbent = [route('V0', ['0'], 10)]
    selected, diagnostics = selection.select_candidate_routes(data, [], incumbent, 0)
    assert selected == incumbent and diagnostics['fallback_reason'] == 'TIME_LIMIT'
