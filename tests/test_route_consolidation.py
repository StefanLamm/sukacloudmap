"""Fresh-batch cost regressions: small reorder and whole-route consolidation.

The transport double uses synthetic directed measurements. No real locations,
provider requests, business database or private workbook is needed.
"""
from datetime import datetime, timedelta
import time

import pytest

from app.route_verification import complete, validate_driving
from app.routing_engine import StopSearch, assemble, evaluate_route, normalize
from test_route_deep import BatchDriving
from test_route_neighborhood import problem


def five_stops(*, deadlines=False, capacity=100, cars=3):
    body, _ = problem(n=5, cars=cars, capacity=capacity, common='100')
    for n, point in enumerate(body['warehouses'] + body['tasks']):
        point.update(longitude=117 + n / 100, latitude=31 + n / 100, crs='GCJ02')
    if deadlines:
        # Only 0,2,4,1,3 meets these fresh ten-second arrivals. It cannot be
        # obtained from 0,1,2,3,4 by reversing one contiguous interval.
        for task, seconds in zip(body['tasks'], [10, 40, 20, 50, 30]):
            task['latest_arrival_at'] = (datetime.fromisoformat('2026-11-13T06:00:00+08:00') +
                                         timedelta(seconds=seconds)).isoformat()
    return body, BatchDriving(body, old_seconds=1, new_seconds=10)


def plan(body, road, routes):
    data = normalize(body)
    materialized = [evaluate_route(data, vid, ids, road) for vid, ids in routes]
    covered = {i for r in materialized for i in r['task_ids']}
    return assemble(data, materialized, set(data['tasks']) - covered, [])


def validate(body, road, value, **kwargs):
    return validate_driving(body, road, value, [], cancelled=kwargs.pop('cancelled', lambda: False),
        control=kwargs.pop('control', lambda: None),
        repair_control=kwargs.pop('repair_control', lambda: None),
        progress=lambda value: None, selection_seconds=.2, **kwargs)


@pytest.mark.parametrize('cars', [1, 3])
def test_five_stop_fresh_repair_finds_order_outside_single_interval_reversal(cars):
    body, road = five_stops(deadlines=True, cars=cars)
    ids = [str(i) for i in range(5)]
    target = ['0', '2', '4', '1', '3']
    assert target not in [ids[:a] + list(reversed(ids[a:b + 1])) + ids[b + 1:]
                          for a in range(5) for b in range(a + 1, 5)]
    value = plan(body, road, [('V0', ids)])
    result, final = validate(body, road, value)
    assert complete(normalize(body), result)
    assert result['routes'][0]['task_ids'] == target
    assert len(result['routes']) == 1 and result['total_fee'] == '18.00'
    assert final['repairs'][0]['method'] == 'complete_small_reorder'
    assert final['repairs'][0]['small_reorder']['orders_examined'] == 120
    assert all(n == 1 for n in road.counts.values())


@pytest.mark.parametrize('old_lookup', ['missing', 'too_slow'])
def test_missing_or_stale_historical_metrics_do_not_reject_a_fresh_order(old_lookup):
    body, road = five_stops(deadlines=True)
    value = plan(body, road, [('V0', [str(i) for i in range(5)])])
    original = road.known_segment
    def lookup(a, b):
        row = original(a, b)
        if row and row['received_at'] < time.time() - 100:
            return None if old_lookup == 'missing' else {**row, 'seconds': 1000}
        return row
    road.known_segment = lookup
    result, final = validate(body, road, value)
    assert complete(normalize(body), result) and result['total_fee'] == '18.00'
    assert len(result['routes']) == 1 and not result['common']
    assert final['selected_subset_of_collected']


def test_two_feasible_routes_merge_only_after_full_interleaved_reordering():
    body, road = five_stops(deadlines=True)
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    result, final = validate(body, road, value)
    assert value['total_fee'] == '26.00' and result['total_fee'] == '18.00'
    assert len(result['routes']) == 1 and not result['common']
    assert result['routes'][0]['task_ids'] == ['0', '2', '4', '1', '3']
    assert not final['repairs'] and final['consolidations'][0]['improved']
    assert final['actual_calls'] <= 25 and all(n == 1 for n in road.counts.values())


def test_fewer_cars_are_rejected_when_the_total_delivery_cost_increases():
    body, road = five_stops()
    road.new_meters = lambda a, b: 10000 if a['id'] == 'W' else 100000
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    result, final = validate(body, road, value)
    assert len(result['routes']) == 2 and result['total_fee'] == '320.00'
    assert not final['consolidations'][0]['improved']


