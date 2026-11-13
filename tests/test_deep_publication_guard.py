"""Independent final publication gates using simulated provider HTTP receipts.

No business database or real AMap service is accessed. Search/collector flags
cannot replace the independent worker's proof of source, amount and scope.
"""
from copy import deepcopy
import time

import httpx
import pytest

from app.api import ApiError
from app.config import Settings
from app.plan_service import verify_deep_result
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad
from app.route_verification import DrivingCollector
from app.routing_engine import assemble, check_result, evaluate_route, normalize


class NoWaitLimiter:
    def acquire(self, service, **kwargs):
        pass

    def defer(self, service, retry):
        pass


@pytest.fixture
def fresh_case(tmp_path):
    body = {
        'source': 'FROZEN', 'delivery_date': '2026-11-13',
        'warehouses': [{'id': 'W', 'longitude': '117', 'latitude': '31', 'crs': 'GCJ02',
                        'earliest_departure_at': '2026-11-13T06:00:00+08:00'}],
        'vehicles': [{'id': 'V', 'plate': '单元测试车', 'warehouse_id': 'W', 'available': True,
                      'max_pieces': 400}],
        'quotes': {'Q': {'mode': 'KM', 'per_km': '5.8'}},
        'tasks': [{'id': task, 'warehouse_id': 'W', 'pieces': 10, 'quote_id': 'Q',
                   'common_unit_price': '7', 'longitude': longitude, 'latitude': '31.1',
                   'crs': 'GCJ02', 'open_at': '2026-11-13T06:00:00+08:00',
                   'close_at': '2026-11-13T22:00:00+08:00', 'unload_minutes': 15,
                   'latest_arrival_at': None}
                  for task, longitude in [('A', '117.1'), ('B', '117.2')]],
    }
    requested = []

    def provider(request):
        requested.append((request.url.params['origin'], request.url.params['destination']))
        return httpx.Response(200, json={'status': '1', 'infocode': '10000', 'route': {
            'paths': [{'distance': '1000', 'cost': {'duration': '60'},
                       'steps': [{'polyline': ';'.join(requested[-1])}]}]}})

    settings = Settings(_env_file=None, environment='test', planning_route_mode='DRIVING',
                        driving_live_calls_enabled=True, amap_web_service_key='unit-placeholder',
                        amap_rate_limit_path=tmp_path/'private-rate.db',
                        amap_reference_graph_path=tmp_path/'private-graph.db')
    road = DrivingRoad(settings, call_limit=None, limiter=NoWaitLimiter(),
                       transport=httpx.MockTransport(provider))
    data = normalize(body)
    collector = DrivingCollector(road, lambda: False, lambda: None)
    try:
        route = evaluate_route(data, 'V', ['A', 'B'], collector)
        result = assemble(data, [route], [], [])
        finished = int(time.time())
        selected = collector.frozen.verify(data, result, finished)
        result['algorithm'] = {
            'mode': 'DAILY_DEEP_PORTFOLIO', 'optimality_proven': False,
            'final_validation': {
                'mode': 'FRESH_DRIVING_VALIDATED', 'fresh': True,
                'network_started_at': collector.started_at, 'network_finished_at': finished,
                'actual_calls': 2, 'total_job_calls': 2, 'source_count': 2,
                'selected_segments': 2, 'selected_provider_count': 2,
                'selected_provider_sources': selected, 'selected_subset_of_collected': True,
            },
        }
        assert road.calls == len(requested) == 2
        assert not result['confirmable'] and result['road_source'] == 'DRIVING'
    finally:
        collector.close()
    try:
        yield body, road, result
    finally:
        road.close()


def validation(result):
    return result['algorithm']['final_validation']


def reseal(body, result):
    """Keep the ordinary independent proof valid to exercise the second gate."""
    for route in result['routes']:
        for segment in route['segments']:
            segment['evidence_hash'] = content_hash({k: v for k, v in segment.items()
                                                     if k != 'evidence_hash'})
    result['check'] = check_result(normalize(body), result)
    assert {issue['code'] for issue in result['check']['issues']} == {'DRIVING_REFERENCE'}


def assert_rejected(body, result, road, code='DEEP_ROAD_RECEIPT', *, cancelled=False):
    with pytest.raises(ApiError) as caught:
        verify_deep_result(body, result, road, was_cancelled=cancelled)
    assert caught.value.code == code


def test_real_shaped_mock_http_receipts_pass_fresh_publication(fresh_case):
    body, road, result = fresh_case
    original = deepcopy(result)
    assert verify_deep_result(body, result, road) is None
    assert result == original


@pytest.mark.parametrize('change', [
    {'actual_calls': 0}, {'actual_calls': True}, {'actual_calls': 3},
    {'total_job_calls': 3}, {'source_count': 1}, {'source_count': 999},
    {'selected_provider_count': 1}, {'selected_segments': 1},
    {'selected_subset_of_collected': False},
])
def test_producer_counter_claims_do_not_replace_observed_road_counts(fresh_case, change):
    body, road, result = fresh_case
    validation(result).update(change)
    assert_rejected(body, result, road)


