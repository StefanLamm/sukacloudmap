"""Frozen identity streams isolate retention from time-limited generation."""
from copy import deepcopy

import pytest

from app.route_retention import CandidateRetention, replay_frozen_stream
from app.routing_engine import assemble, evaluate_route, normalize
from test_route_ortools import problem


def spec(task, car='V0'):
    return {'vehicle_id': car, 'task_ids': [task]}


def test_first_exactly_matches_original_ordered_dictionary_rule():
    events = [spec('a'), spec('b'), spec('a'), spec('c'), spec('b'), spec('d')]
    original = {}
    duplicates = dropped = 0
    for row in events:
        key = row['vehicle_id'], tuple(row['task_ids'])
        if key in original:
            duplicates += 1
        elif len(original) < 2:
            original[key] = row
        else:
            dropped += 1
    pool = CandidateRetention('first', limit=2)
    for row in events:
        pool.observe(row)
    assert pool.values() == list(original.values())
    details = pool.diagnostics()
    assert details['duplicate_observations'] == duplicates
    assert details['capacity_rejected_observations'] == dropped


def test_batch_dedup_admits_late_unique_routes_without_callback_pruning():
    pool = CandidateRetention('batch_dedup', limit=2)
    for row in [spec('a'), spec('a'), spec('b'), spec('c')]:
        pool.observe(row)
    assert not pool.entries and len(pool.pending) == 4
    assert pool.counts['flushes'] == 0
    assert pool.values() == [spec('b'), spec('c')]
    assert pool.diagnostics()['duplicate_observations'] == 1
    assert pool.diagnostics()['evicted_candidates'] == 1


def test_diversity_keeps_late_minority_quote_group_and_distinct_physical_car():
    data = {'tasks': {
        'a': {'warehouse_id': 'W', 'signature': 'A', 'common_unit_price': '10'},
        'b': {'warehouse_id': 'W', 'signature': 'A', 'common_unit_price': '10'},
        'c': {'warehouse_id': 'W', 'signature': 'B', 'common_unit_price': None},
    }}
    events = [spec('a'), spec('b'), spec('a', 'V1'), spec('c')]
    replay = replay_frozen_stream(events, data=data, limit=3, batch_size=10)
    old = replay['policies']['first']['candidates']
    diverse = replay['policies']['diversity']['candidates']
    assert spec('c') not in old and spec('c') in diverse
    assert {row['vehicle_id'] for row in diverse} == {'V0', 'V1'}
    assert replay['stream_observations'] == 4
    assert all(row['diagnostics']['generated_observations'] == 4 for row in replay['policies'].values())
    assert not replay['cold_start_performance_claimed']


def test_bounded_callback_buffer_has_explicit_overflow_counts():
    pool = CandidateRetention('diversity', limit=2, batch_size=2)
    for index in range(20):
        pool.observe(spec(str(index)))
    assert len(pool.pending) == 8
    assert not pool.entries
    assert pool.diagnostics()['buffer_rejected_observations'] == 12
    assert len(pool.values()) == 2


def test_flush_obeys_caller_deadline_without_destroying_retained_pool():
    from app.routing_engine import StopSearch
    pool = CandidateRetention('batch_dedup', limit=2)
    pool.observe(spec('a'))
    pool.flush()
    pool.observe(spec('b'))
    def expired():
        raise StopSearch('TIME_BUDGET')
    pool.control = expired
    with pytest.raises(StopSearch):
        pool.flush()
    assert list(pool.entries.values()) == [spec('a')]
    assert len(pool.pending) == 1
    assert pool.flush_seconds > 0


