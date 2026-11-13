"""Production deep orchestration and actually refreshed-road regressions."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from app.api import ApiError
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, evidence, road_request
from app.route_deep import optimize_deep
from app.route_verification import (CollectedDrivingRoad, DrivingCollector, complete,
                                    replay, validate_driving)
from app.routing_engine import RoadError, StopSearch, assemble, check_result, evaluate_route, normalize
from test_route_neighborhood import problem


def initial(body, road, ids=None):
    data = normalize(body)
    ids = list(data['tasks']) if ids is None else ids
    route = evaluate_route(data, 'V0', ids, road)
    return assemble(data, [route], set(data['tasks']) - set(ids), [])


def stub_rounds(monkeypatch, values):
    seen = []
    index = [0]
    def run(body, road, seconds, seed, cancelled, **kwargs):
        seen.append({'seed': seed, 'seconds': seconds, 'kwargs': kwargs})
        value = deepcopy(values[index[0] % len(values)])
        index[0] += 1
        for route in value['routes']:
            callback = getattr(road, 'candidate_observer', None)
            if callback:
                callback({'vehicle_id': route['vehicle_id'], 'task_ids': route['task_ids']})
        value['algorithm'] = {'candidate_count': 1, 'stop_reason': 'SEARCH_COMPLETED'}
        return value
    monkeypatch.setattr('app.route_deep.optimize_hybrid', run)
    monkeypatch.setattr('app.route_deep.optimize_alns', run)
    monkeypatch.setattr('app.route_deep.optimize_ortools', run)
    return seen


class BatchDriving(DrivingRoad):
    """Deterministic transport double; no network or business database access."""
    def __init__(self, body, old_seconds=1, new_seconds=2, factor=1, fail=None):
        self.settings = SimpleNamespace(road_calls_enabled=True)
        self.call_limit = None
        self.force_refresh = False
        self.allow_historical = True
        self._freshened_keys = set()
        self._fresh_bound_keys = set()
        self.calls = self.hits = self.failures = 0
        self.io_seconds = 0.
        self.old_seconds, self.new_seconds, self.factor = old_seconds, new_seconds, factor
        self.new_meters = None
        self.fail = fail
        self.measured = {}
        self.counts = {}
        self.points = {v['id']: v for v in body['warehouses'] + body['tasks']}

    def known_segment(self, a, b):
        key = content_hash(self.reference_request(a, b))
        if key in self.measured:
            row = self.measured[key]
            return {k: row[k] for k in ('meters', 'seconds', 'received_at')}
        return {'meters': 10000 if a['id'] == 'W' else 2000, 'seconds': self.old_seconds,
                'received_at': int(time.time()) - 7200}

    def segment(self, vehicle, a, b, departure):
        key = content_hash(self.reference_request(a, b))
        refresh = self.force_refresh and key not in self._freshened_keys
        if refresh:
            self.calls += 1
            self.counts[key] = self.counts.get(key, 0) + 1
            if self.fail and self.fail(a, b):
                self.failures += 1
                raise RoadError('ROAD_TIMEOUT', 'Controlled transport failure')
            meters = (self.new_meters(a, b) if self.new_meters is not None else
                      (10000 if a['id'] == 'W' else 2000) * self.factor)
            seconds = self.new_seconds(a, b) if callable(self.new_seconds) else self.new_seconds
            received = int(time.time())
            self._freshened_keys.add(key)
        elif key in self.measured:
            row = self.measured[key]
            meters, seconds, received = row['meters'], row['seconds'], row['received_at']
        else:
            meters, seconds, received = (10000 if a['id'] == 'W' else 2000), self.old_seconds, int(time.time()) - 7200
        row = evidence(road_request(vehicle, a, b, departure, 'DRIVING', strategy=32), meters, seconds,
                       'DRIVING', False, [[a['longitude'], a['latitude']], [b['longitude'], b['latitude']]],
                       'TRUCK_NOT_VERIFIED', time_status='CURRENT_TRAFFIC_REFERENCE' if refresh or key in self.measured else 'HISTORICAL_TRAFFIC_REFERENCE',
                       received_at=received)
        if refresh:
            self.measured[key] = deepcopy(row)
        return row

    def stats(self):
        return {'source': 'DRIVING', 'calls': self.calls, 'cache_hits': self.hits,
                'failures': self.failures, 'call_limit': self.call_limit}


def driving_problem(*, cars=1, common='100', late=False, factor=1, new_seconds=2, fail=None):
    body, _ = problem(n=3, cars=cars, common=common)
    for n, point in enumerate(body['warehouses'] + body['tasks']):
        point.update(longitude=117 + n / 100, latitude=31 + n / 100, crs='GCJ02')
    if late:
        body['tasks'][-1]['close_at'] = '2026-11-13T06:00:15+08:00'
    road = BatchDriving(body, factor=factor, new_seconds=new_seconds, fail=fail)
    return body, road


def test_daily_runs_different_seeds_pools_complete_allocations_and_keeps_lower_fee(monkeypatch):
    body, road = problem(n=3, cars=2)
    first = initial(body, road)
    data = normalize(body)
    split = assemble(data, [evaluate_route(data, 'V0', ['0', '1'], road),
                            evaluate_route(data, 'V1', ['2'], road)], [], [])
    seen = stub_rounds(monkeypatch, [split, first])
    progress = []
    result = optimize_deep(body, road, .5, seed=18, progress=progress.append)
    assert result['total_fee'] == first['total_fee'] == '14.00'
    assert len(seen) >= 4 and len({r['seed'] for r in seen}) == len(seen)
    assert result['algorithm']['complete_round_allocations'] == 2
    assert result['algorithm']['distinct_route_count'] >= 3
    assert result['algorithm']['mode'] == 'DAILY_DEEP_PORTFOLIO'
    assert result['algorithm']['final_validation']['mode'] == 'FIXTURE_VALIDATED'
    assert not result['algorithm']['optimality_proven']
    assert progress[-1]['stage'] == 'COMPLETE' and progress[-1]['best_fee'] == '14.00'
    assert not hasattr(road, 'candidate_observer')


def test_observer_hook_uses_job_road_without_changing_global_search_classes():
    from app.route_search import JointSearch
    body, road = problem(n=3)
    data = normalize(body)
    observed = []
    road.candidate_observer = observed.append
    host = JointSearch(data, road, 5, 1, lambda: False)
    value = evaluate_route(data, 'V0', ['0', '1'], road)
    host.remember(value)
    assert observed == [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}]
    assert 'segments' not in observed[0] and 'fee' not in observed[0]


def test_real_generators_and_generic_column_solver_are_used_on_a_fixture():
    body, road = problem(n=6, cars=2)
    result = optimize_deep(body, road, 1.2, seed=7)
    assert complete(normalize(body), result)
    assert result['algorithm']['round_count'] >= 2
    assert result['algorithm']['actual_seeds'] == list(dict.fromkeys(result['algorithm']['actual_seeds']))
    assert result['algorithm']['final_validation']['fresh'] is False


def test_cancel_retains_only_a_current_verified_warm_start_without_any_new_reads():
    body, road = problem(n=3)
    value = initial(body, road)
    before = road.reads
    result = optimize_deep(body, road, 20, cancelled=lambda: True, initial_result=value)
    assert result['total_fee'] == value['total_fee'] and road.reads == before
    assert result['algorithm']['stop_reason'] == 'CANCELLED'


def test_cancel_rejects_an_old_lower_quote_when_current_metrics_changed():
    body, road = problem(n=3, common='10')
    value = initial(body, road)
    road.costs['W', '0'] = 100000
    result = optimize_deep(body, road, 20, cancelled=lambda: True, initial_result=value)
    assert result['routes'] == [] and result['total_fee'] == '30.00'
    assert result['algorithm']['final_validation']['mode'] == 'COMMON_ONLY_VALIDATED'


@pytest.mark.parametrize('received', [lambda: int(time.time()) - 8 * 86400, lambda: int(time.time()) + 10])
def test_cancel_rejects_expired_or_future_driving_receipts(received):
    body, road = driving_problem(common='10')
    value = initial(body, road)
    stamp = received()
    for route in value['routes']:
        for segment in route['segments']:
            segment['received_at'] = stamp
            segment['evidence_hash'] = content_hash({k: v for k, v in segment.items() if k != 'evidence_hash'})
    result = optimize_deep(body, road, 30, cancelled=lambda: True, initial_result=value)
    assert not result['routes'] and result['total_fee'] == '30.00'
    assert result['algorithm']['final_validation']['fresh'] is False
    assert road.calls == 0


def test_cancel_without_a_complete_mandatory_plan_does_not_return_a_saveable_result():
    body, road = problem(n=3, cars=0, common=None)
    with pytest.raises(ApiError) as exc:
        optimize_deep(body, road, 30, cancelled=lambda: True)
    assert exc.value.code == 'CANCELLED'


def test_no_complete_mandatory_solution_is_an_explicit_failure():
    body, road = problem(n=3, cars=0, common=None)
    with pytest.raises(ApiError) as exc:
        optimize_deep(body, road, .1)
    assert exc.value.code == 'DEEP_NO_FEASIBLE_PLAN'


def test_live_final_quotation_uses_fresh_prices_and_never_refetches_same_direction(monkeypatch):
    body, road = driving_problem(factor=2)
    value = initial(body, road)
    stub_rounds(monkeypatch, [value])
    result = optimize_deep(body, road, 2)
    assert value['total_fee'] == '14.00' and result['total_fee'] == '28.00'
    final = result['algorithm']['final_validation']
    assert final['mode'] == 'FRESH_DRIVING_VALIDATED' and final['fresh']
    assert final['actual_calls'] == final['selected_segments'] == 3
    assert all(n == 1 for n in road.counts.values())
    assert final['network_started_at'] <= final['oldest_received_at'] <= final['network_finished_at']
    assert not road.force_refresh and road.allow_historical


def test_late_route_repairs_against_one_frozen_actual_batch_using_only_actual_receipts():
    body, road = driving_problem(late=True, new_seconds=10)
    body['tasks'][0]['close_at'] = '2026-11-13T06:00:15+08:00'
    value = initial(body, road)
    result, final = validate_driving(body, road, value, [], cancelled=lambda: False,
        control=lambda: None, repair_control=lambda: None, progress=lambda value: None, selection_seconds=.1)
    assert complete(normalize(body), result)
    assert len(result['routes'][0]['task_ids']) == 2
    assert len(result['common']) == 1 and result['common'][0]['task_id'] in {'0', '2'}
    assert final['selected_segments'] == 2 and final['source_count'] >= 3
    assert final['selected_subset_of_collected'] is True and final['fresh'] is True
    assert all(n == 1 for n in road.counts.values())
    assert all(s['actual_source_receipt']['received_at'] == s['received_at'] for r in result['routes'] for s in r['segments'])


def test_repair_can_use_an_unused_physical_car_when_it_costs_less_than_common():
    body, road = driving_problem(cars=2, late=True, new_seconds=10)
    body['tasks'][0]['close_at'] = '2026-11-13T06:00:15+08:00'
    value = initial(body, road)
    result, final = validate_driving(body, road, value, [], cancelled=lambda: False,
        control=lambda: None, repair_control=lambda: None, progress=lambda value: None, selection_seconds=.1)
    assert complete(normalize(body), result) and not result['common']
    assert len(result['routes']) == 2 and len({r['vehicle_id'] for r in result['routes']}) == 2
    assert result['total_fee'] == '22.00'
    assert final['new_bridge_edges'] > 0


def test_network_failure_can_only_fallback_to_priced_common_or_actual_measured_edges():
    body, road = driving_problem(fail=lambda a, b: b['id'] == '1')
    value = initial(body, road)
    result, final = validate_driving(body, road, value, [], cancelled=lambda: False,
        control=lambda: None, repair_control=lambda: None, progress=lambda value: None, selection_seconds=.1)
    assert complete(normalize(body), result)
    assert '1' in {r['task_id'] for r in result['common']}
    assert final['failures'] > 0
    assert all(s['provider_evidence_hash'] == s['actual_source_receipt']['evidence_hash']
               for route in result['routes'] for s in route['segments'])


def test_no_common_and_no_feasible_fresh_time_fails_without_an_invalid_plan():
    body, road = driving_problem(common=None, late=True, new_seconds=20)
    value = initial(body, road)
    with pytest.raises(ApiError) as exc:
        validate_driving(body, road, value, [], cancelled=lambda: False, control=lambda: None,
                         repair_control=lambda: None, progress=lambda value: None)
    assert exc.value.code == 'DEEP_NO_FEASIBLE_PLAN'
    assert not road.force_refresh and road.allow_historical


def test_frozen_batch_rejects_a_resealed_fake_provider_receipt():
    body, road = driving_problem()
    data = normalize(body)
    collector = DrivingCollector(road, lambda: False, lambda: None)
    try:
        value = replay(data, {'routes': [{'vehicle_id': 'V0', 'task_ids': ['0']}], 'common_ids': ['1', '2']}, collector)
        assert collector.frozen.verify(data, value, int(time.time()))
        segment = value['routes'][0]['segments'][0]
        segment['provider_evidence_hash'] = 'fake'
        segment['evidence_hash'] = content_hash({k: v for k, v in segment.items() if k != 'evidence_hash'})
        assert {v['code'] for v in check_result(data, value)['issues']} == {'DRIVING_REFERENCE'}
        with pytest.raises(ApiError) as exc:
            collector.frozen.verify(data, value, int(time.time()))
        assert exc.value.code == 'DEEP_ROAD_RECEIPT'
    finally:
        collector.close()


def test_optional_repair_timeout_still_keeps_already_measured_prefix_fallback():
    body, road = driving_problem(late=True, new_seconds=10)
    value = initial(body, road)
    def no_optional_budget():
        raise StopSearch('TIME_BUDGET')
    result, final = validate_driving(body, road, value, [], cancelled=lambda: False, control=lambda: None,
        repair_control=no_optional_budget, progress=lambda value: None, selection_seconds=0)
    assert complete(normalize(body), result)
    assert [r['task_id'] for r in result['common']] == ['2']
    assert final['selected_subset_of_collected']


def test_other_complete_round_can_become_cheaper_after_actual_road_refresh():
    body, road = driving_problem()
    value = initial(body, road)
    def new_meters(a, b):
        if a['id'] == 'W':
            return 10000
        return 50000 if (a['id'], b['id']) == ('0', '1') else 1000
    road.new_meters = new_meters
    alternative = {'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '2', '1']}], 'common_ids': []}
    result, final = validate_driving(body, road, value, [], cancelled=lambda: False, control=lambda: None,
        repair_control=lambda: None, progress=lambda value: None, selection_seconds=.1,
        complete_recipes=[alternative], max_bridge_edges=20)
    assert result['total_fee'] == '12.00'
    assert result['routes'][0]['task_ids'] == ['0', '2', '1']
    assert final['whole_plan_comparisons'] == 1
    assert final['actual_calls'] == 5 and final['selected_segments'] == 3
    assert all(count == 1 for count in road.counts.values())


def test_progress_never_calls_partial_unassigned_amount_a_complete_candidate_fee():
    body, road = problem(n=3, cars=0, common=None)
    updates = []
    with pytest.raises(ApiError) as exc:
        optimize_deep(body, road, .1, progress=updates.append)
    assert exc.value.code == 'DEEP_NO_FEASIBLE_PLAN'
    assert updates and all(row.get('best_fee') is None for row in updates)
