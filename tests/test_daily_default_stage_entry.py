"""Daily default stages enter real orchestration without native search or HTTP."""
from copy import deepcopy

import pytest

from app.route_deep import optimize_deep
from app.route_verification import complete
from app.routing_engine import normalize
from test_route_columns import problem, plan
from test_route_deep import stub_rounds


@pytest.mark.parametrize('experiment_argument', ['omitted', 'none'])
@pytest.mark.parametrize('budget,task_count,expect_cycles', [
    (300, 2, False), (300, 9, False), (300, 16, False),
    (599, 9, False), (600, 9, True), (2000, 8, False), (2000, 9, True),
])
def test_daily_default_disables_unadopted_stages_and_keeps_legacy_long_budget_loop(
        monkeypatch, budget, task_count, expect_cycles, experiment_argument):
    # Generic tickets cover small and larger inputs plus both sides of the
    # existing loop thresholds; budgets are values, never actual sleep times.
    body, road = problem(n=task_count, cars=3, capacity=task_count)
    original = deepcopy(body)
    baseline = plan(body, road)
    native_result = plan(body, road, [('V0', ['0'])])
    events = []
    sources = stub_rounds(monkeypatch, [native_result])

    def collect(data, current_road, control, **kwargs):
        control()
        assert len(data['tasks']) == task_count and current_road is road
        events.append('critical_edges')
        return {'skipped': False, 'stop_reason': 'NO_MISSING_EDGES', 'new_network_calls': 0}
    monkeypatch.setattr('app.route_edge_discovery.collect_promising_edges', collect)

    def generate(search, *, enabled, seconds, seed):
        raise AssertionError('Fragment experiments must not run by daily default')
    monkeypatch.setattr('app.route_fragment_merging.generate_fragment_routes', generate)
    def legacy_trunks(search, *, scope):
        search.control()
        assert search.data['tasks'].keys() == normalize(body)['tasks'].keys()
        events.append('legacy_' + scope)
    monkeypatch.setattr('app.route_macro.seed_long_routes', legacy_trunks)
    monkeypatch.setattr('app.route_search.JointSearch.pack', lambda search: search.control())
    monkeypatch.setattr('app.route_columns.select_portfolio',
        lambda body, road, results, **kwargs: deepcopy(results[0]))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}))

    def joint(search, *, scalable):
        raise AssertionError('The withdrawn joint group must not run by daily default')
    monkeypatch.setattr('app.route_joint_reoptimization.reoptimize_joint_routes', joint)

    def cycles(search, *, quote_rotation, selection_loop, gain_allocation):
        search.control()
        assert selection_loop and not quote_rotation and not gain_allocation
        events.append('candidate_expansion')
        return {'skipped': False, 'stop_reason': 'TEST_CYCLES_ENTERED'}
    monkeypatch.setattr('app.route_adaptive_cycles.expand_candidate_cycles', cycles)

    kwargs = {} if experiment_argument == 'omitted' else {'experiments': None}
    result = optimize_deep(body, road, budget, **kwargs)
    assert events == ['critical_edges', 'legacy_common', 'legacy_compatible'] + (
        ['candidate_expansion'] if expect_cycles else [])
    assert body == original and complete(normalize(body), result)
    assert float(result['total_fee']) == task_count * 100 - 90 < float(baseline['total_fee'])
    assert sources and sources[0]['kwargs']['initial_result']['total_fee'] == baseline['total_fee']
    assert result['algorithm']['experiments'] == {
        'diagnostics': True, 'candidate_retention': 'first', 'native_candidate_retention': 'first',
        'complete_assignment_pricing': False, 'fragment_merging': False,
        'joint_reoptimization': False, 'scalable_joint_search': False, 'stage_budget_reservation': False,
        'critical_edge_collection': True, 'quote_group_rotation': False, 'gain_time_allocation': False,
        'generation_selection_loop': expect_cycles,
    }
    stages = {row['stage']: row for row in result['algorithm']['stages']}
    seed = stages['CURRENT_INPUT_COLD_SEED']['details']
    assert seed['seed_generator_policy'] == 'LONG_TRUNK_MACROS'
    assert seed['fragment_merging_skipped_reason'] == 'SWITCH_DISABLED'
    assert not seed['seed_accepted'] and seed['seed_complete']
    assert not stages['CRITICAL_EDGE_COLLECTION']['details']['skipped']
    assert stages['JOINT_REOPTIMIZATION']['details']['stop_reason'] == 'SWITCH_DISABLED'
    assert stages['JOINT_REOPTIMIZATION']['details']['skipped']
    expansion = stages['CANDIDATE_EXPANSION']['details']
    assert expansion['stop_reason'] == ('TEST_CYCLES_ENTERED' if expect_cycles else 'SWITCH_DISABLED')
    assert expansion['skipped'] is (not expect_cycles)


@pytest.mark.parametrize('flags,expect_joint', [
    ({}, False), ({'joint_reoptimization': True}, True),
    ({'joint_reoptimization': True, 'scalable_joint_search': True, 'stage_budget_reservation': True}, True),
    ({'fragment_merging': True}, False), ({'complete_assignment_pricing': True}, False),
])
def test_explicit_experiment_dicts_still_isolate_components(monkeypatch, flags, expect_joint):
    body, road = problem(n=9, cars=3, capacity=9)
    baseline = plan(body, road)
    stub_rounds(monkeypatch, [baseline])
    observed = []
    def seed(data, current_road, incumbent, *args):
        observed.append(vars(current_road.deep_experiments))
        return incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}, []
    monkeypatch.setattr('app.route_deep._pre_selection_macros', seed)
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}))
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(baseline))
    entered = []
    def refinement(data, road, incumbent, budget, seed, cancelled, method, **kwargs):
        entered.append(method)
        return incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}
    monkeypatch.setattr('app.route_deep._optional_refinement', refinement)
    result = optimize_deep(body, road, 2000, experiments=flags)
    assert complete(normalize(body), result) and observed
    assert entered == (['joint'] if expect_joint else [])
    actual = result['algorithm']['experiments']
    assert actual == observed[0]
    for key in ('complete_assignment_pricing', 'fragment_merging', 'joint_reoptimization',
                'scalable_joint_search', 'stage_budget_reservation', 'critical_edge_collection',
                'generation_selection_loop', 'quote_group_rotation', 'gain_time_allocation'):
        assert actual[key] is flags.get(key, False)
    assert actual['diagnostics'] is True
