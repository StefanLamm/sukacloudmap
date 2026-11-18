"""Exercise manager scheduling with fake workers, clocks and independent audits."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import evaluate_delivery_stages as study


def test_disabled_pause_has_no_io_clock_or_callback(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled control must not do work')
    monkeypatch.setattr(study, 'save', forbidden)
    monkeypatch.setattr(study, 'time', SimpleNamespace(monotonic=forbidden, time=forbidden, sleep=forbidden))
    periods = []
    assert study.wait_between_runs(None, tmp_path, {}, periods=periods, changed=forbidden) is None
    assert periods == []
    assert not (tmp_path / 'pause-state.json').exists()


def test_absent_pause_does_not_create_ack_or_consume_time(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Absent control must not wait')
    monkeypatch.setattr(study, 'time', SimpleNamespace(monotonic=forbidden, time=forbidden, sleep=forbidden))
    periods = []
    assert study.wait_between_runs(tmp_path / 'control', tmp_path, {}, periods=periods, changed=forbidden) is None
    assert periods == []
    assert not (tmp_path / 'pause-state.json').exists()


def test_pause_rejects_relative_or_self_acknowledging_control(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        study.wait_between_runs('relative', tmp_path, {}, periods=[])
    with pytest.raises(ValueError, match='different'):
        study.wait_between_runs(tmp_path / 'pause-state.json', tmp_path, {}, periods=[])
    with pytest.raises(ValueError, match='different'):
        study.wait_between_runs(tmp_path / 'missing/../pause-state.json', tmp_path, {}, periods=[])
    alias = tmp_path / 'control-alias'
    alias.symlink_to(tmp_path / 'pause-state.json')
    with pytest.raises(ValueError, match='different'):
        study.wait_between_runs(alias, tmp_path, {}, periods=[])
    with pytest.raises(SystemExit):
        study.parser().parse_args(['--pause-file', 'relative'])


def test_pause_acknowledges_then_waits_for_operator_and_records_interval(monkeypatch, tmp_path, capsys):
    control = tmp_path / 'control'
    control.touch()
    context = {'completed_runs': 2, 'previous_run': {'independent_audit_finished': True},
               'next_run': {'side': 'candidate', 'seed': 1}}
    clock = {'seconds': 100., 'polls': 0}
    periods, changes = [], []

    def sleep(seconds):
        state = study.load(tmp_path / 'pause-state.json')
        assert state['status'] == 'PAUSED_BETWEEN_RUNS'
        assert state['context'] == context
        assert state['excluded_from_worker_budgets'] is True
        assert state['manager_pid'] > 0
        assert changes[0]['status'] == 'PAUSED_BETWEEN_RUNS'
        clock['seconds'] += seconds
        clock['polls'] += 1
        if clock['polls'] == 3:
            control.unlink()  # Only the operator removes the marker.

    monkeypatch.setattr(study, 'time', SimpleNamespace(
        monotonic=lambda: clock['seconds'], time=lambda: 1000 + clock['seconds'], sleep=sleep))
    period = study.wait_between_runs(control, tmp_path, context, periods=periods,
                                    changed=lambda: changes.append(deepcopy(periods[-1])))
    assert period is periods[0]
    assert clock['polls'] == 3
    assert period['seconds'] == .75
    assert period['started_at'] == 1100
    assert period['ended_at'] == 1100.75
    assert period['status'] == 'RESUMED_BETWEEN_RUNS'
    assert [row['status'] for row in changes] == ['PAUSED_BETWEEN_RUNS', 'RESUMED_BETWEEN_RUNS']
    assert study.load(tmp_path / 'pause-state.json') == period
    events = [json.loads(line)['event'] for line in capsys.readouterr().out.splitlines()]
    assert events == ['PAUSED_BETWEEN_RUNS', 'RESUMED_BETWEEN_RUNS']


def test_interrupted_pause_records_end_and_keeps_operator_marker(monkeypatch, tmp_path):
    control = tmp_path / 'control'
    control.touch()
    clock = {'seconds': 0.}

    def sleep(_):
        clock['seconds'] = 2.5
        raise KeyboardInterrupt

    monkeypatch.setattr(study, 'time', SimpleNamespace(
        monotonic=lambda: clock['seconds'], time=lambda: 2000 + clock['seconds'], sleep=sleep))
    periods = []
    with pytest.raises(KeyboardInterrupt):
        study.wait_between_runs(control, tmp_path, {}, periods=periods)
    assert control.exists()
    assert periods[0]['seconds'] == 2.5
    assert periods[0]['status'] == 'PAUSE_INTERRUPTED'
    assert study.load(tmp_path / 'pause-state.json') == periods[0]


def test_manifest_write_failure_does_not_leave_a_waiting_ack(tmp_path):
    control = tmp_path / 'control'
    control.touch()

    def broken_manifest():
        raise OSError('Cannot persist pause manifest')

    with pytest.raises(OSError, match='manifest'):
        study.wait_between_runs(control, tmp_path, {}, periods=[], changed=broken_manifest)
    assert control.exists()
    assert not (tmp_path / 'pause-state.json').exists()


def test_manager_pauses_after_audit_preserves_abba_and_excludes_wait(monkeypatch, tmp_path):
    control = tmp_path / 'control'
    output = tmp_path / 'results'
    clock = {'seconds': 0.}
    events, specs = [], []
    identity = {'case_file': str(tmp_path / 'case.json'), 'graph_hash': 'measured-graph',
                'historical_hints_hash': 'same-hints'}
    monkeypatch.setattr(study, 'source_manifest', lambda _: {'sha256': 'frozen-source', 'files': {}})
    monkeypatch.setattr(study, 'case_identity', lambda _: identity)

    def launch(argv, **kwargs):
        spec = study.load(argv[-1])
        specs.append(spec)
        events.append(('worker', spec['side'], spec['seed']))
        if len(specs) == 1:
            control.touch()  # A pause requested during a run must not interrupt it.
            assert not (output / 'pause-state.json').exists()
        assert kwargs['timeout'] == 915
        clock['seconds'] += 7
        row = {key: spec[key] for key in ('identity', 'compute_seconds', 'wait_seconds', 'seed',
                                         'hash_seed', 'profile_name')}
        row['status'] = 'RETURNED'
        study.save(Path(spec['run_folder']) / 'worker.json', row)
        return SimpleNamespace(returncode=0)

    def audit(spec, row):
        events.append(('audit', spec['side'], spec['seed']))
        if len(specs) == 1:
            assert control.exists()
            assert not (output / 'pause-state.json').exists()
        clock['seconds'] += 3
        return {**row, 'status': 'VALID', 'audit': {'valid': True, 'total_fee': '100.00'}}

    def sleep(_):
        assert events[-1] == ('audit', 'baseline', 0)
        state = study.load(output / 'pause-state.json')
        assert state['status'] == 'PAUSED_BETWEEN_RUNS'
        assert state['context']['completed_runs'] == 1
        assert state['context']['previous_run']['independent_audit_finished'] is True
        assert study.load(output / 'runs/0000-daily-c0-b300-s0-baseline/completed.json')['status'] == 'VALID'
        manifest = study.load(output / 'manifest.json')
        assert manifest['pause_control']['periods'][0]['status'] == 'PAUSED_BETWEEN_RUNS'
        clock['seconds'] += 5
        control.unlink()

    monkeypatch.setattr(study.subprocess, 'run', launch)
    monkeypatch.setattr(study, 'independent_check', audit)
    monkeypatch.setattr(study, 'time', SimpleNamespace(
        monotonic=lambda: clock['seconds'], time=lambda: 3000 + clock['seconds'], sleep=sleep))
    report = study.evaluate({'output': str(output), 'baseline_source': str(tmp_path / 'old'),
                             'candidate_source': str(tmp_path / 'new'), 'cases': [identity['case_file']],
                             'profiles': [{'name': 'daily', 'baseline_options': {}, 'candidate_options': {}}],
                             'budgets': [300], 'seeds': [0, 1], 'hash_seed': 0,
                             'pause_file': str(control)})
    assert events == [('worker', 'baseline', 0), ('audit', 'baseline', 0),
                      ('worker', 'candidate', 0), ('audit', 'candidate', 0),
                      ('worker', 'candidate', 1), ('audit', 'candidate', 1),
                      ('worker', 'baseline', 1), ('audit', 'baseline', 1)]
    assert report['finished'] is True
    assert len(report['runs']) == 4
    assert all(row['supervisor_seconds'] == 7 for row in report['runs'])
    assert all(spec['compute_seconds'] == 300 and spec['wait_seconds'] == 915
               and spec['options'] == {} and spec['hash_seed'] == 0 and not spec['warm_start']
               and spec['identity'] == identity for spec in specs)
    assert report['manifest']['pause_control']['periods'][0]['seconds'] == 5
    assert study.load(output / 'manifest.json')['pause_control'] == report['manifest']['pause_control']


def test_manager_without_pause_never_calls_pause_helper(monkeypatch, tmp_path):
    # Empty runs isolate the optional manager metadata from any optimization.
    monkeypatch.setattr(study, 'source_manifest', lambda _: {'sha256': 'frozen', 'files': {}})
    monkeypatch.setattr(study, 'case_identity', lambda _: {})
    monkeypatch.setattr(study, 'wait_between_runs', lambda *a, **kw: pytest.fail('Disabled pause called'))
    config = {'output': str(tmp_path / 'result'), 'baseline_source': str(tmp_path / 'old'),
              'candidate_source': str(tmp_path / 'new'), 'cases': [], 'profiles': [],
              'budgets': [300], 'seeds': [0], 'hash_seed': 0}
    report = study.evaluate(config)
    assert 'pause_control' not in report['manifest']
    assert 'pause_file' not in report['manifest']['config']
    assert not (tmp_path / 'result/pause-state.json').exists()
