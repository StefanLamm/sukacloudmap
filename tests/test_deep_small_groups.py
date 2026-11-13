"""A single optional small-group phase must preserve the daily search budget."""
from copy import deepcopy

import pytest

from app.route_deep import _Budget, _post_selection_small_groups, optimize_deep
from app.route_verification import complete
from app.routing_engine import StopSearch, normalize
from test_route_columns import plan, problem
from test_route_deep import driving_problem, initial, stub_rounds


def split_problem():
    body, road = problem(n=4, cars=2)
    road.matrix['1', '2']['meters'] = 1000
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    assert value['total_fee'] == '22.00'
    return body, road, value


def test_post_selection_phase_improves_a_complete_incumbent_once():
    body, road, value = split_problem()
    original = deepcopy(value)
    observed = []
    road.candidate_observer = observed.append
    result, details = _post_selection_small_groups(normalize(body), road, value,
        _Budget(road, 20, lambda: False), 7, lambda: False)
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert value == original and observed
    assert details['budget_seconds'] == 1
    assert details['new_network_calls'] == 0 and not details['skipped']
    assert len(details['methods']) <= 2


@pytest.mark.parametrize('spent', [16.6, 16.59, 19.])
def test_final_seventeen_percent_cannot_be_spent_on_small_groups(monkeypatch, spent):
    body, road, value = split_problem()
    budget = _Budget(road, 20, lambda: False)
    monkeypatch.setattr(budget, 'compute', lambda: spent)
    def forbidden(*args, **kwargs):
        raise AssertionError('No unused pre-validation budget')
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', forbidden)
    result, details = _post_selection_small_groups(normalize(body), road, value, budget, 7, lambda: False)
    assert result is value
    assert details['skipped'] and details['stop_reason'] == 'NO_UNUSED_SELECTION_BUDGET'


def test_missing_metrics_do_not_fetch_unknown_roads_or_lose_incumbent():
    body, road, value = split_problem()
    road.known_segment = lambda a, b: None
    def forbidden(*args, **kwargs):
        raise AssertionError('Unknown directions must not be requested')
    road.segment = forbidden
    result, details = _post_selection_small_groups(normalize(body), road, value,
        _Budget(road, 20, lambda: False), 7, lambda: False)
    assert result is value and details['new_network_calls'] == 0


def test_tiny_unused_budget_cannot_start_the_native_selectors_minimum_slice(monkeypatch):
    body, road, value = split_problem()
    budget = _Budget(road, 20, lambda: False)
    monkeypatch.setattr(budget, 'compute', lambda: 16.54)
    def forbidden(search):
        raise AssertionError('Native 50 ms minimum needs reserved selection time')
    monkeypatch.setattr('app.route_search.JointSearch.pack', forbidden)
    def seed(search):
        search.evaluations += 1
        search.pack()
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', seed)
    result, details = _post_selection_small_groups(normalize(body), road, value, budget, 7, lambda: False)
    assert result is value and details['budget_seconds'] == pytest.approx(.06)


@pytest.mark.parametrize('reason', ['TIME_BUDGET', 'CANDIDATE_BUDGET'])
def test_local_search_limits_keep_the_complete_incumbent(monkeypatch, reason):
    body, road, value = split_problem()
    def stop(search):
        raise StopSearch(reason)
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', stop)
    result, details = _post_selection_small_groups(normalize(body), road, value,
        _Budget(road, 20, lambda: False), 7, lambda: False)
    assert result is value and details['stop_reason'] == reason


@pytest.mark.parametrize('reason', ['TIME_BUDGET', 'CANCELLED'])
def test_driving_call_limit_is_capped_and_restored_even_when_interrupted(monkeypatch, reason):
    body, road = driving_problem()
    value = initial(body, road)
    road.call_limit = 100
    road.force_refresh = True
    def stop(search):
        assert search.road.call_limit == road.calls == 0
        raise StopSearch(reason)
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', stop)
    if reason == 'CANCELLED':
        with pytest.raises(StopSearch) as exc:
            _post_selection_small_groups(normalize(body), road, value,
                _Budget(road, 20, lambda: False), 7, lambda: False)
        assert exc.value.reason == 'CANCELLED'
    else:
        result, details = _post_selection_small_groups(normalize(body), road, value,
            _Budget(road, 20, lambda: False), 7, lambda: False)
        assert result is value and details['new_network_calls'] == 0
    assert road.call_limit == 100 and road.force_refresh and road.calls == 0


def test_daily_runs_original_multi_start_then_pool_then_one_small_phase(monkeypatch):
    body, road, value = split_problem()
    seen = stub_rounds(monkeypatch, [value])
    events = []
    def select(*args, **kwargs):
        events.append('pool')
        return deepcopy(value)
    monkeypatch.setattr('app.route_columns.select_portfolio', select)
    import app.route_small_groups as small
    real_seed = small.seed_small_groups
    def seed(search):
        events.append('small')
        return real_seed(search)
    monkeypatch.setattr(small, 'seed_small_groups', seed)
    result = optimize_deep(body, road, 20, seed=3)
    assert events == ['pool', 'small']
    assert len(seen) == 4 and seen[0]['seconds'] == pytest.approx(20 * .64 / 4, abs=.05)
    assert result['total_fee'] == '13.00'
    stage = [s for s in result['algorithm']['stages'] if s['stage'] == 'SMALL_GROUP_INTENSIFICATION']
    assert len(stage) == 1 and stage[0]['details']['new_network_calls'] == 0
    assert result['algorithm']['complete_round_allocations'] >= 2


def test_full_observation_pool_cannot_discard_the_new_complete_plan(monkeypatch):
    body, road, value = split_problem()
    filled = [False]
    prior_observations = []
    prior = prior_observations.append
    road.candidate_observer = prior
    def run(*args, **kwargs):
        if not filled[0]:
            # Unique opaque proposal identities fill the ordinary observation
            # pool; the stub selector deliberately ignores those invalid cars.
            for n in range(10000):
                road.candidate_observer({'vehicle_id': 'unused-' + str(n), 'task_ids': ['0']})
            filled[0] = True
        result = deepcopy(value)
        result['algorithm'] = {'candidate_count': 1}
        return result
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, run)
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(value))
    result = optimize_deep(body, road, 20)
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert result['algorithm']['distinct_route_count'] == 10000
    assert result['algorithm']['dropped_candidate_observations'] > 0
    assert result['algorithm']['complete_round_allocations'] >= 2
    assert road.candidate_observer is prior


def test_cancel_during_optional_phase_retains_prior_complete_plan_and_observer(monkeypatch):
    body, road, value = split_problem()
    stub_rounds(monkeypatch, [value])
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(value))
    prior = lambda spec: None
    road.candidate_observer = prior
    state = {'cancelled': False}
    def cancel(search):
        state['cancelled'] = True
        raise StopSearch('CANCELLED')
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', cancel)
    result = optimize_deep(body, road, 20, cancelled=lambda: state['cancelled'])
    assert complete(normalize(body), result) and result['total_fee'] == value['total_fee']
    assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert road.candidate_observer is prior
