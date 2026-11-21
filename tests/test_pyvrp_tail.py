"""Fresh-receipt regressions for the optional PyVRP stage."""
from copy import deepcopy
import time

import pytest

from app import route_pyvrp_runtime, route_pyvrp_tail as tail
from app.road_service import DrivingRoad, TruckRoad, evidence, road_request
from app.route_verification import CollectedDrivingRoad, complete, replay
from app.routing_engine import RoadError, StopSearch, normalize


def fixture(*, provider_request=False):
    quotes = [{'mode': 'KM', 'per_km': '1'},
              {'mode': 'BASE_STOPS', 'base_price': '6', 'included_stops': 3, 'extra_stop_price': '1'}]

    def point(identifier, position):
        return {'id': identifier, 'longitude': 118. + position / 100, 'latitude': 31., 'crs': 'GCJ02'}

    opening = '2026-11-13T06:00:00+08:00'
    body = {
        'delivery_date': '2026-11-13',
        'warehouses': [{**point('W', 0), 'earliest_departure_at': opening}],
        'stores': [{**point(i, n), 'name': i, 'code': i, 'business_open_at': opening,
                    'business_close_at': '2026-11-13T20:00:00+08:00', 'dedicated_quote_version': quotes[int(n > 2)]}
                   for n, i in enumerate('ABCD', 1)],
        'tasks': [{'id': 'TICKET-' + i, 'store_id': i, 'warehouse_id': 'W', 'pieces': 1, 'unload_minutes': 0,
                   'common_unit_price': {'A': None, 'B': '100', 'C': None, 'D': '50'}[i]} for i in 'ABCD'],
        'vehicles': [{'id': f'V{n}', 'day_warehouse_id': 'W', 'day_available': True, 'max_pieces': 3}
                     for n in range(3)],
    }
    data = normalize(body)
    points = {**data['warehouses'], **data['tasks']}
    received_at = int(time.time())
    receipts = {}
    for (left, right), meters in {('W', 'A'): 1000, ('A', 'B'): 10000, ('W', 'B'): 1000,
                                  ('B', 'A'): 1000, ('W', 'C'): 500}.items():
        a, b = points[left], points[right]
        request = (DrivingRoad.reference_request(a, b) if provider_request else
                   road_request(data['vehicles']['V0'], a, b, opening, 'DRIVING', strategy=32))
        receipts[CollectedDrivingRoad.key(a, b)] = evidence(
            request, meters, 10, 'DRIVING', False, [[a['longitude'], a['latitude']], [b['longitude'], b['latitude']]],
            'TRUCK_NOT_VERIFIED', received_at=received_at, time_status='CURRENT_TRAFFIC_REFERENCE')
    road = DrivingRoad.__new__(DrivingRoad)
    road.fresh_only = True
    road.batch_started_at = received_at
    road.batch_receipt = receipts.get

    def forbid(*args):
        pytest.fail('The optional stage must never use an unsealed hint or live segment.')

    road.known_segment = road.segment = forbid
    sealed = CollectedDrivingRoad(receipts, received_at, allow_expired=True)
    incumbent = replay(data, {'routes': [{'vehicle_id': 'V0', 'task_ids': ['A', 'B']},
                                         {'vehicle_id': 'V1', 'task_ids': ['C']}], 'common_ids': ['D']}, sealed)
    assert incumbent['total_fee'] == '67.00' and complete(data, incumbent)
    return body, road, data, incumbent, receipts


def improve(body, road, incumbent, **kwargs):
    return tail.improve(body, road, incumbent, seconds=kwargs.pop('seconds', 5.), seed=2,
                        control=kwargs.pop('control', lambda: None), cancelled=kwargs.pop('cancelled', lambda: False))


@pytest.mark.parametrize('provider_request', [False, True])
def test_s4_receipt_rebinding_keeps_mandatory_and_outside_incidence_and_adopts_actual_fee(
        monkeypatch, provider_request):
    body, road, data, incumbent, receipts = fixture(provider_request=provider_request)
    original = deepcopy(incumbent)

    def generate(group, rows, *, seconds, seed, control):
        control()
        assert 0 < seconds <= 2. and seed == 2
        assert set(group['tasks']) == {'A', 'B'} and set(group['vehicles']) == {'V0', 'V2'}
        assert set(rows) == {(None, 'A', 1000, 10), ('A', 'B', 10000, 10),
                             (None, 'B', 1000, 10), ('B', 'A', 1000, 10)}
        return [{'vehicle_id': 'V2', 'task_ids': ['B', 'A']},
                {'vehicle_id': 'V1', 'task_ids': ['B', 'A']},
                {'vehicle_id': 'V2', 'task_ids': ['C']}], {'stop_reason': 'SEARCH_COMPLETED'}

    monkeypatch.setattr(route_pyvrp_runtime, 'generate', generate)
    result, details = improve(body, road, incumbent)
    assert details['stop_reason'] == 'ACCEPTED_STRICT_WHOLE_FEE_GAIN'
    assert result['total_fee'] == '58.00' and details['direct_fee_reduction'] == '9.00'
    assert details['candidate_count'] == 1 and details['new_network_calls'] == 0
    assert complete(data, result) and incumbent == original
    assert result['common'] == incumbent['common'] and incumbent['routes'][1] in result['routes']
    changed = next(row for row in result['routes'] if row['task_ids'] == ['B', 'A'])
    assert changed['vehicle_id'] in {'V0', 'V2'}
    assert all(row['request']['vehicle_id'] == changed['vehicle_id'] for row in changed['segments'])
    assert {row['provider_evidence_hash'] for row in changed['segments']} <= {
        row['evidence_hash'] for row in receipts.values()}


