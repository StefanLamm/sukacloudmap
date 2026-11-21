from importlib.metadata import PackageNotFoundError, version
import subprocess
from time import monotonic

import pytest

from app import route_pyvrp_runtime as runtime
from app.routing_engine import StopSearch


def model_group():
    tasks = {
        key: {'warehouse_id': 'w', 'signature': 'km', 'quote': {'mode': 'KM', 'per_km': '5.8'},
              'open_at': '2026-01-02T00:00:00', 'close_at': '2026-01-02T01:00:00', 'pieces': 1,
              'longitude': x, 'latitude': 0, 'unload_minutes': 1, 'common_unit_price': '100'}
        for key, x in [('a', 1), ('b', 2)]
    }
    return {'tasks': tasks,
            'warehouses': {'w': {'id': 'w', 'earliest_departure_at': '2026-01-01T23:59:00'}},
            'vehicles': {'active': {'available': True, 'warehouse_id': 'w', 'max_pieces': 2},
                         'unavailable': {'available': False, 'warehouse_id': 'w', 'max_pieces': 4}}}


ALL_DIRECTIONS = [(None, 'a', 100, 30), (None, 'b', 200, 30), ('a', 'b', 200, 30), ('b', 'a', 300, 30)]


def fake_child(monkeypatch, code):
    old_popen = subprocess.Popen
    started = []

    def popen(command, **kwargs):
        process = old_popen([command[0], '-B', '-c', code, command[-1]], **kwargs)
        started.append((process, kwargs['env']))
        return process

    monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
    return started


@pytest.mark.parametrize('reason', ['CANCELLED', 'WALL_BUDGET', 'TIME_BUDGET'])
def test_upstream_stop_reaps_child_and_preserves_exception(monkeypatch, reason):
    processes = fake_child(monkeypatch, 'import time; time.sleep(20)')
    error = StopSearch(reason)

    def control():
        if processes:
            raise error

    with pytest.raises(StopSearch) as stopped:
        runtime.generate(model_group(), ALL_DIRECTIONS, seconds=2, seed=0, control=control)
    assert stopped.value is error
    assert len(processes) == 1 and processes[0][0].poll() is not None


def test_local_budget_returns_empty_even_when_native_setup_cannot_stop(monkeypatch):
    processes = fake_child(monkeypatch, 'import time; time.sleep(20)')
    started = monotonic()
    proposals, diagnostics = runtime.generate(model_group(), ALL_DIRECTIONS, seconds=.25, seed=0, control=lambda: None)
    assert proposals == [] and diagnostics['stop_reason'] == 'TIME_BUDGET'
    assert monotonic() - started < 3
    assert len(processes) == 1 and processes[0][0].poll() is not None


def test_valid_proposals_retain_observed_orders_and_only_local_package_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('SUKA_TEST_SETTING', 'private')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///private.db')
    code = """
import json, sys
from pathlib import Path
message = {'schema': 1, 'proposals': [[0, 0, [0]], [0, 0, [0, 1]], [0, 0, [1, 0]]],
           'diagnostics': {'stop_reason': 'SEARCH_COMPLETED', 'version': '0.14.0'}}
(Path(sys.argv[1]) / 'result.json').write_text(json.dumps(message))
"""
    processes = fake_child(monkeypatch, code)
    proposals, diagnostics = runtime.generate(model_group(), ALL_DIRECTIONS, seconds=2, seed=3, control=lambda: None)
    assert diagnostics['stop_reason'] == 'SEARCH_COMPLETED'
    assert proposals == [{'vehicle_id': 'active', 'task_ids': ids} for ids in [['a'], ['a', 'b'], ['b', 'a']]]
    child_env = processes[0][1]
    assert child_env['PYTHONPATH'] == str(tmp_path.resolve())
    assert child_env.keys().isdisjoint({'SUKA_TEST_SETTING', 'DATABASE_URL'})