def test_frozen_candidate_pool_change_is_priced_by_exact_selector():
    from app.route_columns import select_portfolio
    from scripts.benchmark_algorithms import independent_audit
    body, road, _ = problem(n=3, cars=1, quote={'mode': 'KM', 'per_km': '200'})
    body['quotes']['B'] = {'mode': 'KM', 'per_km': '1'}
    body['tasks'][2]['quote_id'] = 'B'
    data = normalize(body)
    replay = replay_frozen_stream([spec('0'), spec('1'), spec('2')], limit=2, data=data)
    baseline = assemble(data, [], set(data['tasks']), [])
    results = {}
    for policy, row in replay['policies'].items():
        selected = select_portfolio(body, road, [baseline], seconds=1, candidate_recipes=row['candidates'])
        assert independent_audit(data, selected, road)['valid']
        results[policy] = selected['total_fee']
    assert results['first'] == '300.00'
    assert results['batch_dedup'] == results['diversity'] == '201.00'


def test_complete_incumbent_is_separate_from_evicted_ordinary_pool():
    body, road, _ = problem(n=2, cars=2)
    data = normalize(body)
    incumbent = assemble(data, [evaluate_route(data, 'V0', ['0', '1'], road)], [], [])
    before = deepcopy(incumbent)
    pool = CandidateRetention('batch_dedup', limit=1, data=data)
    pool.protect_allocation(incumbent)
    pool.protect_allocation(incumbent)
    pool.observe({'vehicle_id': 'V0', 'task_ids': ['0', '1']})
    pool.flush()
    pool.observe(spec('1', 'V1'))
    assert pool.values() == [spec('1', 'V1')]
    assert pool.protected_allocations[0]['routes'] == [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}]
    assert len(pool.protected_allocations) == 1
    assert incumbent == before
    illegal = deepcopy(incumbent)
    illegal['routes'][0]['task_ids'].pop()
    with pytest.raises(ValueError):
        pool.protect_allocation(illegal)


def test_replay_is_deterministic_does_not_mutate_stream_and_counts_global_duplicates():
    events = [spec('a'), spec('b'), {'kind': 'flush'}, spec('a'), spec('c'), spec('d')]
    before = deepcopy(events)
    first = replay_frozen_stream(events, limit=2, batch_size=3)
    second = replay_frozen_stream(events, limit=2, batch_size=3)
    for output in (first, second):
        for row in output['policies'].values():
            row['diagnostics'].pop('flush_seconds')
    assert first == second and events == before
    assert first['stream_unique_identities'] == 4
    assert first['stream_duplicate_observations'] == 1


def test_trace_complete_recipes_survive_pool_truncation_but_are_not_declared_validated():
    allocation = {'routes': [{'vehicle_id': 'V0', 'task_ids': ['a', 'b']}], 'common_ids': ['c']}
    events = [spec('a'), {'kind': 'allocation', 'allocation': allocation}, spec('b'), spec('c'),
              {'kind': 'allocation', 'allocation': deepcopy(allocation)}, {'kind': 'flush'}]
    replay = replay_frozen_stream(events, limit=1, explicit_boundaries=True)
    assert replay['complete_recipes'] == [allocation]
    assert replay['complete_recipe_count'] == 1
    assert replay['stream_observations'] == 3
    assert replay['trace_allocations_independently_validated'] is False


@pytest.mark.parametrize('policy', ['first', 'batch_dedup', 'diversity'])
def test_native_retention_forwards_real_physical_identities_and_preserves_best_assignment(policy):
    from app.route_ortools import generate_ortools_candidates
    body, road, _ = problem(n=3, cars=2)
    events = []
    rows, details = generate_ortools_candidates(
        normalize(body), lambda v, a, b: road.known_segment(a, b), .4,
        vehicle_independent=True, candidate_retention=policy, candidate_stream_observer=events.append)
    assert rows
    assert details['candidate_retention_policy'] == policy
    assert details['candidate_stream_capture_enabled'] is True
    assert details['candidate_stream_capture_overhead_in_budget'] is True
    assert events and any(row.get('kind') == 'flush' for row in events)
    assert all(row['vehicle_id'] in {'V0', 'V1'} for row in rows)
    assert all(not set(row['task_ids']) - {'0', '1', '2'} for row in rows)
    assert road.evaluated == 0


