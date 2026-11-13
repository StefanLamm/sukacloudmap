"""Runtime routing and fail-closed data guards; no real service is restarted."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from scripts.runtime_isolation import clean_environment, runtime_roots, service_root, validate_test_runtime


def load_script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / name
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manager = load_script('manage-services.py')
test_runner = load_script('start-test.py')
launcher = load_script('start-external-test.py')


def test_test_banner_does_not_mislabel_live_roads_or_expose_paths(client, settings):
    settings.runtime_label = '测试版 · 独立数据，修改不影响稳定版'
    response = client.get('/api/runtime')
    assert response.status_code == 200
    assert response.json() == {
        'environment': 'test', 'test_fixture': False,
        'default_delivery_date': None, 'label': settings.runtime_label,
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    stable = workspace / 'data/runtimes/stable-release'
    test = workspace / 'data/runtimes/test-working'
    for root in (stable, test):
        (root / 'data').mkdir(parents=True)
        with sqlite3.connect(root / 'data/application.db') as db:
            db.execute('CREATE TABLE example(value TEXT)')
            db.execute('INSERT INTO example VALUES (?)', (root.name,))
        (root / '.env').write_text(
            f'SUKA_DATABASE_URL=sqlite:///{root / "data/application.db"}\n'
            f'SUKA_AMAP_REFERENCE_GRAPH_PATH={root / "data/reference-graph.db"}\n'
            f'SUKA_AMAP_RATE_LIMIT_PATH={workspace / "data/amap/rate-limit.db"}\n'
            'SUKA_MAP_AUTO_GEOCODE_ENABLED=false\n'
        )
    manifest = workspace / 'data/runtime-isolation.json'
    manifest.write_text(json.dumps({'stable_root': str(stable), 'test_root': str(test)}))
    managed = workspace / 'data/managed-services'
    managed.mkdir()
    monkeypatch.setattr(manager, 'ROOT', workspace)
    monkeypatch.setattr(manager, 'FOLDER', managed)
    monkeypatch.setattr(test_runner, 'ROOT', test)
    settings = Settings(_env_file=test / '.env', environment='test',
                        public_origin='http://127.0.0.1:8002',
                        cookie_namespace='test_local', secure_cookie=False,
                        allowed_hosts=['127.0.0.1', 'localhost'])
    return SimpleNamespace(workspace=workspace, stable=stable, test=test,
                           manifest=manifest, settings=settings)


def test_both_service_pairs_resolve_to_separate_code_and_python(isolated):
    env = isolated
    for name, root, port, entry in [
        ('main', env.stable, 8000, 'start.sh'),
        ('external', env.stable, 8770, 'start-external-test.py'),
        ('test', env.test, 8002, 'start-test.py'),
        ('test-external', env.test, 8771, 'start-external-test.py'),
    ]:
        actual_port, command, cwd = manager.service_spec(name)
        assert (actual_port, cwd) == (port, root)
        assert str(root / 'scripts' / entry) in command
        if name != 'main':
            assert command[0] == str(root / '.venv/bin/python')
    assert manager.service_spec('test-external')[1][-2:] == ['--port', '8771']


def test_stable_can_be_frozen_before_test_is_ready(isolated):
    env = isolated
    env.manifest.write_text(json.dumps({'stable_root': str(env.stable)}))
    assert service_root(env.workspace, 'main') == env.stable
    assert manager.all_services('start') == ['main']
    with pytest.raises(RuntimeError, match='尚未准备'):
        manager.service_spec('test')


@pytest.mark.parametrize('bad', ['missing_stable', 'same', 'outside', 'symlink', 'malformed'])
def test_invalid_pointer_never_falls_back_to_live_workspace(isolated, bad):
    env = isolated
    content = {'stable_root': str(env.stable), 'test_root': str(env.test)}
    if bad == 'missing_stable':
        del content['stable_root']
    elif bad == 'same':
        content['test_root'] = str(env.stable)
    elif bad == 'outside':
        content['test_root'] = str(env.workspace)
    elif bad == 'symlink':
        link = env.test.parent / 'alias'
        link.symlink_to(env.test, target_is_directory=True)
        content['test_root'] = str(link)
    env.manifest.write_text('not json' if bad == 'malformed' else json.dumps(content))
    with pytest.raises(RuntimeError, match='未回退'):
        manager.service_spec('main')


def test_missing_manifest_does_not_create_a_test_service_on_business_database(isolated):
    env = isolated
    env.manifest.unlink()
    with pytest.raises(RuntimeError, match='尚未准备'):
        manager.service_spec('test')


@pytest.mark.parametrize('name', ['trial', 'history', 'driving'])
def test_legacy_sources_cannot_start_once_runtime_isolation_enabled(isolated, name):
    with pytest.raises(RuntimeError, match='旧验收入口'):
        manager.start(name)
    assert manager.all_services('start') == ['main', 'test']
    assert name in manager.all_services('stop')


def test_old_workspace_process_cannot_be_treated_as_frozen_main(isolated, monkeypatch):
    env = isolated
    monkeypatch.setattr(manager, 'owned_pid', lambda name: 12345)
    monkeypatch.setattr(manager, 'identity', lambda pid: str(env.workspace / '.venv/bin/python') + ' -m uvicorn')
    assert manager.ready_url('main') is None
    with pytest.raises(RuntimeError, match='旧目录'):
        manager.start('main')


def test_managed_main_tracks_its_own_shell_exec_before_readiness(isolated, monkeypatch):
    env = isolated
    pid = 12345
    current = ['before launch']
    captured = {}

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def connect_ex(self, address):
            assert address == ('127.0.0.1', 8000)
            return 1

    class Process:
        def __init__(self): self.pid = pid
        def poll(self):
            current[0] = str(env.stable / '.venv/bin/python') + ' -m uvicorn'
            return None

    def popen(command, **kwargs):
        current[0] = '/bin/bash ' + str(env.stable / 'scripts/start.sh')
        captured.update(command=command, **kwargs)
        return Process()

    monkeypatch.setattr(manager.socket, 'socket', Socket)
    monkeypatch.setattr(manager.subprocess, 'Popen', popen)
    monkeypatch.setattr(manager, 'identity', lambda supplied: current[0])
    monkeypatch.setattr(manager.os, 'getpgid', lambda supplied: pid)
    monkeypatch.setattr(manager.httpx, 'get', lambda *args, **kwargs: httpx.Response(200, json={'status': 'ok'}))
    monkeypatch.setenv('SUKA_DATABASE_URL', 'sqlite:///unintended-business.db')
    manager.start('main')
    assert captured['cwd'] == env.stable
    assert captured['command'] == ['/bin/bash', str(env.stable / 'scripts/start.sh')]
    assert captured['start_new_session'] is True
    assert 'SUKA_DATABASE_URL' not in captured['env']
    saved = json.loads((manager.FOLDER / 'main.json').read_text())
    assert saved == {'pid': pid, 'identity': current[0]}
    assert 'uvicorn' in saved['identity']


def test_clean_environment_ignores_terminal_overrides_and_preserves_path(monkeypatch):
    monkeypatch.setenv('SUKA_DATABASE_URL', 'sqlite:///main.db')
    monkeypatch.setenv('suka_amap_reference_graph_path', '/main/reference.db')
    monkeypatch.setenv('TUNNEL_TOKEN', 'not-a-real-token')
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    env = clean_environment()
    assert not any(key.upper().startswith(('SUKA_', 'TUNNEL_')) for key in env)
    assert env['PATH'] == '/usr/bin:/bin'


def test_test_runner_ignores_inherited_main_data_and_keeps_shared_rate_limiter(isolated, monkeypatch):
    env = isolated
    monkeypatch.setenv('SUKA_DATABASE_URL', 'sqlite:///' + str(env.stable / 'data/application.db'))
    monkeypatch.setenv('suka_amap_reference_graph_path', str(env.stable / 'data/reference-graph.db'))
    settings = test_runner.test_settings()
    assert validate_test_runtime(env.test, settings) == env.test / 'data/application.db'
    assert settings.amap_reference_graph_path == env.test / 'data/reference-graph.db'
    assert settings.amap_rate_limit_path == env.workspace / 'data/amap/rate-limit.db'
    assert settings.cookie_name == 'suka_test_local_session'
    assert not settings.map_auto_geocode_enabled


@pytest.mark.parametrize('change', ['business_database', 'business_graph', 'same_cache', 'missing', 'symlink', 'hardlink', 'postgres'])
def test_test_data_guard_rejects_shared_missing_and_aliased_files(isolated, change):
    env = isolated
    settings = env.settings
    database = env.test / 'data/application.db'
    if change == 'business_database':
        settings = settings.model_copy(update={'database_url': env.settings.database_url.__class__('sqlite:///' + str(env.stable / 'data/application.db'))})
    elif change == 'business_graph':
        settings = settings.model_copy(update={'amap_reference_graph_path': env.stable / 'data/reference-graph.db'})
    elif change == 'same_cache':
        settings = settings.model_copy(update={'amap_reference_graph_path': database})
    elif change == 'missing':
        database.unlink()
    elif change == 'symlink':
        database.unlink()
        database.symlink_to(env.stable / 'data/application.db')
    elif change == 'hardlink':
        database.unlink()
        os.link(env.stable / 'data/application.db', database)
    elif change == 'postgres':
        settings = settings.model_copy(update={'database_url': env.settings.database_url.__class__('postgresql://localhost/main')})
    with pytest.raises(RuntimeError, match='测试|缓存'):
        validate_test_runtime(env.test, settings)


@pytest.mark.parametrize('updates', [
    {'cookie_namespace': ''}, {'cookie_namespace': 'stable'}, {'environment': 'development'},
    {'public_origin': 'http://127.0.0.1:8000'}, {'map_auto_geocode_enabled': True},
])
def test_test_data_guard_requires_isolated_login_and_background_policy(isolated, updates):
    with pytest.raises(RuntimeError):
        validate_test_runtime(isolated.test, isolated.settings.model_copy(update=updates))


def test_test_origin_preserves_strict_https_protection(isolated):
    settings = test_runner.test_settings('https://isolated-test.trycloudflare.com')
    assert settings.environment == 'production'
    assert settings.secure_cookie
    assert settings.cookie_name == '__Host-suka_test_external_session'
    assert settings.allowed_hosts == ['isolated-test.trycloudflare.com']
    assert settings.public_origin == 'https://isolated-test.trycloudflare.com'
    assert validate_test_runtime(isolated.test, settings) == isolated.test / 'data/application.db'


@pytest.mark.parametrize('url', ['http://x.trycloudflare.com', 'https://evil.example',
                               'https://x.trycloudflare.com@evil.example',
                               'https://x.trycloudflare.com/path', 'https://x.trycloudflare.com?query=1'])
def test_test_origin_rejects_unexpected_public_destinations(isolated, url):
    with pytest.raises(ValueError):
        test_runner.test_settings(url)


def test_launcher_rejects_workspace_before_any_public_tunnel(isolated, monkeypatch):
    monkeypatch.setattr(launcher, 'ROOT', isolated.workspace)
    with pytest.raises(RuntimeError, match='manage-services'):
        launcher.ensure_runtime_launch()


def test_external_ports_cannot_swap_stable_and_test_profiles(isolated, monkeypatch):
    monkeypatch.setattr(launcher, 'ROOT', isolated.test)
    launcher.ensure_runtime_launch(8771)
    with pytest.raises(RuntimeError, match='不匹配'):
        launcher.ensure_runtime_launch(8770)
    monkeypatch.setattr(launcher, 'ROOT', isolated.stable)
    launcher.ensure_runtime_launch(8770)
    with pytest.raises(RuntimeError, match='不匹配'):
        launcher.ensure_runtime_launch(8771)


@pytest.mark.parametrize('service, port, host', [
    ('external', 8770, 'stable-test.trycloudflare.com'),
    ('test-external', 8771, 'isolated-test.trycloudflare.com'),
])
def test_external_readiness_uses_its_runtime_and_checks_actual_public_route(isolated, monkeypatch, service, port, host):
    root = isolated.stable if service == 'external' else isolated.test
    folder = root / 'data/external-test'
    folder.mkdir()
    url = 'https://' + host
    (folder / 'current.json').write_text(json.dumps({'status': 'RUNNING', 'port': port, 'url': url}))
    monkeypatch.setattr(manager, 'owned_pid', lambda name: 12345)
    monkeypatch.setattr(manager, 'identity', lambda pid: str(root / '.venv/bin/python') + ' launcher')
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={'status': 'ok'})

    monkeypatch.setattr(manager.httpx, 'get', get)
    assert manager.ready_url(service) == url
    assert calls[0][0] == f'http://127.0.0.1:{port}/healthz'
    assert calls[0][1]['headers'] == {'host': host}
    assert calls[1][0] == url + '/healthz'

    def disconnected(url, **kwargs):
        if url.startswith('https:'):
            raise httpx.ConnectError('tunnel disconnected')
        return httpx.Response(200, json={'status': 'ok'})

    monkeypatch.setattr(manager.httpx, 'get', disconnected)
    assert manager.ready_url(service) is None
