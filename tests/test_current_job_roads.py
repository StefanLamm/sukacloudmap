import httpx
import pytest
from pydantic import SecretStr

from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, TruckRoad
from app.route_verification import DrivingCollector
from app.routing_engine import RoadError
from test_amap_qps import truck_response
from test_deep_road_call_budget import NoWait
from test_driving_reference import AT, response
from test_routing_engine import truck_data


@pytest.mark.parametrize('adapter,payload,mode', [
    (DrivingRoad, response, 'DRIVING'), (TruckRoad, truck_response, 'TRUCK'),
])
def test_new_jobs_never_read_prior_measurements(settings, adapter, payload, mode):
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    settings.planning_route_mode = mode
    settings.driving_live_calls_enabled = settings.truck_live_calls_enabled = True
    car, origin, destination = truck_data()
    requests = []

    def deny_cache(*_):
        pytest.fail('a live planning job used a cross-job cache')

    def handler(request):
        requests.append(request)
        body = payload()
        paths = body['route']['paths'] if mode == 'DRIVING' else body['data']['route']['paths']
        paths[0]['distance'] = str(len(requests) * 1000)
        return httpx.Response(200, json=body)

    def new_job():
        return adapter(settings, production_planning=True, fresh_only=True,
                       cache_get=deny_cache, cache_put=deny_cache,
                       limiter=NoWait(), transport=httpx.MockTransport(handler))

    first = new_job()
    assert first.segment(car, origin, destination, AT)['meters'] == 1000
    assert first.segment(car, origin, destination, AT)['meters'] == 1000
    second = new_job()
    if mode == 'DRIVING':
        assert second.known_segment(origin, destination) is None
    assert second.segment(car, origin, destination, AT)['meters'] == 2000
    assert len(requests) == 2 and second.stats()['call_limit'] is None


def test_verification_reuses_this_jobs_provider_measurement(settings):
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    settings.planning_route_mode = 'DRIVING'
    settings.driving_live_calls_enabled = True
    car, origin, destination = truck_data()
    road = DrivingRoad(settings, production_planning=True, fresh_only=True, limiter=NoWait(),
                       transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response())))
    key = content_hash(road.reference_request(origin, destination))
    assert road.known_segment(origin, destination) is None and road.batch_receipt(key) is None
    initial = road.segment(car, origin, destination, AT)
    assert road.known_segment(origin, destination)['meters'] == initial['meters']
    collector = DrivingCollector(road, lambda: False, lambda: None)
    try:
        selected = collector.segment({**car, 'id':'OTHER'}, origin, destination, AT)
        assert selected['request']['vehicle_id'] == 'OTHER'
        assert selected['provider_evidence_hash'] == initial['evidence_hash']
        assert road.calls == 1 and collector.reused_directions == 1
    finally:
        collector.close()


def test_disabled_live_roads_never_fall_back_to_cache(settings):
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    settings.planning_route_mode = 'DRIVING'
    car, origin, destination = truck_data()
    road = DrivingRoad(settings, production_planning=True, fresh_only=True,
                       cache_get=lambda *_: pytest.fail('old cache was read'), limiter=NoWait(),
                       transport=httpx.MockTransport(lambda _: pytest.fail('network is disabled')))
    with pytest.raises(RoadError, match='当前入口明确关闭外部算路'):
        road.segment(car, origin, destination, AT)
    assert road.calls == 0 and road.known_segment(origin, destination) is None
