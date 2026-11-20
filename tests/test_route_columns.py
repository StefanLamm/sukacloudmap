"""Independent finite-pool and current-road checks for daily joint selection."""
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
import random
from types import SimpleNamespace
import threading
import time

import pytest

import app.route_columns as columns
from app.road_service import evidence, road_request
from app.routing_engine import assemble, check_result, evaluate_route, normalize


class MatrixRoad:
    def __init__(self, matrix):
        self.matrix = matrix
        self.strict_reads = self.metric_reads = 0
        self.drift = False

    def known_segment(self, origin, destination):
        self.metric_reads += 1
        return self.matrix.get((origin['id'], destination['id']))

    def segment(self, vehicle, origin, destination, departure):
        self.strict_reads += 1
        row = self.matrix[(origin['id'], destination['id'])]
        return evidence(road_request(vehicle, origin, destination, departure, 'FIXTURE'),
                        row['meters'] * (2 if self.drift else 1), row['seconds'], 'FIXTURE')

    def stats(self):
        return {'calls': 0, 'fixture_reads': self.strict_reads}


def problem(n=4, cars=2, common='100', capacity=4):
    body = {'source': 'FIXTURE', 'delivery_date': '2026-11-13',
            'warehouses': [{'id': 'W', 'longitude': 0., 'latitude': 0.,
                            'earliest_departure_at': '2026-11-13T06:00:00+08:00'}],
            'vehicles': [{'id': 'V' + str(i), 'warehouse_id': 'W', 'available': True,
                          'max_pieces': capacity} for i in range(cars)],
            'quotes': {'Q': {'mode': 'KM', 'per_km': '1'}},
            'tasks': [{'id': str(i), 'store_id': str(i), 'warehouse_id': 'W', 'pieces': 1,
                       'quote_id': 'Q', 'common_unit_price': common, 'longitude': float(i),
                       'latitude': 0., 'open_at': '2026-11-13T06:00:00+08:00',
                       'close_at': '2026-11-13T23:00:00+08:00',
                       'latest_arrival_at': None, 'unload_minutes': 0} for i in range(n)]}
    ids = ['W'] + [str(i) for i in range(n)]
    matrix = {(a, b): {'meters': 10000 if a == 'W' else 100000, 'seconds': 60}
              for a in ids for b in ids if a != b}
    if n >= 2:
        matrix['0', '1']['meters'] = 1000
    if n >= 4:
        matrix['2', '3']['meters'] = 1000
    return body, MatrixRoad(matrix)


def plan(body, road, routes=()):
    data = normalize(body)
    evaluated = [evaluate_route(data, vehicle, list(ids), road) for vehicle, ids in routes]
    assigned = {i for row in evaluated for i in row['task_ids']}
    common = {i for i, task in data['tasks'].items() if i not in assigned and task['common_unit_price'] is not None}
    return assemble(data, evaluated, common, set(data['tasks']) - common - assigned)


def proposals(*orders):
    return [{'vehicle_id': 'V0', 'task_ids': list(order), 'fee': '0', 'meters': 0, 'seconds': 0}
            for order in orders]


def test_cross_run_recipes_reassign_real_cars_and_match_integer_pool_minimum():
    body, road = problem()
    left = plan(body, road, [('V0', ['0', '1'])])
    right = plan(body, road, [('V0', ['2', '3'])])
    original = deepcopy([left, right])
    result = columns.select_portfolio(body, road, [left, right], seconds=3)
    assert left['total_fee'] == right['total_fee'] == '211.00'
    assert result['total_fee'] == '22.00'
    assert len({r['vehicle_id'] for r in result['routes']}) == 2
    assert not result['common'] and not result['unassigned']
    assert result['algorithm']['class_count'] == 1
    assert result['algorithm']['finite_pool_cost_optimal']
    assert not result['algorithm']['optimality_proven']
    assert result['algorithm']['integer_objective_cents'] == 2200
    assert check_result(normalize(body), result)['valid']
    assert [left, right] == original