def test_saturated_first_native_pool_skips_callbacks_unless_explicit_trace_enabled(monkeypatch):
    from app.route_ortools import _MeasuredModel
    body, road, _ = problem(n=1, cars=1)
    model = _MeasuredModel(normalize(body), lambda v, a, b: road.known_segment(a, b),
                           lambda: None, 0, vehicle_independent=True,
                           strategy='GUIDED_LOCAL_SEARCH', first_strategy='PARALLEL_CHEAPEST_INSERTION')
    try:
        model.candidate_specs = {('old', (str(index),)): {} for index in range(10000)}
        collected = []
        monkeypatch.setattr(model, '_collect', lambda next_value: collected.append('sample'))
        callback = model.callbacks[-1]
        callback()
        assert not collected and model.solution_count == 1
        model.candidate_stream_observer = lambda row: None
        callback()
        assert collected == ['sample'] and model.solution_count == 2
        model.candidate_stream_observer = None
        model.candidate_limit = None
        callback()
        assert collected == ['sample', 'sample'] and model.solution_count == 3
    finally:
        model.dispose()


@pytest.mark.parametrize('limit', [10000, None])
def test_native_collection_preserves_late_identity_only_without_fixed_limit(limit):
    from app.route_ortools import _MeasuredModel
    from types import SimpleNamespace
    model = _MeasuredModel.__new__(_MeasuredModel)
    model.virtual = [({'id': 'V0'}, None)]
    model.routing = SimpleNamespace(Start=lambda vi: 0, IsEnd=lambda node: node == 2)
    model.manager = SimpleNamespace(IndexToNode=lambda node: node)
    model.task_nodes = {1: 'late'}
    model.ids = ['late']
    model.retention = None
    model.candidate_limit = limit
    model.stage_trace = None
    model.trace_scope = None
    model.candidate_specs = {('old', (str(index),)): {} for index in range(10000)}
    trace = []
    model.candidate_stream_observer = trace.append
    assignment = model._collect(lambda node: node + 1)
    assert assignment == trace == [{'vehicle_id': 'V0', 'task_ids': ['late']}]
    assert len(model.candidate_specs) == (10001 if limit is None else 10000)
    assert (('V0', ('late',)) in model.candidate_specs) == (limit is None)


def test_replay_rejects_external_invalid_identities():
    for event in [spec(''), {'vehicle_id': 'V', 'task_ids': ['a', 'a']}, {'vehicle_id': 'V', 'task_ids': 'abc'}]:
        with pytest.raises(ValueError):
            replay_frozen_stream([event])


def test_replay_command_reads_gzip_stream_and_protects_mandatory_incumbent(tmp_path, monkeypatch):
    import gzip
    import json
    import scripts.replay_candidate_retention as module
    body, road, _ = problem(n=2, cars=1)
    body['tasks'][0]['common_unit_price'] = None
    data = normalize(body)
    incumbent = assemble(data, [evaluate_route(data, 'V0', ['0', '1'], road)], [], [])
    allocation = {'routes': [{'vehicle_id': 'V0', 'task_ids': ['0', '1']}], 'common_ids': []}
    stream = tmp_path / 'stream.jsonl.gz'
    input_file = tmp_path / 'input.json.gz'
    incumbent_file = tmp_path / 'incumbent.json.gz'
    stream.write_bytes(gzip.compress((json.dumps({'kind': 'allocation', 'allocation': allocation}) + '\n' +
                                      json.dumps(spec('1')) + '\n' + json.dumps({'kind': 'flush'})).encode()))
    input_file.write_bytes(gzip.compress(json.dumps(body).encode()))
    incumbent_file.write_bytes(gzip.compress(json.dumps({'result': incumbent}).encode()))
    road.graph_hash = 'FIXED_FIXTURE_HASH'
    road.close = lambda: None
    monkeypatch.setattr(module, 'FrozenEvidenceRoad', lambda path: road)
    report = module.compare(stream, input_file, tmp_path / 'unused.db', output=tmp_path / 'report.json',
                            incumbent_result=incumbent_file, seconds=0, explicit_boundaries=True)
    assert report['complete_recipe_count'] == 1
    assert report['trace_allocations_independently_validated'] is False
    assert all(row['fee'] == '2.00' and row['audit']['valid'] for row in report['policies'].values())
