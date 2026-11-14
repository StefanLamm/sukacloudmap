"""Long measured prefixes and independent complete-allocation safeguards."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.route_macro import seed_long_routes
from app.route_search import JointSearch
from app.routing_engine import StopSearch, check_result, normalize
from test_route_columns import plan, problem
from test_route_cost_cleanup import GuardedMeasuredRoad


def prepared(body, road, routes=(), seconds=30):
    data = normalize(body)
    initial = plan(body, road, routes)
    assert check_result(data, initial)['valid']
    search = JointSearch(data, road, seconds, 0, lambda: False, initial)
    search.setup_fleet()
    assert search.consider([search.compact(row) for row in initial['routes']], force=True)
    return search, initial


def trunk_problem(*, capacity=400, close='23:00'):
    body, road = problem(n=19, cars=2, common='5.30', capacity=capacity)
    for n, task in enumerate(body['tasks']):
        task.update(pieces=21 if n == 18 else 19, province='示例省', city='甲市' if n < 10 else '乙市',
                    close_at='2026-11-13T' + close + ':00+08:00')
    for (origin, _), metric in road.matrix.items():
        metric.update(meters=1000000 if origin == 'W' else 5000, seconds=60)
    return body, road


def test_nineteen_store_cross_city_activation_grows_past_ten_losing_stops(monkeypatch):
    body, road = trunk_problem()
    search, initial = prepared(body, road)
    original = deepcopy(initial)
    def forbidden(*args, **kwargs):
        raise AssertionError('Generator must not start a native selector')
    monkeypatch.setattr(search, 'pack', forbidden)
    monkeypatch.setattr('app.route_selection.select_candidate_routes', forbidden)
    details = seed_long_routes(search, max_seeds=4)
    # Independent synthetic-road arithmetic: first 10 stores cost 1045, while
    # even the largest 10-store common bill is only 1017.60. Eleven can turn
    # profitable. No positive-single-stop/positive-city rule may stop growth.
    assert Decimal('1045') > Decimal('1017.60')
    assert details['negative_growth_prefixes'] >= 10
    assert details['maximum_prefix_stores'] == 19
    assert search.best['total_fee'] == '1090.00' < initial['total_fee'] == '1923.90'
    assert len(search.best['routes']) == 1 and search.best['routes'][0]['pieces'] == 363
    assert {search.tasks[item]['city'] for item in search.best['routes'][0]['task_ids']} == {'甲市', '乙市'}
    assert any(len(row['task_ids']) == 10 and Decimal(row['fee']) >
               sum((search.common_cost[item] for item in row['task_ids']), Decimal(0)) for row in search.pool.values())
    assert details['native_selection_runs'] == details['new_network_calls'] == 0
    assert 0 < len(details['accepted_samples']) <= 32
    assert initial == original and check_result(search.data, search.best)['valid']


def insertion_problem(*, middle=False):
    body, road = problem(n=4 if middle else 3, cars=1, common='100', capacity=4 if middle else 3)
    for task in body['tasks']:
        task.update(province='安徽省', city='合肥市', district='蜀山区', region_code='340104')
    if middle:
        # Appending greedily takes A->C->D. B has to be inserted between A/C;
        # neither the reverse bridge C->B nor a first-stop B->A exists.
        meters = {('W', '0'): 1000, ('W', '1'): 9000, ('W', '2'): 9000, ('W', '3'): 9000,
                  ('0', '2'): 100, ('2', '3'): 100, ('0', '1'): 500, ('1', '2'): 500}
    else:
        # A is the closest warehouse entry, while the full route is B->A->C.
        # Once A->C has grown, its missing C->B cannot be guessed or reversed.
        meters = {('W', '0'): 1000, ('W', '1'): 2000, ('W', '2'): 3000,
                  ('1', '0'): 1000, ('0', '2'): 1000}
    road.matrix = {pair: {'meters': value, 'seconds': 60} for pair, value in meters.items()}
    original_segment = road.segment
    def known_only(vehicle, origin, destination, departure):
        assert (origin['id'], destination['id']) in road.matrix, 'An unknown directed road must never be requested'
        return original_segment(vehicle, origin, destination, departure)
    road.segment = known_only
    return body, road


def test_two_seed_allowance_reaches_all_stores_only_by_inserting_before_the_near_gateway():
    body, road = insertion_problem()
    append, initial = prepared(body, road)
    seed_long_routes(append, max_seeds=1)
    assert append.best['total_fee'] == '102.00'
    assert append.best['routes'][0]['task_ids'] == ['0', '2']
    search, _ = prepared(body, road)
    details = seed_long_routes(search, max_seeds=2)
    # Independent route arithmetic: W->B 2km, B->A 1km, A->C 1km.
    assert search.best['total_fee'] == '4.00' and initial['total_fee'] == '300.00'
    assert search.best['routes'][0]['task_ids'] == ['1', '0', '2']
    assert details['seeds_started'] == 2 and details['selected_insertion_growths'] == 2
    assert details['insertion_exact_trials'] <= details['selected_growth_prefixes'] * 3
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('forward_bridge, expected', [(True, '2.10'), (False, '101.20')])
def test_middle_insertion_requires_both_forward_bridges_not_the_reverse(forward_bridge, expected):
    body, road = insertion_problem(middle=True)
    if not forward_bridge:
        del road.matrix['1', '2']
        road.matrix['2', '1'] = {'meters': 500, 'seconds': 60}
    search, _ = prepared(body, road)
    details = seed_long_routes(search, max_seeds=2)
    assert search.best['total_fee'] == expected
    if forward_bridge:
        assert search.best['routes'][0]['task_ids'] == ['0', '1', '2', '3']
        assert search.best['routes'][0]['meters'] == 2100
    else:
        assert all(row['task_ids'] != ['0', '1', '2', '3'] for row in search.pool.values())
    assert details['insertion_bridge_positions'] > 0
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('constraint', ['capacity', 'business_close'])
def test_insertion_cannot_turn_a_cheap_bridge_into_an_invalid_full_route(constraint):
    body, road = insertion_problem(middle=True)
    if constraint == 'capacity':
        body['vehicles'][0]['max_pieces'] = 3
    else:
        body['tasks'][1]['close_at'] = '2026-11-13T06:01:00+08:00'
    search, _ = prepared(body, road)
    seed_long_routes(search, max_seeds=2)
    assert search.best['total_fee'] == '101.20'
    assert not any(len(row['task_ids']) == 4 for row in search.pool.values())
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('quote, expected', [
    ({'mode': 'BASE_STOPS', 'base_price': '5', 'included_stops': 1, 'extra_stop_price': '2', 'per_km': None}, '9.00'),
    ({'mode': 'KM_REGION', 'per_km': '1', 'region_surcharge': {
        'amount': '7', 'billing_mode': 'PER_STORE',
        'targets': [{'province_code': '340000', 'province_name': '安徽省', 'city_code': '340100',
                     'city_name': '合肥市', 'district_code': '', 'district_name': ''}]}}, '25.00'),
])
def test_insertion_uses_complete_base_or_region_fee_not_its_bridge_ranking_proxy(quote, expected):
    body, road = insertion_problem()
    body['quotes']['Q'] = quote
    search, _ = prepared(body, road)
    seed_long_routes(search, max_seeds=2)
    assert search.best['routes'][0]['task_ids'] == ['1', '0', '2']
    assert search.best['total_fee'] == search.best['routes'][0]['fee'] == expected
    assert search.best['common'] == [] and check_result(search.data, search.best)['valid']


def test_many_city_focuses_reserve_global_seed_chains_inside_the_original_limit():
    body, road = problem(n=16, cars=1, common='100', capacity=1)
    for index, task in enumerate(body['tasks']):
        task.update(province='示例省', city='城市' + str(index))
        road.matrix['W', task['id']]['meters'] = 1000 + index
    search, _ = prepared(body, road)
    details = seed_long_routes(search, max_seeds=36, max_stops=1)
    # Sixteen focuses and multiple policies can fill all 36 chains. Reserve
    # unfocused alternatives while retaining the original total chain bound.
    assert details['seeds_started'] == 36
    assert details['global_seed_chains'] == 9
    assert details['focused_seed_chains'] == 27
    assert details['candidate_count'] <= 14000 and check_result(search.data, search.best)['valid']


def test_missing_measured_bridge_is_not_requested_or_replaced_by_coordinates():
    body, road = trunk_problem()
    for pair in list(road.matrix):
        if pair[0] != 'W':
            del road.matrix[pair]
    search, initial = prepared(body, road)
    def forbidden(*args, **kwargs):
        raise AssertionError('No profitable complete proposal; no strict road request expected')
    road.segment = forbidden
    details = seed_long_routes(search, max_seeds=4)
    assert search.best['total_fee'] == initial['total_fee']
    assert all(len(row['task_ids']) == 1 for row in search.pool.values())
    assert details['maximum_prefix_stores'] == 1 and details['new_network_calls'] == 0


def test_long_route_generation_respects_physical_capacity():
    body, road = trunk_problem(capacity=350)
    search, _ = prepared(body, road)
    seed_long_routes(search, max_seeds=4)
    assert all(row['pieces'] <= 350 for row in search.pool.values())
    assert not any(len(row['task_ids']) == 19 for row in search.pool.values())
    assert check_result(search.data, search.best)['valid']


def test_late_second_stop_cannot_be_hidden_by_future_profitable_stops():
    body, road = trunk_problem(close='06:01')
    search, initial = prepared(body, road)
    details = seed_long_routes(search, max_seeds=4)
    assert search.best['total_fee'] == initial['total_fee']
    assert details['infeasible_orders'] > 0
    assert all(len(row['task_ids']) == 1 for row in search.pool.values())
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('scope', ['common', 'compatible'])
def test_existing_mandatory_line_and_its_optional_companion_are_fixed(scope):
    body, road = problem(n=6, cars=2, capacity=4)
    body['tasks'][0]['common_unit_price'] = None
    for pair, metric in road.matrix.items():
        metric['meters'] = 1000
    search, initial = prepared(body, road, [('V0', ['0', '1'])])
    fixed = deepcopy(initial['routes'][0])
    observed = []
    road.candidate_observer = observed.append
    seed_long_routes(search, scope=scope, max_seeds=4)
    kept = next(row for row in search.best['routes'] if row['vehicle_id'] == 'V0')
    assert kept == fixed
    assert observed and all(not {'0', '1'} & set(row['task_ids']) for row in observed)
    assert all(row['vehicle_id'] == 'V1' for row in observed)
    assert check_result(search.data, search.best)['valid']


def test_common_scope_cannot_reuse_an_occupied_physical_vehicle():
    body, road = problem(n=4, cars=1, common='100', capacity=4)
    search, initial = prepared(body, road, [('V0', ['0', '1'])])
    observed = []
    road.candidate_observer = observed.append
    details = seed_long_routes(search, scope='common', max_seeds=4)
    assert search.best == initial and observed == []
    assert details['candidate_count'] == 0


@pytest.mark.parametrize('scope', ['common', 'compatible'])
def test_unavailable_large_group_cannot_hide_a_smaller_usable_group(scope):
    body, road = problem(n=5, cars=2, common='100', capacity=4)
    body['quotes']['R'] = {'mode': 'KM', 'per_km': '2'}
    body['warehouses'].append({**body['warehouses'][0], 'id': 'OTHER'})
    body['vehicles'][1]['warehouse_id'] = 'OTHER'
    body['tasks'][3]['quote_id'] = 'R'
    body['tasks'][4].update(warehouse_id='OTHER', common_unit_price='200')
    road.matrix['OTHER', '4'] = {'meters': 10000, 'seconds': 60}
    search, initial = prepared(body, road, [('V0', ['3'])])
    # The 300-unit Q group cannot use V0, already assigned to another quote.
    # The 200-unit OTHER group does have an unused fitting car and must not be
    # lost before max_groups=1 is applied. Compatible may also use R's own V0.
    assert initial['total_fee'] == '520.00'
    details = seed_long_routes(search, scope=scope, max_groups=1, max_seeds=4)
    assert details['groups_without_available_car'] >= 1
    assert search.best['total_fee'] == '330.00'
    assert next(row for row in search.best['routes'] if row['vehicle_id'] == 'V0')['task_ids'] == ['3']
    assert next(row for row in search.best['routes'] if row['vehicle_id'] == 'V1')['task_ids'] == ['4']
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('scope', ['common', 'compatible'])
def test_all_overcapacity_group_is_filtered_before_the_group_limit(scope):
    body, road = problem(n=2, cars=1, common='100', capacity=2)
    body['tasks'][0]['pieces'] = 100
    body['quotes']['R'] = {'mode': 'KM', 'per_km': '2'}
    body['tasks'][1]['quote_id'] = 'R'
    search, _ = prepared(body, road)
    details = seed_long_routes(search, scope=scope, max_groups=1, max_seeds=4)
    assert details['groups_without_available_car'] == 1
    assert search.best['total_fee'] == '10020.00'
    assert search.best['routes'][0]['task_ids'] == ['1']
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('scope', ['common', 'compatible'])
def test_oversized_high_value_tickets_cannot_hide_the_only_fitting_seed(scope):
    body, road = problem(n=6, cars=1, common='100', capacity=2)
    for task in body['tasks'][:-1]:
        task['pieces'] = 100
    search, initial = prepared(body, road)
    seed_long_routes(search, scope=scope, max_seeds=4)
    assert initial['total_fee'] == '50100.00'
    assert search.best['total_fee'] == '50010.00'
    assert search.best['routes'][0]['task_ids'] == ['5']
    assert check_result(search.data, search.best)['valid']


def test_complete_quote_and_warehouse_boundaries_remain_candidate_constraints():
    body, road = problem(n=4, cars=3, common='100', capacity=4)
    body['quotes']['R'] = {'mode': 'KM', 'per_km': '2'}
    body['warehouses'].append({**body['warehouses'][0], 'id': 'OTHER'})
    body['vehicles'][2]['warehouse_id'] = 'OTHER'
    body['tasks'][2]['quote_id'] = 'R'
    body['tasks'][3].update(quote_id='R', warehouse_id='OTHER')
    for task in body['tasks']:
        road.matrix['OTHER', task['id']] = {'meters': 1000, 'seconds': 60}
    search, _ = prepared(body, road)
    seed_long_routes(search, max_groups=4, max_seeds=12)
    for row in search.pool.values():
        selected = [search.tasks[item] for item in row['task_ids']]
        assert len({task['signature'] for task in selected}) == 1
        assert all(task['warehouse_id'] == search.data['vehicles'][row['vehicle_id']]['warehouse_id'] for task in selected)
    assert check_result(search.data, search.best)['valid']


def test_compatible_columns_enable_joint_two_to_three_car_repartition(monkeypatch):
    from app.route_columns import select_portfolio
    body, road = problem(n=6, cars=3, common='20', capacity=3)
    for n, task in enumerate(body['tasks']):
        task.update(province='示例省', city='示例城' + str(n // 2))
    for (origin, target), metric in road.matrix.items():
        metric['meters'] = (10000 if 'W' in (origin, target) else 1000 if int(origin) // 2 == int(target) // 2 else 15000)
    search, initial = prepared(body, road, [('V0', ['0', '2', '4']), ('V1', ['1', '3', '5'])])
    assert initial['total_fee'] == '80.00'
    observed = []
    road.candidate_observer = observed.append
    monkeypatch.setattr(search, 'pack', lambda: (_ for _ in ()).throw(AssertionError('No native pack inside generator')))
    details = seed_long_routes(search, scope='compatible', max_seeds=12)
    # Any one new 2-store line costs >=11 plus >=80 common for the other four;
    # any new 3-store line spanning cities costs >=26 plus >=60 common. Neither
    # individually beats 80 when it overlaps both old lines. Joint selection
    # can instead choose all three 11-unit same-city pairs for exactly 33.
    assert details.get('complete_actual_improvements', 0) == 0 and search.best['total_fee'] == '80.00'
    assert observed and details['native_selection_runs'] == 0
    selected = select_portfolio(body, road, [initial], candidate_recipes=observed, seconds=3)
    assert selected['total_fee'] == '33.00' and len(selected['routes']) == 3
    assert len({row['vehicle_id'] for row in selected['routes']}) == 3
    assert check_result(normalize(body), selected)['valid']


@pytest.mark.parametrize('limit', [None, 100, 2])
def test_driving_reads_are_capped_without_raising_an_existing_lower_limit(limit):
    body, _ = problem(n=3, cars=2, common='100', capacity=3)
    road = GuardedMeasuredRoad()
    road.calls, road.call_limit = 5, limit
    search, _ = prepared(body, road)
    road.require_read_only = True
    details = seed_long_routes(search, max_seeds=4)
    assert road.call_limit == limit and road.calls == 5
    assert road.seen_limits and all(value == (5 if limit is None else min(5, limit)) for value in road.seen_limits)
    assert details['new_network_calls'] == 0


@pytest.mark.parametrize('reason', ['CANCELLED', 'PHASE_BUDGET', 'TIME_BUDGET', 'WALL_BUDGET'])
def test_cancel_and_phase_limits_propagate_and_restore_the_driving_limit(reason):
    body, _ = problem(n=3, cars=2, common='100', capacity=3)
    road = GuardedMeasuredRoad()
    road.calls, road.call_limit = 5, 100
    search, initial = prepared(body, road)
    def stop():
        assert road.call_limit == 5
        raise StopSearch(reason)
    search.control = stop
    with pytest.raises(StopSearch) as exc:
        seed_long_routes(search)
    assert exc.value.reason == reason
    assert road.call_limit == 100 and search.best == initial


def test_cancel_after_a_real_improvement_keeps_the_complete_cheaper_incumbent():
    body, _ = problem(n=3, cars=2, common='100', capacity=3)
    road = GuardedMeasuredRoad()
    road.calls, road.call_limit = 5, 100
    search, initial = prepared(body, road)
    state = {'cancelled': False}
    original = search.consider
    def consider(rows, **kwargs):
        result = original(rows, **kwargs)
        if result:
            state['cancelled'] = True
        return result
    search.consider = consider
    search.cancelled = lambda: state['cancelled']
    with pytest.raises(StopSearch) as exc:
        seed_long_routes(search)
    assert exc.value.reason == 'CANCELLED' and road.call_limit == 100
    assert Decimal(search.best['total_fee']) < Decimal(initial['total_fee'])
    assert check_result(search.data, search.best)['valid']


def test_observer_exception_restores_the_driving_limit_without_publishing_a_partial_plan():
    body, _ = problem(n=3, cars=2, common='100', capacity=3)
    road = GuardedMeasuredRoad()
    road.calls, road.call_limit = 5, 100
    search, initial = prepared(body, road)
    def fail(spec):
        raise RuntimeError('Observer failed')
    road.candidate_observer = fail
    with pytest.raises(RuntimeError, match='Observer failed'):
        seed_long_routes(search)
    assert road.call_limit == 100 and search.best == initial


def test_own_candidate_limit_keeps_a_valid_incumbent_without_native_selection():
    body, road = problem(n=4, cars=2, common='100', capacity=4)
    search, _ = prepared(body, road)
    details = seed_long_routes(search, max_evaluations=1)
    assert details['candidate_count'] == 1 and details['stop_reason'] == 'CURRENT_ORDER_LIMIT'
    assert details['native_selection_runs'] == 0 and check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('options', [{'scope': 'unknown'}, {'max_stops': 41}, {'max_evaluations': 0}, {'max_seeds': True}])
def test_invalid_macro_options_are_rejected(options):
    body, road = problem()
    search, _ = prepared(body, road)
    with pytest.raises(ValueError):
        seed_long_routes(search, **options)
