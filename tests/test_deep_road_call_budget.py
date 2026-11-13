"""Finite HTTP quotas must leave room for a genuinely fresh final batch."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import time

import httpx
from pydantic import SecretStr
import pytest

from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, evidence
from app.route_deep import baseline_result, optimize_deep
from app.route_verification import complete
from app.routing_engine import RoadError, StopSearch, assemble, evaluate_route, normalize
from test_route_neighborhood import problem


class NoWait:
    def acquire(self, service, *, cancelled):
        assert not cancelled()

    def defer(self, service, retry):
        pass


@pytest.fixture
def live_road(settings, monkeypatch):
    settings.planning_route_mode = 'DRIVING'
    settings.driving_live_calls_enabled = True
    settings.amap_web_service_key = SecretStr('TEST_ONLY_DEEP_CALL_BUDGET')
    clock = SimpleNamespace(now=int(time.time()))
    monkeypatch.setattr('app.road_service.time.time', lambda: clock.now)
    opened = []

    def create(limit=6, *, cached=False, n=3):
        body, _ = problem(n=n, cars=1, capacity=n)
        for index, point in enumerate(body['warehouses'] + body['tasks']):
            point.update(longitude=117 + index / 100, latitude=31 + index / 100, crs='GCJ02')
        cache, requests = {}, []

        def handler(request):
            clock.now += 1
            origin, destination = request.url.params['origin'], request.url.params['destination']
            trunk = origin == '117.000000,31.000000'
            # Different final prices prove that old search receipts are not
            # relabelled as this batch's fresh provider response.
            meters = (12000 if trunk else 2500) if road.force_refresh else (10000 if trunk else 2000)
            requests.append({'fresh': road.force_refresh, 'received_at': clock.now,
                             'origin': origin, 'destination': destination})
            return httpx.Response(200, json={'status': '1', 'infocode': '10000', 'route': {'paths': [
                {'distance': str(meters), 'cost': {'duration': '1'},
                 'steps': [{'polyline': origin + ';' + destination}]}]}})

        road = DrivingRoad(settings, limit, cache_get=cache.get,
                           cache_put=lambda key, row: cache.__setitem__(key, deepcopy(row)),
                           transport=httpx.MockTransport(handler), limiter=NoWait(), allow_historical=True)
        opened.append(road)
        if cached:
            origin = body['warehouses'][0]
            for destination in body['tasks']:
                request = DrivingRoad.reference_request(origin, destination)
                row = evidence(request, 10000 if origin['id'] == 'W' else 2000, 1, 'DRIVING', False,
                    [[origin['longitude'], origin['latitude']], [destination['longitude'], destination['latitude']]],
                    'TRUCK_NOT_VERIFIED', time_status='CURRENT_TRAFFIC_REFERENCE', received_at=clock.now - 100)
                cache[content_hash(request)] = row
                origin = destination
            road.seed_reference_cache(cache.values())
        return body, road, requests

    yield create
    for road in opened:
        road.close()


def source_rounds(monkeypatch, callback):
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, callback)


def all_common_round(body, road, *args, **kwargs):
    return baseline_result(normalize(body))


def test_search_exhausts_its_cap_but_final_collects_new_receipts_within_total(live_road, monkeypatch):
    body, road, requests = live_road(6)
    data = normalize(body)
    caps, old_receipts = [], set()

    def run(*args, **kwargs):
        caps.append(road.call_limit)
        route = evaluate_route(data, 'V0', ['0', '1', '2'], road)
        assert not route['errors'] and road.calls == 3
        old_receipts.update(segment['evidence_hash'] for segment in route['segments'])
        with pytest.raises(RoadError) as failure:
            road.segment(data['vehicles']['V0'], data['warehouses']['W'], data['tasks']['2'],
                         data['warehouses']['W']['earliest_departure_at'])
        assert failure.value.code == 'ROAD_CALL_BUDGET'
        return assemble(data, [route], [], [])

    source_rounds(monkeypatch, run)
    result = optimize_deep(body, road, 2)
    final, quota = result['algorithm']['final_validation'], result['algorithm']['road_call_budget']
    assert caps and set(caps) == {3}
    assert quota == {'enabled': True, 'total_limit': 6, 'eligible_task_count': 3,
                     'calls_at_entry': 0, 'search_call_limit': 3, 'reserved_for_validation': 3,
                     'reservation_policy': 'ONE_ATTEMPT_PER_ELIGIBLE_TASK',
                     'calls_after_search': 3, 'search_calls': 3, 'remaining_before_validation': 3}
    assert road.call_limit == road.calls == len(requests) == 6
    assert [row['fresh'] for row in requests] == [False] * 3 + [True] * 3
    assert final['mode'] == 'FRESH_DRIVING_VALIDATED' and final['fresh']
    assert final['actual_calls'] == final['selected_segments'] == 3
    assert final['oldest_received_at'] > max(row['received_at'] for row in requests if not row['fresh'])
    assert all(segment['provider_evidence_hash'] not in old_receipts
               for route in result['routes'] for segment in route['segments'])
    assert result['total_fee'] == '17.00' and Decimal(result['total_fee']) < Decimal('300')
    assert complete(data, result) and not result['common']


def test_low_quota_uses_cached_routes_without_search_http_and_reserves_all_calls(live_road, monkeypatch):
    body, road, requests = live_road(2, cached=True)
    data = normalize(body)

    def run(*args, **kwargs):
        assert road.call_limit == 0 and road.calls == 0
        route = evaluate_route(data, 'V0', ['0', '1', '2'], road)
        assert not route['errors'] and route['fee'] == '14.00'
        return assemble(data, [route], [], [])

    source_rounds(monkeypatch, run)
    result = optimize_deep(body, road, 2)
    quota = result['algorithm']['road_call_budget']
    assert quota['search_call_limit'] == quota['search_calls'] == 0
    assert quota['reserved_for_validation'] == quota['remaining_before_validation'] == 2
    assert road.call_limit == road.calls == len(requests) == 2
    assert all(row['fresh'] for row in requests)
    assert complete(data, result) and Decimal(result['total_fee']) < Decimal('300')
    assert result['algorithm']['final_validation']['actual_calls'] == 2


def test_low_quota_without_known_roads_returns_honest_common_fallback(live_road, monkeypatch):
    body, road, requests = live_road(2)
    data = normalize(body)

    def run(*args, **kwargs):
        assert road.call_limit == 0
        route = evaluate_route(data, 'V0', ['0'], road)
        assert any(error['code'] == 'ROAD_CALL_BUDGET' for error in route['errors'])
        return baseline_result(data)

    source_rounds(monkeypatch, run)
    result = optimize_deep(body, road, .2)
    assert not requests and road.calls == 0 and road.call_limit == 2
    assert result['total_fee'] == '300.00'
    assert result['algorithm']['final_validation']['mode'] == 'COMMON_ONLY_VALIDATED'
    assert not result['algorithm']['final_validation']['fresh']


@pytest.mark.parametrize('entry,reserved,cap', [(1, 3, 3), (4, 2, 4), (6, 0, 6), (8, 0, 6)])
def test_entry_calls_are_accounted_without_raising_an_exhausted_limit(live_road, monkeypatch, entry, reserved, cap):
    body, road, requests = live_road(6)
    road.calls = entry
    seen = []

    def run(*args, **kwargs):
        seen.append(road.call_limit)
        return all_common_round(*args, **kwargs)

    source_rounds(monkeypatch, run)
    from app.route_columns import select_portfolio
    from app.route_small_groups import seed_small_groups
    nested_caps = []

    def pooled(*args, **kwargs):
        nested_caps.append(road.call_limit)
        return select_portfolio(*args, **kwargs)

    def small_groups(*args, **kwargs):
        nested_caps.append(road.call_limit)
        return seed_small_groups(*args, **kwargs)

    monkeypatch.setattr('app.route_columns.select_portfolio', pooled)
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', small_groups)
    result = optimize_deep(body, road, 2)
    quota = result['algorithm']['road_call_budget']
    assert seen and set(seen) == {cap}
    assert len(nested_caps) == 2 and all(value <= cap for value in nested_caps)
    assert quota['reserved_for_validation'] == reserved and quota['search_calls'] == 0
    assert quota['calls_at_entry'] == quota['calls_after_search'] == entry
    assert quota['remaining_before_validation'] == max(0, 6 - entry)
    assert road.call_limit == 6 and road.calls == entry and not requests


def test_reserve_counts_only_tasks_with_a_quote_and_an_available_same_warehouse_car(live_road, monkeypatch):
    body, road, _ = live_road(9, n=6)
    body['vehicles'][0]['max_pieces'] = 3
    body['vehicles'].append({'id': 'OFF', 'warehouse_id': 'OTHER', 'max_pieces': 10, 'available': False})
    body['tasks'][3]['quote_id'] = None
    body['tasks'][4]['warehouse_id'] = 'OTHER'
    body['tasks'][5]['pieces'] = 4
    source_rounds(monkeypatch, all_common_round)
    result = optimize_deep(body, road, .2)
    quota = result['algorithm']['road_call_budget']
    assert quota['eligible_task_count'] == quota['reserved_for_validation'] == 3
    assert quota['search_call_limit'] == 6


@pytest.mark.parametrize('stage', ['source', 'pool', 'small_groups'])
@pytest.mark.parametrize('failure', ['exception', 'cancel'])
def test_exception_or_cancel_restores_total_limit_and_observer(live_road, monkeypatch, stage, failure):
    body, road, requests = live_road(6)
    cancelled = [False]
    old_observer = lambda spec: None
    road.candidate_observer = old_observer

    def fail(*args, **kwargs):
        assert road.call_limit in {0, 3}
        if failure == 'exception':
            raise RuntimeError('Controlled failure')
        cancelled[0] = True
        raise StopSearch('CANCELLED')

    source_rounds(monkeypatch, fail if stage == 'source' else all_common_round)
    if stage == 'pool':
        monkeypatch.setattr('app.route_columns.select_portfolio', fail)
    elif stage == 'small_groups':
        monkeypatch.setattr('app.route_small_groups.seed_small_groups', fail)
    if failure == 'exception':
        with pytest.raises(RuntimeError, match='Controlled failure'):
            optimize_deep(body, road, 2, cancelled=lambda: cancelled[0])
    else:
        result = optimize_deep(body, road, 2, cancelled=lambda: cancelled[0])
        assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert road.call_limit == 6 and road.candidate_observer is old_observer
    assert not requests


@pytest.mark.parametrize('limit,enabled', [(0, True), (None, True), (6, False)])
def test_offline_unlimited_and_disabled_live_calls_do_not_reserve(live_road, monkeypatch, limit, enabled):
    body, road, requests = live_road(limit)
    road.settings.driving_live_calls_enabled = enabled
    seen = []

    def run(*args, **kwargs):
        seen.append(road.call_limit)
        return all_common_round(*args, **kwargs)

    source_rounds(monkeypatch, run)
    result = optimize_deep(body, road, .2)
    assert seen and all(cap == limit for cap in seen)
    assert road.call_limit == limit and not requests
    quota = result['algorithm']['road_call_budget']
    assert not quota['enabled'] and quota['reserved_for_validation'] == 0
    assert quota['search_call_limit'] == limit


def test_non_driving_adapter_retains_its_limit(monkeypatch):
    body, road = problem(n=3)
    road.call_limit = 6
    source_rounds(monkeypatch, all_common_round)
    result = optimize_deep(body, road, .2)
    assert road.call_limit == 6
    quota = result['algorithm']['road_call_budget']
    assert not quota['enabled'] and quota['reserved_for_validation'] == 0
    assert quota['search_call_limit'] == 6