def test_single_car_cannot_use_two_equivalent_patterns():
    body, road = problem(cars=1)
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1'], ['2', '3']))
    assert result['total_fee'] == '211.00'
    assert len(result['routes']) == 1
    assert len(result['common']) == 2


def test_vehicle_classes_use_complete_valid_choices_not_just_shared_warehouse():
    body, road = problem(n=2)
    body['vehicles'][0]['max_pieces'] = 1
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1']))
    assert result['total_fee'] == '11.00'
    assert result['routes'][0]['vehicle_id'] == 'V1'
    assert result['algorithm']['class_count'] == 2


def test_shared_metric_pricing_filters_ineligible_cars_and_binds_each_selected_car():
    body, road = problem(cars=6, capacity=2)
    body['warehouses'].append({**body['warehouses'][0], 'id': 'OTHER'})
    body['vehicles'].extend([
        {'id': 'WRONG', 'warehouse_id': 'OTHER', 'available': True, 'max_pieces': 100},
        {'id': 'SMALL', 'warehouse_id': 'W', 'available': True, 'max_pieces': 1},
        {'id': 'OFF', 'warehouse_id': 'W', 'available': False, 'max_pieces': 100},
    ])
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1'], ['2', '3']))
    assert result['total_fee'] == '22.00'
    assert len(result['routes']) == len({r['vehicle_id'] for r in result['routes']}) == 2
    details = result['algorithm']
    assert details['metric_evaluations'] == 2
    assert details['shared_metric_proposals'] == 10
    assert details['eligible_vehicle_options'] == 12
    assert details['ineligible_vehicle_options'] == 4
    for route in result['routes']:
        assert route['vehicle_id'].startswith('V')
        assert all(s['request']['vehicle_id'] == route['vehicle_id'] for s in route['segments'])
    assert check_result(normalize(body), result)['valid']


def test_shared_pricing_keeps_different_capacity_choice_sets_separate():
    body, road = problem(n=2, cars=3, capacity=2)
    body['vehicles'][0]['max_pieces'] = 1
    body['vehicles'][2]['max_pieces'] = 3
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1']))
    assert result['total_fee'] == '11.00'
    assert result['routes'][0]['vehicle_id'] in {'V1', 'V2'}
    assert result['algorithm']['metric_evaluations'] == 2
    assert result['algorithm']['shared_metric_proposals'] == 0
    assert result['algorithm']['class_count'] == 2


def test_shared_pricing_does_not_copy_metric_estimates_as_actual_evidence():
    body, road = problem(n=2, cars=6)
    old = plan(body, road)
    road.drift = True
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['0', '1']))
    # Compact metric cost is 11, but each actual bound route costs 22. The
    # evidence mismatch must not be promoted to a proven eleven-yuan plan.
    assert result['total_fee'] == '22.00'
    assert result['algorithm']['materialization_mismatch']
    assert not result['algorithm']['finite_pool_cost_optimal']
    assert check_result(normalize(body), result)['valid']
    route = result['routes'][0]
    assert all(s['request']['vehicle_id'] == route['vehicle_id'] for s in route['segments'])


def test_ineligible_fleet_still_observes_cancellation_before_the_next_order(monkeypatch):
    body, road = problem(n=3, cars=1, capacity=1)
    old = plan(body, road)
    excluded = []
    stopped = False
    original = columns._pricing_fleet

    def prefilter(*args, **kwargs):
        nonlocal stopped
        fleet = original(*args, **kwargs)
        assert not fleet
        excluded.append(tuple(args[1]))
        stopped = True
        return fleet

    monkeypatch.setattr(columns, '_pricing_fleet', prefilter)
    result = columns.select_portfolio(body, road, [old], seconds=3,
        cancelled=lambda: stopped, candidate_recipes=proposals(['0', '1'], ['1', '2'], ['0', '2']))
    assert len(excluded) == 1
    assert result['total_fee'] == old['total_fee']
    assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert check_result(normalize(body), result)['valid']


