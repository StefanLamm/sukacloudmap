"""Offline preparation and mocked GET-only critical road extensions."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx
from pydantic import SecretStr
import pytest

from app.config import Settings
from app.planning_inputs import content_hash, encode
from app.road_service import DrivingRoad
from scripts.benchmark_algorithms import private_json, read_only, write_graph
import scripts.collect_critical_algorithm_roads as module
from test_algorithm_benchmark import measured_edges, problem


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table(path, name):
    with read_only(path) as db:
        return [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]


@pytest.fixture
def frozen_case(tmp_path):
    body = problem()
    body['quotes']['B'] = {'mode': 'KM', 'per_km': '2'}
    body['tasks'][2]['quote_id'] = 'B'
    root = tmp_path / 'original'
    root.mkdir()
    graph = root / 'frozen.db'
    meta = write_graph(graph, body, measured_edges(body, [('W', 'T1')]))
    input_file, case_file, result_file = root / 'input.json', root / 'case.json', root / 'result.json'
    private_json(input_file, body)
    private_json(case_file, {'body_file': 'input.json', 'road_database': str(graph),
                             'input_hash': content_hash(body), 'graph_hash': meta['graph_hash']})
    private_json(result_file, {'algorithm': {'road_stats': {'graph_hash': meta['graph_hash']},
        'diagnostics': {'missing_edges': [
            {'from_id': 'T1', 'to_id': 'T2', 'attempts': 4},
            {'from_id': 'W', 'to_id': 'T2', 'attempts': 10},
            {'from_id': 'T2', 'to_id': 'T1', 'attempts': 3},
            {'from_id': 'W', 'to_id': 'T1', 'attempts': 100},
            {'from_id': 'T1', 'to_id': 'T3', 'attempts': 999},
            {'from_id': 'T2', 'to_id': 'T2', 'attempts': 100},
        ]}}})
    return {'root': root, 'body': body, 'graph': graph, 'case': case_file, 'result': result_file,
            'files': [graph, input_file, case_file, result_file]}


def prepared(case, **kwargs):
    return module.prepare(case['case'], case['result'], **kwargs)


def mock_live(monkeypatch, tmp_path, handler):
    class NoWaitLimiter:
        def acquire(self, service, **kwargs):
            pass
        def defer(self, service, attempt):
            pass
    class MockDrivingRoad(DrivingRoad):
        def __init__(self, *args, **kwargs):
            kwargs.update(transport=httpx.MockTransport(handler), limiter=NoWaitLimiter())
            super().__init__(*args, **kwargs)
    settings = Settings(_env_file=None, environment='test', driving_live_calls_enabled=True,
        planning_route_mode='DRIVING', amap_web_service_key=SecretStr('fixture-not-a-real-key'),
        amap_rate_limit_path=tmp_path / 'shared-test-pacing.db',
        amap_reference_graph_path=tmp_path / 'must-not-create-stable-graph.db')
    monkeypatch.setattr(module, 'Settings', lambda **kwargs: settings)
    monkeypatch.setattr(module, 'DrivingRoad', MockDrivingRoad)
    return settings


def response(request):
    origin, destination = request.url.params['origin'], request.url.params['destination']
    return httpx.Response(200, json={'status': '1', 'infocode': '10000', 'route': {'paths': [
        {'distance': '2000', 'cost': {'duration': '120'},
         'steps': [{'polyline': origin + ';' + destination}]}]}})


def test_prepare_ranks_observed_count_and_opportunity_only_without_writing_sources(frozen_case):
    hashes = {path: sha(path) for path in frozen_case['files']}
    manifest = prepared(frozen_case)
    assert [(row['from_id'], row['to_id']) for row in manifest['jobs']] == [
        ('W', 'T2'), ('T1', 'T2'), ('T2', 'T1')]
    assert manifest['selected_directions'] == 3
    assert manifest['rejected']['ALREADY_MEASURED'] == 1
    assert manifest['rejected']['WAREHOUSE_OR_QUOTE_INCOMPATIBLE'] == 1
    assert manifest['ranking_is_a_proposal_not_a_saving_proof']
    assert all(sha(path) == value for path, value in hashes.items())


def test_live_mock_adds_only_missing_directions_and_preserves_every_old_metric_and_hint(
        frozen_case, tmp_path, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        return response(request)
    settings = mock_live(monkeypatch, tmp_path, handle)
    original_metrics, original_hints = table(frozen_case['graph'], 'metrics'), table(frozen_case['graph'], 'hints')
    original_hashes = {path: sha(path) for path in frozen_case['files']}
    output = tmp_path / 'new-isolated-collection'
    report = module.execute(prepared(frozen_case), output)
    assert report['status'] == 'COMPLETED'
    assert report['actual_http_attempts'] == len(requests) == 3
    assert report['new_directed_edges'] == 3
    assert report['source_business_database_written'] is False
    new_case = module.load_json(report['case_file'])
    assert new_case['input_hash'] == content_hash(frozen_case['body'])
    assert new_case['graph_hash'] != new_case['parent_graph_hash']
    assert table(new_case['road_database'], 'hints') == original_hints
    new_metrics = {row[0]: row for row in table(new_case['road_database'], 'metrics')}
    assert all(new_metrics[row[0]] == row for row in original_metrics)
    assert all(sha(path) == value for path, value in original_hashes.items())
    assert not settings.amap_reference_graph_path.exists()
    assert all((output / name).stat().st_mode & 0o777 == 0o600 for name in (
        'collection-cache.db', 'collected-reference.db', 'expanded-frozen-roads.db', 'jobs.json'))
    assert not Path(new_case['road_database'] + '-wal').exists()


def test_max_calls_is_global_and_counts_retry_get_attempts(frozen_case, tmp_path, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(429) if len(requests) == 1 else response(request)
    mock_live(monkeypatch, tmp_path, handle)
    report = module.execute(prepared(frozen_case, max_calls=2), tmp_path / 'limited')
    assert report['status'] == 'INCOMPLETE' and report['stop_reason'] == 'CALL_BUDGET'
    assert report['actual_http_attempts'] == len(requests) == 2
    assert sum(row['http_attempts'] for row in report['attempts']) == 2
    assert report['new_directed_edges'] == 1
    assert any(row.get('error_code') == 'ROAD_CALL_BUDGET' for row in report['attempts'])


def test_wait_deadline_prevents_following_get_and_reports_inflight_overrun(frozen_case, tmp_path, monkeypatch):
    clock, requests = [0.], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    def handle(request):
        requests.append(request)
        clock[0] = 5.
        return response(request)
    mock_live(monkeypatch, tmp_path, handle)
    report = module.execute(prepared(frozen_case, wait_seconds=1), tmp_path / 'deadline')
    assert len(requests) == report['actual_http_attempts'] == 1
    assert report['stop_reason'] == 'WAIT_DEADLINE'
    assert report['new_directed_edges'] == 1
    assert report['deadline_overrun_seconds'] == 4


def test_import_refuses_tampered_evidence_without_inventing_a_road(frozen_case, tmp_path):
    manifest = prepared(frozen_case)
    job = manifest['jobs'][0]
    imported = tmp_path / 'untrusted.db'
    write_graph(imported, frozen_case['body'], measured_edges(frozen_case['body'], [('W', 'T2')]))
    with sqlite3.connect(imported) as db:
        row = db.execute('SELECT provider_key,content FROM metrics').fetchone()
        item = module.decode_evidence(row[1])
        item['meters'] += 1
        db.execute('UPDATE metrics SET content=? WHERE provider_key=?',
                   (gzip.compress(encode(item).encode(), mtime=0), row[0]))
        db.commit()
    with pytest.raises(ValueError, match='integrity'):
        module.import_reference(imported, job)


def test_import_preserves_actual_original_evidence_hash_and_timestamp(frozen_case, tmp_path):
    job = prepared(frozen_case)['jobs'][0]
    original = measured_edges(frozen_case['body'], [('W', 'T2')])[0]
    imported = tmp_path / 'measured.db'
    write_graph(imported, frozen_case['body'], [original])
    item = module.import_reference(imported, job)
    assert item == original


def test_offline_import_never_reads_credentials_or_constructs_road_client_and_records_missing(
        frozen_case, tmp_path, monkeypatch):
    manifest = prepared(frozen_case)
    imported = tmp_path / 'existing-read-only-evidence.db'
    original = measured_edges(frozen_case['body'], [('W', 'T2')])[0]
    write_graph(imported, frozen_case['body'], [original])
    before = {path: sha(path) for path in [*frozen_case['files'], imported]}
    def forbidden_settings(**kwargs):
        raise AssertionError('Offline import must never read credentials')
    class NoRoadClient(DrivingRoad):
        def __init__(self, *args, **kwargs):
            raise AssertionError('Offline import must never create a road client')
    monkeypatch.setattr(module, 'Settings', forbidden_settings)
    monkeypatch.setattr(module, 'DrivingRoad', NoRoadClient)
    output = tmp_path / 'offline-expanded'
    report = module.execute(manifest, output, offline_import=True, evidence_databases=[imported])
    assert report['collection_mode'] == 'OFFLINE_VALIDATED_EVIDENCE_IMPORT'
    assert report['status'] == 'INCOMPLETE'
    assert report['stop_reason'] == 'OFFLINE_EVIDENCE_INCOMPLETE'
    assert report['actual_http_attempts'] == report['measured_provider_keys'] == 0
    assert report['new_directed_edges'] == report['imported_provider_keys'] == 1
    assert report['road_stats']['credentials_read'] is False
    assert report['road_stats']['http_client_created'] is False
    assert sum(row['status'] == 'MISSING' for row in report['attempts']) == 2
    assert not (output / 'collected-reference.db').exists()
    assert not (output / 'collection-cache.db').exists()
    assert all(sha(path) == value for path, value in before.items())
    case = module.load_json(report['case_file'])
    new_metrics = {row[0]: row for row in table(case['road_database'], 'metrics')}
    assert all(new_metrics[row[0]] == row for row in table(frozen_case['graph'], 'metrics'))
    assert table(case['road_database'], 'hints') == table(frozen_case['graph'], 'hints')
    assert case['parent_graph_hash'] != case['graph_hash']


def test_offline_import_requires_explicit_evidence_source(frozen_case, tmp_path, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError('Offline validation must not read Settings')
    monkeypatch.setattr(module, 'Settings', forbidden)
    with pytest.raises(ValueError, match='at least one'):
        module.execute(prepared(frozen_case), tmp_path / 'missing-offline-source', offline_import=True)
    assert not (tmp_path / 'missing-offline-source').exists()


def test_execute_refuses_output_overwrite_before_accessing_provider_settings(frozen_case, tmp_path, monkeypatch):
    output = tmp_path / 'already-there'
    output.mkdir()
    marker = output / 'keep.txt'
    marker.write_text('keep')
    def forbidden(**kwargs):
        raise AssertionError('Existing output must be rejected before provider setup')
    monkeypatch.setattr(module, 'Settings', forbidden)
    with pytest.raises(FileExistsError):
        module.execute(prepared(frozen_case), output)
    assert marker.read_text() == 'keep'


def test_prepared_source_and_job_tampering_are_rejected(frozen_case, tmp_path):
    manifest = prepared(frozen_case)
    changed = deepcopy(manifest)
    changed['jobs'][0]['vehicle']['max_pieces'] = 999
    with pytest.raises(ValueError, match='ranking'):
        module.execute(changed, tmp_path / 'do-not-create')
    assert not (tmp_path / 'do-not-create').exists()
    result = module.load_json(frozen_case['result'])
    result['algorithm']['road_stats']['graph_hash'] = 'wrong-graph'
    private_json(frozen_case['result'], result)
    with pytest.raises(ValueError, match='source changed'):
        module.validate_source(manifest)
    with pytest.raises(ValueError, match='exact frozen'):
        prepared(frozen_case)


@pytest.mark.parametrize('args', [(1, 24, 120), (12, -1, 120), (12, 97, 120),
                                  (12, 24, 0), (12, 24, float('nan')), (12, True, 120)])
def test_budgets_are_bounded_and_finite(args):
    with pytest.raises(ValueError):
        module.validate_limits(*args)