def test_missing_source_count_is_rejected(fresh_case):
    body, road, result = fresh_case
    validation(result).pop('source_count')
    assert_rejected(body, result, road)


def test_fresh_metadata_cannot_claim_calls_the_adapter_never_observed(fresh_case):
    body, road, result = fresh_case
    road.calls = 0
    assert_rejected(body, result, road)


@pytest.mark.parametrize('offline', ['zero_limit', 'disabled_service'])
def test_offline_entry_rejects_fresh_labels_even_with_well_formed_receipts(fresh_case, offline):
    body, road, result = fresh_case
    if offline == 'zero_limit':
        road.call_limit = 0
    else:
        road.settings.driving_live_calls_enabled = False
    assert_rejected(body, result, road)


def test_normal_live_completion_requires_fresh_validation(fresh_case):
    body, road, result = fresh_case
    validation(result).update(mode='OFFLINE_REFERENCE_VALIDATED', fresh=False)
    assert_rejected(body, result, road, 'DEEP_ROAD_VERIFICATION_REQUIRED')


def test_late_user_cancellation_preserves_valid_fresh_result_but_not_forged_receipt(fresh_case):
    body, road, result = fresh_case
    assert verify_deep_result(body, result, road, was_cancelled=True) is None
    metadata = result['routes'][0]['segments'][0]['actual_source_receipt']
    metadata['evidence_hash'] = '0' * 64
    reseal(body, result)
    assert_rejected(body, result, road, cancelled=True)


def test_cancelled_reference_result_is_allowed_without_a_fresh_claim(fresh_case):
    body, road, result = fresh_case
    validation(result).update(mode='OFFLINE_REFERENCE_VALIDATED', fresh=False, actual_calls=0)
    assert verify_deep_result(body, result, road, was_cancelled=True) is None


@pytest.mark.parametrize('offset', [-7*86400-1, 60])
def test_cancelled_reference_rejects_expired_or_future_receipt(fresh_case, offset):
    body, road, result = fresh_case
    validation(result).update(mode='OFFLINE_REFERENCE_VALIDATED', fresh=False)
    for route in result['routes']:
        for segment in route['segments']:
            segment['received_at'] = int(time.time()) + offset
    reseal(body, result)
    assert_rejected(body, result, road, 'DEEP_ROAD_REFERENCE_EXPIRED', cancelled=True)


@pytest.mark.parametrize('kind', ['missing', 'other_coordinate'])
def test_receipt_coordinate_binding_is_independent_of_outer_segment_binding(fresh_case, kind):
    body, road, result = fresh_case
    segment = result['routes'][0]['segments'][0]
    metadata = segment['actual_source_receipt']
    if kind == 'missing':
        metadata['request']['origin'] = {}
    else:
        metadata['request']['origin']['longitude'] = '118'
    # Reseal the forged source and outer evidence: coordinates must still fail.
    raw = {**metadata, 'polyline': segment['polyline']}
    metadata['evidence_hash'] = content_hash({k: v for k, v in raw.items() if k != 'evidence_hash'})
    segment['provider_evidence_hash'] = metadata['evidence_hash']
    reseal(body, result)
    assert_rejected(body, result, road)


def test_missing_actual_receipt_is_rejected_despite_valid_outer_binding(fresh_case):
    body, road, result = fresh_case
    result['routes'][0]['segments'][0].pop('actual_source_receipt')
    reseal(body, result)
    assert_rejected(body, result, road)


def test_selected_provider_source_map_must_match_actual_receipts(fresh_case):
    body, road, result = fresh_case
    first = next(iter(validation(result)['selected_provider_sources'].values()))
    first['meters'] += 1
    assert_rejected(body, result, road)


@pytest.mark.parametrize('claim', [{'confirmable': True}, {'road_source': 'LIVE'}])
def test_driving_reference_cannot_be_promoted_to_truck_execution_proof(fresh_case, claim):
    body, road, result = fresh_case
    result.update(claim)
    assert_rejected(body, result, road, 'DEEP_DRIVING_SCOPE')


def test_common_only_has_a_complete_price_proof_without_any_road_claim(fresh_case):
    body, road, _ = fresh_case
    data = normalize(body)
    result = assemble(data, [], set(data['tasks']), [])
    result['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'final_validation': {
        'mode': 'COMMON_ONLY_VALIDATED', 'fresh': False, 'selected_segments': 0}}
    assert result['total_fee'] == '140.00'
    assert verify_deep_result(body, result, road) is None


@pytest.mark.parametrize('claim', [
    {'mode': 'FRESH_DRIVING_VALIDATED', 'fresh': True, 'selected_segments': 0},
    {'mode': 'COMMON_ONLY_VALIDATED', 'fresh': True, 'selected_segments': 0},
    {'mode': 'COMMON_ONLY_VALIDATED', 'fresh': False, 'selected_segments': 1},
    {},
])
def test_common_only_cannot_invent_an_actual_driving_validation(fresh_case, claim):
    body, road, _ = fresh_case
    data = normalize(body)
    result = assemble(data, [], set(data['tasks']), [])
    result['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'final_validation': claim}
    assert_rejected(body, result, road)
