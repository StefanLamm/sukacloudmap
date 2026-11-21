"""Isolated legacy seed/small-group behavior with newer daily stages disabled."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.route_deep import _Budget, _post_selection_small_groups, optimize_deep
from app.route_verification import complete, recipe
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
    assert len(details['methods']) <= 4
    cleanup = next(row for row in details['methods'] if row['method'] == 'deterministic_cost_cleanup')
    assert Decimal(cleanup['after_fee']) <= Decimal(cleanup['before_fee'])


def test_fast_polish_and_cleanup_release_time_for_late_exact_group_improvement(monkeypatch):
    import app.route_small_groups as small
    body, road = problem(n=4, cars=2)
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    assert value['total_fee'] == '22.00'
    # The cheaper full route is the last four-store permutation. The existing
    # two lines cost 11 each; W->3->2->1->0 costs 10+1+1+1 = 13.
    for pair in [('3', '2'), ('2', '1'), ('1', '0')]:
        road.matrix[pair]['meters'] = 1000
    original = deepcopy(value)
    clock = [0.]
    monkeypatch.setattr('app.route_deep.time.monotonic', lambda: clock[0])

    def fast_polish(search, vehicle_ids):
        clock[0] += .1
        search.control()
    def fast_cleanup(search):
        clock[0] += .025
        search.control()
    monkeypatch.setattr('app.route_cost_cleanup.polish_route_orders', fast_polish)
    monkeypatch.setattr('app.route_cost_cleanup.improve_cost_cleanup', fast_cleanup)
    actual_evaluate = small.evaluate_candidate
    evaluated = []
    def measured_work(*args, **kwargs):
        # Charge deterministic work to each real candidate evaluation, without
        # sleeping or replacing the evaluator/constraint/fee oracle.
        clock[0] += .011
        evaluated.append(tuple(args[2]))
        return actual_evaluate(*args, **kwargs)
    monkeypatch.setattr(small, 'evaluate_candidate', measured_work)
    pack_allowances = []
    def record_pack(search):
        search.control()
        pack_allowances.append(search.remaining())
    monkeypatch.setattr('app.route_search.JointSearch.pack', record_pack)

    result, details = _post_selection_small_groups(normalize(body), road, value,
        _Budget(road, 20, lambda: False), 7, lambda: False)
    stage = next(row for row in details['methods'] if row['method'] == 'small_compatible_groups')
    # A fixed quarter-second slice stops before this late permutation; the
    # recovered allowance reaches it and still preserves native-pack time.
    assert ('3', '2', '1', '0') in evaluated
    assert stage['elapsed_seconds'] > details['budget_seconds'] * .25
    assert stage['stop_reason'] == 'PHASE_BUDGET'
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert result['routes'][0]['task_ids'] == ['3', '2', '1', '0']
    assert pack_allowances and min(pack_allowances) >= .075
    assert details['compute_seconds'] < details['budget_seconds'] == 1
    assert details['new_network_calls'] == 0 and value == original


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
    assert complete(normalize(body), result)
    assert Decimal(result['total_fee']) <= Decimal(value['total_fee'])
    assert details['budget_seconds'] == pytest.approx(.06)


@pytest.mark.parametrize('reason', ['TIME_BUDGET', 'CANDIDATE_BUDGET'])
def test_local_search_limits_keep_the_complete_incumbent_or_checked_improvement(monkeypatch, reason):
    body, road, value = split_problem()
    original = deepcopy(value)
    def stop(search):
        raise StopSearch(reason)
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', stop)
    result, details = _post_selection_small_groups(normalize(body), road, value,
        _Budget(road, 20, lambda: False), 7, lambda: False)
    assert complete(normalize(body), result) and details['stop_reason'] == reason
    assert Decimal(result['total_fee']) <= Decimal(value['total_fee'])
    assert value == original


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


def test_daily_runs_current_input_seed_then_original_rounds_pool_and_one_post_phase(monkeypatch):
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
    progress = []
    result = optimize_deep(body, road, 20, seed=3, initial_result=None, progress=progress.append, experiments={})
    assert events == ['pool', 'small']
    seed = result['algorithm']['stages'][0]
    assert seed['stage'] == 'CURRENT_INPUT_COLD_SEED' and seed['total_fee'] == '13.00'
    assert seed['details']['seed_origin'] == 'CURRENT_INPUT'
    assert seed['details']['seed_complete'] is True and seed['details']['seed_accepted'] is True
    assert seed['details']['seed_fee'] == '13.00'
    assert len(seen) == 4 and seen[0]['kwargs']['initial_result']['total_fee'] == '13.00'
    assert [row['kwargs']['initial_result'] is None for row in seen] == [False, True, False, False]
    assert complete(normalize(body), seen[0]['kwargs']['initial_result'])
    assert seen[0]['seconds'] == pytest.approx((20 * .64 - seed['details']['compute_seconds']) / 4, abs=.05)
    assert result['total_fee'] == '13.00'
    assert result['algorithm']['initial_plan_supplied'] is False
    assert result['algorithm']['initial_plan_accepted'] is False
    assert progress[0]['stage'] == 'MULTI_START' and progress[0]['message'] == '构建初始配送方案'
    assert any(row['round_index'] == 1 and row['message'] == '正在比较不同专车与共配组合' for row in progress)
    assert next(row for row in progress if row['stage'] == 'COLUMN_SELECTION')['message'] == '正在比较配送方案总费用'
    assert progress[-1]['message'] == '配送方案已完成核算'
    stage = [s for s in result['algorithm']['stages'] if s['stage'] == 'SMALL_GROUP_INTENSIFICATION']
    assert len(stage) == 1 and stage[0]['details']['new_network_calls'] == 0
    assert result['algorithm']['complete_round_allocations'] >= 2


def preserve_tail(monkeypatch):
    monkeypatch.setattr('app.route_columns.select_portfolio',
        lambda body, road, results, **kwargs: deepcopy(results[0]))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}))


@pytest.mark.parametrize('seconds, base_seed, expected_rounds', [(30, 0, 4), (300, 7, 5)])
def test_true_cold_uses_exactly_one_independent_native_first_solution(monkeypatch, seconds, base_seed, expected_rounds):
    body, road, _ = split_problem()
    current = plan(body, road, [('V0', ['0', '1', '2', '3'])])
    seen = stub_rounds(monkeypatch, [current])
    preserve_tail(monkeypatch)
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, baseline, *args: (deepcopy(current), {'elapsed_seconds': 0}, []))
    result = optimize_deep(body, road, seconds, seed=base_seed, initial_result=None, experiments={})
    rounds = result['algorithm']['rounds']
    assert len(seen) == len(rounds) == expected_rounds
    assert [row['kwargs']['initial_result'] is None for row in seen] == [index == 1 for index in range(expected_rounds)]
    assert seen[1]['kwargs']['strategy'] == 'GUIDED_LOCAL_SEARCH'
    assert seen[1]['kwargs']['common_penalty_multiplier'] == 4
    for index, row in enumerate(rounds):
        assert row['seed'] == (base_seed + (index + 1) * 104729) % 2147483647
        assert row['initialization_source'] == ('INDEPENDENT_NATIVE_FIRST_SOLUTION' if index == 1 else 'CURRENT_BEST')
        assert row['initial_plan_supplied_to_round'] is (index != 1)
        if index != 1:
            assert seen[index]['kwargs']['initial_result']['total_fee'] == '13.00'
    # A larger original budget schedules a second GLS profile; it must retain
    # the current best rather than creating a second independent restart.
    if expected_rounds == 5:
        assert rounds[4]['strategy'] == 'GUIDED_LOCAL_SEARCH'
        assert seen[4]['kwargs']['initial_result'] is not None
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert result['algorithm']['initial_plan_supplied'] is False
    assert result['algorithm']['initial_plan_accepted'] is False


@pytest.mark.parametrize('valid_warm', [True, False])
def test_external_warm_argument_keeps_every_round_initialized_even_if_rejected(monkeypatch, valid_warm):
    body, road, _ = split_problem()
    current = plan(body, road, [('V0', ['0', '1', '2', '3'])])
    external = deepcopy(current)
    if not valid_warm:
        external['total_fee'] = '0.00'
    seen = stub_rounds(monkeypatch, [current])
    preserve_tail(monkeypatch)
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, baseline, *args: (deepcopy(current), {'elapsed_seconds': 0}, []))
    result = optimize_deep(body, road, 300, initial_result=external, experiments={})
    assert len(seen) == 5 and all(row['kwargs']['initial_result'] is not None for row in seen)
    assert all(row['kwargs']['initial_result']['total_fee'] == '13.00' for row in seen)
    assert all(row['initialization_source'] == 'CURRENT_BEST'
               and row['initial_plan_supplied_to_round'] for row in result['algorithm']['rounds'])
    assert result['algorithm']['initial_plan_supplied'] is True
    assert result['algorithm']['initial_plan_accepted'] is valid_warm
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)


@pytest.mark.parametrize('independent_result', ['dearer_complete', 'cheaper_partial'])
def test_independent_native_round_cannot_displace_a_lower_complete_incumbent(monkeypatch, independent_result):
    body, road, dearer = split_problem()
    if independent_result == 'cheaper_partial':
        body['tasks'][0]['common_unit_price'] = None
    protected = plan(body, road, [('V0', ['0', '1', '2', '3'])])
    candidate = (plan(body, road, [('V0', ['1', '2', '3'])])
                 if independent_result == 'cheaper_partial' else dearer)
    assert protected['total_fee'] == '13.00'
    if independent_result == 'cheaper_partial':
        assert candidate['total_fee'] == '12.00' and not complete(normalize(body), candidate)
    else:
        assert candidate['total_fee'] == '22.00' and complete(normalize(body), candidate)
        monkeypatch.setattr('app.route_deep._pre_selection_macros',
            lambda data, road, baseline, *args: (deepcopy(protected), {'elapsed_seconds': 0}, []))
    # With mandatory demand the real seed helper skips its incomplete baseline;
    # the first source round supplies the same checked 13-unit incumbent.
    seen = stub_rounds(monkeypatch, [protected, candidate, protected, protected])
    preserve_tail(monkeypatch)
    result = optimize_deep(body, road, 30, initial_result=None, experiments={})
    assert seen[1]['kwargs']['initial_result'] is None
    assert [row['kwargs']['initial_result']['total_fee'] for row in seen[2:]] == ['13.00', '13.00']
    assert result['algorithm']['rounds'][1]['total_fee'] == candidate['total_fee']
    assert result['algorithm']['rounds'][1]['complete'] is (independent_result == 'dearer_complete')
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert recipe(result) == recipe(protected)
    assert result['algorithm']['initial_plan_supplied'] is False
    assert result['algorithm']['initial_plan_accepted'] is False


def test_mandatory_baseline_skips_seed_without_claiming_a_complete_low_fee(monkeypatch):
    body, road, _ = split_problem()
    body['tasks'][0]['common_unit_price'] = None
    value = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    seen = stub_rounds(monkeypatch, [value])
    preserve_tail(monkeypatch)
    progress = []
    result = optimize_deep(body, road, 20, initial_result=None, progress=progress.append, experiments={})
    seed = result['algorithm']['stages'][0]
    assert seed['stage'] == 'CURRENT_INPUT_COLD_SEED' and seed['complete'] is False
    assert seed['details']['skipped'] and seed['details']['candidate_count'] == 0
    assert seed['details']['seed_complete'] is False and seed['details']['seed_accepted'] is False
    assert seed['details']['seed_fee'] is None
    assert seen[0]['kwargs']['initial_result']['unassigned'][0]['task_id'] == '0'
    assert progress[0]['best_fee'] is None
    assert result['total_fee'] == '22.00' and complete(normalize(body), result)
    assert not result['algorithm']['initial_plan_supplied'] and not result['algorithm']['initial_plan_accepted']


@pytest.mark.parametrize('partial_seed', [False, True])
def test_lower_verified_warm_plan_survives_more_expensive_or_partial_current_input_seed(monkeypatch, partial_seed):
    body, road, more_expensive = split_problem()
    if partial_seed:
        body['tasks'][0]['common_unit_price'] = None
    warm = plan(body, road, [('V0', ['0', '1', '2', '3'])])
    assert warm['total_fee'] == '13.00'
    original = deepcopy(warm)
    seen = stub_rounds(monkeypatch, [warm])
    preserve_tail(monkeypatch)
    if not partial_seed:
        def seed(data, road, incumbent, *args):
            assert incumbent['routes'] == [] and incumbent['total_fee'] == '400.00'
            return deepcopy(more_expensive), {'elapsed_seconds': 0}, []
        monkeypatch.setattr('app.route_deep._pre_selection_macros', seed)
    result = optimize_deep(body, road, 20, initial_result=warm, experiments={})
    stage = result['algorithm']['stages'][0]
    assert stage['complete'] and stage['total_fee'] == '13.00'
    assert stage['details']['seed_origin'] == 'CURRENT_INPUT'
    assert stage['details']['seed_complete'] is (not partial_seed)
    assert stage['details']['seed_accepted'] is False
    assert stage['details']['seed_fee'] == (None if partial_seed else '22.00')
    assert seen[0]['kwargs']['initial_result']['total_fee'] == '13.00'
    assert result['total_fee'] == '13.00' and warm == original
    assert result['algorithm']['initial_plan_supplied'] is True
    assert result['algorithm']['initial_plan_accepted'] is True


def test_current_input_seed_compute_is_charged_to_the_original_source_allowance(monkeypatch):
    body, road, value = split_problem()
    clock = [0.]
    monkeypatch.setattr('app.route_deep.time.monotonic', lambda: clock[0])
    calls, durations, selection_seconds, independent_rounds = [], [], [], []
    def seed(data, road, incumbent, *args):
        calls.append('seed')
        assert incumbent['routes'] == [] and incumbent['total_fee'] == '400.00'
        clock[0] += 1.8
        return deepcopy(value), {'elapsed_seconds': 1.8, 'compute_seconds': 1.8}, []
    def run(body, road, seconds, run_seed, cancelled, **kwargs):
        calls.append(run_seed)
        durations.append(seconds)
        independent_rounds.append(kwargs['initial_result'] is None)
        clock[0] += seconds
        result = deepcopy(value)
        result['algorithm'] = {'candidate_count': 1}
        return result
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, run)
    monkeypatch.setattr('app.route_deep._pre_selection_macros', seed)
    preserve_tail(monkeypatch)
    def select(body, road, results, **kwargs):
        selection_seconds.append(kwargs['seconds'])
        return deepcopy(results[0])
    monkeypatch.setattr('app.route_columns.select_portfolio', select)
    result = optimize_deep(body, road, 30, seed=0, initial_result=None, experiments={})
    assert calls == ['seed', 104729, 209458, 314187, 418916]
    assert independent_rounds == [False, True, False, False]
    assert durations[0] == pytest.approx(4.35)
    assert sum(durations) + 1.8 == pytest.approx(30 * .64)
    assert selection_seconds == pytest.approx([30 * (.83 - .64)])
    assert [row['initial_plan_supplied_to_round'] for row in result['algorithm']['rounds']] == [True, False, True, True]
    assert result['total_fee'] == '22.00' and complete(normalize(body), result)


def test_cancel_before_first_round_returns_the_verified_current_seed_and_restores_observer(monkeypatch):
    body, road, _ = split_problem()
    prior = lambda spec: None
    road.candidate_observer = prior
    state = {'cancelled': False, 'strict_reads': None}
    def cancel(body, road, seconds, seed, cancelled, **kwargs):
        assert kwargs['initial_result']['total_fee'] == '13.00'
        state.update(cancelled=True, strict_reads=road.strict_reads)
        raise StopSearch('CANCELLED')
    monkeypatch.setattr('app.route_deep.optimize_hybrid', cancel)
    result = optimize_deep(body, road, 20, cancelled=lambda: state['cancelled'], initial_result=None, experiments={})
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert result['algorithm']['stages'][0]['details']['seed_accepted'] is True
    assert road.candidate_observer is prior and road.strict_reads == state['strict_reads']
    assert not result['algorithm']['initial_plan_supplied'] and not result['algorithm']['initial_plan_accepted']


def test_early_seed_sees_reserved_search_call_cap_and_cancellation_restores_it(monkeypatch):
    body, road = driving_problem()
    warm = initial(body, road)
    road.call_limit = 10
    prior = lambda spec: None
    road.candidate_observer = prior
    state = {'cancelled': False}
    def cancel_seed(data, road, incumbent, *args):
        assert road.call_limit == 7 and road.candidate_observer is not prior
        assert incumbent['routes'] == []
        state['cancelled'] = True
        raise StopSearch('CANCELLED')
    monkeypatch.setattr('app.route_deep._pre_selection_macros', cancel_seed)
    result = optimize_deep(body, road, 20, initial_result=warm, cancelled=lambda: state['cancelled'], experiments={})
    assert result['total_fee'] == warm['total_fee']
    assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert road.call_limit == 10 and road.calls == 0 and road.candidate_observer is prior


def test_joint_search_observer_keeps_identity_only_without_inspecting_metric_opt_in():
    from app.route_search import JointSearch
    body, road, value = split_problem()
    observed = []
    class Observer:
        @property
        def accepts_metrics(self):
            raise AssertionError('The source search must not inspect metric opt-in')
        def __call__(self, spec):
            observed.append(spec)
    road.candidate_observer = Observer()
    route = value['routes'][0]
    candidate = JointSearch(normalize(body), road, 1, 0, lambda: False).remember(route)
    assert observed == [{'vehicle_id': route['vehicle_id'], 'task_ids': route['task_ids']}]
    observed[0]['task_ids'].clear()
    assert route['task_ids'] == candidate['task_ids'] == ['0', '1']


def test_source_observations_keep_fifo_distinct_orders_without_fee_ranking(monkeypatch):
    body, road, value = split_problem()
    first = {'vehicle_id': 'V0', 'task_ids': ['0', '1']}
    reverse = {'vehicle_id': 'V0', 'task_ids': ['1', '0']}
    other = {'vehicle_id': 'V1', 'task_ids': ['2', '3']}
    sent = [False]
    def run(*args, **kwargs):
        if not sent[0]:
            for spec in [first, reverse, first, other]:
                road.candidate_observer(deepcopy(spec))
            sent[0] = True
        result = deepcopy(value)
        result['algorithm'] = {'candidate_count': 1}
        return result
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, run)
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}, []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}))
    received = []
    def select(*args, **kwargs):
        received.extend(deepcopy(list(kwargs['candidate_recipes'])))
        return deepcopy(value)
    monkeypatch.setattr('app.route_columns.select_portfolio', select)
    result = optimize_deep(body, road, 20, experiments={})
    assert received == [first, reverse, other]
    assert all(set(spec) == {'vehicle_id', 'task_ids'} for spec in received)
    assert result['algorithm']['distinct_route_count'] == 3
    assert result['algorithm']['dropped_candidate_observations'] == 0
    assert complete(normalize(body), result)


@pytest.mark.parametrize('start, supplied, accepted', [
    ('none', False, False), ('valid', True, True), ('invalid_fee', True, False),
])
def test_actual_initial_argument_and_validation_determine_cold_flags(monkeypatch, start, supplied, accepted):
    body, road, value = split_problem()
    stub_rounds(monkeypatch, [value])
    candidate = None if start == 'none' else deepcopy(value)
    if start == 'invalid_fee':
        candidate['total_fee'] = '0.00'
    monkeypatch.setattr('app.route_deep._pre_selection_macros',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}, []))
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}))
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(value))
    result = optimize_deep(body, road, 20, initial_result=candidate, experiments={})
    assert result['algorithm']['initial_plan_supplied'] is supplied
    assert result['algorithm']['initial_plan_accepted'] is accepted
    assert result['total_fee'] == value['total_fee'] and complete(normalize(body), result)


def test_full_seed_fifo_preserves_complete_allocations_and_independent_useful_recipe(monkeypatch):
    body, road, value = split_problem()
    prior_observations = []
    prior = prior_observations.append
    road.candidate_observer = prior
    round_inputs = []
    def run(*args, **kwargs):
        start = kwargs['initial_result']
        round_inputs.append(None if start is None else start['total_fee'])
        result = deepcopy(value)
        result['algorithm'] = {'candidate_count': 1}
        return result
    for name in ('optimize_hybrid', 'optimize_alns', 'optimize_ortools'):
        monkeypatch.setattr('app.route_deep.' + name, run)
    improved = plan(body, road, [('V0', ['0', '1', '2', '3'])])
    assert improved['total_fee'] == '13.00'
    late = {'vehicle_id': 'V0', 'task_ids': ['0', '1', '2', '3']}
    def macro(data, road, incumbent, *args):
        # Seed prefixes can fill the normal FIFO before main. Its useful final
        # recipe still has an independent priority entry, and the complete seed
        # must survive both the full FIFO and worse complete source rounds.
        for n in range(10000):
            road.candidate_observer({'vehicle_id': 'unused-' + str(n), 'task_ids': ['0']})
        road.candidate_observer(deepcopy(late))
        return deepcopy(improved), {'elapsed_seconds': 0}, [deepcopy(late)]
    monkeypatch.setattr('app.route_deep._pre_selection_macros', macro)
    received = {}
    def select(*args, **kwargs):
        kwargs['candidate_recipes'] = list(kwargs['candidate_recipes'])
        received.update(deepcopy(kwargs))
        return deepcopy(improved)
    monkeypatch.setattr('app.route_columns.select_portfolio', select)
    result = optimize_deep(body, road, 20, experiments={})
    assert round_inputs == ['13.00', None, '13.00', '13.00']
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert result['algorithm']['distinct_route_count'] == 10000
    assert result['algorithm']['dropped_candidate_observations'] > 0
    assert len(received['candidate_recipes']) == 10001
    assert received['candidate_recipes'][0] == late
    assert received['candidate_recipes'][1] == {'vehicle_id': 'unused-0', 'task_ids': ['0']}
    assert received['candidate_recipes'][-1] == {'vehicle_id': 'unused-9999', 'task_ids': ['0']}
    assert recipe(value) in received['complete_recipes'] and recipe(improved) in received['complete_recipes']
    assert result['algorithm']['complete_round_allocations'] >= 2
    assert road.candidate_observer is prior


@pytest.mark.parametrize('experiments', [None, {'candidate_retention': 'batch_dedup',
                                               'native_candidate_retention': 'diversity'}])
def test_daily_planning_delivers_candidate_after_first_ten_thousand(monkeypatch, experiments):
    body, road, value = split_problem()
    stub_rounds(monkeypatch, [value])
    late = {'vehicle_id': 'V0', 'task_ids': ['0', '1', '2', '3']}
    def collect(data, road, incumbent, *args):
        for n in range(10000):
            road.candidate_observer({'vehicle_id': 'unused-' + str(n), 'task_ids': ['0']})
        road.candidate_observer(deepcopy(late))
        return deepcopy(value), {'elapsed_seconds': 0}, []
    monkeypatch.setattr('app.route_deep._pre_selection_macros', collect)
    monkeypatch.setattr('app.route_deep._post_selection_small_groups',
        lambda data, road, incumbent, *args: (incumbent, {'elapsed_seconds': 0}))
    result = optimize_deep(body, road, 20, experiments=experiments, deadline_limited_candidates=True)
    assert result['total_fee'] == '13.00' and complete(normalize(body), result)
    assert recipe(result)['routes'] == [late]
    assert result['algorithm']['candidate_pool_limit'] is None
    assert result['algorithm']['candidate_retention']['policy'] == 'deadline_dedup'
    assert result['algorithm']['distinct_route_count'] > 10000
    assert result['algorithm']['dropped_candidate_observations'] == 0
    assert not hasattr(road, 'deadline_limited_candidates')


def test_cancel_during_post_retains_current_seed_and_restores_observer(monkeypatch):
    body, road, value = split_problem()
    original = deepcopy(value)
    stub_rounds(monkeypatch, [value])
    monkeypatch.setattr('app.route_columns.select_portfolio', lambda *args, **kwargs: deepcopy(value))
    prior = lambda spec: None
    road.candidate_observer = prior
    state = {'cancelled': False}
    def cancel(search):
        state['cancelled'] = True
        raise StopSearch('CANCELLED')
    monkeypatch.setattr('app.route_small_groups.seed_small_groups', cancel)
    result = optimize_deep(body, road, 20, cancelled=lambda: state['cancelled'], experiments={})
    assert complete(normalize(body), result) and result['total_fee'] == '13.00'
    macro = next(row for row in result['algorithm']['stages'] if row['stage'] == 'CURRENT_INPUT_COLD_SEED')
    assert macro['total_fee'] == result['total_fee']
    assert value == original
    assert result['algorithm']['stop_reason'] == 'CANCELLED'
    assert road.candidate_observer is prior
