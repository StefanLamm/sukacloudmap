"""Independent synthetic-road counterexamples for joint-mode ALNS.

These fixtures are explicitly non-geographic. Their exact costs are supplied
by the road adapter; coordinate hints deliberately cannot quote a delivery.
"""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.road_service import evidence, road_request
from app.route_neighborhood import AdaptiveNeighborhood, _LocalBudget, improve_neighborhood
from app.route_search import JointSearch
from app.routing_engine import RoadError, StopSearch, assemble, check_result, evaluate_route, normalize


class MatrixRoad:
    def __init__(self, costs):
        self.costs = costs
        self.reads = 0

    def known_segment(self, a, b):
        value = self.costs.get((a['id'], b['id']))
        return None if value is None else {'meters': value, 'seconds': max(1, value // 100)}

    def segment(self, vehicle, a, b, at):
        row = self.known_segment(a, b)
        if row is None:
            raise RoadError('ROAD_CALL_BUDGET', 'Unknown synthetic edge')
        self.reads += 1
        return evidence(road_request(vehicle, a, b, at, 'FIXTURE'), row['meters'], row['seconds'], 'FIXTURE')

    def stats(self):
        return {'calls': 0, 'fixture_segments': self.reads}


def problem(n=9, cars=4, capacity=9, common='100'):
    ids = [str(i) for i in range(n)]
    tasks = [{'id': i, 'store_id': i, 'warehouse_id': 'W', 'pieces': 1,
              'quote_id': 'Q', 'common_unit_price': common,
              # Deliberately unhelpful coordinates: tested prices must come
              # from the directed matrix rather than guessed geography.
              'longitude': 0, 'latitude': 0,
              'open_at': '2026-11-13T06:00:00+08:00',
              'close_at': '2026-11-13T23:59:00+08:00',
              'unload_minutes': 0, 'latest_arrival_at': None} for i in ids]
    body = {'source': 'FIXTURE', 'delivery_date': '2026-11-13',
            'warehouses': [{'id': 'W', 'longitude': 0, 'latitude': 0,
                            'earliest_departure_at': '2026-11-13T06:00:00+08:00'}],
            'vehicles': [{'id': f'V{i}', 'warehouse_id': 'W', 'max_pieces': capacity,
                          'available': True} for i in range(cars)],
            'quotes': {'Q': {'mode': 'KM', 'per_km': '1'}}, 'tasks': tasks}
    costs = {('W', i): 10000 for i in ids}
    costs.update({(a, b): (2000 if int(a) // 3 == int(b) // 3 else 100000)
                  for a in ids for b in ids if a != b})
    return body, MatrixRoad(costs)


def host_for(body, road, routes=(), seconds=60, seed=1):
    data = normalize(body)
    full = [evaluate_route(data, vid, ids, road) for vid, ids in routes]
    covered = {i for r in full for i in r['task_ids']}
    common = {i for i, row in data['tasks'].items() if row['common_unit_price'] is not None}
    initial = assemble(data, full, common - covered, set(data['tasks']) - common - covered)
    host = JointSearch(data, road, seconds, seed, lambda: False, initial)
    host.setup_fleet()
    return host


def assert_real_matrix_accounting(host):
    result = host.best
    fee = Decimal(0)
    assigned = []
    for route in result['routes']:
        assert len(route['segments']) == len(route['task_ids'])
        assert route['pieces'] <= host.data['vehicles'][route['vehicle_id']]['max_pieces']
        previous = host.data['vehicles'][route['vehicle_id']]['warehouse_id']
        meters = 0
        for task_id, segment in zip(route['task_ids'], route['segments']):
            assert segment['request']['vehicle_id'] == route['vehicle_id']
            assert segment['request']['from_id'] == previous
            assert segment['request']['to_id'] == task_id
            meters += host.road.costs[(previous, task_id)]
            previous = task_id
        assert route['meters'] == meters
        assert Decimal(route['fee']) == Decimal(meters) / 1000
        fee += Decimal(route['fee'])
        assigned.extend(route['task_ids'])
    for common in result['common']:
        task = host.tasks[common['task_id']]
        fee += Decimal(task['common_unit_price']) * task['pieces']
        assigned.append(task['id'])
    assert len(assigned) == len(set(assigned)) == len(host.tasks)
    assert Decimal(result['total_fee']) == fee
    assert check_result(host.data, result)['valid']


def test_alns_uses_more_cars_when_fewer_loaded_cars_cost_more():
    body, road = problem()
    host = host_for(body, road, [('V0', [str(i) for i in range(9)])])
    assert host.best['total_fee'] == '222.00'
    diagnostic = improve_neighborhood(host, seconds=20, iteration_limit=12, seed=1)
    # Three disconnected clusters need a 10 km trunk and 2+2 km each; 42 is
    # the independently known bound, despite initial one-car 100% utilization.
    assert host.best['total_fee'] == '42.00'
    assert len(host.best['routes']) == 3
    assert diagnostic['incumbent_improvements'] > 0
    assert not diagnostic['optimality_proven']
    assert diagnostic['route_evidence_policy'] == 'HOST_EVALUATE_AND_INDEPENDENT_CHECK'
    assert_real_matrix_accounting(host)


def test_trunk_repair_finds_profitable_batch_even_when_each_singleton_loses():
    body, road = problem(n=6, cars=2, capacity=6, common='20')
    for key in road.costs:
        road.costs[key] = 60000 if key[0] == 'W' else 1000
    host = host_for(body, road)
    assert host.best['total_fee'] == '120.00'
    worker = AdaptiveNeighborhood(host, seconds=20, iteration_limit=1, seed=5)
    worker.DESTROY = ('related',)
    worker.REPAIR = ('trunk',)
    worker.removal_count = lambda: 6
    diagnostic = worker.run()
    # No dedicated singleton pays (60 > 20), but six share a trunk (65 < 120).
    assert host.best['total_fee'] == '65.00'
    assert len(host.best['routes']) == 1
    assert host.best['common'] == []
    assert diagnostic['operator_attempts']['repair/trunk'] == 1
    assert_real_matrix_accounting(host)


def test_merge_reduces_vehicle_count_when_shared_trunks_are_cheaper():
    body, road = problem(n=6, cars=6, capacity=3)
    initial = [(f'V{i}', [str(i)]) for i in range(6)]
    host = host_for(body, road, initial)
    assert host.best['total_fee'] == '60.00'
    improve_neighborhood(host, seconds=20, iteration_limit=8, seed=8)
    assert host.best['total_fee'] == '28.00'
    assert len(host.best['routes']) == 2
    assert all(r['pieces'] == 3 for r in host.best['routes'])
    assert_real_matrix_accounting(host)


def test_unknown_roads_never_become_quotes_from_coordinate_hints():
    body, road = problem(n=3, cars=2, capacity=3)
    road.costs = {('W', '0'): 10000, ('0', '1'): 2000, ('1', '2'): 2000}
    host = host_for(body, road, [('V0', ['0', '1', '2'])])
    before = deepcopy(host.best)
    improve_neighborhood(host, seconds=10, iteration_limit=6, seed=4)
    assert host.best['total_fee'] == before['total_fee'] == '14.00'
    assert host.best['unassigned'] == []
    assert_real_matrix_accounting(host)


def test_local_zero_budget_keeps_incumbent_without_evaluating_roads():
    body, road = problem()
    host = host_for(body, road, [('V0', [str(i) for i in range(9)])])
    before, reads = deepcopy(host.best), road.reads
    diagnostic = improve_neighborhood(host, seconds=0, iteration_limit=10)
    assert host.best == before and road.reads == reads
    assert diagnostic['stop_reason'] == 'NEIGHBORHOOD_TIME_BUDGET'


def test_cancellation_propagates_and_keeps_checked_incumbent():
    body, road = problem()
    host = host_for(body, road, [('V0', [str(i) for i in range(9)])])
    before, reads = deepcopy(host.best), road.reads
    host.cancelled = lambda: True
    with pytest.raises(StopSearch) as caught:
        improve_neighborhood(host, seconds=10)
    assert caught.value.reason == 'CANCELLED'
    assert host.best == before and road.reads == reads


def test_fixed_seed_and_iteration_count_are_reproducible():
    results = []
    for _ in range(2):
        body, road = problem(n=6, cars=3, capacity=3)
        host = host_for(body, road)
        diagnostic = improve_neighborhood(host, seconds=30, iteration_limit=5, seed=81)
        results.append((host.best['total_fee'], [(r['vehicle_id'], r['task_ids']) for r in host.best['routes']],
                        diagnostic['operator_attempts']))
    assert results[0] == results[1]


def test_long_run_progress_history_remains_bounded():
    body, road = problem(n=1, cars=1)
    host = host_for(body, road)
    worker = AdaptiveNeighborhood(host, seconds=30, iteration_limit=0)
    for _ in range(1200):
        worker._record('progress')
    assert len(worker.curve) <= 512
    assert worker.curve[0]['kind'] == 'initial'


def test_dedicated_time_windows_remain_hard_but_common_does_not_have_arrival():
    body, road = problem(n=3, cars=2, capacity=3)
    body['tasks'][2]['close_at'] = '2026-11-13T06:00:00+08:00'
    body['tasks'][2]['latest_arrival_at'] = '2026-11-13T06:00:00+08:00'
    host = host_for(body, road)
    improve_neighborhood(host, seconds=20, iteration_limit=6, seed=30)
    assert host.best['total_fee'] == '112.00'
    assert [r['task_id'] for r in host.best['common']] == ['2']
    assert host.best['common'][0]['arrival_at'] is None
    assert not host.best['common'][0]['arrival_time_required']
    assert_real_matrix_accounting(host)


def test_no_eligible_dedicated_jobs_does_not_spin_for_entire_long_budget():
    body, road = problem()
    for row in body['tasks']:
        row['quote_id'] = None
    host = host_for(body, road)
    diagnostic = improve_neighborhood(host, seconds=24000)
    assert diagnostic['iterations'] == 0
    assert diagnostic['stop_reason'] == 'NO_ELIGIBLE_DEDICATED_TASKS'
    assert host.best['total_fee'] == '900.00'


def test_local_budget_uses_host_compute_clock_without_hiding_wall_elapsed(monkeypatch):
    import app.route_neighborhood as module
    body, road = problem(n=3, cars=2)
    host = host_for(body, road, seconds=1000)
    wall, compute = [host.started], [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: wall[0])
    host.compute_elapsed = lambda: compute[0]
    worker = AdaptiveNeighborhood(host, seconds=1, iteration_limit=0)
    wall[0] += 20  # e.g. provider I/O excluded by the host's compute clock
    worker.tick()
    diagnostics = worker.run()
    assert diagnostics['elapsed_seconds'] == 20
    assert diagnostics['compute_elapsed_seconds'] == 0
    compute[0] = 1.01
    with pytest.raises(_LocalBudget):
        worker.tick()
