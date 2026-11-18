"""Serial paired cost experiments on immutable inputs and measured roads.

Each solver gets a fresh process and the same input, road graph, historical
hints, seed, compute budget and total-wait cutoff. The manager independently
recounts every returned plan using one pinned evaluator source tree. No network
adapter or business-database write is used. Profiles are compared separately;
results from different graphs, compute budgets or seeds are never pooled as one
transport bill. Private inputs and full results belong in an ignored data folder.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal
import gzip
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ENV = {'PYTHONUNBUFFERED': '1', 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
       'MKL_NUM_THREADS': '1', 'VECLIB_MAXIMUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'}
DEFAULT_ENTRYPOINT = 'app.route_deep:optimize_deep'


class IntegrityError(ValueError):
    pass


def road_execution_policy(enabled, *, adapter='FrozenEvidenceRoad',
                          evidence_scope='OFFLINE_FROZEN_DRIVING_REFERENCE'):
    """Describe search semantics separately from the immutable road evidence.

    The production marker is interpreted by each pinned source version. Older
    sources may retain their own count stops; this helper never patches them.
    A production search policy cannot turn this offline adapter into a live one.
    """
    if not isinstance(enabled, bool):
        raise ValueError('production_search_policy must be a boolean')
    if (adapter, evidence_scope) not in {
            ('FrozenEvidenceRoad', 'OFFLINE_FROZEN_DRIVING_REFERENCE'),
            ('FormulaRoad', 'SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE')}:
        raise ValueError('Production-policy evaluation requires a recognized offline road adapter')
    return {'policy_id': 'OFFLINE_SEARCH_POLICY_V1', 'adapter': adapter,
            'evidence_scope': evidence_scope,
            'search_policy': 'VERSION_NATIVE_PRODUCTION' if enabled else 'RESEARCH_DEFAULT',
            'production_planning': enabled, 'http_enabled': False,
            'unknown_edges': 'REJECT_NO_ESTIMATION_OR_COLLECTION',
            'wall_deadline_policy': ('WORKER_LAUNCH_PLUS_WAIT_SECONDS' if enabled
                                     else 'EXISTING_RESEARCH_DEFAULT'),
            'timer': 'time.monotonic',
            'count_limit_policy': ('FOLLOW_SOURCE_NATIVE_PRODUCTION_MARKER' if enabled
                                   else 'FOLLOW_SOURCE_RESEARCH_DEFAULT')}


def apply_road_execution_policy(road, enabled, *, wall_deadline, cancelled,
                                adapter='FrozenEvidenceRoad',
                                evidence_scope='OFFLINE_FROZEN_DRIVING_REFERENCE'):
    """Attach only the production caller contract; no metric or source edits."""
    policy = road_execution_policy(enabled, adapter=adapter, evidence_scope=evidence_scope)
    if enabled:
        if type(road).__name__ != adapter:
            raise ValueError('Offline search policy does not accept a live road adapter')
        if (isinstance(wall_deadline, bool) or wall_deadline is None
                or not math.isfinite(float(wall_deadline))):
            raise ValueError('Production search requires a finite absolute wall deadline')
        road.production_planning = True
        road.wall_deadline = float(wall_deadline)
        road.timer = time.monotonic
        road.cancelled = cancelled
        if not hasattr(road, 'settings'):
            road.settings = SimpleNamespace()
        road.settings.road_calls_enabled = False
    return policy


def road_execution_state(road, launched):
    """Record actual worker values without making absolute times pair keys."""
    return {'worker_launched_monotonic': launched,
            'road_production_planning': bool(getattr(road, 'production_planning', False)),
            'road_wall_deadline_monotonic': getattr(road, 'wall_deadline', None),
            'road_timer': ('time.monotonic' if getattr(road, 'timer', None) is time.monotonic
                           else 'SOURCE_DEFAULT_TIMER')}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def absolute_pause_file(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Pause control must be an absolute path')
    return os.path.abspath(path)


def wait_between_runs(pause_file, output, context, *, periods, changed=lambda: None):
    """Acknowledge only a quiescent manager, after its prior audit has ended.

    The serial manager calls this before launching each fresh worker. It never
    runs inside a worker or its independent audit, and none of this waiting is
    part of a worker's compute or supervisor deadline. The operator owns the
    control file: create it, wait for this acknowledgement, do other work, then
    remove it to resume the same pending run.
    """
    if pause_file is None:
        return None
    control = Path(absolute_pause_file(pause_file))
    output = Path(output)
    state_file = output / 'pause-state.json'
    if control.resolve() == state_file.resolve():
        raise ValueError('Pause control and acknowledgement must be different files')
    if not control.exists():
        return None
    started = time.monotonic()
    period = {'status': 'PAUSED_BETWEEN_RUNS', 'pause_number': len(periods) + 1,
              'pause_file': str(control), 'manager_pid': os.getpid(),
              'started_at': time.time(), 'ended_at': None, 'seconds': None,
              'context': deepcopy(context), 'excluded_from_worker_budgets': True}
    periods.append(period)

    def publish():
        changed()
        save(state_file, period)
        print(json.dumps({'event': period['status'], **period}, ensure_ascii=False), flush=True)

    publish()
    try:
        while control.exists():
            time.sleep(.25)
    except BaseException:
        period.update(status='PAUSE_INTERRUPTED', ended_at=time.time(),
                      seconds=round(max(0., time.monotonic() - started), 6))
        publish()
        raise
    period.update(status='RESUMED_BETWEEN_RUNS', ended_at=time.time(),
                  seconds=round(max(0., time.monotonic() - started), 6))
    publish()
    return period


def source_manifest(root):
    root = Path(root).resolve()
    paths = sorted([*(root / 'app').rglob('*.py'), *(root / 'scripts').rglob('*.py')])
    paths += [root / name for name in ('requirements.lock', 'pyproject.toml') if (root / name).is_file()]
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    if not (root / 'app/routing_engine.py').is_file():
        raise IntegrityError('Source tree has no routing engine')
    return {'sha256': digest(files), 'files': files}


def case_identity(case_path):
    """Road content and hint content are separate pinned comparison identities."""
    from scripts.benchmark_algorithms import FrozenEvidenceRoad
    path = Path(case_path).resolve()
    case = load(path)
    body = load(path.parent / case['body_file'])
    if digest(body) != case['input_hash']:
        raise IntegrityError('Frozen input changed')
    road = FrozenEvidenceRoad(case['road_database'])
    try:
        if road.graph_hash != case['graph_hash']:
            raise IntegrityError('Frozen roads changed')
        baseline = load(path.parent / case['baseline_file']) if case.get('baseline_file') else None
        if case.get('baseline_hash') is not None and digest(baseline) != case['baseline_hash']:
            raise IntegrityError('Frozen warm baseline changed')
        with Path(case['road_database']).open('rb') as stream:
            graph_file_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'case_file': str(path), 'input_hash': digest(body), 'graph_hash': road.graph_hash,
                'graph_file_sha256': graph_file_hash, 'historical_hints_hash': digest(road.route_seed_samples),
                'baseline_hash': digest(baseline), 'case_manifest_hash': digest(case)}
    finally:
        road.close()


def _version_manifest():
    from importlib.metadata import PackageNotFoundError, version
    versions = {}
    for name in ('ortools', 'scipy', 'numpy'):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = None
    return {'python': platform.python_version(), 'executable': sys.executable,
            'platform': platform.platform(), 'packages': versions,
            'environment': {key: os.environ.get(key) for key in (*ENV, 'PYTHONHASHSEED')}}


def worker(spec_path):
    """Run with imports rooted in the explicitly selected solver source tree."""
    launched = time.monotonic()
    spec = load(spec_path)
    source = Path(spec['source_root']).resolve()
    sys.path.insert(0, str(source))
    if os.environ.get('PYTHONHASHSEED') != str(spec['hash_seed']):
        raise IntegrityError('Worker hash seed differs')
    if spec['hash_seed'] == 0 and sys.flags.hash_randomization != 0:
        raise IntegrityError('Zero hash seed was not active at process startup')
    if source_manifest(source)['sha256'] != spec['source_hash']:
        raise IntegrityError('Solver source changed before run')
    identity = case_identity(spec['case_file'])
    if identity != spec['identity']:
        raise IntegrityError('Experiment case identity changed before run')
    from scripts.benchmark_algorithms import FrozenEvidenceRoad, replay_baseline
    from app.routing_engine import normalize
    case_path = Path(spec['case_file'])
    case = load(case_path)
    body = load(case_path.parent / case['body_file'])
    data = normalize(body)
    wait_deadline = launched + spec['wait_seconds']
    # The subprocess supervisor also enforces this full wall cutoff. Leave a
    # small interval for the complete incumbent and diagnostic artifact to save.
    deadline_state = {'triggered': False}
    def cancelled():
        reached = time.monotonic() >= wait_deadline - min(.5, spec['wait_seconds'] * .02)
        deadline_state['triggered'] = deadline_state['triggered'] or reached
        return reached
    road = FrozenEvidenceRoad(case['road_database'], cancelled=cancelled)
    road.settings = SimpleNamespace(planning_optimizer_profile=spec.get('optimizer_profile', 'HYBRID'))
    production_policy = spec.get('production_search_policy', False)
    execution_policy = road_execution_policy(production_policy)
    if spec.get('road_execution_policy', execution_policy) != execution_policy:
        raise IntegrityError('Worker road execution policy differs from the pinned specification')
    if production_policy and 'road_execution_policy' not in spec:
        raise IntegrityError('Production road execution policy is missing from the specification')
    apply_road_execution_policy(road, production_policy, wall_deadline=wait_deadline,
                                cancelled=cancelled)
    candidate_stream = [] if spec.get('capture_candidate_stream') else None
    native_stream = [] if spec.get('capture_native_candidate_stream') else None
    if candidate_stream is not None:
        road.candidate_stream_observer = candidate_stream.append
    if native_stream is not None:
        road.native_candidate_stream_observer = native_stream.append
    before = resource.getrusage(resource.RUSAGE_SELF)
    row = {key: spec[key] for key in ('side', 'profile_name', 'case_file', 'identity', 'source_hash',
                                     'compute_seconds', 'wait_seconds', 'seed', 'hash_seed', 'entrypoint', 'options')}
    row.update(hash_randomization=sys.flags.hash_randomization, runtime=_version_manifest(),
               warm_start=spec.get('warm_start', False), external_calls=0, source_business_database_written=False,
               capture_candidate_stream=candidate_stream is not None,
               capture_native_candidate_stream=native_stream is not None,
               production_search_policy=production_policy,
               road_execution_policy=execution_policy,
               **road_execution_state(road, launched),
               timing_scope='DIAGNOSTIC_STREAM_CAPTURE' if candidate_stream is not None or native_stream is not None else 'NORMAL_COLD_START')
    started = time.monotonic()
    try:
        initial = None
        if spec.get('warm_start') and case.get('baseline_file'):
            initial = replay_baseline(data, load(case_path.parent / case['baseline_file']), road)
        module, function_name = spec['entrypoint'].split(':', 1)
        function = getattr(importlib.import_module(module), function_name)
        options = deepcopy(spec['options'])
        last_progress = {'stage': None, 'written_at': -math.inf, 'writes': 0, 'seconds': 0.}
        def progress(value):
            now = time.monotonic()
            stage = value.get('stage')
            if stage == last_progress['stage'] and now - last_progress['written_at'] < 3:
                return
            before_write = time.monotonic()
            save(Path(spec['run_folder']) / 'progress.json', {
                'side': spec['side'], 'profile_name': spec['profile_name'],
                'elapsed_seconds': round(now - started, 6), 'progress': value})
            last_progress.update(stage=stage, written_at=now, writes=last_progress['writes'] + 1,
                                 seconds=last_progress['seconds'] + time.monotonic() - before_write)
        parameters = inspect.signature(function).parameters
        if 'progress' in parameters and 'progress' not in options:
            options['progress'] = progress
        if 'initial_result' in parameters:
            options['initial_result'] = deepcopy(initial)
        elif initial is not None:
            raise IntegrityError('Selected function cannot accept the requested warm baseline')
        if 'wall_budget_seconds' in parameters:
            # A relative budget begins at function entry, while the shared hard
            # cutoff begins at worker launch. Pass only the unused interval.
            options['wall_budget_seconds'] = max(0., wait_deadline - time.monotonic())
            row['applied_wall_budget_seconds'] = options['wall_budget_seconds']
        result = function(deepcopy(body), road, spec['compute_seconds'], spec['seed'], cancelled, **options)
        row['progress_artifact_writes'] = last_progress['writes']
        row['progress_artifact_seconds'] = round(last_progress['seconds'], 6)
        stop_reason = result.get('algorithm', {}).get('stop_reason')
        deadline_exhausted = (deadline_state['triggered'] or time.monotonic() >= wait_deadline
                              or stop_reason in {'WALL_BUDGET', 'CANCELLED'})
        row.update(status='RETURNED_TIMEOUT' if deadline_exhausted else 'RETURNED',
                   deadline_exhausted=deadline_exhausted, solver_stop_reason=stop_reason,
                   solve_seconds=round(time.monotonic() - started, 6),
                   result_algorithm=result.get('algorithm', {}), road_stats=road.stats())
        result_path = Path(spec['run_folder']) / 'result.json.gz'
        result_path.write_bytes(gzip.compress(json.dumps(result, ensure_ascii=False, allow_nan=False,
                                                        separators=(',', ':')).encode(), mtime=0))
        result_path.chmod(0o600)
        row['result_file'] = str(result_path)
    except Exception as exc:
        deadline_exhausted = deadline_state['triggered'] or time.monotonic() >= wait_deadline
        row.update(status='TIMEOUT' if deadline_exhausted else 'ERROR',
                   deadline_exhausted=deadline_exhausted,
                   exception_type=type(exc).__name__, exception=str(exc),
                   traceback=traceback.format_exc(), solve_seconds=round(time.monotonic() - started, 6))
    finally:
        after = resource.getrusage(resource.RUSAGE_SELF)
        row.update(worker_seconds=round(time.monotonic() - launched, 6),
                   cpu_seconds=round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 6),
                   lifetime_peak_rss_bytes=after.ru_maxrss if sys.platform == 'darwin' else after.ru_maxrss * 1024,
                   source_unchanged=source_manifest(source)['sha256'] == spec['source_hash'])
        if not row['source_unchanged']:
            row['status'] = 'SOURCE_CHANGED'
        road.close()
        if candidate_stream is not None:
            stream_path = Path(spec['run_folder']) / 'candidate-stream.json.gz'
            stream_path.write_bytes(gzip.compress(json.dumps(candidate_stream, ensure_ascii=False, allow_nan=False,
                                                            separators=(',', ':')).encode(), mtime=0))
            stream_path.chmod(0o600)
            row.update(candidate_stream_file=str(stream_path), candidate_stream_events=len(candidate_stream),
                       candidate_stream_hash=digest(candidate_stream), stream_saved_after_solve_timer=True)
        if native_stream is not None:
            stream_path = Path(spec['run_folder']) / 'native-candidate-stream.json.gz'
            stream_path.write_bytes(gzip.compress(json.dumps(native_stream, ensure_ascii=False, allow_nan=False,
                                                            separators=(',', ':')).encode(), mtime=0))
            stream_path.chmod(0o600)
            row.update(native_candidate_stream_file=str(stream_path), native_candidate_stream_events=len(native_stream),
                       native_candidate_stream_hash=digest(native_stream))
        save(Path(spec['run_folder']) / 'worker.json', row)
    return row


def independent_check(spec, row):
    """Manager-side audit uses the evaluator's pinned code, never solver flags."""
    from scripts.benchmark_algorithms import FrozenEvidenceRoad, independent_audit
    from app.routing_engine import normalize
    timed_out = row.get('deadline_exhausted', False) or row['status'] in {'TIMEOUT', 'RETURNED_TIMEOUT'}
    if row['status'] not in {'RETURNED', 'RETURNED_TIMEOUT', 'TIMEOUT'} or not row.get('result_file'):
        return row
    path = Path(spec['case_file'])
    case = load(path)
    body = load(path.parent / case['body_file'])
    result = json.loads(gzip.decompress(Path(row['result_file']).read_bytes()))
    road = FrozenEvidenceRoad(case['road_database'])
    try:
        audit = independent_audit(normalize(body), result, road)
        row.update(audit=audit, status='TIMEOUT' if timed_out else 'VALID' if audit['valid'] else 'INVALID',
                   complete_result_available=audit['valid'], deadline_exhausted=timed_out,
                   independent_evaluator_source_hash=spec['evaluator_source_hash'])
        # Keep an otherwise valid result visible when its compute budget was
        # overrun; the solver's distinct compute measurement includes final QA.
        algorithm = result.get('algorithm', {})
        measured = algorithm.get('compute_seconds')
        row['reported_compute_seconds'] = measured
        row['compute_overrun_seconds'] = (round(max(0., float(measured) - spec['compute_seconds']), 6)
                                          if measured is not None else None)
        row['wait_overrun_seconds'] = round(max(0., row.get('supervisor_seconds', 0.) - spec['wait_seconds']), 6)
    finally:
        road.close()
    return row


