"""Independent switches, actual-pool changes and complete-fee protection."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.route_adaptive_cycles as cycles
from app.route_deep import _Budget, _optional_refinement
from app.route_search import JointSearch
from app.routing_engine import StopSearch, check_result, normalize
from test_route_columns import plan, problem
from test_route_cost_cleanup import GuardedMeasuredRoad


def prepared(body, road, routes=()):
    initial = plan(body, road, routes)
    assert check_result(normalize(body), initial)['valid']
    search = JointSearch(normalize(body), road, 10, 0, lambda: False, initial)
    search.setup_fleet()
    assert search.consider([search.compact(row) for row in initial['routes']], force=True)
    return search, initial


def generator(record, propose):
    def generate(data, lookup, seconds, seed, cancelled, **kwargs):
        record.append({'tasks': sorted(data['tasks']), 'vehicles': sorted(data['vehicles']),
                       'seconds': seconds, 'strategy': kwargs['strategy']})
        rows = propose(data, len(record))
        kwargs['on_batch'](rows)
        return rows, {'stop_reason': 'SEARCH_COMPLETED'}
    return generate


def test_quote_rotation_alone_visits_each_group_and_uses_global_physical_fleet(monkeypatch):
    body, road = problem(n=4, cars=1, common='100', capacity=4)
    body['quotes']['R'] = {'mode': 'BASE_STOPS', 'base_price': '3', 'included_stops': 4, 'extra_stop_price': '1'}
    for task in body['tasks'][2:]:
        task['quote_id'] = 'R'
    search, initial = prepared(body, road)
    calls = []
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', generator(calls,
        lambda data, index: [{'vehicle_id': 'V0', 'task_ids': sorted(data['tasks'])}]))
    details = cycles.expand_candidate_cycles(search, quote_rotation=True, max_cycles=8)
    assert len(calls) == 2 and {tuple(row['tasks']) for row in calls} == {('0', '1'), ('2', '3')}
    assert all(row['group_task_count'] == 2 for row in details['cycles'])
    assert details['generation_selection_loop'] is details['gain_time_allocation'] is False
    assert search.best['total_fee'] == '203.00' < initial['total_fee'] == '400.00'
    assert len(search.best['routes']) == len({row['vehicle_id'] for row in search.best['routes']}) == 1
    assert check_result(search.data, search.best)['valid']


def test_selection_loop_alone_generates_new_orders_before_global_selection(monkeypatch):
    body, road = problem(n=4, cars=1, common='100', capacity=4)
    search, _ = prepared(body, road)
    calls = []
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', generator(calls,
        lambda data, index: [{'vehicle_id': 'V0', 'task_ids': ['0', '2'] if index == 1 else ['0', '1']}]))
    details = cycles.expand_candidate_cycles(search, selection_loop=True, max_cycles=3)
    assert len(calls) == 3 and all(len(row['tasks']) == 4 for row in calls)
    assert details['quote_group_rotation'] is details['gain_time_allocation'] is False
    assert [row['selection_runs'] for row in details['cycles']] == [1, 1, 0]
    assert search.best['total_fee'] == '211.00'
    assert check_result(search.data, search.best)['valid']


def test_gain_allocation_has_observable_behavior_when_enabled_alone(monkeypatch):
    body, road = problem(n=4, cars=1, common='100', capacity=4)
    search, _ = prepared(body, road)
    calls = []
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', generator(calls,
        lambda data, index: [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}]))
    details = cycles.expand_candidate_cycles(search, gain_allocation=True, max_cycles=4)
    assert len(calls) == 4, 'The independent gain allocation switch must get a chance to use observed gain.'
    assert {row['strategy'] for row in calls[:3]} == {'GUIDED_LOCAL_SEARCH', 'SIMULATED_ANNEALING', 'TABU_SEARCH'}
    assert calls[-1]['strategy'] == calls[0]['strategy'], 'The fourth slice should use the measured profitable action.'
    assert details['quote_group_rotation'] is details['generation_selection_loop'] is False
    assert check_result(search.data, search.best)['valid']


def test_invalid_new_identity_does_not_reselect_an_unchanged_pool(monkeypatch):
    body, road = problem(n=4, cars=1, common='100', capacity=1)
    search, initial = prepared(body, road)
    packs = []
    monkeypatch.setattr(search, 'pack', lambda: packs.append(True))
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', generator([],
        lambda data, index: [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}]))
    details = cycles.expand_candidate_cycles(search, selection_loop=True, max_cycles=2)
    assert packs == [], 'An infeasible identity added no retained priced column.'
    assert not search.pool and search.best == initial
    assert all(row['selection_runs'] == 0 for row in details['cycles'])


def test_dominated_order_does_not_reselect_same_car_task_set_pool(monkeypatch):
    body, road = problem(n=2, cars=1, common='100', capacity=2)
    search, initial = prepared(body, road, [('V0', ['0', '1'])])
    packs = []
    monkeypatch.setattr(search, 'pack', lambda: packs.append(True))
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', generator([],
        lambda data, index: [{'vehicle_id': 'V0', 'task_ids': ['1', '0']}]))
    details = cycles.expand_candidate_cycles(search, selection_loop=True, max_cycles=2)
    assert packs == [], 'A dearer order for the same retained pattern must not restart the solved pool.'
    assert len(search.pool) == 1 and search.best == initial
    assert all(row['selection_runs'] == 0 for row in details['cycles'])


def test_expired_budget_preserves_complete_incumbent_before_generation(monkeypatch):
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    search, initial = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    original = deepcopy(search.best)
    search.search_seconds = 0
    def forbidden(*args, **kwargs):
        raise AssertionError('An expired compute budget must not generate candidates')
    monkeypatch.setattr(cycles, 'generate_ortools_candidates', forbidden)
    details = cycles.expand_candidate_cycles(search, selection_loop=True)
    assert details['stop_reason'] == 'TIME_BUDGET'
    assert search.best == original == initial
    assert check_result(search.data, search.best)['valid']


def test_cancel_is_not_swallowed_and_complete_incumbent_stays_intact():
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    search, _ = prepared(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    original = deepcopy(search.best)
    search.cancelled = lambda: True
    with pytest.raises(StopSearch, match='CANCELLED'):
        cycles.expand_candidate_cycles(search, quote_rotation=True)
    assert search.best == original


def test_optional_refinement_respects_pre_validation_ceiling():
    body, road = problem(n=4, cars=2, common=None, capacity=2)
    initial = plan(body, road, [('V0', ['0', '1']), ('V1', ['2', '3'])])
    budget = SimpleNamespace(seconds=10., compute=lambda: 8.3)
    result, details = _optional_refinement(normalize(body), road, initial, budget, 0, lambda: False, 'cycles')
    assert result is initial and details['skipped'] and details['budget_seconds'] == 0


def test_optional_refinement_keeps_proved_improvement_on_deadline_and_restores_call_cap(monkeypatch):
    body, _ = problem(n=4, cars=2, common=None, capacity=4)
    road = GuardedMeasuredRoad()
    for a, b in [('0', '2'), ('1', '3')]:
        road.metrics[a, b] = 100000
    initial = plan(body, road, [('V0', ['0', '2']), ('V1', ['1', '3'])])
    original = deepcopy(initial)
    original_limit = road.call_limit
    road.deep_experiments = SimpleNamespace(quote_group_rotation=False, generation_selection_loop=True, gain_time_allocation=False)
    def refine(search, **kwargs):
        candidate = search.evaluate('V0', ['0', '1', '2', '3'])
        search.quick_exchange(candidate)
        raise StopSearch('TIME_BUDGET')
    monkeypatch.setattr(cycles, 'expand_candidate_cycles', refine)
    budget = _Budget(road, 10, lambda: False)
    result, details = _optional_refinement(normalize(body), road, initial, budget, 0, lambda: False, 'cycles')
    assert result['total_fee'] == '4.00' and initial['total_fee'] == '202.00'
    assert Decimal(result['total_fee']) < Decimal(initial['total_fee'])
    assert details['stop_reason'] == 'TIME_BUDGET' and details['fee_reduction'] == '198.00'
    assert road.call_limit == original_limit and initial == original
    assert check_result(normalize(body), result)['valid']


@pytest.mark.parametrize('reason',['CANCELLED','WALL_BUDGET'])
def test_local_phase_returns_its_checked_improvement_before_global_stop(monkeypatch,reason):
    from app.route_deep import _Budget, _optional_refinement
    import app.route_adaptive_cycles as cycles
    from app.route_experiments import DeepExperiments
    from app.routing_engine import StopSearch, normalize
    body,road=problem(n=4,cars=2,common=None,capacity=4)
    initial=plan(body,road,[('V0',['0','2']),('V1',['1','3'])])
    road.deep_experiments=DeepExperiments(generation_selection_loop=True)
    state={'cancelled':False}
    def refine(search,**kwargs):
        candidate=search.evaluate('V0',['0','1','2','3'])
        search.quick_exchange(candidate)
        assert search.best['total_fee']<initial['total_fee']
        if reason=='CANCELLED': state['cancelled']=True
        else: search.road.wall_deadline=0
        raise StopSearch(reason)
    monkeypatch.setattr(cycles,'expand_candidate_cycles',refine)
    result,details=_optional_refinement(normalize(body),road,initial,_Budget(road,10,lambda:state['cancelled']),
                                       0,lambda:state['cancelled'],'cycles')
    assert result['total_fee']<initial['total_fee'] and not result['unassigned']
    assert details['stop_reason']==reason
