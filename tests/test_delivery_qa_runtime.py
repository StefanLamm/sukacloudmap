"""Third-loopback QA isolation; only temporary fixture SQLite files are touched."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from app.config import Settings
from scripts.runtime_isolation import runtime_roots, validate_test_runtime
from scripts.serve_delivery_qa import (backup_readonly, cancel_copied_queues, check, prepare,
                                       protected_table_hashes, verify_snapshot)


def database(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE accounts(id TEXT PRIMARY KEY,password_hash TEXT);
          CREATE TABLE login_sessions(token_hash TEXT PRIMARY KEY,account_id TEXT);
          CREATE TABLE plan_versions(id TEXT PRIMARY KEY,content TEXT);
          CREATE TABLE planning_jobs(id TEXT PRIMARY KEY,status TEXT,cancel_requested INTEGER,
             lease_until INTEGER,finished_at INTEGER,error_code TEXT,error_message TEXT);
          CREATE TABLE location_tasks(id TEXT PRIMARY KEY);
          INSERT INTO accounts VALUES('a','test-fixture-private-hash');
          INSERT INTO login_sessions VALUES('existing-session','a');
          INSERT INTO plan_versions VALUES('old-plan','existing-business-plan');
          INSERT INTO planning_jobs VALUES('job','RUNNING',0,999,NULL,NULL,NULL);
          INSERT INTO location_tasks VALUES('location');
        ''')


def env_file(root, origin='http://127.0.0.1:8002', namespace='test_local'):
    settings = {
        'environment': 'test', 'database_url': 'sqlite:///' + str(root / 'data/application.db'),
        'amap_reference_graph_path': str(root / 'data/reference-graph.db'),
        'amap_rate_limit_path': str(root.parents[1] / 'amap/rate-limit.db'),
        'public_origin': origin, 'allowed_hosts': ['127.0.0.1', 'localhost'], 'secure_cookie': False,
        'cookie_namespace': namespace, 'map_auto_geocode_enabled': False,
        'amap_web_service_key': 'test-fixture-do-not-print', 'driving_job_max_calls': 350,
    }
    (root / '.env').write_text('\n'.join('SUKA_' + k.upper() + '=' + json.dumps(v) for k, v in settings.items()) + '\n')


@pytest.fixture
def workspace(tmp_path):
    workspace = tmp_path / 'workspace'
    stable, test = (workspace / 'data/runtimes' / n for n in ('stable-original', 'test-original'))
    for root in (stable, test):
        (root / 'data').mkdir(parents=True)
        database(root / 'data/application.db')
        with sqlite3.connect(root / 'data/reference-graph.db') as db:
            db.execute('CREATE TABLE metrics(value TEXT)')
            db.execute("INSERT INTO metrics VALUES('measured-fixture')")
        env_file(root)
    (workspace / 'data/runtime-isolation.json').write_text(json.dumps({'stable_root': str(stable), 'test_root': str(test)}))
    return workspace, stable, test


def qa_record(workspace, name='qa-new'):
    root = workspace / 'data/runtimes' / name
    (root / 'data').mkdir(parents=True)
    database(root / 'data/application.db')
    env_file(root, 'http://127.0.0.1:8003', namespace='test_delivery_qa')
    (root / 'source-build-manifest.json').write_text('{}')
    registry = workspace / 'data/runtime-isolation.json'
    payload = json.loads(registry.read_text())
    payload.setdefault('qa_runtimes', {})[name] = {
        'root': str(root), 'public_origin': 'http://127.0.0.1:8003', 'cookie_namespace': 'test_delivery_qa',
        'source_manifest_sha256': hashlib.sha256((root / 'source-build-manifest.json').read_bytes()).hexdigest(),
    }
    registry.write_text(json.dumps(payload))
    return root


def test_explicit_qa_8003_does_not_change_original_test_8002_rule(workspace):
    ws, stable, test = workspace
    qa = qa_record(ws)
    settings = Settings(_env_file=qa / '.env')
    assert validate_test_runtime(qa, settings) == qa / 'data/application.db'
    assert runtime_roots(ws) == {'stable_root': stable, 'test_root': test}
    original = Settings(_env_file=test / '.env')
    assert validate_test_runtime(test, original) == test / 'data/application.db'
    assert settings.cookie_name != original.cookie_name
    with pytest.raises(RuntimeError, match='8002'):
        validate_test_runtime(test, original.model_copy(update={'public_origin': 'http://127.0.0.1:8003'}))
    with pytest.raises(RuntimeError, match='独立登录 Cookie'):
        validate_test_runtime(test, original.model_copy(update={'cookie_namespace': 'test_delivery_qa'}))
    for update in ({'public_origin': 'http://127.0.0.1:8002'}, {'environment': 'development'},
                   {'cookie_namespace': 'test_external'}, {'cookie_namespace': 'test_local'}):
        with pytest.raises(RuntimeError, match='8003'):
            validate_test_runtime(qa, settings.model_copy(update=update))


def test_unregistered_or_changed_qa_manifest_never_uses_original_database(workspace):
    ws, _, _ = workspace
    qa = qa_record(ws)
    settings = Settings(_env_file=qa / '.env')
    (qa / 'source-build-manifest.json').write_text('{"changed":true}')
    with pytest.raises(RuntimeError, match='QA 验收登记无效'):
        validate_test_runtime(qa, settings)


def test_qa_paths_remain_private_and_no_symlinked_database(workspace):
    ws, _, test = workspace
    qa = qa_record(ws)
    settings = Settings(_env_file=qa / '.env')
    with pytest.raises(RuntimeError, match='专属目录'):
        validate_test_runtime(qa, settings.model_copy(update={'database_url': Settings(_env_file=test / '.env').database_url}))
    (qa / 'data/application.db').unlink()
    (qa / 'data/application.db').symlink_to(test / 'data/application.db')
    with pytest.raises(RuntimeError, match='专属目录'):
        validate_test_runtime(qa, settings)


def test_backup_preserves_accounts_history_sessions_and_only_cancels_copy(workspace, tmp_path):
    _, _, test = workspace
    source = test / 'data/application.db'
    target = tmp_path / 'copy.db'
    before = protected_table_hashes(source)
    backup_readonly(source, target)
    cancel_copied_queues(target)
    assert protected_table_hashes(source) == before == protected_table_hashes(target)
    assert source.stat().st_ino != target.stat().st_ino and target.stat().st_nlink == 1
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT status FROM planning_jobs').fetchone()[0] == 'RUNNING'
        assert db.execute('SELECT COUNT(*) FROM location_tasks').fetchone()[0] == 1
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT status FROM planning_jobs').fetchone()[0] == 'CANCELLED'
        assert db.execute('SELECT COUNT(*) FROM location_tasks').fetchone()[0] == 0
        assert db.execute('SELECT token_hash FROM login_sessions').fetchone()[0] == 'existing-session'
    with pytest.raises(RuntimeError, match='拒绝覆盖'):
        backup_readonly(source, target)


def test_prepare_snapshots_only_isolated_source_and_appends_registry_without_serving(workspace):
    ws, stable, test = workspace
    source = ws / 'data/development/code'
    (source / 'app').mkdir(parents=True)
    (source / 'frontend/dist/assets').mkdir(parents=True)
    (source / 'app/routing_engine.py').write_text('# isolated candidate fixture\n')
    (source / 'frontend/dist/index.html').write_text('<script src="/assets/app.js"></script>')
    (source / 'frontend/dist/assets/app.js').write_text('/* built fixture */')
    (source / '.env').write_text('UNTRACKED_PRIVATE_FIXTURE=do-not-copy')
    subprocess.run(['git', 'init', '-b', 'test'], cwd=source, check=True, capture_output=True)
    subprocess.run(['git', 'add', 'app', 'frontend'], cwd=source, check=True, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-m', 'Fixture isolated source'], cwd=source, check=True, capture_output=True)
    before = {str(root): ((root / '.env').read_bytes(), protected_table_hashes(root / 'data/application.db')) for root in (stable, test)}
    result = prepare(ws, source, 'qa-integration')
    assert result['status'] == 'PREPARED_NOT_STARTED'
    target = Path(result['qa_root'])
    settings, checked = check(target)
    assert checked['status'] == 'QA_GUARDS_VALID' and settings.environment == 'test'
    assert settings.driving_job_max_calls is None and settings.truck_job_max_calls is None
    assert settings.cookie_namespace == 'test_delivery_qa' and 'UNTRACKED_PRIVATE_FIXTURE' not in (target / '.env').read_text()
    assert settings.amap_web_service_key.get_secret_value() == 'test-fixture-do-not-print'
    assert runtime_roots(ws) == {'stable_root': stable, 'test_root': test}
    for root in (stable, test):
        assert before[str(root)] == ((root / '.env').read_bytes(), protected_table_hashes(root / 'data/application.db'))
    proof = json.loads((target / 'data/preservation-proof.json').read_text())
    assert proof['source_before_equals_after'] and proof['copy_protected_equals_source']
    assert proof['no_listener_started'] and proof['no_service_stopped']
    assert (target / 'app/routing_engine.py').stat().st_ino != (source / 'app/routing_engine.py').stat().st_ino
    (target / 'frontend/dist/assets/app.js').write_text('changed after snapshot')
    with pytest.raises(RuntimeError, match='构建与已登记清单不符'):
        verify_snapshot(target)


def test_prepare_refuses_workspace_dirty_source_and_existing_runtime(workspace):
    ws, _, _ = workspace
    with pytest.raises(RuntimeError, match='独立源码'):
        prepare(ws, ws, 'qa-invalid')