@pytest.mark.parametrize('common', ['5', '5.5'])
def test_all_optional_column_dominated_by_common_never_inflates_the_model(common):
    body, road = problem(n=2, cars=6, common=common)
    old = plan(body, road)
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['0', '1']))
    assert result['total_fee'] == old['total_fee']
    assert not result['routes']
    assert result['algorithm']['column_count'] == 0
    assert result['algorithm']['common_dominated_metric_groups'] == 1
    assert result['algorithm']['common_dominated_vehicle_options'] == 6
    assert result['algorithm']['finite_pool_cost_optimal']


def test_dominated_proposal_filter_keeps_whole_source_hints_and_mandatory_columns():
    body, road = problem(cars=2, capacity=2)
    road.matrix['W', '2']['meters'] = 249000
    old = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    assert old['total_fee'] == '261.00'
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['2', '3']))
    assert result['total_fee'] == '211.00'
    assert result['algorithm']['common_dominated_metric_groups'] == 1
    assert result['algorithm']['finite_pool_cost_optimal']
    assert check_result(normalize(body), result)['valid']

    body['tasks'][2]['common_unit_price'] = None
    mandatory = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    required = columns.select_portfolio(body, road, [mandatory], seconds=3,
                                        candidate_recipes=proposals(['2', '3']))
    assert required['total_fee'] == '261.00'
    assert required['algorithm']['common_dominated_metric_groups'] == 0
    assert any('2' in route['task_ids'] for route in required['routes'])
    assert check_result(normalize(body), required)['valid']


def test_cheaper_order_can_dominate_incumbent_without_breaking_model_hint():
    body, road = problem(n=2, cars=1)
    road.matrix['1', '0']['meters'] = 100
    old = plan(body, road, [('V0', ['0', '1'])])
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['1', '0']))
    assert old['total_fee'] == '11.00'
    assert result['total_fee'] == '10.10'
    assert result['routes'][0]['task_ids'] == ['1', '0']
    assert result['algorithm']['finite_pool_cost_optimal']


def test_required_dedicated_delivery_has_no_common_or_missing_model_choice():
    body, road = problem(n=2, cars=1, capacity=1)
    body['tasks'][0]['common_unit_price'] = None
    road.matrix['W', '0']['meters'] = 100000
    old = plan(body, road, [('V0', ['0'])])
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['0'], ['1']))
    assert result['total_fee'] == '200.00'
    assert result['routes'][0]['task_ids'] == ['0']
    assert result['common'][0]['task_id'] == '1'
    assert not result['unassigned']


def test_missing_required_route_is_visible_and_never_claims_complete_or_optimal():
    body, road = problem(n=1, cars=0, common=None)
    result = columns.select_portfolio(body, road, [], seconds=3)
    assert [r['task_id'] for r in result['unassigned']] == ['0']
    assert not result['algorithm']['complete_plan_verified']
    assert not result['algorithm']['finite_pool_cost_optimal']
    assert result['algorithm']['solver_status'] == 'INFEASIBLE'


@pytest.mark.parametrize('policy,expected', [('ONCE', '6.00'), ('PER_REGION', '9.00'), ('PER_STORE', '12.00')])
def test_special_region_actual_prices_are_recomputed_per_current_policy(policy, expected):
    body, road = problem(n=3, cars=1)
    for row in road.matrix.values():
        row['meters'] = 1000
    for i, task in enumerate(body['tasks']):
        task.update(province='安徽省', city='合肥市', district='蜀山区' if i < 2 else '包河区',
                    region_code='340104' if i < 2 else '340111')
    body['quotes']['Q'].update(mode='KM_REGION', region_surcharge={
        'amount': '3', 'billing_mode': policy,
        'targets': [{'province_code': '340000', 'province_name': '安徽省', 'city_code': '340100',
                     'city_name': '合肥市', 'district_code': '', 'district_name': ''}]})
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1', '2']))
    assert result['total_fee'] == expected
    assert check_result(normalize(body), result)['valid']


