from types import SimpleNamespace
import pytest
from app.route_edge_discovery import collect_promising_edges, compatible_edge_proposals
from app.routing_engine import StopSearch, normalize
from test_route_ortools import problem


def measured_problem(n=60):
    body, _, _ = problem(n=n, capacity=n)
    for i, row in enumerate([*body['warehouses'], *body['tasks']]):
        row.update(longitude=117+i*.01, latitude=31+i*.001, crs='GCJ02')
    return normalize(body)


class Road:
    production_planning = True
    settings = SimpleNamespace(road_calls_enabled=True)
    def __init__(self): self.calls=0
    def stats(self): return {'calls':self.calls}
    def known_segment(self,a,b): return None
    def segment(self,v,a,b,at): self.calls+=1


def test_discovery_continues_after_old_350_total_threshold():
    road = Road()
    result = collect_promising_edges(measured_problem(), road, lambda:None)
    assert road.calls > 350
    assert result['measured_directions'] == road.calls == result['proposed_directions']
    assert result['total_call_limit'] is None


def test_frozen_comparison_cannot_collect_candidate_only_roads():
    road = Road(); road.production_planning=False
    result = collect_promising_edges(measured_problem(3), road, lambda:None)
    assert result['skipped'] and result['stop_reason']=='FROZEN_ROAD_COMPARISON_NO_HTTP'
    assert road.calls==0


def test_discovery_obeys_cancellation_and_never_reverses_measured_receipts():
    road=Road()
    def control():
        if road.calls>=5: raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch,match='CANCELLED'):
        collect_promising_edges(measured_problem(10), road, control)
    assert road.calls==5
    directions={(a,b) for a,b,_ in compatible_edge_proposals(measured_problem(3))}
    assert ('0','1') in directions and ('1','0') in directions


def test_adapter_wall_error_uses_normal_search_interruption_path():
    from app.routing_engine import RoadError
    road=Road()
    def stopped(*args): raise RoadError('WALL_BUDGET','deadline reached')
    road.segment=stopped
    with pytest.raises(StopSearch,match='WALL_BUDGET'):
        collect_promising_edges(measured_problem(2),road,lambda:None)


def test_discovery_time_share_leaves_later_stages_unlimited(monkeypatch):
    import app.route_edge_discovery as discovery
    clock=SimpleNamespace(now=0.)
    monkeypatch.setattr(discovery,'monotonic',lambda:clock.now)
    road=Road(); original=road.segment
    def delayed(*args):
        original(*args);clock.now+=.1
    road.segment=delayed
    diagnostics={}
    result=collect_promising_edges(measured_problem(10),road,lambda:None,
                                  phase_wall_seconds=.35,diagnostics=diagnostics)
    assert result is diagnostics and result['stop_reason']=='EDGE_DISCOVERY_TIME_SHARE'
    first=road.calls
    assert 1<=first<result['proposed_directions']
    result=collect_promising_edges(measured_problem(10),road,lambda:None)
    assert road.calls>first and result['stop_reason']=='PROPOSALS_EXHAUSTED'


def live_truck(settings, tmp_path):
    import httpx
    from pydantic import SecretStr
    from app.road_service import TruckRoad
    from test_amap_qps import Clock, pacer, truck_response
    settings.planning_route_mode = 'TRUCK'
    settings.truck_live_calls_enabled = True
    settings.amap_web_service_key = SecretStr('MOCK_ONLY')
    clock, requests = Clock(), []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=truck_response())
    road = TruckRoad(settings, call_limit=10, production_planning=True,
        transport=httpx.MockTransport(handle), limiter=pacer(tmp_path / 'truck-qps.db', clock), timer=clock)
    assert not hasattr(road, 'known_segment') and road.call_limit is None
    return road, requests


