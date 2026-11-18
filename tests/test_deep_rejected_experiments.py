"""Withdraw failed experiments without losing priced/cancelled-run evidence."""
from dataclasses import fields

import pytest

from app.route_deep import _Budget, _optional_refinement, baseline_result, optimize_deep
from app.route_experiments import DeepExperiments
from app.route_retention import replay_frozen_stream
from app.routing_engine import StopSearch, normalize
from test_route_columns import problem, plan
from test_route_deep import stub_rounds


def test_removed_experiments_and_economic_policy_are_not_production_options():
    names = {item.name for item in fields(DeepExperiments)}
    assert not names & {'native_continuous_search', 'activation_prizes'}
    with pytest.raises(ValueError):
        DeepExperiments(candidate_retention='economic_diversity')
    with pytest.raises(ValueError):
        replay_frozen_stream([], policies=('economic_diversity',))


def test_priced_capture_remains_diagnostic_in_existing_three_retention_policies():
    identity = {'vehicle_id': 'V0', 'task_ids': ['0']}
    events = [{'kind': 'priced_candidate', **identity, 'fee': '1.00'}, identity]
    replay = replay_frozen_stream(events)
    assert set(replay['policies']) == {'first', 'batch_dedup', 'diversity'}
    assert replay['stream_observations'] == 1
    assert all(row['candidates'] == [identity] for row in replay['policies'].values())


def test_incomplete_incumbent_records_its_actual_skip_reason():
    body, road = problem(n=4, common=None)
    data = normalize(body)
    result, details = _optional_refinement(data, road, baseline_result(data), _Budget(road, 10, lambda: False),
                                          0, lambda: False, 'joint')
    assert result['unassigned']
    assert details['skipped'] and details['stop_reason'] == 'NO_COMPLETE_INCUMBENT'


@pytest.mark.parametrize('method,module,function,attribute', [
    ('joint', 'app.route_joint_reoptimization', 'reoptimize_joint_routes', 'joint_reoptimization_diagnostics'),
    ('cycles', 'app.route_adaptive_cycles', 'expand_candidate_cycles', 'candidate_expansion_diagnostics'),
])
@pytest.mark.parametrize('reason', ['CANCELLED', 'WALL_BUDGET'])
def test_optional_global_stop_keeps_finalized_nested_diagnostics_without_resuming(
        monkeypatch, method, module, function, attribute, reason):
    body, road = problem(n=4)
    data = normalize(body)
    initial = plan(body, road)
    road.deep_experiments = DeepExperiments()
    records = {'cycles': [{'routing': {'assignment_pricing': {'records': [{'actual_fee': '9.00'}]}}}]}
    def stop(search, **kwargs):
        exc = StopSearch(reason)
        setattr(exc, attribute, records)
        try:
            raise exc
        finally:
            records['finalized'] = True
    monkeypatch.setattr(module + '.' + function, stop)
    with pytest.raises(StopSearch) as caught:
        _optional_refinement(data, road, initial, _Budget(road, 10, lambda: False),
                             0, lambda: False, method)
    details = caught.value.optional_refinement_diagnostics
    assert details['finalized'] and details['cycles'] is records['cycles']
    assert not details['skipped'] and details['stop_reason'] == reason
    assert details['after_fee'] == initial['total_fee'] and details['fee_reduction'] == '0.00'


def test_entered_refinement_global_stop_is_logged_and_cannot_run_following_phase(monkeypatch):
    body, road = problem(n=4)
    initial = plan(body, road)
    stub_rounds(monkeypatch, [initial])
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}, []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0., 'candidate_count': 0}))
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: initial)
    entered = []
    def stopped(data, road, incumbent, *args, **kwargs):
        entered.append(args[3])
        exc = StopSearch('CANCELLED')
        exc.optional_refinement_diagnostics = {'elapsed_seconds': .02, 'candidate_count': 3,
            'skipped': False, 'stop_reason': 'CANCELLED', 'cycles': [{'routing': {'attempted': True}}]}
        raise exc
    monkeypatch.setattr('app.route_deep._optional_refinement', stopped)
    result = optimize_deep(body, road, 10, initial_result=initial,
        experiments={'joint_reoptimization': True, 'generation_selection_loop': True})
    assert entered == ['joint']
    algorithm = result['algorithm']
    assert algorithm['stop_reason'] == 'CANCELLED'
    joint = next(row for row in algorithm['stages'] if row['stage'] == 'JOINT_REOPTIMIZATION')
    assert joint['details']['cycles'][0]['routing']['attempted']
    assert joint['details']['stop_reason'] == 'CANCELLED' and not joint['details']['skipped']
    expansion = next(row for row in algorithm['stages'] if row['stage'] == 'CANDIDATE_EXPANSION')
    assert expansion['details']['stop_reason'] == 'NOT_REACHED_CANCELLED'
