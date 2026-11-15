"""Controlled comparisons must retain failures and reject unequal conditions."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from app.planning_inputs import content_hash
from app.routing_engine import assemble, normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad, private_json, write_graph
import scripts.evaluate_delivery_stages as study
from test_algorithm_benchmark import problem, measured_edges


def frozen_case(tmp_path):
    body = problem()
    graph = tmp_path / 'roads.db'
    write_graph(graph, body, measured_edges(body))
    road = FrozenEvidenceRoad(graph)
    case = {'body_file': 'input.json', 'input_hash': content_hash(body), 'baseline_file': None,
            'road_database': str(graph), 'graph_hash': road.graph_hash}
    road.close()
    private_json(tmp_path / 'input.json', body)
    private_json(tmp_path / 'case.json', case)
    return tmp_path / 'case.json', body


def valid_row(side, fee, budget=15):
    return {'side': side, 'profile_name': 'only-stage-two', 'identity': {'graph_hash': 'G', 'historical_hints_hash': 'H'},
            'compute_seconds': budget, 'wait_seconds': 90, 'seed': 0, 'hash_seed': 0,
            'status': 'VALID', 'run_folder': side, 'audit': {'valid': True, 'total_fee': fee}}


def test_all_regressions_failures_and_budgets_remain_visible():
    old, new = valid_row('baseline', '100.00'), valid_row('candidate', '102.00')
    regression = study.pair_summary(old, new)
    improved = study.pair_summary(valid_row('baseline', '100.00', 600), valid_row('candidate', '90.00', 600))
    timeout = valid_row('candidate', '1.00')
    timeout['status'] = 'TIMEOUT'
    incomplete = study.pair_summary(old, timeout)
    report = study.summarize([regression, improved, incomplete], [old, new, timeout])
    assert regression['saving'] == '-2.00' and regression['outcome'] == 'REGRESSED'
    assert len(report['groups']) == 2  # Different compute budgets never share a total.
    assert report['regressions'] == [regression]
    assert report['failures_and_timeouts'] == [timeout]
    assert incomplete['outcome'] == 'INCOMPLETE_COMPARISON'


def test_comparison_rejects_different_roads_hints_seed_or_budget():
    old = valid_row('baseline', '100.00')
    for key, value in [('compute_seconds', 600), ('wait_seconds', 120), ('seed', 1), ('hash_seed', 1),
                       ('identity', {'graph_hash': 'OTHER', 'historical_hints_hash': 'H'}),
                       ('identity', {'graph_hash': 'G', 'historical_hints_hash': 'OTHER'})]:
        new = valid_row('candidate', '99.00')
        new[key] = value
        with pytest.raises(study.IntegrityError, match='unequal'):
            study.pair_summary(old, new)


def test_hint_change_is_detected_even_when_road_metric_hash_is_unchanged(tmp_path):
    case_path, _ = frozen_case(tmp_path)
    before = study.case_identity(case_path)
    graph = study.load(case_path)['road_database']
    with sqlite3.connect(graph) as connection:
        connection.execute('INSERT INTO hints VALUES (?,?,?)', ('H', json.dumps({'vehicle_id': 'V1'}), 42))
        connection.commit()
    after = study.case_identity(case_path)
    assert before['graph_hash'] == after['graph_hash']
    assert before['historical_hints_hash'] != after['historical_hints_hash']
    assert before['graph_file_sha256'] != after['graph_file_sha256']


def test_serial_alternation_profile_separation_and_independent_audit(tmp_path, monkeypatch):
    case_path, body = frozen_case(tmp_path)
    calls = []
    current = []
    def run(command, **kwargs):
        assert not current  # Every run finishes before the next starts.
        spec = study.load(command[-1])
        current.append(spec['side'])
        calls.append(spec)
        assert kwargs['env']['PYTHONHASHSEED'] == '0'
        assert kwargs['timeout'] == 100
        result = assemble(normalize(body), [], [task['id'] for task in body['tasks']], [])
        result['algorithm'] = {'compute_seconds': .5}
        folder = Path(spec['run_folder'])
        result_path = folder / 'result.json.gz'
        result_path.write_bytes(gzip.compress(json.dumps(result).encode()))
        study.save(folder / 'worker.json', {**{key: spec[key] for key in ('identity', 'compute_seconds', 'wait_seconds', 'seed', 'hash_seed', 'profile_name')},
                                          'status': 'RETURNED', 'result_file': str(result_path), 'worker_seconds': 1})
        current.pop()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(study.subprocess, 'run', run)
    config = {'baseline_source': str(study.ROOT), 'candidate_source': str(study.ROOT),
              'cases': [str(case_path)], 'profiles': [{'name': 'retention', 'baseline_options': {},
              'candidate_options': {'experiments': {'candidate_retention': 'diversity'}}}],
              'budgets': [15], 'seeds': [0, 1], 'hash_seed': 0, 'wait_seconds': 100,
              'output': str(tmp_path / 'results')}
    report = study.evaluate(config)
    assert [call['side'] for call in calls] == ['baseline', 'candidate', 'candidate', 'baseline']
    assert [call['seed'] for call in calls] == [0, 0, 1, 1]
    assert all(call['options'] == {} for call in calls if call['side'] == 'baseline')
    assert all(call['options']['experiments']['candidate_retention'] == 'diversity' for call in calls if call['side'] == 'candidate')
    assert all(row['status'] == 'VALID' and row['audit']['total_fee'] == '150.00' for row in report['runs'])
    assert report['finished'] and len(report['comparisons']) == 2
    with pytest.raises(study.IntegrityError, match='never overwritten'):
        study.evaluate(config)


def test_timeout_is_preserved_and_does_not_abort_following_side(tmp_path, monkeypatch):
    case_path, _ = frozen_case(tmp_path)
    calls = []
    def run(command, **kwargs):
        spec = study.load(command[-1])
        calls.append(spec['side'])
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(study.subprocess, 'run', run)
    report = study.evaluate({'baseline_source': str(study.ROOT), 'candidate_source': str(study.ROOT),
                             'cases': [str(case_path)], 'profiles': [{'name': 'diag'}], 'budgets': [1],
                             'seeds': [0], 'hash_seed': 0, 'wait_seconds': .1, 'output': str(tmp_path / 'runs')})
    assert calls == ['baseline', 'candidate']
    assert [row['status'] for row in report['failures_and_timeouts']] == ['TIMEOUT', 'TIMEOUT']
    assert report['comparisons'][0]['outcome'] == 'INCOMPLETE_COMPARISON'


def test_timeout_fallback_is_audited_but_never_scored_as_completed(tmp_path):
    case_path, body = frozen_case(tmp_path)
    result = assemble(normalize(body), [], [task['id'] for task in body['tasks']], [])
    result['algorithm'] = {'stop_reason': 'CANCELLED', 'compute_seconds': .5}
    path = tmp_path / 'fallback.json.gz'
    path.write_bytes(gzip.compress(json.dumps(result).encode()))
    row = {'status': 'RETURNED_TIMEOUT', 'deadline_exhausted': True, 'result_file': str(path)}
    checked = study.independent_check({'case_file': str(case_path), 'evaluator_source_hash': 'FIXED',
                                       'compute_seconds': 1, 'wait_seconds': 10}, row)
    assert checked['status'] == 'TIMEOUT' and checked['audit']['valid']
    assert checked['complete_result_available']
    old = valid_row('baseline', '100.00')
    new = {**valid_row('candidate', '99.00'), **checked}
    assert study.pair_summary(old, new)['outcome'] == 'INCOMPLETE_COMPARISON'


def test_worker_passes_remaining_wall_allowance_and_latches_deadline(tmp_path, monkeypatch):
    case_path, body = frozen_case(tmp_path)
    now = [100.]
    monkeypatch.setattr(study.time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('PYTHONHASHSEED', '1')
    def optimizer(body, road, budget, seed, cancelled, *, initial_result=None, wall_budget_seconds=None):
        assert wall_budget_seconds == 10
        now[0] += 9.9
        assert cancelled()
        result = assemble(normalize(body), [], [task['id'] for task in body['tasks']], [])
        result['algorithm'] = {'stop_reason': 'CANCELLED'}
        return result
    real_import = study.importlib.import_module
    monkeypatch.setattr(study.importlib, 'import_module', lambda name, *a, **kw:
                        SimpleNamespace(optimizer=optimizer) if name == 'deadline_test' else real_import(name, *a, **kw))
    folder = tmp_path / 'run';folder.mkdir()
    spec = {'source_root': str(study.ROOT), 'source_hash': study.source_manifest(study.ROOT)['sha256'],
            'case_file': str(case_path), 'identity': study.case_identity(case_path), 'side': 'candidate',
            'profile_name': 'deadline', 'compute_seconds': 100, 'wait_seconds': 10, 'seed': 0,
            'hash_seed': 1, 'entrypoint': 'deadline_test:optimizer', 'options': {}, 'run_folder': str(folder)}
    study.save(folder / 'spec.json', spec)
    row = study.worker(folder / 'spec.json')
    assert row['status'] == 'RETURNED_TIMEOUT' and row['deadline_exhausted']
    assert row['applied_wall_budget_seconds'] == 10 and Path(row['result_file']).is_file()