def test_live_truck_without_shared_matrix_skips_discovery_but_keeps_per_actual_car_http(settings, tmp_path):
    from test_routing_engine import truck_data
    road, requests = live_truck(settings, tmp_path)
    result = collect_promising_edges(measured_problem(3), road, lambda: None)
    assert result['skipped'] and result['stop_reason'] == 'VEHICLE_DEPENDENT_ADAPTER_NO_MATRIX'
    assert result['proposed_directions'] == result['measured_directions'] == result['new_network_calls'] == 0
    assert not requests and not road.local and road.calls == 0 and road.call_limit is None
    # Discovery must not disable ordinary exact-profile requests or relabel a
    # first car's receipt for a second physical vehicle.
    car, origin, destination = truck_data()
    departure = '2026-11-13T06:00:00+08:00'
    first = road.segment(car, origin, destination, departure)
    other = {**car, 'id': 'second-actual-car', 'length_m': '7', 'plate': '苏A54321'}
    second = road.segment(other, origin, destination, departure)
    assert road.calls == len(requests) == 2 and road.call_limit is None
    assert first['request']['vehicle_id'] == car['id']
    assert second['request']['vehicle_id'] == other['id']
    assert first['request']['profile'] != second['request']['profile']


@pytest.mark.parametrize('production,live,reason', [
    (False, True, 'FROZEN_ROAD_COMPARISON_NO_HTTP'),
    (True, False, 'LIVE_CALLS_DISABLED'),
])
def test_no_matrix_guard_preserves_frozen_and_disabled_skip_reasons(settings, tmp_path, production, live, reason):
    road, requests = live_truck(settings, tmp_path)
    road.production_planning = production
    road.settings.truck_live_calls_enabled = live
    result = collect_promising_edges(measured_problem(3), road, lambda: None)
    assert result['skipped'] and result['stop_reason'] == reason
    assert road.calls == 0 and not requests


def test_daily_default_truck_can_continue_after_no_matrix_discovery_skip(settings, tmp_path, monkeypatch):
    from copy import deepcopy
    from app.route_deep import optimize_deep
    from app.route_verification import complete
    from app.routing_engine import assemble, evaluate_route
    from test_routing_engine import truck_data
    body, _, _ = problem(n=2, cars=1, capacity=2)
    profile, origin, destination = truck_data()
    body['vehicles'][0].update({k: v for k, v in profile.items() if k != 'id'})
    body['warehouses'][0].update({k: v for k, v in origin.items() if k != 'id'})
    for task in body['tasks']:
        task.update({k: v for k, v in destination.items() if k != 'id'})
    original = deepcopy(body)
    road, requests = live_truck(settings, tmp_path)
    entered = []
    def source(current_body, current_road, seconds, seed, cancelled, **kwargs):
        if not entered:
            # No erroneous vehicle-independent prefetch occurred before main.
            assert not requests and current_road is road
        entered.append('main_search')
        data = normalize(current_body)
        route = evaluate_route(data, 'V0', ['0'], current_road)
        assert route['segments'][0]['request']['vehicle_id'] == 'V0'
        # Real Truck receipts still have their existing time-verification
        # limitation. Keep valid common delivery rather than relaxing it.
        assert {e['code'] for e in route['errors']} == {'ROAD_TIME_UNVERIFIED'}
        result = assemble(data, [], set(data['tasks']), set())
        result['algorithm'] = {'candidate_count': 1, 'stop_reason': 'SEARCH_COMPLETED'}
        return result
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, source)
    monkeypatch.setattr('app.route_columns.select_portfolio',
        lambda body, road, results, **kwargs: deepcopy(results[0]))
    def joint(search, **kwargs):
        raise AssertionError('The daily default must not enter the withdrawn joint group')
    monkeypatch.setattr('app.route_joint_reoptimization.reoptimize_joint_routes', joint)
    result = optimize_deep(body, road, 300)
    flags = result['algorithm']['experiments']
    assert not flags['complete_assignment_pricing'] and not flags['fragment_merging']
    assert flags['critical_edge_collection'] and flags['diagnostics']
    assert body == original and complete(normalize(body), result)
    assert result['total_fee'] == '200.00' and not result['routes'] and not result['unassigned']
    assert entered == ['main_search'] * 5
    assert road.calls == len(requests) == 1 and road.call_limit is None
    discovery = next(s for s in result['algorithm']['stages'] if s['stage'] == 'CRITICAL_EDGE_COLLECTION')
    assert discovery['details']['skipped']
    assert discovery['details']['stop_reason'] == 'VEHICLE_DEPENDENT_ADAPTER_NO_MATRIX'
    assert discovery['details']['new_network_calls'] == 0
    joint_stage = next(s for s in result['algorithm']['stages'] if s['stage'] == 'JOINT_REOPTIMIZATION')
    assert joint_stage['details']['skipped'] and joint_stage['details']['stop_reason'] == 'SWITCH_DISABLED'
