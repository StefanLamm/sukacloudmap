from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.route_deep import _Budget, optimize_deep
from app.route_diagnostics import SearchDiagnostics
from app.route_experiments import DeepExperiments
from app.route_search import JointSearch
from app.routing_engine import normalize
from test_route_neighborhood import problem


def test_compute_and_total_wait_have_separate_deadlines(monkeypatch):
    now = [100.]
    monkeypatch.setattr('app.route_deep.time.monotonic', lambda: now[0])
    road = SimpleNamespace(io_seconds=10.)
    budget = _Budget(road, 5, lambda: False, wall_seconds=8)
    now[0] += 6
    road.io_seconds += 5
    assert budget.elapsed() == 6 and budget.compute() == 1
    budget.control()
    now[0] += 2
    with pytest.raises(Exception, match='WALL_BUDGET'):
        budget.control(compute=False)


def test_diagnostics_measure_first_complete_curve_and_final_cost_without_mutating_plan():
    body, road = problem(n=3, cars=1)
    result = optimize_deep(body, road, .3, experiments={'diagnostics': True})
    diag = result['algorithm']['diagnostics']
    assert diag['first_complete']['total_fee'] == '300.00'
    assert diag['first_complete_elapsed_seconds'] >= 0
    fees = [float(row['total_fee']) for row in diag['fee_curve']]
    assert fees == sorted(fees, reverse=True)
    assert diag['final_road_fee_change']['after_fee'] == result['total_fee']
    assert not hasattr(road, 'search_diagnostics') and not hasattr(road, 'deep_experiments')


def test_missing_edge_and_rejection_are_distinct_from_legal_common_baseline():
    body, road = problem(n=2, cars=1)
    road.costs.pop(('W', '0'))
    budget = _Budget(road, 2, lambda: False)
    diag = SearchDiagnostics(budget, 2)
    road.search_diagnostics = diag
    search = JointSearch(normalize(body), road, 1, 0, lambda: False)
    assert search.evaluate('V0', ['0']) is None
    assert search.evaluate('V0', ['0']) is None
    assert diag.counts['missing_metric_lookups'] == 1
    assert diag.counts['rejected_candidates'] == 1
    assert diag.counts['evaluation_cache_hits'] == 1
    assert diag.first_complete['total_fee'] == '200.00'
    assert diag.missing_edges[('W', '0')] == 1


def test_subproblem_prices_cannot_pollute_whole_order_curve():
    body, road = problem(n=2)
    data = normalize(body)
    diag = SearchDiagnostics(_Budget(road, 2, lambda: False), 2)
    road.search_diagnostics = diag
    small = {**data, 'tasks': {k: v for k, v in data['tasks'].items() if k == '0'}}
    JointSearch(small, road, 1, 0, lambda: False)
    assert diag.first_complete is None


def test_per_job_switches_are_validated_and_existing_hooks_are_restored():
    with pytest.raises(ValueError):
        DeepExperiments(candidate_retention='unknown')
    with pytest.raises(ValueError):
        DeepExperiments(joint_reoptimization='false')
    body, road = problem(n=2)
    old = object()
    road.search_diagnostics = old
    result = optimize_deep(body, road, .1, experiments={'diagnostics': False})
    assert result['algorithm']['diagnostics'] == {'enabled': False}
    assert road.search_diagnostics is old


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_invalid_wall_deadline_is_rejected(value):
    with pytest.raises(ValueError):
        _Budget(SimpleNamespace(), 1, lambda: False, wall_seconds=value)


def test_large_production_profile_preserves_existing_progress_stages_and_can_be_disabled(monkeypatch):
    from test_route_deep import initial, stub_rounds
    body, road = problem(n=9, cars=3)
    data = normalize(body)
    from app.routing_engine import assemble, evaluate_route
    value = assemble(data, [evaluate_route(data, f'V{n}', [str(i) for i in range(n * 3, n * 3 + 3)], road)
                            for n in range(3)], [], [])
    stub_rounds(monkeypatch, [value])
    def unchanged(data, road, incumbent, *args):
        return incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, incumbent, *args: (*unchanged(data, road, value), []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups', unchanged)
    methods = []
    def refinement(data, road, incumbent, budget, seed, cancelled, method, **kwargs):
        methods.append(method)
        return incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}
    monkeypatch.setattr('app.route_deep._optional_refinement', refinement)
    progress = []
    result = optimize_deep(body, road, 600, initial_result=value, progress=progress.append)
    assert result['check']['valid'] and result['total_fee'] == value['total_fee']
    assert methods == ['cycles']
    expansion = [row for row in progress if row.get('optimization_phase') == 'CANDIDATE_EXPANSION']
    assert expansion and all(row['stage'] == 'COLUMN_SELECTION' for row in expansion)
    assert progress[-1]['stage'] == 'COMPLETE' and 'optimization_phase' not in progress[-1]
    methods.clear()
    result = optimize_deep(body, road, 600, initial_result=value, experiments={})
    assert result['check']['valid'] and methods == []