def pair_summary(baseline, candidate):
    same_keys = ('identity', 'compute_seconds', 'wait_seconds', 'seed', 'hash_seed')
    if any(baseline[key] != candidate[key] for key in same_keys):
        raise IntegrityError('Attempted to compare unequal experiment conditions')
    policies = []
    for value in (baseline, candidate):
        enabled = value.get('production_search_policy', False)
        expected = road_execution_policy(enabled)
        recorded = value.get('road_execution_policy', expected)
        if recorded != expected or (enabled and 'road_execution_policy' not in value):
            raise IntegrityError('Missing or inconsistent road execution policy')
        policies.append((enabled, recorded))
    if policies[0] != policies[1]:
        raise IntegrityError('Attempted to compare unequal road execution policies')
    row = {key: baseline[key] for key in same_keys}
    row.update(production_search_policy=policies[0][0], road_execution_policy=policies[0][1])
    row.update(profile_name=candidate['profile_name'], baseline_status=baseline['status'],
               candidate_status=candidate['status'], baseline_run=baseline['run_folder'], candidate_run=candidate['run_folder'])
    if baseline['status'] == candidate['status'] == 'VALID':
        old = Decimal(baseline['audit']['total_fee'])
        new = Decimal(candidate['audit']['total_fee'])
        row.update(baseline_fee=str(old), candidate_fee=str(new), saving=str(old - new),
                   outcome='IMPROVED' if new < old else 'REGRESSED' if new > old else 'EQUAL')
    else:
        row['outcome'] = 'INCOMPLETE_COMPARISON'
    return row