def test_all_common_target_group_can_use_an_idle_car_without_a_native_warm_start(monkeypatch):
    body, road, _, _, receipts = fixture()
    body['tasks'][0]['common_unit_price'] = '120'
    data = normalize(body)
    sealed = CollectedDrivingRoad(receipts, road.batch_started_at, allow_expired=True)
    incumbent = replay(data, {'routes': [{'vehicle_id': 'V1', 'task_ids': ['C']}],
                              'common_ids': ['A', 'B', 'D']}, sealed)
    monkeypatch.setattr(route_pyvrp_runtime, 'generate', lambda group, rows, **_: (
        [{'vehicle_id': 'V2', 'task_ids': ['B', 'A']}], {'stop_reason': 'SEARCH_COMPLETED'}))
    result, details = improve(body, road, incumbent)
    assert incumbent['total_fee'] == '276.00' and details['direct_fee_reduction'] == '218.00'
    assert result['total_fee'] == '58.00' and complete(data, result)
    assert incumbent['routes'][0] in result['routes']


def test_cancellation_after_whole_proof_returns_the_checked_improvement(monkeypatch):
    body, road, data, incumbent, _ = fixture()
    assemble = tail.assemble
    cancelled = False

    def checked_assembly(*args):
        nonlocal cancelled
        whole = assemble(*args)
        cancelled = True
        return whole

    monkeypatch.setattr(tail, 'assemble', checked_assembly)
    monkeypatch.setattr(route_pyvrp_runtime, 'generate', lambda *args, **kwargs: (
        [{'vehicle_id': 'V2', 'task_ids': ['B', 'A']}], {'stop_reason': 'SEARCH_COMPLETED'}))
    result, details = improve(body, road, incumbent, cancelled=lambda: cancelled)
    assert cancelled and complete(data, result) and result['total_fee'] == '58.00'
    assert details['stop_reason'] == 'ACCEPTED_STRICT_WHOLE_FEE_GAIN'
    assert [row['task_id'] for row in result['common']] == ['D']


@pytest.mark.parametrize('altered', ['missing', 'opposite_receipt'])
def test_unmeasured_directed_connection_never_becomes_a_quote(monkeypatch, altered):
    body, road, data, incumbent, receipts = fixture()
    a, b = data['tasks']['A'], data['tasks']['B']
    bad_key, reverse_key = CollectedDrivingRoad.key(b, a), CollectedDrivingRoad.key(a, b)
    if altered == 'missing':
        del receipts[bad_key]
    else:
        receipts[bad_key] = receipts[reverse_key]

    def generate(group, rows, **kwargs):
        assert all(row[:2] != ('B', 'A') for row in rows)
        return [{'vehicle_id': 'V2', 'task_ids': ['B', 'A']}], {'stop_reason': 'SEARCH_COMPLETED'}

    monkeypatch.setattr(route_pyvrp_runtime, 'generate', generate)
    result, details = improve(body, road, incumbent)
    assert result is incumbent and details['stop_reason'] == 'NO_STRICT_SCOPE_FEE_GAIN'


@pytest.mark.parametrize('reason', ['CANCELLED', 'WALL_BUDGET', 'TIME_BUDGET', 'SEARCH_WALL_RESERVE'])
def test_selector_interrupt_preserves_hard_stop_without_another_proof(monkeypatch, reason):
    body, road, _, incumbent, _ = fixture()
    stopped = False
    monkeypatch.setattr(route_pyvrp_runtime, 'generate', lambda *_args, **_kwargs:
        ([{'vehicle_id': 'V0', 'task_ids': ['B', 'A']}], {'stop_reason': 'SEARCH_COMPLETED'}))

    def selector(*args, cancelled, **kwargs):
        nonlocal stopped
        stopped = True
        assert cancelled()
        return {}

    def control():
        if stopped and reason != 'CANCELLED':
            raise StopSearch(reason)

    monkeypatch.setattr(tail, 'select_portfolio', selector)
    with pytest.raises(StopSearch) as caught:
        improve(body, road, incumbent, control=control, cancelled=lambda: stopped and reason == 'CANCELLED')
    assert caught.value.reason == reason and caught.value.pyvrp_tail_diagnostics['candidate_count'] == 1


def test_anchor_road_failure_preserves_whole_incumbent(monkeypatch):
    body, road, _, incumbent, _ = fixture()
    monkeypatch.setattr(route_pyvrp_runtime, 'generate', lambda *_args, **_kwargs:
        ([{'vehicle_id': 'V0', 'task_ids': ['B', 'A']}], {'stop_reason': 'SEARCH_COMPLETED'}))

    def unavailable(*args):
        raise RoadError('ROAD_NOT_COLLECTED', 'The protected route has no current measurement.')

    monkeypatch.setattr(tail, 'replay', unavailable)
    result, details = improve(body, road, incumbent)
    assert result is incumbent and details['stop_reason'] == 'SCOPE_INCUMBENT_NOT_MEASURED'


def test_tiny_headroom_and_truck_scope_skip_before_receipt_reads():
    body, road, _, incumbent, _ = fixture()

    def must_skip(*args):
        pytest.fail('Skipped phase cannot read receipts')

    road.batch_receipt = must_skip
    assert improve(body, road, incumbent, seconds=0.)[1]['stop_reason'] == 'INSUFFICIENT_HEADROOM'
    result, details = improve(body, TruckRoad.__new__(TruckRoad), incumbent)
    assert result is incumbent and details['stop_reason'] == 'UNSUPPORTED_ROAD'
