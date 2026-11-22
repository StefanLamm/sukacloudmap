"""Daily default stages enter real orchestration without native search or HTTP."""
from copy import deepcopy

import pytest

from app.road_service import DrivingRoad, TruckRoad
from app.route_deep import optimize_deep
from app.route_pyvrp_tail import _measure
from app.route_verification import complete
from app.routing_engine import normalize
from test_pyvrp_receipt_index import raw
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
        'generation_selection_loop': expect_cycles, 'pyvrp_group_generation': budget < 600,
        'pyvrp_indexed_current_receipts': False,
    }
    stages = {row['stage']: row for row in result['algorithm']['stages']}
    seed = stages['CURRENT_INPUT_COLD_SEED']['details']
    assert seed['seed_generator_policy'] == 'LONG_TRUNK_MACROS'
    assert seed['fragment_merging_skipped_reason'] == 'SWITCH_DISABLED'
    assert not seed['seed_accepted'] and seed['seed_complete']
    assert not stages['CRITICAL_EDGE_COLLECTION']['details']['skipped']
    assert stages['JOINT_REOPTIMIZATION']['details']['stop_reason'] == 'SWITCH_DISABLED'
    assert stages['JOINT_REOPTIMIZATION']['details']['skipped']
    assert stages['PYVRP_GROUP_GENERATION']['details']['stop_reason'] == (
        'UNSUPPORTED_ROAD' if budget < 600 else 'SWITCH_DISABLED')
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
                'generation_selection_loop', 'quote_group_rotation', 'gain_time_allocation',
                'pyvrp_group_generation', 'pyvrp_indexed_current_receipts'):
        assert actual[key] is flags.get(key, False)
    assert actual['diagnostics'] is True


@pytest.mark.parametrize('budget,mode', [(15, 'skip'), (300, 'better'), (2000, 'better'),
                                         (300, 'worse'), (300, 'cancelled'), (300, 'cancel_after_checked')])
def test_pyvrp_group_stage_at_the_ordinary_deep_entry(monkeypatch, budget, mode):
    body, road = problem(n=9, cars=3, capacity=9)
    incumbent = plan(body, road, [('V0', ['0'])])
    cheaper = plan(body, road, [('V0', ['0', '1'])])
    assert complete(normalize(body), cheaper)
    assert float(cheaper['total_fee']) < float(incumbent['total_fee'])
    stub_rounds(monkeypatch, [incumbent])
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, baseline, *args: (incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}, []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, baseline, *args: (baseline, {'elapsed_seconds': 0., 'candidate_count': 0}))
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(incumbent))
    events = []
    stopped = False

    def refine(current_body, current_road, current_best, *, seconds, seed, control, cancelled):
        nonlocal stopped
        assert current_body is body and current_road is road and seconds == 20.
        assert current_best['total_fee'] == incumbent['total_fee'] and seed == 42 and not cancelled()
        control()
        events.append('refined')
        if mode == 'cancelled':
            stopped = True
            control()
            raise AssertionError('The parent must observe cancellation.')
        if mode == 'cancel_after_checked':
            stopped = True
        result = plan(body, road) if mode == 'worse' else cheaper
        return result, {'skipped': False, 'stop_reason': 'TEST_RETURNED', 'candidate_count': 3}

    monkeypatch.setattr('app.route_pyvrp_tail.improve', refine)
    result = optimize_deep(body, road, budget, seed=42, cancelled=lambda: stopped,
                           experiments={'pyvrp_group_generation': True})
    stage = next(row for row in result['algorithm']['stages'] if row['stage'] == 'PYVRP_GROUP_GENERATION')
    assert complete(normalize(body), result)
    assert not result['algorithm']['experiments']['pyvrp_indexed_current_receipts']
    if mode == 'skip':
        assert events == [] and stage['details']['stop_reason'] == 'INSUFFICIENT_PHASE_BUDGET'
    else:
        assert events == ['refined']
    if mode in {'better', 'cancel_after_checked'}:
        assert stage['after_fee'] == result['total_fee'] == cheaper['total_fee']
        assert result['algorithm']['candidate_count'] >= 3
    else:
        assert stage['after_fee'] == result['total_fee'] == incumbent['total_fee']
    if mode in {'cancelled', 'cancel_after_checked'}:
        assert stage['details']['stop_reason'] == result['algorithm']['stop_reason'] == 'CANCELLED'


