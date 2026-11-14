"""Complete-cost cleanup, including optional outsourcing and nonlinear quotes."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.road_service import DrivingRoad, evidence, road_request
from app.route_cost_cleanup import improve_cost_cleanup, polish_route_orders
from app.route_search import JointSearch
from app.routing_engine import RoadError, StopSearch, assemble, check_result, evaluate_route, normalize


class MeasuredRoad:
    def __init__(self, *, ordinary=1000, departure=1000):
        self.ordinary, self.departure = ordinary, departure
        self.metrics, self.missing = {}, set()
        self.calls = 0
        self.call_limit = 100
        self.materialized_segments = 0
        self.require_read_only = False
        self.seen_limits = []

    def preflight(self, *args):
        pass

    def known_segment(self, origin, destination):
        pair = origin['id'], destination['id']
        if pair in self.missing:
            return None
        return {'meters': self.metrics.get(pair, self.departure if pair[0] == 'W' else self.ordinary),
                'seconds': 60}

    def segment(self, vehicle, origin, destination, departure):
        if self.require_read_only:
            self.seen_limits.append(self.call_limit)
            assert self.call_limit <= self.calls
        metric = self.known_segment(origin, destination)
        if metric is None:
            raise RoadError('ROAD_EDGE_NOT_FROZEN', 'Missing measured direction')
        self.materialized_segments += 1
        return evidence(road_request(vehicle, origin, destination, departure, 'FIXTURE'),
                        **metric, source='FIXTURE')

    def stats(self):
        return {'calls': self.calls}


class GuardedMeasuredRoad(MeasuredRoad, DrivingRoad):
    """Use the production DrivingRoad quota guard with a binding fixture adapter."""


def problem(*, cars=1, count=9, capacity=9, common=None):
    return {
        'source': 'FIXTURE', 'delivery_date': '2026-11-13',
        'warehouses': [{'id': 'W', 'longitude': 117., 'latitude': 31.,
                        'earliest_departure_at': '2026-11-13T06:00:00+08:00'}],
        'vehicles': [{'id': 'V' + str(i), 'warehouse_id': 'W', 'available': True,
                      'max_pieces': capacity} for i in range(cars)],
        'quotes': {'Q': {'mode': 'KM', 'per_km': '1'}},
        'tasks': [{'id': str(i), 'store_id': str(i), 'warehouse_id': 'W',
                   'pieces': 1, 'quote_id': 'Q', 'common_unit_price': common,
                   'longitude': 117. + (i + 1) / 100, 'latitude': 31. + (i + 1) / 100,
                   'open_at': '2026-11-13T06:00:00+08:00',
                   'close_at': '2026-11-13T23:00:00+08:00',
                   'latest_arrival_at': None, 'unload_minutes': 0} for i in range(count)]}


def prepared(body, road, *, routes=None, seed=0):
    data = normalize(body)
    if routes is None:
        routes = [('V0', [str(i) for i in range(len(body['tasks']))])]
    initial = assemble(data, [evaluate_route(data, vid, ids, road) for vid, ids in routes], [], [])
    assert check_result(data, initial)['valid']
    search = JointSearch(data, road, 20, seed, lambda: False, initial)
    search.setup_fleet()
    # These cases explicitly polish a valid current/manual allocation. Normal
    # adopt_initial correctly rejects an all-optional plan dearer than common;
    # establish the intended incumbent through the host's full proof path.
    assert search.consider([search.compact(row) for row in initial['routes']], force=True)
    return search, initial


def outlier_problem():
    body, road = problem(), MeasuredRoad()
    body['tasks'][4]['common_unit_price'] = '2'
    road.metrics.update({('3', '4'): 10000, ('4', '5'): 10000})
    return body, road


def test_profitable_prefix_can_follow_a_whole_route_common_improvement():
    body, road = problem(common='2'), MeasuredRoad(ordinary=5000)
    search, initial = prepared(body, road)
    original = deepcopy(initial)
    assert initial['total_fee'] == '41.00'
    diagnostics = improve_cost_cleanup(search)
    # Outsourcing all nine costs 18; keeping the cheap first stop costs 1+8*2.
    assert search.best['total_fee'] == '17.00'
    assert search.best['routes'][0]['task_ids'] == ['0']
    assert len(search.best['common']) == 8
    assert diagnostics['accepted_by_kind']['whole_to_common'] == 1
    assert diagnostics['accepted_by_kind']['prefix'] == 1
    assert check_result(search.data, search.best)['valid']
    assert initial == original


def test_internal_optional_outlier_returns_to_common_at_lower_whole_cost():
    body, road = outlier_problem()
    search, initial = prepared(body, road)
    assert initial['total_fee'] == '27.00'
    diagnostics = improve_cost_cleanup(search)
    assert search.best['total_fee'] == '10.00'
    assert [row['task_id'] for row in search.best['common']] == ['4']
    assert search.best['routes'][0]['task_ids'] == ['0', '1', '2', '3', '5', '6', '7', '8']
    assert diagnostics['accepted_by_kind']['single_delete'] == 1
    assert diagnostics['before_fee'] == '27.00' and diagnostics['after_fee'] == '10.00'
    assert diagnostics['new_network_calls'] == 0
    assert check_result(search.data, search.best)['valid']


def test_unknown_shortcut_keeps_complete_route_without_attempting_materialization():
    body, road = outlier_problem()
    road.missing.add(('3', '5'))
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    segment_count = road.materialized_segments
    diagnostics = improve_cost_cleanup(search)
    assert search.best == previous
    assert road.materialized_segments == segment_count
    assert diagnostics['missing_metric_candidates'] == 1
    assert diagnostics['accepted'] == 0 and diagnostics['new_network_calls'] == 0


def test_mandatory_ticket_never_becomes_an_outsourced_or_missing_ticket():
    body, road = outlier_problem()
    body['tasks'][4]['common_unit_price'] = None
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    diagnostics = improve_cost_cleanup(search)
    assert search.best == previous and not search.best['common']
    assert diagnostics['candidate_count'] == 0
    assert check_result(search.data, search.best)['valid']


def test_two_optional_stops_can_jointly_remove_a_nonlinear_region_charge():
    body, road = problem(), MeasuredRoad()
    body['quotes']['Q'] = {'mode': 'KM_REGION', 'per_km': '1', 'region_surcharge': {
        'amount': '100', 'billing_mode': 'PER_REGION', 'targets': [{
            'province_code': '320000', 'province_name': '江苏省',
            'city_code': '320800', 'city_name': '淮安市',
            'district_code': '320803', 'district_name': '淮安区'}]}}
    for row in body['tasks']:
        row.update(province='江苏省', city='南京市', district='测试区', region_code='320102')
    for index in (3, 5):
        body['tasks'][index].update(common_unit_price='5', city='淮安市', district='淮安区', region_code='320803')
    search, initial = prepared(body, road)
    assert initial['total_fee'] == '109.00'
    diagnostics = improve_cost_cleanup(search)
    # Either single removal still pays the same region charge: 8+100+5=113.
    # Removing both is 7 km plus 10 common fees, with the original quote intact.
    assert search.best['total_fee'] == '17.00'
    assert {row['task_id'] for row in search.best['common']} == {'3', '5'}
    assert diagnostics['accepted_by_kind'] == {'double_delete': 1}
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('cross_meters, expected_fee, expected_cars', [(1000, '9.00', 1), (100000, '18.00', 2)])
def test_pair_merge_uses_full_price_and_rejects_a_dearer_single_car(cross_meters, expected_fee, expected_cars):
    body, road = problem(cars=2), MeasuredRoad()
    road.metrics.update({('W', '4'): 10000, ('3', '4'): cross_meters, ('8', '0'): 100000})
    search, initial = prepared(body, road, routes=[('V0', ['0', '1', '2', '3']),
                                                 ('V1', ['4', '5', '6', '7', '8'])])
    assert initial['total_fee'] == '18.00'
    diagnostics = improve_cost_cleanup(search)
    assert search.best['total_fee'] == expected_fee
    assert len(search.best['routes']) == expected_cars
    assert len({row['vehicle_id'] for row in search.best['routes']}) == expected_cars
    assert diagnostics['new_network_calls'] == 0
    assert check_result(search.data, search.best)['valid']


def test_pair_merge_cannot_steal_an_occupied_larger_physical_vehicle():
    body, road = problem(cars=3), MeasuredRoad()
    body['vehicles'][0]['max_pieces'] = body['vehicles'][1]['max_pieces'] = 4
    body['vehicles'][2]['max_pieces'] = 10
    body['quotes']['OTHER'] = {'mode': 'KM', 'per_km': '2'}
    body['tasks'][8]['quote_id'] = 'OTHER'
    search, _ = prepared(body, road, routes=[('V0', ['0', '1', '2', '3']),
                                           ('V1', ['4', '5', '6', '7']), ('V2', ['8'])])
    previous = deepcopy(search.best)
    diagnostics = improve_cost_cleanup(search)
    assert search.best == previous
    assert diagnostics['route_pairs_without_capacity'] == 1
    assert diagnostics['accepted'] == 0
    assert check_result(search.data, search.best)['valid']


def test_candidate_limit_retains_the_last_checked_whole_allocation():
    body, road = problem(common='2'), MeasuredRoad(ordinary=5000)
    search, _ = prepared(body, road)
    diagnostics = improve_cost_cleanup(search, max_candidates=1)
    assert diagnostics['stop_reason'] == 'CANDIDATE_LIMIT'
    assert diagnostics['candidate_count'] == 1
    assert search.best['total_fee'] == '18.00'
    assert not search.best['routes'] and len(search.best['common']) == 9
    assert check_result(search.data, search.best)['valid']


def test_budget_expiry_after_an_improvement_retains_complete_evidence():
    body, road = problem(common='2'), MeasuredRoad(ordinary=5000)
    search, _ = prepared(body, road)
    original_consider, original_control = search.consider, search.control
    expired = False

    def consider(*args, **kwargs):
        nonlocal expired
        accepted = original_consider(*args, **kwargs)
        expired = expired or accepted
        return accepted

    def control():
        original_control()
        if expired:
            raise StopSearch('TIME_BUDGET')

    search.consider, search.control = consider, control
    diagnostics = improve_cost_cleanup(search)
    assert diagnostics['stop_reason'] == 'TIME_BUDGET'
    assert search.best['total_fee'] == '18.00'
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('seed', [0, 1, 29])
def test_cleanup_is_independent_of_the_search_random_seed(seed):
    body, road = outlier_problem()
    search, _ = prepared(body, road, seed=seed)
    diagnostics = improve_cost_cleanup(search)
    assert search.best['total_fee'] == '10.00'
    assert search.best['routes'][0]['task_ids'] == ['0', '1', '2', '3', '5', '6', '7', '8']
    assert diagnostics['seed_independent']
    assert diagnostics['candidate_count'] == 1


def test_driving_materialization_cannot_use_http_and_restores_the_original_limit():
    body, road = problem(common='2'), GuardedMeasuredRoad(ordinary=5000)
    search, _ = prepared(body, road)
    road.calls, road.call_limit, road.require_read_only = 2, 7, True
    diagnostics = improve_cost_cleanup(search)
    assert search.best['total_fee'] == '17.00'
    assert road.seen_limits and set(road.seen_limits) == {2}
    assert road.call_limit == 7 and road.calls == 2
    assert diagnostics['new_network_calls'] == 0


@pytest.mark.parametrize('reason', ['CANCELLED', 'WALL_BUDGET', 'PHASE_BUDGET'])
def test_cancellation_or_wall_expiry_propagates_and_restores_driving_limit(reason):
    body, road = problem(common='2'), GuardedMeasuredRoad(ordinary=5000)
    search, _ = prepared(body, road)
    road.calls, road.call_limit = 2, 7
    previous = deepcopy(search.best)

    def stop():
        raise StopSearch(reason)

    search.control = stop
    with pytest.raises(StopSearch) as failure:
        improve_cost_cleanup(search)
    assert failure.value.reason == reason
    assert search.best == previous
    assert road.call_limit == 7


def test_unrelated_failure_does_not_leave_the_driving_provider_limit_changed(monkeypatch):
    body, _ = outlier_problem()
    road = GuardedMeasuredRoad()
    road.metrics.update({('3', '4'): 10000, ('4', '5'): 10000})
    search, _ = prepared(body, road)
    road.calls, road.call_limit = 2, 7
    previous = deepcopy(search.best)

    def fail(*args, **kwargs):
        raise RuntimeError('Deliberate evaluator failure')

    monkeypatch.setattr('app.route_cost_cleanup.evaluate_candidate', fail)
    with pytest.raises(RuntimeError, match='Deliberate evaluator failure'):
        improve_cost_cleanup(search)
    assert search.best == previous
    assert road.call_limit == 7


def test_incomplete_incumbent_is_not_silently_promoted_to_a_complete_plan():
    body, road = problem(), MeasuredRoad()
    search = JointSearch(normalize(body), road, 20, 0, lambda: False)
    previous = deepcopy(search.best)
    diagnostics = improve_cost_cleanup(search)
    assert diagnostics['skipped'] and diagnostics['stop_reason'] == 'INCOMPLETE_INCUMBENT'
    assert search.best == previous


def reorder_problem(road_type=MeasuredRoad):
    body = problem()
    road = road_type(ordinary=10000, departure=10000)
    desired = ['0', '1', '3', '2', '4', '5', '6', '7', '8']
    road.metrics.update({pair: 1000 for pair in zip(['W'] + desired[:-1], desired)})
    return body, road, desired


@pytest.mark.parametrize('seed', [0, 17])
def test_order_polish_lowers_a_nine_stop_line_without_reassigning_its_tickets(seed):
    body, road, desired = reorder_problem()
    untouched = deepcopy(body)
    search, initial = prepared(body, road, seed=seed)
    old_initial = deepcopy(initial)
    assert initial['total_fee'] == '36.00'
    diagnostics = polish_route_orders(search, ['V0'])
    # Every one of the nine directed legs is at least 1 km; this order reaches
    # the independent fixed-store-set bound of 9 km under the unchanged quote.
    assert search.best['total_fee'] == '9.00'
    assert search.best['routes'][0]['task_ids'] == desired
    assert search.best['routes'][0]['vehicle_id'] == 'V0'
    assert not search.best['common'] and not search.best['unassigned']
    assert diagnostics['accepted'] > 0 and diagnostics['after_fee'] == '9.00'
    assert diagnostics['target_store_sets_and_physical_cars_unchanged']
    assert check_result(search.data, search.best)['valid']
    assert body == untouched and initial == old_initial


def test_order_polish_preserves_the_other_line_and_common_allocation_exactly():
    body, road, desired = reorder_problem()
    template = deepcopy(body['tasks'][-1])
    for item in ('9', '10', '11'):
        body['tasks'].append({**deepcopy(template), 'id': item, 'store_id': item,
                              'common_unit_price': '7' if item == '11' else None})
    body['vehicles'].append({'id': 'V1', 'warehouse_id': 'W', 'available': True, 'max_pieces': 9})
    data = normalize(body)
    initial = assemble(data, [evaluate_route(data, 'V0', [str(i) for i in range(9)], road),
                              evaluate_route(data, 'V1', ['9', '10'], road)], ['11'], [])
    assert check_result(data, initial)['valid'] and initial['total_fee'] == '63.00'
    search = JointSearch(data, road, 20, 0, lambda: False, initial)
    search.setup_fleet()
    fixed = deepcopy(next(row for row in search.best['routes'] if row['vehicle_id'] == 'V1'))
    common = deepcopy(search.best['common'])
    diagnostics = polish_route_orders(search, ['V0', 'V0'])
    assert search.best['total_fee'] == '36.00'
    assert [row['vehicle_id'] for row in search.best['routes']] == ['V0', 'V1']
    assert search.best['routes'][0]['task_ids'] == desired
    assert search.best['routes'][1] == fixed and search.best['common'] == common
    assert diagnostics['requested_vehicle_count'] == diagnostics['eligible_vehicle_count'] == 1
    assert diagnostics['fixed_allocations_unchanged']
    assert check_result(data, search.best)['valid']


def test_cheaper_permutation_that_misses_delivery_deadlines_is_rejected():
    body, road, _ = reorder_problem()
    # With every leg lasting one minute, these deadlines force the original
    # sequence. A cheaper 2/3 swap would arrive at ticket 2 one minute too late.
    for index, row in enumerate(body['tasks']):
        row['latest_arrival_at'] = f'2026-11-13T06:{index + 1:02d}:00+08:00'
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    diagnostics = polish_route_orders(search, ['V0'])
    assert search.best == previous and search.best['total_fee'] == '36.00'
    assert diagnostics['infeasible_orders'] > 0 and diagnostics['accepted'] == 0
    assert check_result(search.data, search.best)['valid']


def test_unknown_directed_bridges_never_trigger_order_polish_road_requests():
    body, road = problem(), MeasuredRoad()
    original = [str(i) for i in range(9)]
    known = set(zip(['W'] + original[:-1], original))
    road.missing = {(origin, destination) for origin in ['W', *original] for destination in original
                    if origin != destination and (origin, destination) not in known}
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    segments = road.materialized_segments
    diagnostics = polish_route_orders(search, ['V0'])
    assert search.best == previous and road.materialized_segments == segments
    assert diagnostics['candidate_count'] > 0
    assert diagnostics['missing_metric_orders'] == diagnostics['candidate_count']
    assert diagnostics['new_network_calls'] == 0


@pytest.mark.parametrize('mode', ['BASE_STOPS', 'BASE_STOPS_REGION', 'KM', 'KM_REGION'])
def test_order_invariant_quotes_skip_all_permutation_work(mode):
    body, road, _ = reorder_problem()
    quote = ({'mode': mode, 'base_price': '100', 'included_stops': 3, 'extra_stop_price': '5'}
             if mode.startswith('BASE') else {'mode': mode, 'per_km': '0'})
    if mode.endswith('_REGION'):
        quote['region_surcharge'] = {'amount': '10', 'billing_mode': 'ONCE', 'targets': [{
            'province_code': '320000', 'province_name': '江苏省', 'city_code': '320800',
            'city_name': '淮安市', 'district_code': '320803', 'district_name': '淮安区'}]}
        for row in body['tasks']:
            row.update(province='江苏省', city='淮安市', district='淮安区', region_code='320803')
    body['quotes']['Q'] = quote
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    segments, evaluations = road.materialized_segments, search.evaluations
    diagnostics = polish_route_orders(search, ['V0'])
    assert search.best == previous
    assert road.materialized_segments == segments and search.evaluations == evaluations
    assert diagnostics['candidate_count'] == 0 and diagnostics['accepted'] == 0
    assert diagnostics['order_invariant_price_targets_skipped'] == 1
    assert diagnostics['skipped'] and diagnostics['stop_reason'] == 'NO_PRICE_SENSITIVE_TARGETS'


def test_distance_only_improvements_cannot_replace_an_equal_cent_quote():
    body, _, desired = reorder_problem()
    road = MeasuredRoad(ordinary=1001, departure=1001)
    road.metrics.update({pair: 1000 for pair in zip(['W'] + desired[:-1], desired)})
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    assert previous['total_fee'] == '9.00' and previous['routes'][0]['meters'] == 9003
    diagnostics = polish_route_orders(search, ['V0'])
    # The 9000-meter route rounds to the same 9.00 fee. This pass promises a
    # strict expenditure reduction, so the original complete result is retained.
    assert search.best == previous and diagnostics['accepted'] == 0
    assert diagnostics['nonimproving_metric_orders'] > 0


def test_order_polish_candidate_cap_keeps_the_last_complete_allocation():
    body, road, _ = reorder_problem()
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    diagnostics = polish_route_orders(search, ['V0'], max_candidates=1)
    assert diagnostics['candidate_count'] == 1 and diagnostics['stop_reason'] == 'CANDIDATE_LIMIT'
    assert search.best == previous
    assert check_result(search.data, search.best)['valid']


def test_order_polish_time_expiry_keeps_an_already_materialized_improvement():
    body, road, _ = reorder_problem()
    search, _ = prepared(body, road)
    original_consider, original_control = search.consider, search.control
    expired = False

    def consider(*args, **kwargs):
        nonlocal expired
        answer = original_consider(*args, **kwargs)
        expired = expired or answer
        return answer

    def control():
        original_control()
        if expired:
            raise StopSearch('TIME_BUDGET')

    search.consider, search.control = consider, control
    diagnostics = polish_route_orders(search, ['V0'])
    assert diagnostics['stop_reason'] == 'TIME_BUDGET' and diagnostics['accepted'] == 1
    assert Decimal(search.best['total_fee']) < Decimal('36.00')
    assert check_result(search.data, search.best)['valid']


def test_driving_order_polish_only_materializes_within_a_read_only_cap():
    body, road, desired = reorder_problem(GuardedMeasuredRoad)
    search, _ = prepared(body, road)
    road.calls, road.call_limit, road.require_read_only = 2, 7, True
    diagnostics = polish_route_orders(search, ['V0'])
    assert search.best['total_fee'] == '9.00' and search.best['routes'][0]['task_ids'] == desired
    assert road.seen_limits and set(road.seen_limits) == {2}
    assert road.call_limit == 7 and road.calls == 2 and diagnostics['new_network_calls'] == 0


@pytest.mark.parametrize('reason', ['CANCELLED', 'WALL_BUDGET', 'PHASE_BUDGET'])
def test_order_polish_propagates_cancellation_and_restores_driving_allowance(reason):
    body, road, _ = reorder_problem(GuardedMeasuredRoad)
    search, _ = prepared(body, road)
    road.calls, road.call_limit = 2, 7
    previous = deepcopy(search.best)

    def stop():
        raise StopSearch(reason)

    search.control = stop
    with pytest.raises(StopSearch) as failure:
        polish_route_orders(search, ['V0'])
    assert failure.value.reason == reason and road.call_limit == 7
    assert search.best == previous


def test_order_polish_restores_driving_allowance_on_unrelated_failure(monkeypatch):
    body, road, _ = reorder_problem(GuardedMeasuredRoad)
    search, _ = prepared(body, road)
    road.calls, road.call_limit = 2, 7
    previous = deepcopy(search.best)

    def fail(*args, **kwargs):
        raise RuntimeError('Deliberate order-evaluator failure')

    monkeypatch.setattr('app.route_cost_cleanup.evaluate_candidate', fail)
    with pytest.raises(RuntimeError, match='Deliberate order-evaluator failure'):
        polish_route_orders(search, ['V0'])
    assert road.call_limit == 7 and search.best == previous


def test_order_polish_ignores_unknown_explicit_targets_without_touching_other_lines():
    body, road, _ = reorder_problem()
    search, _ = prepared(body, road)
    previous = deepcopy(search.best)
    diagnostics = polish_route_orders(search, ['not-a-current-car'])
    assert diagnostics['unknown_target_vehicles_skipped'] == 1
    assert diagnostics['skipped'] and search.best == previous
