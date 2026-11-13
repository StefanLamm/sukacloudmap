"""Registered local test workers can read only their explicitly enabled graph."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.config import Settings
from app.road_service import DrivingRoad
from test_driving_reference_policy import NoWait
from test_road_graph import raw_write, sample
from test_routing_engine import truck_data


@pytest.fixture
def registered_runtime(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    stable = workspace / 'data/runtimes/stable-release'
    test = workspace / 'data/runtimes/test-working'
    for root in (stable, test):
        (root / 'data').mkdir(parents=True)
        with sqlite3.connect(root / 'data/application.db') as db:
            db.execute('CREATE TABLE sentinel(value TEXT PRIMARY KEY)')
            db.execute('INSERT INTO sentinel VALUES (?)', (root.name,))
    manifest = workspace / 'data/runtime-isolation.json'
    manifest.write_text(json.dumps({'stable_root': str(stable), 'test_root': str(test)}))
    values = {'environment': 'test', 'database_url': 'sqlite:///' + str(test / 'data/application.db'),
              'public_origin': 'http://127.0.0.1:8002', 'allowed_hosts': ['127.0.0.1', 'localhost'],
              'secure_cookie': False, 'cookie_namespace': 'test_local', 'map_auto_geocode_enabled': False,
              'amap_reference_graph_path': str(test / 'data/reference-graph.db'),
              'amap_reference_graph_test_enabled': True,
              'amap_rate_limit_path': str(workspace / 'data/rate-limit.db'),
              'amap_web_service_key': 'TEST_ONLY_RUNTIME_REFERENCE_KEY'}
    (test / '.env').write_text(''.join('SUKA_' + k.upper() + '=' + json.dumps(v) + '\n' for k, v in values.items()))
    settings = Settings(_env_file=test / '.env')
    monkeypatch.setattr('app.road_service.ROOT', test)
    return SimpleNamespace(workspace=workspace, stable=stable, test=test,
                           manifest=manifest, settings=settings)


def create_road(settings):
    return DrivingRoad(settings, call_limit=0, allow_historical=True, limiter=NoWait(),
        transport=httpx.MockTransport(lambda _: pytest.fail('Runtime graph smoke must not call HTTP')))


def test_default_test_settings_do_not_even_stat_the_configured_business_graph(settings, monkeypatch):
    assert settings.amap_reference_graph_test_enabled is False
    protected = settings.amap_reference_graph_path
    original = Path.stat
    def guarded(path, *args, **kwargs):
        if path == protected:
            raise AssertionError('A default unit test must not inspect the business graph')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', guarded)
    road = create_road(settings)
    try:
        assert road.graph is None and road.stats()['reference_graph_edges'] == 0
    finally:
        road.close()


def test_real_registered_test_runner_loads_measured_graph_without_http(registered_runtime, monkeypatch):
    env = registered_runtime
    vehicle, origin, destination = truck_data()
    key, receipt = sample(age=7200, origin=origin, destination=destination)
    raw_write(env.settings.amap_reference_graph_path, key, receipt)
    before = (env.test / 'data/application.db').read_bytes()
    path = Path(__file__).resolve().parents[1] / 'scripts/start-test.py'
    spec = importlib.util.spec_from_file_location('runtime_graph_runner', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'ROOT', env.test)
    # Exercise actual configuration loading and the actual startup isolation
    # guard, then the worker's actual road adapter; no enabling gate is mocked.
    configured = runner.test_settings()
    assert configured.amap_reference_graph_test_enabled
    road = create_road(configured)
    try:
        assert road.stats()['reference_graph_edges'] == 1
        assert road.known_segment(origin, destination)['meters'] == receipt['meters']
        bound = road.segment(vehicle, origin, destination, '2026-11-14T09:00:00+08:00')
        assert bound['received_at'] == receipt['received_at']
        assert bound['time_status'] == 'HISTORICAL_TRAFFIC_REFERENCE'
        assert bound['meters'] == receipt['meters'] and road.calls == 0
        assert road.stats()['reference_graph_edges'] == 1
    finally:
        road.close()
    assert (env.test / 'data/application.db').read_bytes() == before


@pytest.mark.parametrize('bad', ['outside', 'symlink', 'hardlink', 'graph_is_database',
                                  'database_outside', 'unregistered', 'workspace', 'parent_symlink', 'directory'])
def test_opt_in_cannot_open_shared_or_unregistered_graphs(registered_runtime, monkeypatch, bad):
    env = registered_runtime
    source = env.stable / 'data/reference-graph.db'
    key, receipt = sample()
    raw_write(source, key, receipt)
    before = source.read_bytes()
    path = env.settings.amap_reference_graph_path
    if bad == 'outside':
        env.settings.amap_reference_graph_path = source
    elif bad == 'symlink':
        path.symlink_to(source)
    elif bad == 'hardlink':
        os.link(source, path)
    elif bad == 'graph_is_database':
        env.settings.amap_reference_graph_path = env.test / 'data/application.db'
    elif bad == 'database_outside':
        env.settings.database_url = SecretStr('sqlite:///' + str(env.stable / 'data/application.db'))
    elif bad == 'unregistered':
        env.manifest.write_text(json.dumps({'stable_root': str(env.stable)}))
    elif bad == 'workspace':
        monkeypatch.setattr('app.road_service.ROOT', env.workspace)
    elif bad == 'parent_symlink':
        (env.test / 'data/alias').symlink_to(env.stable / 'data', target_is_directory=True)
        env.settings.amap_reference_graph_path = env.test / 'data/alias/reference-graph.db'
    elif bad == 'directory':
        path.mkdir()
    with pytest.raises(RuntimeError):
        create_road(env.settings)
    assert source.read_bytes() == before


def test_enabled_missing_graph_stays_empty_until_fresh_evidence_is_stored(registered_runtime):
    env = registered_runtime
    path = env.settings.amap_reference_graph_path
    assert not path.exists()
    road = create_road(env.settings)
    try:
        assert road.graph is not None and road.stats()['reference_graph_edges'] == 0
        assert not path.exists()
        key, receipt = sample(age=10)
        assert road.graph.put_reference(key, receipt)
        assert path.is_file() and path.stat().st_nlink == 1
        assert path.stat().st_mode & 0o777 == 0o600
        assert road.graph.get_recent(key) == receipt
        assert road.calls == 0
    finally:
        road.close()
    assert not (env.stable / 'data/reference-graph.db').exists()