@pytest.mark.parametrize('incompatibility', ['capacity', 'warehouse', 'quote'])
def test_consolidation_preserves_capacity_warehouse_and_quote_boundaries(incompatibility):
    body, road = five_stops(capacity=3 if incompatibility == 'capacity' else 100)
    if incompatibility == 'warehouse':
        body['warehouses'].append({**body['warehouses'][0], 'id': 'X', 'longitude': 118})
        body['vehicles'][1]['warehouse_id'] = 'X'
        for row in body['tasks'][2:]:
            row['warehouse_id'] = 'X'
    elif incompatibility == 'quote':
        body['quotes']['Q2'] = {'mode': 'KM', 'per_km': '2'}
        for row in body['tasks'][2:]:
            row['quote_id'] = 'Q2'
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    result, final = validate(body, road, value)
    assert complete(normalize(body), result) and len(result['routes']) == 2
    assert final['consolidations'] == []
    assert len({r['vehicle_id'] for r in result['routes']}) == 2


def test_an_occupied_larger_vehicle_cannot_be_reused_for_a_merge():
    body, road = five_stops(capacity=2, cars=3)
    body['vehicles'][2]['max_pieces'] = 10
    # A distinct quote prevents merging the third route into either pair.
    body['quotes']['Q2'] = {'mode': 'KM', 'per_km': '2'}
    body['tasks'][4]['quote_id'] = 'Q2'
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3']), ('V2', ['4'])])
    result, final = validate(body, road, value)
    assert complete(normalize(body), result) and len(result['routes']) == 3
    assert len({r['vehicle_id'] for r in result['routes']}) == 3
    assert final['consolidations'] == []


def test_bridge_limit_keeps_the_existing_complete_plan_and_never_exceeds_limit():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    result, final = validate(body, road, value, max_bridge_edges=0)
    assert complete(normalize(body), result)
    assert result['total_fee'] == '26.00' and len(result['routes']) == 2
    assert final['new_bridge_edges'] == 0 and final['actual_calls'] == 5


def test_optional_timeout_retains_a_fresh_complete_plan():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    def expired():
        raise StopSearch('TIME_BUDGET')
    result, final = validate(body, road, value, repair_control=expired)
    assert complete(normalize(body), result) and result['total_fee'] == '26.00'
    assert final['actual_calls'] == 5 and not final['consolidations']


def test_timeout_after_a_verified_merge_keeps_that_improvement():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    calls = [0]
    def bounded():
        calls[0] += 1
        if calls[0] >= 12:
            raise StopSearch('TIME_BUDGET')
    result, final = validate(body, road, value, repair_control=bounded)
    assert complete(normalize(body), result) and result['total_fee'] == '18.00'
    assert len(result['routes']) == 1
    assert final['consolidations'][0]['stop_reason'] == 'TIME_BUDGET'
    assert final['selected_subset_of_collected']


@pytest.mark.parametrize('optional_check_limit', [None, 500])
def test_all_mandatory_repairs_finish_before_optional_full_order_search(optional_check_limit):
    body, _ = problem(n=13, cars=2, capacity=20, common=None)
    body['warehouses'].append({**body['warehouses'][0], 'id': 'X'})
    body['vehicles'][1]['warehouse_id'] = 'X'
    start = datetime.fromisoformat('2026-11-13T06:00:00+08:00')
    for n, task in enumerate(body['tasks']):
        position, width = (n, 8) if n < 8 else (n - 8, 5)
        task['warehouse_id'] = 'W' if n < 8 else 'X'
        task['latest_arrival_at'] = (start + timedelta(seconds=(width - position) * 10)).isoformat()
    for n, point in enumerate(body['warehouses'] + body['tasks']):
        point.update(longitude=117 + n / 100, latitude=31 + n / 100, crs='GCJ02')
    road = BatchDriving(body, old_seconds=1, new_seconds=10)
    value = plan(body, road, [('V0', [str(i) for i in range(8)]),
                             ('V1', [str(i) for i in range(8, 13)])])
    checks = [0]
    def limited():
        checks[0] += 1
        if optional_check_limit is not None and checks[0] > optional_check_limit:
            raise StopSearch('TIME_BUDGET')
    result, final = validate(body, road, value, max_bridge_edges=64, repair_control=limited)
    assert complete(normalize(body), result) and not result['common']
    assert len(result['routes']) == 2 and result['total_fee'] == '34.00'
    assert [r['method'] for r in final['repairs']] == ['reorder_repair'] * 2
    assert final['repairs'][0]['small_reorder']['new_bridge_edges'] <= 32
    assert final['new_bridge_edges'] <= 64