def summarize(pairs, runs):
    """Use every paired fee; no best-of-seed selection or graph mixing."""
    groups = {}
    for row in pairs:
        key = (row['profile_name'], row['identity']['graph_hash'], row['identity']['historical_hints_hash'],
               row['compute_seconds'], row['wait_seconds'], row.get('production_search_policy', False),
               digest(row.get('road_execution_policy', road_execution_policy(False))))
        groups.setdefault(key, []).append(row)
    summaries = []
    for key, rows in groups.items():
        valid = [row for row in rows if row['outcome'] != 'INCOMPLETE_COMPARISON']
        savings = [Decimal(row['saving']) for row in valid]
        summaries.append({'profile_name': key[0], 'graph_hash': key[1], 'historical_hints_hash': key[2],
                          'compute_seconds': key[3], 'wait_seconds': key[4], 'pair_count': len(rows),
                          'production_search_policy': key[5],
                          'road_execution_policy': rows[0].get('road_execution_policy', road_execution_policy(False)),
                          'valid_pairs': len(valid), 'improved': sum(row['outcome'] == 'IMPROVED' for row in rows),
                          'equal': sum(row['outcome'] == 'EQUAL' for row in rows),
                          'regressed': sum(row['outcome'] == 'REGRESSED' for row in rows),
                          'incomplete': len(rows) - len(valid),
                          'all_pair_saving_sum': str(sum(savings, Decimal(0))),
                          'median_pair_saving': str(statistics.median(savings)) if savings else None,
                          'fee_scope': 'SUM_OF_ALL_PAIRED_EXPERIMENTS_NOT_ACTUAL_TRANSPORT_SPEND'})
    return {'comparisons': pairs, 'groups': summaries,
            'regressions': [row for row in pairs if row['outcome'] == 'REGRESSED'],
            'failures_and_timeouts': [row for row in runs if row['status'] != 'VALID'],
            'compute_overruns': [row for row in runs if (row.get('compute_overrun_seconds') or 0) > .05]}