def test_actual_cent_objective_cannot_substitute_rounding_or_proposal_fee():
    body, road = problem(n=2)
    body['quotes']['Q']['per_km'] = '5.8'
    for task in body['tasks']:
        task['common_unit_price'] = '.015'
    for row in road.matrix.values():
        row['meters'] = 1
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0'], ['1']))
    assert result['total_fee'] == '.02'.rjust(4, '0')
    assert result['algorithm']['integer_objective_cents'] == 2
    assert all(route['fee'] == '0.01' for route in result['routes'])


@pytest.mark.parametrize('reason', ['unavailable', 'broken'])
def test_solver_failure_keeps_cheapest_current_verified_plan(monkeypatch, reason):
    body, road = problem()
    old = plan(body, road, [('V0', ['0', '1'])])
    def fail():
        if reason == 'unavailable':
            raise ImportError('not installed')
        raise RuntimeError('native failure')
    monkeypatch.setattr(columns, '_solver_api', fail)
    result = columns.select_portfolio(body, road, [old], seconds=3,
                                      candidate_recipes=proposals(['2', '3']))
    assert result['total_fee'] == old['total_fee']
    assert result['algorithm']['fallback']
    assert not result['algorithm']['finite_pool_cost_optimal']


@pytest.mark.parametrize('seconds,cancelled,reason', [(0, lambda: False, 'TIME_BUDGET'),
                                                    (3, lambda: True, 'CANCELLED')])
def test_interrupt_keeps_current_incumbent_without_fetching_more_evidence(seconds, cancelled, reason):
    body, road = problem()
    old = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    before = road.strict_reads
    result = columns.select_portfolio(body, road, [old], seconds=seconds, cancelled=cancelled)
    assert result['total_fee'] == old['total_fee'] == '22.00'
    assert road.strict_reads == before
    assert result['algorithm']['stop_reason'] == reason
    assert check_result(normalize(body), result)['valid']


def test_road_price_changes_never_reuse_cheaper_old_evidence():
    body, road = problem(n=2, cars=1)
    old = plan(body, road, [('V0', ['0', '1'])])
    road.matrix['W', '0']['meters'] = 20000
    result = columns.select_portfolio(body, road, [old], seconds=3)
    assert old['total_fee'] == '11.00'
    assert result['total_fee'] == '21.00'
    assert check_result(normalize(body), result)['valid']


def test_current_matrix_and_materialized_fee_mismatch_clears_certificate():
    body, road = problem()
    source = plan(body, road)
    road.drift = True
    result = columns.select_portfolio(body, road, [source], seconds=3,
                                      candidate_recipes=proposals(['0', '1'], ['2', '3']))
    assert result['total_fee'] == '44.00'
    assert check_result(normalize(body), result)['valid']
    assert result['algorithm']['materialization_mismatch']
    assert not result['algorithm']['finite_pool_cost_optimal']


def test_native_cancel_watcher_stops_and_is_joined(monkeypatch):
    cp = columns._solver_api()
    started = time.monotonic()
    stop = threading.Event()
    class FakeSolver:
        parameters = SimpleNamespace()
        wall_time = .1
        def solve(self, model):
            assert stop.wait(1), 'cancellation watcher did not stop native solver'
            return 0
        def stop_search(self):
            stop.set()
        def status_name(self, status):
            return 'UNKNOWN'
    monkeypatch.setattr(columns, '_solver_api', lambda: SimpleNamespace(
        CpModel=cp.CpModel, CpSolver=FakeSolver, OPTIMAL=cp.OPTIMAL, FEASIBLE=cp.FEASIBLE))
    body, road = problem(n=2)
    data = normalize(body)
    incumbent = plan(body, road)
    selected, diagnostic = columns._solve(data, [], incumbent, 2,
                                           lambda: time.monotonic() - started > .05)
    assert selected is None and diagnostic['interrupted'] == 'CANCELLED'
    assert not any(t.name == 'suka-column-cancel' and t.is_alive() for t in threading.enumerate())


