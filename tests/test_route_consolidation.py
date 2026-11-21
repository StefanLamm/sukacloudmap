"""Fresh-batch cost regressions: small reorder and whole-route consolidation.

The transport double uses synthetic directed measurements. No real locations,
provider requests, business database or private workbook is needed.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import time

import pytest

from app.route_verification import CollectedDrivingRoad, complete, validate_driving
from app.routing_engine import StopSearch, assemble, evaluate_route, normalize
from test_route_deep import BatchDriving, stub_rounds
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
        progress=kwargs.pop('progress', lambda value: None), selection_seconds=.2, **kwargs)


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


def test_cancellation_before_optional_work_keeps_the_fresh_complete_initial_plan():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    state = {'cancelled': False}

    def cancel_optional():
        state['cancelled'] = True
        raise StopSearch('CANCELLED')

    result, final = validate(body, road, value, repair_control=cancel_optional,
                             cancelled=lambda: state['cancelled'])
    assert complete(normalize(body), result) and result['total_fee'] == '26.00'
    assert len(result['routes']) == 2 and final['fresh']
    assert final['optional_stop_reason'] == 'CANCELLED' and final['verified_checkpoints'] == 1
    assert final['selected_segments'] == final['actual_calls'] == road.calls == 5
    assert not road.force_refresh and road.allow_historical


def test_default_deep_cancellation_keeps_the_fresh_merge_and_passes_publication_proof(settings, monkeypatch):
    import httpx
    from pydantic import SecretStr
    from app.plan_service import _verify_current_job_roads, verify_deep_result
    from app.road_service import DrivingRoad
    from app.route_columns import select_portfolio
    from app.route_deep import optimize_deep
    from test_deep_road_call_budget import NoWait
    from test_driving_reference import response

    body, historical_road = five_stops()
    body['source'] = 'DRIVING'
    anchor = plan(body, historical_road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    warehouse = body['warehouses'][0]
    warehouse_point = f"{warehouse['longitude']:.6f},{warehouse['latitude']:.6f}"
    settings.amap_web_service_key = SecretStr('TEST_ONLY_FRESH_CANCEL')
    settings.planning_route_mode = 'DRIVING'
    settings.driving_live_calls_enabled = True
    requests, cancelled_at = [], {}

    def provider(request):
        assert not cancelled_at, 'fresh validation contacted the provider after cancellation'
        origin, destination = (request.url.params[name] for name in ('origin', 'destination'))
        requests.append((origin, destination))
        payload = response()
        path = payload['route']['paths'][0]
        path.update(distance='10000' if origin == warehouse_point else '2000', cost={'duration': '10'},
                    steps=[{'polyline': f'{origin};{destination}'}])
        return httpx.Response(200, json=payload)

    def cancelled():
        return bool(cancelled_at)

    road = DrivingRoad(settings, production_planning=True, fresh_only=True, cancelled=cancelled,
                       limiter=NoWait(), transport=httpx.MockTransport(provider))

    def progress(update):
        if update.get('consolidation_searches') == 1 and not cancelled_at:
            cancelled_at.update(fee=update['best_fee'], calls=road.calls)

    def select_before_validation(body, adapter, results, **kwargs):
        if adapter is road:
            return deepcopy(anchor)
        assert not cancelled(), 'read-only selection restarted after cancellation'
        return select_portfolio(body, adapter, results, **kwargs)

    stub_rounds(monkeypatch, [anchor])
    monkeypatch.setattr('app.route_edge_discovery.collect_promising_edges', lambda *args, **kwargs: {})
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
                        lambda data, adapter, incumbent, *args: (incumbent, {'elapsed_seconds': 0}, []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
                        lambda data, adapter, incumbent, *args: (incumbent, {'elapsed_seconds': 0}))
    monkeypatch.setattr('app.route_columns.select_portfolio', select_before_validation)

    result = optimize_deep(body, road, 20, progress=progress, cancelled=cancelled)
    _verify_current_job_roads(result, road)
    verify_deep_result(body, result, road, was_cancelled=True)
    final = result['algorithm']['final_validation']
    assert anchor['total_fee'] == '26.00' and cancelled_at['fee'] == result['total_fee'] == '18.00'
    assert result['algorithm']['stop_reason'] == final['optional_stop_reason'] == 'CANCELLED'
    assert len(result['routes']) == 1 and len(result['routes'][0]['task_ids']) == 5 and not result['common']
    assert complete(normalize(body), result) and result['execution_scope'] == 'REFERENCE_ONLY'
    assert final['mode'] == 'FRESH_DRIVING_VALIDATED' and final['fresh'] and final['selected_subset_of_collected']
    assert final['verified_checkpoints'] == 2 and final['consolidations'][0]['after_fee'] == '18.00'
    assert final['selected_segments'] == final['selected_provider_count'] == 5
    assert final['total_job_calls'] == road.calls == len(requests) == cancelled_at['calls']


def test_cancellation_during_the_next_receipt_proof_keeps_only_the_previous_checkpoint(monkeypatch):
    from app.road_service import DrivingRoad

    body, road = five_stops()
    anchor = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    base_verify = CollectedDrivingRoad.verify
    base_reference = DrivingRoad.valid_reference
    checking_merge = False
    receipts_checked = []

    def verify(collector, data, result, finished_at, control=None):
        nonlocal checking_merge
        checking_merge = result['total_fee'] == '18.00'
        try:
            return base_verify(collector, data, result, finished_at, control=control)
        finally:
            checking_merge = False

    def reference(receipt, **options):
        valid = base_reference(receipt, **options)
        if checking_merge:
            assert valid
            receipts_checked.append(road.calls)
        return valid

    monkeypatch.setattr(CollectedDrivingRoad, 'verify', verify)
    monkeypatch.setattr(DrivingRoad, 'valid_reference', staticmethod(reference))
    result, final = validate(body, road, anchor, cancelled=lambda: bool(receipts_checked))
    assert receipts_checked == [road.calls] and road.calls > final['selected_segments']
    assert complete(normalize(body), result) and result['total_fee'] == '26.00'
    assert final['fresh'] and final['verified_checkpoints'] == 1 and final['optional_stop_reason'] == 'CANCELLED'
    assert final['total_job_calls'] == road.calls and final['selected_provider_count'] == 5


def test_absolute_wall_after_a_complete_checkpoint_still_stops_validation():
    body, road = five_stops()
    anchor = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    reached = {}

    def progress(update):
        if update.get('consolidation_searches') == 1:
            reached.update(fee=update['best_fee'], calls=road.calls)

    def control():
        if reached:
            raise StopSearch('WALL_BUDGET')

    with pytest.raises(StopSearch) as stopped:
        validate(body, road, anchor, progress=progress, control=control)
    assert stopped.value.reason == 'WALL_BUDGET' and reached['fee'] == '18.00'
    assert road.calls == reached['calls']


def test_cancellation_before_all_mandatory_routes_are_fresh_keeps_no_partial_checkpoint():
    body, road = five_stops()
    for task in body['tasks']:
        task['common_unit_price'] = None
    anchor = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    with pytest.raises(StopSearch) as stopped:
        validate(body, road, anchor, cancelled=lambda: road.calls > 0)
    assert stopped.value.reason == 'CANCELLED' and road.calls == 1


def test_failed_direction_is_not_retried_by_other_permutations():
    body, road = five_stops()
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3', '4'])])
    road.fail = lambda a, b: a['id'] == '1' and b['id'] == '2'
    result, final = validate(body, road, value)
    assert complete(normalize(body), result)
    assert final['failed_provider_directions'] == 1
    assert all(n == 1 for n in road.counts.values())