def evaluate(config):
    production_policy = config.get('production_search_policy', False)
    execution_policy = road_execution_policy(production_policy)
    pause_file = config.get('pause_file')
    if pause_file is not None:
        pause_file = absolute_pause_file(pause_file)
    output = Path(config['output']).resolve()
    if pause_file is not None and Path(pause_file).resolve() == (output / 'pause-state.json').resolve():
        raise ValueError('Pause control and acknowledgement must be different files')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / 'manifest.json').exists():
        raise IntegrityError('Use a new output directory; completed experiments are never overwritten')
    sources = {side: Path(config[side + '_source']).resolve() for side in ('baseline', 'candidate')}
    manifests = {side: source_manifest(source) for side, source in sources.items()}
    evaluator_hash = source_manifest(ROOT)['sha256']
    identities = {str(Path(path).resolve()): case_identity(path) for path in config['cases']}
    manifest = {'schema_version': 1, 'created_at': int(time.time()), 'config': config,
                'source_manifests': manifests, 'evaluator_source_hash': evaluator_hash,
                'identities': identities, 'execution_policy': 'STRICTLY_SERIAL_ALTERNATING_PAIRED_FRESH_PROCESSES',
                'objective': 'COMPLETE_VALID_WHOLE_ORDER_ACTUAL_FEE', 'external_calls': 0,
                'source_business_database_written': False, 'warm_start': config.get('warm_start', False)}
    manifest.update(production_search_policy=production_policy, road_execution_policy=execution_policy)
    pause_periods = []
    if pause_file is not None:
        manifest['pause_control'] = {
            'path': pause_file, 'state_file': str(output / 'pause-state.json'),
            'manager_pid': os.getpid(), 'periods': pause_periods,
            'policy': 'BETWEEN_WORKERS_ONLY_AFTER_PREVIOUS_INDEPENDENT_AUDIT_FINISHES',
            'budget_scope': 'PAUSES_EXCLUDED_FROM_ALL_WORKER_COMPUTE_AND_WAIT_BUDGETS'}
    save(output / 'manifest.json', manifest)
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('SUKA_')}
    environment.update(ENV, PYTHONHASHSEED=str(config['hash_seed']))
    script = Path(__file__).resolve()
    runs, pairs = [], []
    ordinal = 0
    for profile in config['profiles']:
        for case_index, (case_path, identity) in enumerate(identities.items()):
            for budget in config['budgets']:
                for seed in config['seeds']:
                    order = ('baseline', 'candidate') if ordinal % 2 == 0 else ('candidate', 'baseline')
                    pair = {}
                    wait_seconds = config.get('wait_seconds')
                    if wait_seconds is None:
                        wait_seconds = budget + min(1800., max(120., budget * 2)) + 15
                    for side in order:
                        if pause_file is not None:
                            previous = ({'run_folder': runs[-1]['run_folder'],
                                         'status': runs[-1]['status'],
                                         'independent_audit_finished': True} if runs else None)
                            wait_between_runs(pause_file, output, {
                                'completed_runs': len(runs), 'previous_run': previous,
                                'next_run': {'side': side, 'profile': profile['name'],
                                             'case': case_path, 'budget': budget,
                                             'seed': seed, 'pair_ordinal': ordinal}},
                                periods=pause_periods,
                                changed=lambda: save(output / 'manifest.json', manifest))
                        if source_manifest(sources[side])['sha256'] != manifests[side]['sha256']:
                            raise IntegrityError('Source changed during experiment batch')
                        if source_manifest(ROOT)['sha256'] != evaluator_hash:
                            raise IntegrityError('Independent evaluator changed during batch')
                        slug = f'{ordinal:04d}-{profile["name"]}-c{case_index}-b{budget}-s{seed}-{side}'
                        folder = output / 'runs' / slug
                        folder.mkdir(mode=0o700, parents=True)
                        options = deepcopy(profile.get('baseline_options', {}) if side == 'baseline' else
                                           profile.get('candidate_options', profile.get('options', {})))
                        spec = {'side': side, 'profile_name': profile['name'], 'source_root': str(sources[side]),
                                'source_hash': manifests[side]['sha256'], 'case_file': case_path, 'identity': identity,
                                'compute_seconds': budget, 'wait_seconds': wait_seconds, 'seed': seed,
                                'hash_seed': config['hash_seed'], 'run_folder': str(folder),
                                'entrypoint': profile.get(side + '_entrypoint', profile.get('entrypoint', DEFAULT_ENTRYPOINT)),
                                'options': options, 'evaluator_source_hash': evaluator_hash,
                                'warm_start': config.get('warm_start', False),
                                'optimizer_profile': profile.get('optimizer_profile', 'HYBRID'),
                                'capture_candidate_stream': profile.get('capture_candidate_stream', False)}
                        spec.update(production_search_policy=production_policy, road_execution_policy=execution_policy)
                        spec['capture_native_candidate_stream'] = profile.get('capture_native_candidate_stream', False)
                        save(folder / 'spec.json', spec)
                        started = time.monotonic()
                        with (folder / 'process.log').open('w') as log:
                            os.chmod(folder / 'process.log', 0o600)
                            try:
                                completed = subprocess.run([config.get('python', sys.executable), str(script), '--worker', str(folder / 'spec.json')],
                                                           cwd=sources[side], env=environment, stdin=subprocess.DEVNULL,
                                                           stdout=log, stderr=subprocess.STDOUT, timeout=wait_seconds, check=False)
                                row = load(folder / 'worker.json') if (folder / 'worker.json').exists() else {
                                    **{key: spec[key] for key in ('identity', 'compute_seconds', 'wait_seconds', 'seed', 'hash_seed', 'profile_name')},
                                    'status': 'PROCESS_FAILED', 'return_code': completed.returncode}
                            except subprocess.TimeoutExpired:
                                row = {**{key: spec[key] for key in ('identity', 'compute_seconds', 'wait_seconds', 'seed', 'hash_seed', 'profile_name')},
                                       'status': 'TIMEOUT', 'deadline_exhausted': True,
                                       'complete_result_available': (folder / 'result.json.gz').exists()}
                                if row['complete_result_available']:
                                    row['result_file'] = str(folder / 'result.json.gz')
                        if ('road_execution_policy' in row and row['road_execution_policy'] != execution_policy
                                or 'production_search_policy' in row and row['production_search_policy'] != production_policy):
                            row.update(status='POLICY_MISMATCH')
                        row.update(side=side, run_folder=str(folder), supervisor_seconds=round(time.monotonic() - started, 6),
                                   production_search_policy=production_policy, road_execution_policy=execution_policy)
                        try:
                            row = independent_check(spec, row)
                            if case_identity(case_path) != identity:
                                raise IntegrityError('Experiment case changed during run')
                            if source_manifest(sources[side])['sha256'] != manifests[side]['sha256']:
                                raise IntegrityError('Solver source changed during run')
                            if source_manifest(ROOT)['sha256'] != evaluator_hash:
                                raise IntegrityError('Independent evaluator changed during run')
                        except Exception as exc:
                            row.update(status='TIMEOUT' if row.get('deadline_exhausted') else 'AUDIT_ERROR',
                                       exception_type=type(exc).__name__, exception=str(exc))
                        save(folder / 'completed.json', row)
                        runs.append(row)
                        pair[side] = row
                        report = {'manifest': manifest, 'runs': runs, **summarize(pairs, runs), 'finished': False}
                        save(output / 'report.json', report)
                        print(json.dumps({'event': 'RUN_COMPLETED', 'side': side, 'profile': profile['name'],
                                          'case': case_path, 'budget': budget, 'seed': seed, 'status': row['status'],
                                          'fee': row.get('audit', {}).get('total_fee')}, ensure_ascii=False), flush=True)
                    pairs.append(pair_summary(pair['baseline'], pair['candidate']))
                    ordinal += 1
                    save(output / 'report.json', {'manifest': manifest, 'runs': runs, **summarize(pairs, runs), 'finished': False})
    report = {'manifest': manifest, 'runs': runs, **summarize(pairs, runs), 'finished': True, 'finished_at': int(time.time())}
    save(output / 'report.json', report)
    return report


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--baseline-source')
    value.add_argument('--candidate-source')
    value.add_argument('--case', action='append', dest='cases')
    value.add_argument('--profiles', help='JSON list with name, baseline_options and candidate_options/options')
    value.add_argument('--budgets', type=float, nargs='+', default=[15])
    value.add_argument('--seeds', type=int, nargs='+', default=[0])
    value.add_argument('--hash-seed', type=int, default=0)
    value.add_argument('--wait-seconds', type=float, help='Full process hard cutoff, independent of compute budget')
    value.add_argument('--pause-file', type=absolute_pause_file,
                       help='Absolute operator-owned file; pause between audited workers while it exists')
    value.add_argument('--warm-start', action='store_true')
    value.add_argument('--production-search-policy', action='store_true',
                       help='Use each source version\'s production search marker on offline frozen roads; HTTP remains unavailable')
    value.add_argument('--output')
    value.add_argument('--python', default=sys.executable)
    value.add_argument('--worker', help=argparse.SUPPRESS)
    return value


