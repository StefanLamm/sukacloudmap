import importlib.util
import errno
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/prepare-test-runtime.py'
    spec = importlib.util.spec_from_file_location('prepare_test_runtime', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def seed_database(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0006_group_workflows');
            CREATE TABLE stores (id TEXT PRIMARY KEY, business_open TEXT, business_close TEXT);
            INSERT INTO stores VALUES ('S1','08:00','18:00');
            CREATE TABLE login_sessions (id TEXT PRIMARY KEY);
            INSERT INTO login_sessions VALUES ('stable-session');
            CREATE TABLE login_throttles (id TEXT PRIMARY KEY);
            INSERT INTO login_throttles VALUES ('stable-throttle');
            CREATE TABLE location_tasks (id TEXT PRIMARY KEY);
            INSERT INTO location_tasks VALUES ('pending-location');
            CREATE TABLE planning_jobs (id TEXT PRIMARY KEY, status TEXT, cancel_requested INTEGER,
                lease_until INTEGER, finished_at INTEGER, error_code TEXT, error_message TEXT);
            INSERT INTO planning_jobs VALUES ('queued','QUEUED',0,999,NULL,NULL,NULL);
            INSERT INTO planning_jobs VALUES ('running','RUNNING',0,999,NULL,NULL,NULL);
            INSERT INTO planning_jobs VALUES ('done','COMPLETED',0,NULL,100,NULL,NULL);
        ''')


def logical_dump(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(db.iterdump())


def test_clone_sanitizes_only_new_copy_and_never_resets_existing(tmp_path):
    helper = module()
    source, target = tmp_path / 'stable.db', tmp_path / 'test.db'
    seed_database(source)
    before = logical_dump(source)
    helper.clone_test_database(source, target)
    assert logical_dump(source) == before
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_nlink == 1
    with sqlite3.connect(target) as db:
        for table in ('login_sessions', 'login_throttles', 'location_tasks'):
            assert db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
        assert db.execute('SELECT business_open,business_close FROM stores').fetchone() == ('08:00', '18:00')
        assert db.execute("SELECT COUNT(*) FROM planning_jobs WHERE status='CANCELLED' AND cancel_requested=1 AND lease_until IS NULL AND finished_at IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT status,finished_at FROM planning_jobs WHERE id='done'").fetchone() == ('COMPLETED', 100)
        db.execute("UPDATE stores SET business_open='09:15'")
        db.commit()
    retained = logical_dump(target)
    with pytest.raises(RuntimeError, match='拒绝覆盖'):
        helper.clone_test_database(source, target)
    assert logical_dump(target) == retained


def test_failed_sanitization_does_not_publish_raw_session_copy(tmp_path):
    helper = module()
    source, target = tmp_path / 'stable.db', tmp_path / 'test.db'
    seed_database(source)
    with sqlite3.connect(source) as db:
        db.execute('DROP TABLE location_tasks')
    before = logical_dump(source)
    with pytest.raises(sqlite3.OperationalError):
        helper.clone_test_database(source, target)
    assert not target.exists()
    assert list(tmp_path.glob('.test-copy-*')) == []
    assert logical_dump(source) == before


def workspace(tmp_path, monkeypatch, helper):
    root = tmp_path / 'workspace'
    stable = root / 'data/runtimes/stable'
    target = root / 'data/runtimes/test-working'
    stable.mkdir(parents=True)
    source = root / 'data/stable.db'
    seed_database(source)
    (stable / '.env').write_text('SUKA_DATABASE_URL=' + json.dumps('sqlite:///' + str(source)) + '\nSUKA_AMAP_RATE_LIMIT_PATH=' + json.dumps(str(root / 'data/shared-rate.db')) + '\n')
    (stable / 'data/external-test').mkdir(parents=True)
    (stable / 'data/external-test/settings.json').write_text('{"edge_discovery":"doh"}')
    (root / 'data/runtime-isolation.json').write_text(json.dumps({'stable_root': str(stable)}))
    (root / 'frontend/dist').mkdir(parents=True)
    (root / 'frontend/dist/index.html').write_text('test frontend')
    (root / 'requirements.lock').write_text('fixed dependencies\n')
    (root / 'sample.py').write_text('source = 1\n')
    (root / '.venv').mkdir()
    (root / '.cache').mkdir()
    (root / '.cache/cloudflared').write_text('fixture binary')
    monkeypatch.setattr(helper, 'ROOT', root)

    def git(args, **kwargs):
        if args[1] == 'branch':
            return 'test\n'
        if args[1] == 'rev-parse':
            return 'fixture-head\n'
        if args[1] == 'ls-files':
            return b'requirements.lock\0sample.py\0'
        raise AssertionError(args)

    monkeypatch.setattr(helper.subprocess, 'check_output', git)
    # Tests do not depend on whether a developer is running real local services.
    import socket
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda *args: errno.ECONNREFUSED)
    return root, stable, target, source


def test_snapshot_refresh_keeps_test_data_and_stable_untouched(tmp_path, monkeypatch):
    helper = module()
    root, stable, target, source = workspace(tmp_path, monkeypatch, helper)
    stable_before = logical_dump(source)
    helper.prepare()
    with sqlite3.connect(target / 'data/application.db') as db:
        db.execute("UPDATE stores SET business_open='10:30'")
        db.execute("INSERT INTO login_sessions VALUES ('test-session')")
        db.commit()
    retained = logical_dump(target / 'data/application.db')
    (root / 'sample.py').write_text('source = 2\n')
    helper.prepare()
    assert (target / 'sample.py').read_text() == 'source = 2\n'
    assert logical_dump(target / 'data/application.db') == retained
    assert logical_dump(source) == stable_before
    assert (stable / '.env').read_text().startswith('SUKA_DATABASE_URL=')
    assert 'test_local' in (target / '.env').read_text()
    assert (target / 'frontend/dist/index.html').read_text() == 'test frontend'


def test_dependency_change_fails_before_rewriting_snapshot(tmp_path, monkeypatch):
    helper = module()
    root, _, target, _ = workspace(tmp_path, monkeypatch, helper)
    helper.prepare()
    before = {path: (target / path).read_bytes() for path in ('requirements.lock', 'sample.py', '.env', 'source-manifest.json')}
    (root / 'requirements.lock').write_text('changed dependencies\n')
    (root / 'sample.py').write_text('source = 99\n')
    with pytest.raises(RuntimeError, match='依赖锁已变化'):
        helper.prepare()
    assert {path: (target / path).read_bytes() for path in before} == before


@pytest.mark.parametrize('relative', ['data', '.env', '.venv', 'frontend', 'requirements.lock'])
def test_snapshot_rejects_shared_symlinks_before_writes(tmp_path, monkeypatch, relative):
    helper = module()
    root, stable, target, _ = workspace(tmp_path, monkeypatch, helper)
    target.mkdir()
    (target / relative).symlink_to(stable)
    with pytest.raises(RuntimeError, match='链接'):
        helper.check_existing_runtime(target)


@pytest.mark.parametrize('code', [errno.EPERM, errno.EACCES, errno.ETIMEDOUT, errno.ENETUNREACH])
def test_unknown_port_state_cannot_rewrite_snapshot(tmp_path, monkeypatch, code):
    helper = module()
    _, _, target, _ = workspace(tmp_path, monkeypatch, helper)
    import socket
    monkeypatch.setattr(socket.socket, 'connect_ex', lambda *args: code)
    with pytest.raises(RuntimeError, match='无法确认测试端口空闲'):
        helper.prepare()
    assert not target.exists()


@pytest.mark.parametrize('returncode,stderr', [(1, 'Operation not permitted'), (2, ''), (1, 'Permission denied')])
def test_failed_process_query_cannot_rewrite_snapshot(tmp_path, monkeypatch, returncode, stderr):
    helper = module()
    root, _, target, _ = workspace(tmp_path, monkeypatch, helper)
    records = root / 'data/managed-services'
    records.mkdir()
    (records / 'test.json').write_text('{"pid":99999}')
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=returncode, stderr=stderr, stdout=''))
    with pytest.raises(RuntimeError, match='无法核实测试进程已停止'):
        helper.prepare()
    assert not target.exists()