def test_random_small_pools_match_independent_exhaustive_allocation_oracle():
    rng = random.Random(73191)
    for _ in range(15):
        body, road = problem(n=4, cars=2, capacity=3)
        for task in body['tasks']:
            task['common_unit_price'] = str(rng.randint(5, 40))
        for row in road.matrix.values():
            row['meters'] = rng.randint(1, 25) * 1000
        orders = {tuple(rng.sample(['0', '1', '2', '3'], rng.randint(1, 3))) for __ in range(7)}
        result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                          candidate_recipes=proposals(*orders))
        data = normalize(body)
        choices = [evaluate_route(data, 'V'+str(v), list(ids), road) for v in range(2) for ids in orders]
        optimum = sum(Decimal(t['common_unit_price']) for t in body['tasks'])
        for count in (1, 2):
            for selected in combinations(choices, count):
                if len({r['vehicle_id'] for r in selected}) != count:
                    continue
                assigned = [i for r in selected for i in r['task_ids']]
                if len(assigned) != len(set(assigned)):
                    continue
                amount = sum(Decimal(r['fee']) for r in selected)
                amount += sum(Decimal(t['common_unit_price']) for t in body['tasks'] if t['id'] not in assigned)
                optimum = min(optimum, amount)
        assert Decimal(result['total_fee']) == optimum
        assert result['algorithm']['finite_pool_cost_optimal']
        assert check_result(data, result)['valid']


def test_all_completed_round_recipes_precede_general_pool_and_survive_solver_failure(monkeypatch):
    body, road = problem()
    recipes = [{'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}],
                'common_ids': ['2', '3'], 'total_fee': '0'} for _ in range(127)]
    recipes.append({'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '1']},
                              {'vehicle_id': 'V1', 'task_ids': ['2', '3']}],
                    'common_ids': [], 'total_fee': '999999'})
    def unavailable():
        raise ImportError('native not installed')
    monkeypatch.setattr(columns, '_solver_api', unavailable)
    monkeypatch.setattr(columns, 'MAX_ORDERS', 0)
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      complete_recipes=recipes,
                                      candidate_recipes=proposals(['1', '3'], ['0', '2']))
    assert result['total_fee'] == '22.00'
    assert result['algorithm']['complete_recipes_priced'] == 128
    assert result['algorithm']['fallback_reason'] == 'SOLVER_UNAVAILABLE'
    assert result['algorithm']['candidate_pool_truncated']
    assert check_result(normalize(body), result)['valid']


def test_deadline_limited_order_stream_prices_during_admission(monkeypatch):
    body, road = problem()
    road.deadline_limited_candidates = True
    monkeypatch.setattr(columns, 'MAX_ORDERS', 1)
    def stream():
        yield proposals(['0', '1'])[0]
        assert road.metric_reads > 0
        yield proposals(['2', '3'])[0]
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=stream())
    assert result['total_fee'] == '22.00'
    assert check_result(normalize(body), result)['valid']
    assert result['algorithm']['unique_orders'] == 2
    assert result['algorithm']['order_limit'] is None


def test_complete_compact_recipe_with_unknown_edge_never_fetches_it():
    body, road = problem(n=2, cars=1)
    source = plan(body, road)
    del road.matrix['W', '0']
    result = columns.select_portfolio(body, road, [source], seconds=3,
                                      complete_recipes=[{'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}],
                                                         'common_ids': []}])
    assert result['total_fee'] == '200.00'
    assert not result['routes']
    assert road.strict_reads == 0
    assert result['algorithm']['complete_recipes_missing_metrics'] == 1