def main():
    command = parser()
    args = command.parse_args()
    if args.worker:
        row = worker(args.worker)
        return 0 if row['status'] == 'RETURNED' else 1
    if not all((args.baseline_source, args.candidate_source, args.cases, args.output)):
        command.error('--baseline-source, --candidate-source, --case and --output are required')
    if any(not math.isfinite(value) or value <= 0 for value in args.budgets):
        command.error('Compute budgets must be finite and positive')
    if args.wait_seconds is not None and (not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0):
        command.error('Wait cutoff must be finite and positive')
    if not 0 <= args.hash_seed <= 2**32 - 1:
        command.error('Hash seed must be an integer in [0, 4294967295]')
    profiles = load(args.profiles) if args.profiles else [{'name': 'diagnostics', 'options': {}}]
    if not isinstance(profiles, list) or not profiles or any(not isinstance(p, dict) or not p.get('name') for p in profiles):
        command.error('Profiles must be a nonempty list of named objects')
    names = [profile['name'] for profile in profiles]
    if len(set(names)) != len(names) or any(not name.replace('_', '').replace('-', '').isalnum() for name in names):
        command.error('Profile names must be unique simple letters/digits/underscore/hyphen')
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    config = {**vars(args), 'profiles': profiles}
    if args.pause_file is None:
        config.pop('pause_file')
    report = evaluate(config)
    return 0 if all(row['status'] == 'VALID' for row in report['runs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