@pytest.mark.parametrize('code', [
    'raise SystemExit(7)',
    "import sys; from pathlib import Path; (Path(sys.argv[1]) / 'result.json').write_text('{')",
    """import json, sys
from pathlib import Path
message = {'schema': 1, 'proposals': [[0, 0, [1, 0]]],
           'diagnostics': {'stop_reason': 'SEARCH_COMPLETED', 'version': '0.14.0'}}
(Path(sys.argv[1]) / 'result.json').write_text(json.dumps(message))""",
])
def test_bad_worker_output_yields_no_proposal(monkeypatch, code):
    fake_child(monkeypatch, code)
    proposals, diagnostics = runtime.generate(model_group(), [(None, 'a', 10, 10)], seconds=2, seed=0,
                                               control=lambda: None)
    assert proposals == [] and diagnostics['stop_reason'] == 'PYVRP_WORKER_ERROR'


@pytest.mark.parametrize('installed,reason', [(None, 'PYVRP_UNAVAILABLE'), ('9.9', 'PYVRP_VERSION_MISMATCH')])
def test_child_reports_optional_package_status(monkeypatch, tmp_path, installed, reason):
    def find_version(_name):
        if installed is None:
            raise PackageNotFoundError('pyvrp')
        return installed

    monkeypatch.setattr(runtime, 'version', find_version)
    payload, *_ = runtime._request(model_group(), ALL_DIRECTIONS, monotonic() + 2, 0, lambda: None)
    runtime._write_json(tmp_path / 'request.json', payload)
    runtime._worker(tmp_path)
    actual = runtime._read_json(tmp_path / 'result.json')
    assert actual['proposals'] == [] and actual['diagnostics']['stop_reason'] == reason


def test_arrival_before_opening_and_incompatible_quote_are_declined():
    data = model_group()
    data['tasks']['a']['latest_arrival_at'] = '2026-01-01T23:59:45'
    with pytest.raises(ValueError):
        runtime.group_facts(data)
    data['tasks']['a'].pop('latest_arrival_at')
    data['tasks']['b']['signature'] = 'different'
    with pytest.raises(ValueError):
        runtime.group_facts(data)


def require_exact_pyvrp():
    try:
        installed = version('pyvrp')
    except PackageNotFoundError:
        pytest.skip('PyVRP 0.14.0 is optional.')
    if installed != '0.14.0':
        pytest.skip('The experiment requires exact PyVRP 0.14.0.')


def test_exact_optional_pyvrp_child_generates_measured_open_overnight_route():
    require_exact_pyvrp()
    data = model_group()
    for task in data['tasks'].values():
        task['common_unit_price'] = None
    data['tasks']['b']['latest_arrival_at'] = '2026-01-02T00:01:30'
    proposals, diagnostics = runtime.generate(data, [(None, 'a', 100, 30), ('a', 'b', 200, 30)], seconds=1,
                                               seed=0, control=lambda: None)
    assert diagnostics['stop_reason'] == 'SEARCH_COMPLETED'
    assert diagnostics['measured_directions'] == 2 and diagnostics['missing_directions'] == 2
    assert {'vehicle_id': 'active', 'task_ids': ['a', 'b']} in proposals


@pytest.mark.parametrize('size', [152, 200])
def test_group_scale_child_returns_proposals_inside_generation_allowance(size):
    require_exact_pyvrp()
    data = model_group()
    template = data['tasks']['a']
    data['tasks'] = {str(i): {**template, 'longitude': i % 16, 'latitude': i // 16, 'pieces': 10,
                             'close_at': '2026-01-02T11:00:00'} for i in range(size)}
    data['vehicles'] = {str(i): {'available': True, 'warehouse_id': 'w', 'max_pieces': 400} for i in range(18)}
    rows = [(None, str(j), 800 + 10 * j, 30 + j) for j in range(size)]
    rows += [(str(i), str(j), 50 + 20 * abs(i - j), 10 + abs(i - j))
             for i in range(size) for j in range(size) if i != j]
    proposals, diagnostics = runtime.generate(data, rows, seconds=8, seed=0, control=lambda: None)
    assert diagnostics['stop_reason'] == 'SEARCH_COMPLETED', diagnostics
    assert diagnostics['elapsed_seconds'] <= 8
    assert diagnostics['measured_directions'] == size**2
    assert proposals and all(set(row) == {'vehicle_id', 'task_ids'} for row in proposals)