def test_five_stop_metric_regression_saves_2149_cents_without_changing_prices():
    # Anonymous directed integer metrics only; positions below are synthetic.
    # Row 0 is the warehouse, rows 1..5 are stores; diagonal entries are unused.
    meters = [[15504, 15924, 20623, 22051, 20365],
              [0, 12218, 13647, 16839, 25859],
              [12614, 0, 6023, 5595, 11113],
              [12917, 6320, 0, 6442, 15498],
              [16666, 6152, 6669, 0, 6209],
              [24120, 11347, 15614, 6653, 0]]
    seconds = [[1867, 2108, 2270, 2735, 2448],
               [0, 2288, 2530, 3094, 3190],
               [2280, 0, 1237, 1746, 1964],
               [2831, 1415, 0, 1538, 1694],
               [3988, 2347, 1727, 0, 1415],
               [3999, 2133, 1693, 1429, 0]]
    body, road = five_stops(capacity=400)
    body['quotes']['Q']['per_km'] = '5.8'
    body['warehouses'][0]['earliest_departure_at'] = '2026-11-13T09:00:00+08:00'
    for task, pieces in zip(body['tasks'], [17, 16, 13, 23, 48]):
        task.update(pieces=pieces, open_at='2026-11-13T10:00:00+08:00',
                    close_at='2026-11-13T22:00:00+08:00', unload_minutes=20)
    def metric(a, b):
        row, column = (0 if a['id'] == 'W' else int(a['id']) + 1), int(b['id'])
        return {'meters': meters[row][column], 'seconds': seconds[row][column]}
    from app.road_service import evidence, road_request
    class Matrix:
        def segment(self, vehicle, a, b, at):
            return evidence(road_request(vehicle, a, b, at, 'FIXTURE'), **metric(a, b), source='FIXTURE')
    value = plan(body, Matrix(), [('V0', ['1', '2', '3', '4']), ('V1', ['0'])])
    road.new_meters = lambda a, b: metric(a, b)['meters']
    road.new_seconds = lambda a, b: metric(a, b)['seconds']
    result, final = validate(body, road, value)
    assert value['total_fee'] == '290.59' and result['total_fee'] == '269.10'
    assert len(result['routes']) == 1 and not result['common']
    route = result['routes'][0]
    assert route['task_ids'] == ['0', '1', '2', '3', '4']
    assert route['pieces'] == 117 and route['meters'] == 46396
    assert route['arrival_end_at'] == '2026-11-13T13:27:58+08:00'
    assert final['consolidations'][0]['orders_examined'] == 120
    assert all(count == 1 for count in road.counts.values())


def test_local_metric_cache_cannot_validate_a_tampered_receipt():
    from app.route_verification import DrivingCollector, _FreshMetricView
    from app.api import ApiError
    body, road = five_stops()
    data = normalize(body)
    collector = DrivingCollector(road, lambda: False, lambda: None)
    try:
        route = evaluate_route(data, 'V0', ['0'], collector)
        value = assemble(data, [route], ['1', '2', '3', '4'], [])
        view = _FreshMetricView(collector.frozen)
        origin, destination = data['warehouses']['W'], data['tasks']['0']
        assert view.known_segment(origin, destination)['meters'] == 10000
        key = collector.frozen.key(origin, destination)
        collector.receipts[key]['meters'] += 1
        assert view.known_segment(origin, destination)['meters'] == 10000
        assert evaluate_route(data, 'V0', ['0'], collector.frozen)['errors']
        with pytest.raises(ApiError):
            collector.frozen.verify(data, value, int(time.time()))
    finally:
        collector.close()


def test_cancellation_propagates_without_promoting_a_partial_merge():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    state = {'cancelled': False}
    def cancel_optional():
        state['cancelled'] = True
        raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch) as exc:
        validate(body, road, value, repair_control=cancel_optional,
                 cancelled=lambda: state['cancelled'])
    assert exc.value.reason == 'CANCELLED'
    assert not road.force_refresh and road.allow_historical


def test_failed_direction_is_not_retried_by_other_permutations():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    road.fail = lambda a, b: a['id'] == '1' and b['id'] == '2'
    result, final = validate(body, road, value)
    assert complete(normalize(body), result)
    assert final['failed_provider_directions'] == 1
    assert all(n == 1 for n in road.counts.values())