def test_incomplete_or_duplicate_whole_recipes_are_only_proposals_not_incumbents():
    body, road = problem(n=2, cars=1)
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      complete_recipes=[
                                          {'routes': [{'vehicle_id': 'V0', 'task_ids': ['0']}], 'common_ids': []},
                                          {'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}], 'common_ids': ['0']},
                                          {'routes': [{'vehicle_id': ['BAD'], 'task_ids': ['0', '1']}], 'common_ids': []}])
    assert result['total_fee'] == '11.00'
    assert result['algorithm']['complete_recipes_priced'] == 0
    assert check_result(normalize(body), result)['valid']


def test_same_money_prefers_shorter_total_distance_before_car_count():
    body, road = problem(n=2)
    body['quotes']['Q'] = {'mode': 'BASE_STOPS', 'base_price': '5',
                           'included_stops': 1, 'extra_stop_price': '5'}
    road.matrix['0', '1']['meters'] = 100000
    source = plan(body, road, [('V0', ['0', '1'])])
    result = columns.select_portfolio(body, road, [source], seconds=3,
                                      candidate_recipes=proposals(['0'], ['1']))
    assert source['total_fee'] == result['total_fee'] == '10.00'
    assert len(result['routes']) == 2
    assert sum(row['meters'] for row in result['routes']) == 20000
    assert result['algorithm']['finite_pool_cost_optimal']
    assert result['algorithm']['distance_tie_optimality_proven']
    assert result['algorithm']['vehicle_tie_optimality_proven']


def test_same_money_and_distance_then_prefers_fewer_real_cars():
    body, road = problem(n=2)
    body['quotes']['Q'] = {'mode': 'BASE_STOPS', 'base_price': '5',
                           'included_stops': 1, 'extra_stop_price': '5'}
    for row in road.matrix.values():
        row['meters'] = 1000
    source = plan(body, road, [('V0', ['0']), ('V1', ['1'])])
    result = columns.select_portfolio(body, road, [source], seconds=3,
                                      candidate_recipes=proposals(['0', '1']))
    assert source['total_fee'] == result['total_fee'] == '10.00'
    assert sum(row['meters'] for row in result['routes']) == 2000
    assert len(result['routes']) == 1
    assert result['algorithm']['vehicle_tie_optimality_proven']
    assert [row['objective'] for row in result['algorithm']['solver_stages']] == [
        'total_fee_cents', 'total_meters_at_fixed_fee', 'vehicles_at_fixed_fee_and_meters']


def test_tie_solver_failure_preserves_proven_first_stage_cost_solution(monkeypatch):
    cp = columns._solver_api()
    count = 0
    class BrokenTieSolver:
        parameters = SimpleNamespace()
        def solve(self, model):
            raise RuntimeError('second-stage native failure')
        def stop_search(self):
            pass
    def factory():
        nonlocal count
        count += 1
        return cp.CpSolver() if count == 1 else BrokenTieSolver()
    monkeypatch.setattr(columns, '_solver_api', lambda: SimpleNamespace(
        CpModel=cp.CpModel, CpSolver=factory, OPTIMAL=cp.OPTIMAL, FEASIBLE=cp.FEASIBLE))
    body, road = problem()
    result = columns.select_portfolio(body, road, [plan(body, road)], seconds=3,
                                      candidate_recipes=proposals(['0', '1'], ['2', '3']))
    assert result['total_fee'] == '22.00'
    assert result['algorithm']['finite_pool_cost_optimal']
    assert not result['algorithm']['distance_tie_optimality_proven']
    assert result['algorithm']['tie_skipped_reason'] == 'TIE_SOLVER_ERROR'
    assert not any(t.name == 'suka-column-cancel' and t.is_alive() for t in threading.enumerate())
    assert check_result(normalize(body), result)['valid']
