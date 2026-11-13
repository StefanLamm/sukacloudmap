"""Scientific comparability and the user deadline must survive orchestration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.planning_inputs import content_hash, encode
from app.routing_engine import assemble, normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad, write_graph
import scripts.run_algorithm_study as study
from test_algorithm_benchmark import problem, measured_edges


def test_waiting_manager_imports_no_application_math_before_source_ready():
    path = Path(study.__file__).resolve()
    code = "import runpy,sys;runpy.run_path(sys.argv[1],run_name='study_import');assert 'app.routing_engine' not in sys.modules;assert 'app.route_search' not in sys.modules"
    result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_source_gate_requires_exact_ready_hashes_and_refuses_source_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(study, 'ROOT', tmp_path)
    monkeypatch.setattr(study, 'MATH_SOURCES', ('math.py',))
    source = tmp_path / 'math.py';source.write_text('version = 1\n')
    hashes = study.source_hashes(['math.py'])
    gate = tmp_path / 'ready.json'
    study.atomic_json(gate, {'status': 'BUILDING', 'source_hashes': hashes})
    assert study.source_gate(gate) is None
    study.atomic_json(gate, {'status': 'READY', 'source_hashes': hashes})
    assert study.source_gate(gate)['source_hashes'] == hashes
    source.write_text('version = 2\n')
    with pytest.raises(study.StudyIntegrityError, match='source changed'):
        study.source_gate(gate)


def test_complete_graph_requires_every_preregistered_alias_measured(tmp_path):
    path = tmp_path / 'roads.db'
    with sqlite3.connect(path) as connection:
        connection.executescript('CREATE TABLE metadata(key TEXT,value TEXT);CREATE TABLE metrics(provider_key TEXT);CREATE TABLE edges(from_id TEXT,to_id TEXT,provider_key TEXT);')
        connection.executemany('INSERT INTO metadata VALUES (?,?)', [('complete', 'true'), ('expected_provider_keys', '2')])
        connection.executemany('INSERT INTO edges VALUES (?,?,?)', [('W', 'A', 'KA'), ('W', 'B', 'KB')])
        connection.execute('INSERT INTO metrics VALUES (?)', ('KA',));connection.commit()
    assert not study.graph_complete(path)
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO metrics VALUES (?)', ('KB',));connection.commit()
    assert study.graph_complete(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET value='false' WHERE key='complete'");connection.commit()
    assert not study.graph_complete(path)


def test_price_precedes_utilization_and_invalid_cheap_runs_are_excluded():
    def row(name, fee, *, status='VALID', valid=True, missing=0, meters=100):
        return {'case_name': 'main225', 'profile_name': name, 'stage': 'screen', 'status': status,
                'audit': {'valid': valid, 'unassigned_tasks': missing, 'total_fee': fee, 'road_meters': meters, 'used_vehicles': 4}}
    rows = [row('fewer-km-higher-fee', '100.00', meters=1), row('lowest-fee', '99.00', meters=1000),
            row('lowest-fee', '105.00'), row('wrong-amount', '.01', valid=False), row('incomplete', '.00', missing=1)]
    ranked = study.profile_ranking(rows, 'main225')
    assert [r['profile_name'] for r in ranked] == ['lowest-fee', 'fewer-km-higher-fee']
    assert ranked[0]['best_fee'] == '99.00' and ranked[0]['median_fee'] == '102.00'


def test_deployment_reserve_and_budget_plan_leave_the_confirmed_cutoff():
    deadline = 1794535675
    cutoff = deadline - 2700
    assert study.datetime.fromtimestamp(cutoff, study.BEIJING).isoformat() == '2026-11-13T09:22:55+08:00'
    assert study.can_start(0, 100, 60, margin=25)
    assert not study.can_start(0, 85, 60, margin=25)
    config = {'screen_seconds': 60, 'small_seconds': 5, 'refine_seconds': 300, 'long_seconds': 900, 'cold_seconds': 180}
    assert sum(study.planned_seconds(config).values()) == 15710


def test_resume_identity_ignores_measurement_age_but_not_input_or_graph():
    before = {'case': {'input_hash': 'I', 'graph_hash': 'G', 'snapshot_id': 'S',
                        'required_directed_edges': 9, 'baseline_file': 'baseline.json',
                        'road_stats': {'oldest_measurement_age_seconds': 100}}}
    after = deepcopy(before);after['case']['road_stats']['oldest_measurement_age_seconds'] = 200
    assert study.case_identities(before) == study.case_identities(after)
    after['case']['input_hash'] = 'CORRECTED_INPUT'
    assert study.case_identities(before) != study.case_identities(after)


def test_formal_cases_use_collection_export_and_reject_changed_export(tmp_path, monkeypatch):
    body = problem();data = normalize(body)
    collection = tmp_path / 'collection';collection.mkdir()
    graph = collection / 'full-roads.db';write_graph(graph, body, measured_edges(body))
    with sqlite3.connect(graph) as connection:
        connection.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', [('complete', 'true'), ('expected_provider_keys', '9')]);connection.commit()
    study.atomic_json(collection / 'collection-input.json', body)
    baseline = assemble(data, [], list(data['tasks']), [])
    study.atomic_json(collection / 'collection-baseline.json', {'input': body, 'input_hash': content_hash(body), 'result': baseline})
    observed = []
    def historical(template, database, day, output, *, road_database):
        observed.append(content_hash(template))
        road = FrozenEvidenceRoad(road_database);digest = road.graph_hash;road.close()
        case = {'graph_hash': digest, 'body_file': 'input.json', 'input_hash': content_hash(template), 'baseline_file': 'baseline.json'}
        study.atomic_json(Path(output) / 'input.json', template)
        study.atomic_json(Path(output) / 'baseline.json', {'result': baseline})
        study.atomic_json(Path(output) / 'case.json', case)
        return case
    monkeypatch.setattr('scripts.benchmark_algorithms.freeze_historical_case', historical)
    config = {'collection_folder': str(collection), 'graph_database': str(graph),
              'output': str(tmp_path / 'study'), 'history_database': str(tmp_path / 'unreadable-history.db')}
    cases = study.prepare_cases(config)
    assert len(cases) == 4 and observed == [content_hash(body)] * 3
    actual = json.loads((tmp_path / 'study/cases/main225/input.json').read_text())
    assert actual == body
    changed = deepcopy(body);changed['tasks'][0]['pieces'] += 1
    study.atomic_json(collection / 'collection-input.json', changed)
    with pytest.raises(study.StudyIntegrityError, match='input hashes differ'):
        study.prepare_cases(config)


def test_a_run_that_changes_frozen_source_never_gets_a_completed_record(tmp_path, monkeypatch):
    monkeypatch.setattr(study, 'ROOT', tmp_path)
    source = tmp_path / 'math.py';source.write_text('version = 1\n')
    expected = study.source_hashes(['math.py'])
    spec = {'source_hashes': expected, 'profile': {'name': 'fake', 'entrypoint': 'a:b', 'options': {}},
            'case_file': str(tmp_path / 'case.json'), 'budget_seconds': 1, 'seed': 0, 'run_output': str(tmp_path / 'run'),
            'warm_start': True, 'case_name': 'main225', 'stage': 'screen', 'source_manifest_hash': 'HASH', 'hard_seconds': 26}
    specification = tmp_path / 'spec.json';study.atomic_json(specification, spec)
    study.atomic_json(tmp_path / 'input.json', {})
    study.atomic_json(tmp_path / 'case.json', {'body_file': 'input.json', 'input_hash': content_hash({}), 'graph_hash': 'G', 'baseline_file': None})
    def changes_source(*args, **kwargs):
        source.write_text('version = 2\n')
        return {'runs': [{'status': 'VALID'}]}
    monkeypatch.setattr('scripts.benchmark_algorithms.run_benchmark', changes_source)
    with pytest.raises(study.StudyIntegrityError, match='source changed'):
        study.run_one(specification)
    assert not (tmp_path / 'run/completed-run.json').exists()


def test_original_warm_baseline_cannot_silently_drift_after_case_freeze(tmp_path):
    study.atomic_json(tmp_path / 'input.json', {'tasks': []})
    study.atomic_json(tmp_path / 'baseline.json', {'total_fee': '100.00'})
    case = {'body_file': 'input.json', 'input_hash': content_hash({'tasks': []}), 'graph_hash': 'G',
            'baseline_file': 'baseline.json', 'baseline_hash': content_hash({'total_fee': '100.00'})}
    study.atomic_json(tmp_path / 'case.json', case)
    assert study.validate_case_files(tmp_path / 'case.json', case) == case
    study.atomic_json(tmp_path / 'baseline.json', {'total_fee': '90.00'})
    with pytest.raises(study.StudyIntegrityError, match='warm baseline changed'):
        study.validate_case_files(tmp_path / 'case.json', case)


def test_worker_resource_units_and_cpu_scope_are_explicit():
    before = SimpleNamespace(ru_utime=1., ru_stime=.5, ru_maxrss=1)
    after = SimpleNamespace(ru_utime=3.5, ru_stime=1.25, ru_maxrss=2 * 1024**2)
    mac = study.worker_resources(before, after, 4.5, platform_name='darwin')
    assert mac['cpu_user_seconds'] == 2.5 and mac['cpu_system_seconds'] == .75
    assert mac['cpu_total_seconds'] == 3.25 and mac['lifetime_peak_rss_mib'] == 2
    assert mac['raw_ru_maxrss_unit'] == 'bytes'
    linux = study.worker_resources(before, after, 4.5, platform_name='linux')
    assert linux['lifetime_peak_rss_mib'] == 2048
    assert linux['raw_ru_maxrss_unit'] == 'kibibytes'
    assert linux['peak_rss_scope'] == 'FRESH_WORKER_LIFETIME_NOT_ALGORITHM_NET_PEAK'


def test_runtime_records_true_installed_versions_and_only_named_thread_settings(monkeypatch):
    monkeypatch.setenv('OMP_NUM_THREADS', '1')
    monkeypatch.setenv('DO_NOT_INCLUDE_PRIVATE_TEST_VALUE', 'not-a-runtime-field')
    actual = study.runtime_versions()
    assert actual['python'] == sys.version
    assert actual['packages']['scipy'] == study.metadata.version('scipy')
    assert actual['packages']['numpy'] == study.metadata.version('numpy')
    assert actual['packages']['ortools'] == study.metadata.version('ortools')
    assert actual['thread_environment_settings']['OMP_NUM_THREADS'] == '1'
    assert 'DO_NOT_INCLUDE_PRIVATE_TEST_VALUE' not in actual['thread_environment_settings']
    assert actual['thread_environment_scope'] == 'ENVIRONMENT_SETTINGS_ONLY_NOT_MEASURED_NATIVE_THREAD_COUNTS'