@pytest.mark.parametrize('mode,budget,phase_enabled,index_enabled', [
    ('daily_driving', 300, True, True),
    ('daily_driving', 599, True, True),
    ('daily_driving', 600, False, False),
    ('daily_driving', 2000, False, False),
    ('explicit_off', 300, True, False),
    ('explicit_on', 600, True, True),
    ('explicit_on', 2000, True, True),
    ('index_only', 2000, False, True),
    ('research', 300, True, False),
    ('disabled_roads', 300, True, False),
    ('truck', 300, True, False),
    ('truck', 2000, False, False),
])
def test_daily_receipt_index_is_bound_before_the_first_fresh_driving_seal(
        settings, monkeypatch, mode, budget, phase_enabled, index_enabled):
    body, matrix = problem(n=2, cars=1, capacity=2)
    for index, point in enumerate(body['warehouses'] + body['tasks']):
        point.update(longitude=118. + index / 100, latitude=31., crs='GCJ02')
    incumbent = plan(body, matrix)
    settings.planning_route_mode = 'TRUCK' if mode == 'truck' else 'DRIVING'
    settings.driving_live_calls_enabled = mode != 'disabled_roads'
    settings.truck_live_calls_enabled = mode == 'truck'
    adapter = TruckRoad if mode == 'truck' else DrivingRoad
    road = adapter(settings, production_planning=mode != 'research', fresh_only=True)
    events = []

    def forbid(*_args):
        pytest.fail('This test must never ask for provider roads.')

    road.segment = forbid
    monkeypatch.setattr('app.route_edge_discovery.collect_promising_edges', lambda *_args, **_kwargs: {})
    stub_rounds(monkeypatch, [incumbent])
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *_args, **_kwargs: deepcopy(incumbent))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
                        lambda _data, _road, current, *_args: (current, {'elapsed_seconds': 0.}))

    def first_seal(data, current, anchor, *_args):
        assert current.deep_experiments.pyvrp_indexed_current_receipts is index_enabled
        if isinstance(current, DrivingRoad):
            evidence = raw(current, data['warehouses']['W'], data['tasks']['0'], 1000)
            current._seal_batch_receipt(*evidence)
            assert (current.indexed_batch_receipts(lambda: None) is not None) is index_enabled
        events.append('first_seal')
        return anchor, {'elapsed_seconds': 0.}, []

    def inspect_phase(_body, current, anchor, *, control, **_kwargs):
        if isinstance(current, DrivingRoad):
            measured, rows = _measure(current, normalize(body), fresh=True, control=control)
            assert measured.indexed_current_receipts is index_enabled
            assert rows == [(None, '0', 1000, 5)]
        else:
            assert not hasattr(current, 'indexed_batch_receipts')
        events.append('pyvrp')
        return anchor, {'stop_reason': 'TESTED', 'candidate_count': 0}

    monkeypatch.setattr('app.route_deep._pre_selection_macros', first_seal)
    monkeypatch.setattr('app.route_pyvrp_tail.improve', inspect_phase)
    flags = None
    if mode in {'explicit_off', 'explicit_on', 'index_only'}:
        flags = {'pyvrp_group_generation': phase_enabled, 'pyvrp_indexed_current_receipts': index_enabled}
    result = optimize_deep(body, road, budget, experiments=flags)
    assert events == ['first_seal'] + (['pyvrp'] if phase_enabled else [])
    assert result['algorithm']['experiments']['pyvrp_group_generation'] is phase_enabled
    assert result['algorithm']['experiments']['pyvrp_indexed_current_receipts'] is index_enabled
    assert result['total_fee'] == incumbent['total_fee'] and road.calls == 0
    assert not hasattr(road, 'deep_experiments')
