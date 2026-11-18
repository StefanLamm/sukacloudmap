"""Pending policy regressions; evaluator doubles, no native solver or real HTTP.

These verify continuation beyond obsolete evaluated-count caps and time/cancel
bounds. They are not cost-effectiveness or exact-optimization benchmarks.
"""
from types import SimpleNamespace

import pytest

from app.routing_engine import StopSearch, check_result, normalize, optimize_legacy, optimize_manual
from app.search_policy import production_search_control
from test_route_columns import plan
from test_route_ortools import problem


def production_fixture(production=True):
    body, road, _ = problem(n=8, cars=1, capacity=8)
    clock = [0.]
    road.production_planning = production
    road.wall_deadline = 1000.
    road.timer = lambda: clock[0]
    road.cancelled = lambda: False
    return body, road, clock


def test_production_control_requires_finite_absolute_deadline_and_research_is_unchanged():
    original = lambda: None
    assert production_search_control(SimpleNamespace(production_planning=False), original) is original
    for deadline in (None, float('inf'), float('nan'), True):
        with pytest.raises(ValueError, match='ABSOLUTE_WALL_DEADLINE'):
            production_search_control(SimpleNamespace(production_planning=True, wall_deadline=deadline), original)


def test_production_control_bounds_cached_work_without_disabling_network_off_cache_use():
    clock, stopped, calls = [0.], [False], []
    road = SimpleNamespace(production_planning=True, wall_deadline=5., timer=lambda: clock[0],
                           cancelled=lambda: stopped[0], settings=SimpleNamespace(road_calls_enabled=False))
    control = production_search_control(road, lambda: calls.append('compute_guard'))
    control()
    assert calls == ['compute_guard']
    road.wall_deadline = 999.  # This scope's original cutoff cannot be extended.
    clock[0] = 5.
    with pytest.raises(StopSearch, match='WALL_BUDGET'):
        control()
    stopped[0] = True
    with pytest.raises(StopSearch, match='CANCELLED'):
        control()


@pytest.mark.parametrize('production', [False, True])
def test_manual_order_search_passes_10000_in_production_but_obeys_compute_control(monkeypatch, production):
    import app.routing_engine as engine
    body, road, _ = production_fixture(production)
    initial = plan(body, road, [('V0', list(map(str, range(8))))])
    baseline = initial['routes'][0]
    evaluated = [0]
    def evaluator(data, vehicle_id, ids, current_road, control=None):
        if control:
            control()
        evaluated[0] += 1
        if evaluated[0] == 1:
            return baseline
        # Controlled infeasible proposals never replace the independently
        # feasible incumbent; only the search-count continuation is under test.
        return {**baseline, 'task_ids': list(ids), 'errors': [{'code': 'CONTROLLED_BLOCKED'}]}
    def bounded_compute():
        if evaluated[0] >= 10003:
            raise StopSearch('TIME_BUDGET')
    monkeypatch.setattr(engine, 'evaluate_route', evaluator)
    result = optimize_manual(body, {'routes': [{'vehicle_id': 'V0', 'task_ids': list(map(str, range(8)))}],
                                   'common_ids': [], 'unassigned_ids': []}, road, bounded_compute)
    if production:
        assert evaluated[0] > 10001 and result['algorithm']['stop_reason'] == 'TIME_BUDGET'
    else:
        assert evaluated[0] <= 10001 and result['algorithm']['stop_reason'] == 'CANDIDATE_BUDGET'
    assert result['total_fee'] == initial['total_fee'] and not result['unassigned']
    assert check_result(normalize(body), result)['valid']


@pytest.mark.parametrize('production', [False, True])
def test_manual_auto_allocation_passes_20000_but_keeps_explicit_tickets_on_time_stop(monkeypatch, production):
    import app.auto_dedicated as auto
    body, road, _ = production_fixture(production)
    initial = plan(body, road, [('V0', list(map(str, range(7))))])
    fixed = initial['routes'][0]
    evaluated = [0]
    def evaluator(data, vehicle_id, ids, current_road, control=None):
        if control:
            control()
        evaluated[0] += 1
        return {**fixed, 'task_ids': list(ids), 'errors': [{'code': 'CONTROLLED_BLOCKED'}]}
    def bounded_compute():
        if evaluated[0] >= 20003:
            raise StopSearch('TIME_BUDGET')
    monkeypatch.setattr(auto, 'evaluate_route', evaluator)
    # recalculate resolves its evaluator in routing_engine; leave that genuine
    # initial fixed route/pricing unchanged. Auto proposals use the local double.
    result = auto.allocate_dedicated(body, {'routes': [{'vehicle_id': 'V0', 'task_ids': list(map(str, range(7)))}],
        'common_ids': [], 'unassigned_ids': [], 'auto_dedicated_ids': ['7']}, road, bounded_compute,
        optimize_routes=True)
    if production:
        assert evaluated[0] > 20000 and result['algorithm']['stop_reason'] == 'TIME_BUDGET'
    else:
        assert evaluated[0] <= 20000 and result['algorithm']['stop_reason'] == 'CANDIDATE_BUDGET'
    covered = [task for route in result['routes'] for task in route['task_ids']]
    common = [row['task_id'] for row in result['common']]
    missing = [row['task_id'] for row in result['unassigned']]
    assert set(covered + common + missing) == set(map(str, range(8)))
    assert len(covered + common + missing) == 8
    assert '7' not in common  # Auto-dedicated choice cannot silently become common.


@pytest.mark.parametrize('production', [False, True])
def test_legacy_small_input_can_pass_20000_before_absolute_deadline(monkeypatch, production):
    import app.routing_engine as engine
    body, road, clock = production_fixture(production)
    evaluated = [0]
    def unavailable(data, vehicle_id, ids, current_road, control=None):
        if control:
            control()
        evaluated[0] += 1
        if evaluated[0] == 20003:
            clock[0] = road.wall_deadline
        return {'vehicle_id': vehicle_id, 'task_ids': list(ids), 'errors': [{'code': 'ROAD_UNAVAILABLE',
            'message': 'Controlled absent candidate evidence'}], 'fee': None}
    monkeypatch.setattr(engine, 'evaluate_route', unavailable)
    result = optimize_legacy(body, road, 100)
    if production:
        assert evaluated[0] > 20000 and result['algorithm']['stop_reason'] == 'WALL_BUDGET'
    else:
        assert evaluated[0] <= 20000 and result['algorithm']['stop_reason'] == 'CANDIDATE_BUDGET'
    assert result['total_fee'] == '800.00' and not result['unassigned']
    assert check_result(normalize(body), result)['valid']


@pytest.mark.parametrize('production', [False, True])
def test_explicit_reference_search_control_keeps_research_count_limit_only(production):
    from app.route_baseline import JointSearch
    body, road, clock = production_fixture(production)
    search = JointSearch(normalize(body), road, 100, 0, lambda: False)
    search.evaluations = search.max_evaluations + 1
    if production:
        search.control()
        clock[0] = road.wall_deadline
        with pytest.raises(StopSearch, match='WALL_BUDGET'):
            search.control()
    else:
        with pytest.raises(StopSearch, match='CANDIDATE_BUDGET'):
            search.control()
