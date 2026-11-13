"""Bounded, restartable local routing study; never sends an HTTP request.

The waiting manager imports only the standard library. A fresh interpreter is
started after the source-ready gate and complete measured graph are available.
Each timed experiment also gets its own fresh process and a hard wall limit.
Only private study artifacts are written; business databases remain read-only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import signal
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))
MATH_SOURCES = (
    'app/routing_engine.py', 'app/route_search.py', 'app/route_selection.py',
    'app/route_metrics.py', 'app/route_ortools.py', 'app/route_neighborhood.py',
    'app/route_portfolio.py', 'app/route_lower_bound.py',
    'scripts/algorithms/route_v3.py',
)
RESEARCH_SOURCES = ('scripts/benchmark_algorithms.py', 'scripts/exact_reference_oracle.py',
                    'scripts/run_algorithm_study.py', 'app/road_service.py',
                    'app/coordinates.py', 'app/planning_inputs.py')
HISTORY_DATES = ('2026-10-06', '2026-10-03', '2026-08-23')
EXPERIMENT_ENV = {'PYTHONUNBUFFERED': '1', 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
                  'MKL_NUM_THREADS': '1', 'VECLIB_MAXIMUM_THREADS': '1', 'NUMEXPR_NUM_THREADS': '1'}


class StudyIntegrityError(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', prefix=path.name + '.',
                                         suffix='.tmp', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError):return default


def source_hashes(paths):
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}


def validate_sources(expected):
    try:actual = source_hashes(expected)
    except OSError as exc:raise StudyIntegrityError('A frozen source file is unavailable') from exc
    changed = [name for name in expected if expected[name] != actual[name]]
    if changed:raise StudyIntegrityError('Frozen source changed: ' + ','.join(changed))
    return actual


def source_gate(path):
    gate = read_json(path)
    if not isinstance(gate, dict) or gate.get('status') != 'READY':return None
    expected = gate.get('source_hashes')
    if not isinstance(expected, dict) or not set(MATH_SOURCES).issubset(expected):return None
    if any(not isinstance(value, str) or len(value) != 64 for value in expected.values()):return None
    validate_sources(expected)
    return gate


def graph_complete(path):
    try:
        with sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True, timeout=.2) as connection:
            metadata = {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM metadata')}
            measured = connection.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]
            known = connection.execute('SELECT COUNT(*) FROM edges e JOIN metrics m ON e.provider_key=m.provider_key').fetchone()[0]
            planned = connection.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
            expected = metadata.get('expected_provider_keys')
            return bool(metadata.get('complete') is True and isinstance(expected, int) and expected > 0
                        and measured == expected and known == planned and planned > 0)
    except (OSError, sqlite3.Error, ValueError, TypeError):return False


def profiles():
    specs = [
        {'name': 'v3_candidates', 'entrypoint': 'scripts.algorithms.route_v3:optimize_joint', 'options': {}},
        {'name': 'gls', 'entrypoint': 'app.route_ortools:optimize_ortools', 'options': {'strategy': 'GUIDED_LOCAL_SEARCH'}},
        {'name': 'sa', 'entrypoint': 'app.route_ortools:optimize_ortools', 'options': {'strategy': 'SIMULATED_ANNEALING'}},
        {'name': 'tabu', 'entrypoint': 'app.route_ortools:optimize_ortools', 'options': {'strategy': 'TABU_SEARCH'}},
        {'name': 'alns', 'entrypoint': 'app.route_portfolio:optimize_alns', 'options': {}},
        {'name': 'hybrid', 'entrypoint': 'app.route_portfolio:optimize_hybrid', 'options': {}},
    ]
    for name, multiplier in [('gls_p075', .75), ('gls_p150', 1.5), ('gls_p200', 2), ('gls_p400', 4)]:
        specs.append({'name': name, 'entrypoint': 'app.route_ortools:optimize_ortools',
                      'options': {'strategy': 'GUIDED_LOCAL_SEARCH', 'common_penalty_multiplier': multiplier}})
    return specs


def profile_ranking(rows, case_name, *, stages=None):
    grouped = defaultdict(list)
    for row in rows:
        if row.get('case_name') != case_name or row.get('status') != 'VALID':continue
        if stages is not None and row.get('stage') not in stages:continue
        audit = row.get('audit') or {}
        if audit.get('unassigned_tasks') or not audit.get('valid'):continue
        fee = Decimal(audit['total_fee'])
        if fee.is_finite() and fee >= 0:grouped[row['profile_name']].append(row)
    ranks = []
    for name, candidates in grouped.items():
        ordered = sorted(candidates, key=lambda row: (Decimal(row['audit']['total_fee']), row['audit']['road_meters'], row['audit']['used_vehicles']))
        fees = sorted(Decimal(row['audit']['total_fee']) for row in candidates)
        median = fees[len(fees) // 2] if len(fees) % 2 else (fees[len(fees) // 2 - 1] + fees[len(fees) // 2]) / 2
        ranks.append({'profile_name': name, 'best': ordered[0], 'valid_runs': len(candidates),
                      'best_fee': str(fees[0]), 'median_fee': str(median), 'worst_fee': str(fees[-1])})
    return sorted(ranks, key=lambda row: (Decimal(row['best_fee']), Decimal(row['median_fee']),
                                         row['best']['audit']['road_meters'], row['profile_name']))


def can_start(now, cutoff, budget, *, margin=20):
    return budget > 0 and now + budget + max(0, margin) < cutoff


def case_identities(cases):
    return {name: {key: case.get(key) for key in ('input_hash', 'graph_hash', 'snapshot_id',
                                                 'required_directed_edges', 'baseline_file', 'baseline_hash')}
            for name, case in cases.items()}


def json_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_case_files(case_path, expected=None):
    path = Path(case_path);case = read_json(path)
    if not isinstance(case, dict):raise StudyIntegrityError('Case manifest is missing')
    if expected is not None and any(case.get(key) != expected.get(key) for key in ('input_hash', 'graph_hash', 'baseline_hash')):
        raise StudyIntegrityError('Case identity changed')
    if json_digest(read_json(path.parent / case['body_file'])) != case['input_hash']:
        raise StudyIntegrityError('Case input file changed')
    if case.get('baseline_file') and json_digest(read_json(path.parent / case['baseline_file'])) != case.get('baseline_hash'):
        raise StudyIntegrityError('Case warm baseline changed')
    return case


def planned_seconds(config):
    n = len(profiles())
    return {'screen': 3 * n * 2 * config['screen_seconds'], 'small_exact_check': n * config['small_seconds'],
            'refine': 3 * 3 * 3 * config['refine_seconds'], 'long_main': 2 * 2 * config['long_seconds'],
            'optional_cold': 2 * config['cold_seconds']}


def runtime_versions():
    packages = {}
    for name in ('scipy', 'numpy', 'ortools'):
        try:packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:packages[name] = None
    return {'python': sys.version, 'python_executable': sys.executable,
            'platform': sys.platform, 'packages': packages,
            'thread_environment_settings': {key: os.environ.get(key) for key in EXPERIMENT_ENV if key != 'PYTHONUNBUFFERED'},
            'thread_environment_scope': 'ENVIRONMENT_SETTINGS_ONLY_NOT_MEASURED_NATIVE_THREAD_COUNTS'}


def worker_resources(before, after, wall_seconds, *, platform_name=None):
    platform_name = platform_name or sys.platform
    multiplier = 1 if platform_name == 'darwin' else 1024
    peak = int(after.ru_maxrss) * multiplier
    user = max(0., after.ru_utime - before.ru_utime)
    system = max(0., after.ru_stime - before.ru_stime)
    return {'cpu_user_seconds': round(user, 6), 'cpu_system_seconds': round(system, 6),
            'cpu_total_seconds': round(user + system, 6), 'worker_interval_wall_seconds': round(wall_seconds, 6),
            'cpu_scope': 'RUN_ONE_INTERVAL_INCLUDING_IMPORTS_BASELINE_VALIDATION_AND_PROOF',
            'lifetime_peak_rss_bytes': peak, 'lifetime_peak_rss_mib': round(peak / 1024**2, 4),
            'peak_rss_scope': 'FRESH_WORKER_LIFETIME_NOT_ALGORITHM_NET_PEAK',
            'raw_ru_maxrss': after.ru_maxrss, 'raw_ru_maxrss_unit': 'bytes' if platform_name == 'darwin' else 'kibibytes'}


def _case_from_collection(config, graph_path, output):
    # Imports occur only inside the post-gate worker.
    from scripts.benchmark_algorithms import FrozenEvidenceRoad, compatible_pairs, private_json
    from app.planning_inputs import content_hash
    from app.routing_engine import normalize
    body = read_json(Path(config['collection_folder']) / 'collection-input.json')
    baseline = read_json(Path(config['collection_folder']) / 'collection-baseline.json')
    if not isinstance(body, dict):raise StudyIntegrityError('Collection input export is missing')
    with sqlite3.connect(f'{Path(graph_path).resolve().as_uri()}?mode=ro', uri=True) as connection:
        meta = {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM metadata')}
    if content_hash(body) != meta.get('input_hash'):raise StudyIntegrityError('Graph and collection input hashes differ')
    if baseline is not None and (baseline.get('input_hash') != content_hash(body)
                                 or content_hash(baseline.get('input')) != content_hash(body)):
        raise StudyIntegrityError('Collection-start baseline belongs to another input')
    output = Path(output);output.mkdir(mode=0o700, parents=True, exist_ok=True)
    road = FrozenEvidenceRoad(graph_path)
    data = normalize(body);required = compatible_pairs(data)
    missing = [(a, b) for a, b in required if road.known_segment(a, b) is None]
    if missing:
        road.close();raise StudyIntegrityError('Formal graph misses required measured edges')
    saved = ({'id': None, 'version': None, 'content_hash': content_hash(baseline),
              'source_export': 'collection-baseline.json', 'result': baseline['result']} if baseline is not None else None)
    case = {'schema_version': 1, 'group_name': 'test2_collection_start', 'group_id': body.get('group_id'),
            'delivery_date': body['delivery_date'], 'snapshot_id': meta.get('snapshot_id'),
            'input_hash': content_hash(body), 'body_file': 'input.json',
            'baseline_file': 'baseline.json' if baseline else None,
            'baseline_hash': content_hash(saved) if saved is not None else None,
            'road_database': str(Path(graph_path).resolve()), 'graph_hash': road.graph_hash,
            'road_stats': road.stats(), 'required_directed_edges': len(required),
            'scope': 'COLLECTION_START_INPUT_PINNED_BEFORE_LATER_MASTER_CORRECTIONS',
            'baseline_policy': 'SAME_COLLECTION_START_PLAN_REPRICED_ON_THE_SAME_FROZEN_GRAPH_FOR_ALL_METHODS'}
    road.close()
    private_json(output / 'input.json', body)
    if baseline is not None:
        private_json(output / 'baseline.json', saved)
    private_json(output / 'case.json', case)
    return body, case


def prepare_cases(config):
    from scripts.benchmark_algorithms import read_only, freeze_historical_case
    folder = Path(config['output']);folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    graph = folder / 'frozen-roads.db'
    if not graph.exists():
        graph.touch(mode=0o600)
        with read_only(config['graph_database']) as source, sqlite3.connect(graph) as destination:
            source.backup(destination)
    if not graph_complete(graph):raise StudyIntegrityError('The copied graph is incomplete')
    with sqlite3.connect(graph) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':raise StudyIntegrityError('Graph SQLite integrity failed')
    body, main = _case_from_collection(config, graph, folder / 'cases/main225')
    cases = {'main225': str(folder / 'cases/main225/case.json')}
    for date in HISTORY_DATES:
        name = {'2026-10-06': 'history110', '2026-10-03': 'history61', '2026-08-23': 'history10'}[date]
        summary = freeze_historical_case(body, config['history_database'], date, folder / 'cases' / name, road_database=graph)
        if summary['graph_hash'] != main['graph_hash']:raise StudyIntegrityError('Historical case graph differs')
        baseline = read_json(folder / 'cases' / name / 'baseline.json')
        summary['baseline_hash'] = json_digest(baseline)
        atomic_json(folder / 'cases' / name / 'case.json', summary)
        cases[name] = str(folder / 'cases' / name / 'case.json')
    return cases


def run_one(specification):
    before = resource.getrusage(resource.RUSAGE_SELF);wall_start = time.monotonic()
    spec = read_json(specification)
    validate_sources(spec['source_hashes'])
    validate_case_files(spec['case_file'], spec.get('expected_case_identity'))
    if str(ROOT) not in sys.path:sys.path.insert(0, str(ROOT))
    from scripts.benchmark_algorithms import Algorithm, run_benchmark
    profile = spec['profile']
    report = run_benchmark(spec['case_file'], [Algorithm(profile['name'], profile['entrypoint'], profile['options'])],
                           budgets=(spec['budget_seconds'],), seeds=(spec['seed'],), output=spec['run_output'],
                           warm_start=spec['warm_start'], cancel_after=spec['budget_seconds'] + 3)
    validate_sources(spec['source_hashes'])
    row = report['runs'][0]
    row.update(case_name=spec['case_name'], profile_name=profile['name'], stage=spec['stage'],
               source_manifest_hash=spec['source_manifest_hash'], source_files_unchanged_during_run=True,
               wall_limit_seconds=spec['hard_seconds'], study_warm_baseline_policy='ORIGINAL_FROZEN_BASELINE_EVERY_RUN')
    row['runtime'] = runtime_versions()
    row['resources'] = worker_resources(before, resource.getrusage(resource.RUSAGE_SELF), time.monotonic() - wall_start)
    atomic_json(Path(spec['run_output']) / 'completed-run.json', row)


def execute_study(config):
    gate = source_gate(config['source_ready'])
    if gate is None:raise StudyIntegrityError('Source-ready gate is not valid')
    expected = {**gate['source_hashes'], **source_hashes(RESEARCH_SOURCES)}
    validate_sources(expected)
    if str(ROOT) not in sys.path:sys.path.insert(0, str(ROOT))
    from app.planning_inputs import content_hash
    from scripts.benchmark_algorithms import FrozenEvidenceRoad, private_json
    from scripts.exact_reference_oracle import exact_reference_oracle
    from app.route_lower_bound import compute_lower_bound
    folder = Path(config['output']);folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    cases = prepare_cases(config)
    source_manifest_hash = content_hash(expected)
    manifest = {'schema_version': 1, 'created_at': int(time.time()), 'source_gate': gate,
                'source_hashes': expected, 'source_manifest_hash': source_manifest_hash,
                'study_config': {key: config[key] for key in ('deadline_epoch', 'cutoff_epoch', 'screen_seconds',
                                 'small_seconds', 'refine_seconds', 'long_seconds', 'cold_seconds')},
                'cases': {name: read_json(path) for name, path in cases.items()}, 'profiles': profiles(),
                'deadline_epoch': config['deadline_epoch'], 'study_cutoff_epoch': config['cutoff_epoch'],
                'planned_optimization_seconds': planned_seconds(config), 'source_business_database_written': False,
                'external_calls': 0, 'warm_start_policy': 'ORIGINAL_FROZEN_BASELINE_EVERY_RUN',
                'comparison_scope': 'SAME_FROZEN_INPUT_GRAPH_AND_SHARED_EXACT_VERIFIER_SELECTOR',
                'v3_scope': 'FROZEN_V3_CANDIDATE_GENERATOR_WITH_CURRENT_SHARED_VERIFIER_AND_SELECTOR'}
    previous_manifest = read_json(folder / 'study_manifest.json')
    if previous_manifest is not None:
        if (previous_manifest['source_manifest_hash'] != source_manifest_hash
                or case_identities(previous_manifest['cases']) != case_identities(manifest['cases'])
                or previous_manifest.get('study_config') != manifest['study_config']):
            raise StudyIntegrityError('Resume manifest does not match the same source and inputs')
        manifest = previous_manifest
    else:atomic_json(folder / 'study_manifest.json', manifest)
    rows = []
    started = time.monotonic()
    skipped = []
    def progress(stage, current=None):
        summary = {'status': 'RUNNING', 'stage': stage, 'current_run': current,
                   'completed_runs': len(rows), 'invalid_or_error_runs': sum(r['status'] != 'VALID' for r in rows),
                   'best_by_case': {name: profile_ranking(rows, name)[:1] for name in cases},
                   'updated_at': int(time.time()), 'elapsed_seconds': round(time.monotonic() - started, 2),
                   'study_seconds_remaining': max(0, round(config['cutoff_epoch'] - time.time(), 2)),
                   'source_manifest_hash': source_manifest_hash, 'skipped_runs': skipped}
        atomic_json(folder / 'study_progress.json', summary)
    def experiment(stage, name, profile, seconds, seed, warm=True):
        validate_sources(expected)
        validate_case_files(cases[name], manifest['cases'][name])
        slug = f'b{seconds}-s{seed}-' + ('warm' if warm else 'cold')
        output = folder / 'results' / stage / name / profile['name'] / slug
        complete = read_json(output / 'completed-run.json')
        if complete is not None:
            if complete.get('source_manifest_hash') != source_manifest_hash:raise StudyIntegrityError('Resume run source mismatch')
            if complete.get('graph_hash') != manifest['cases'][name]['graph_hash'] or complete.get('input_hash') != manifest['cases'][name]['input_hash']:
                raise StudyIntegrityError('Resume run input or graph mismatch')
            complete['resumed_completed_run'] = True;rows.append(complete);progress(stage);return complete
        if not can_start(time.time(), config['cutoff_epoch'], seconds, margin=25):
            skipped.append({'stage': stage, 'case_name': name, 'profile_name': profile['name'], 'seconds': seconds, 'seed': seed,
                            'reason': 'DEPLOYMENT_RESERVE_DEADLINE'});progress(stage);return None
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        hard_seconds = seconds + max(25, int(seconds * .05))
        spec = {'stage': stage, 'case_name': name, 'case_file': cases[name], 'profile': profile,
                'budget_seconds': seconds, 'seed': seed, 'warm_start': warm, 'hard_seconds': hard_seconds,
                'source_hashes': expected, 'source_manifest_hash': source_manifest_hash, 'run_output': str(output)}
        spec['expected_case_identity'] = {key: manifest['cases'][name].get(key) for key in ('input_hash', 'graph_hash', 'baseline_hash')}
        spec_path = output / 'run-spec.json';atomic_json(spec_path, spec)
        progress(stage, {'case_name': name, 'profile_name': profile['name'], 'budget_seconds': seconds,
                         'seed': seed, 'warm_start': warm, 'started_at': int(time.time())})
        log_path = output / 'process.log';log_path.touch(mode=0o600, exist_ok=True)
        timeout = min(hard_seconds, max(1, config['cutoff_epoch'] - time.time()))
        with log_path.open('a') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--run-one', str(spec_path)],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       close_fds=True, env={**os.environ, **EXPERIMENT_ENV})
            try:return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                return_code = -1
        validate_sources(expected)
        row = read_json(output / 'completed-run.json')
        if row is None:
            row = {'case_name': name, 'profile_name': profile['name'], 'algorithm': profile['name'],
                   'stage': stage, 'budget_seconds': seconds, 'seed': seed, 'status': 'ERROR',
                   'exception_type': 'WORKER_TIMEOUT' if return_code == -1 else 'WORKER_EXIT',
                   'return_code': return_code, 'source_manifest_hash': source_manifest_hash,
                   'input_hash': manifest['cases'][name]['input_hash'], 'graph_hash': manifest['cases'][name]['graph_hash'],
                   'log_file': str(log_path)}
            atomic_json(output / 'completed-run.json', row)
        rows.append(row);progress(stage)
        print(json.dumps({'event': 'RUN_COMPLETED', 'stage': stage, 'case': name, 'profile': profile['name'],
                          'seed': seed, 'budget': seconds, 'status': row['status'],
                          'fee': (row.get('audit') or {}).get('total_fee')}, ensure_ascii=False), flush=True)
        return row
    specs = profiles();by_name = {spec['name']: spec for spec in specs}
    # Round-robin profiles/cases gives useful fair partial results early.
    for seed in (0, 1):
        for profile in specs:
            for case in ('main225', 'history110', 'history61'):
                experiment('screen', case, profile, config['screen_seconds'], seed)
    for profile in specs:experiment('small_oracle_check', 'history10', profile, config['small_seconds'], 0)
    refine_choices = {}
    for case in ('main225', 'history110', 'history61'):
        choices = profile_ranking(rows, case, stages={'screen'})[:3]
        refine_choices[case] = [choice['profile_name'] for choice in choices]
    atomic_json(folder / 'refine_choices.json', refine_choices)
    for seed in (0, 1, 2):
        for case in ('main225', 'history110', 'history61'):
            for name in refine_choices[case]:experiment('refine', case, by_name[name], config['refine_seconds'], seed)
    long_choices = [r['profile_name'] for r in profile_ranking(rows, 'main225', stages={'screen', 'refine'})[:2]]
    atomic_json(folder / 'long_choices.json', long_choices)
    for seed in (3, 4):
        for name in long_choices:experiment('long_main', 'main225', by_name[name], config['long_seconds'], seed)
    if config['cold_seconds'] > 0 and time.time() + config['cold_seconds'] * max(1, len(long_choices)) + 300 < config['cutoff_epoch']:
        for name in long_choices:experiment('cold_control', 'main225', by_name[name], config['cold_seconds'], 5, warm=False)
    validate_sources(expected)
    progress('certificates')
    bounds = {}
    for name in ('main225', 'history110', 'history61'):
        if time.time() + 15 >= config['cutoff_epoch']:break
        case = manifest['cases'][name];body = read_json(Path(cases[name]).parent / case['body_file'])
        road = FrozenEvidenceRoad(case['road_database'])
        try:bound = compute_lower_bound(body, road, budget_seconds=10, include_certificate=True)
        finally:road.close()
        bounds[name] = bound
        private_json(folder / 'certificates' / (name + '-lower-bound.json'), bound)
    small = manifest['cases']['history10'];body = read_json(Path(cases['history10']).parent / small['body_file'])
    road = FrozenEvidenceRoad(small['road_database'])
    try:exact_result, exact_proof = exact_reference_oracle(body, road, budget_seconds=min(120, max(0, config['cutoff_epoch'] - time.time() - 5)))
    finally:road.close()
    private_json(folder / 'certificates/history10-exact-result.json', exact_result)
    private_json(folder / 'certificates/history10-exact-proof.json', exact_proof)
    if exact_proof['frozen_model_optimality_proven']:
        optimum = Decimal(exact_proof['audit']['total_fee'])
        if any(row['status'] == 'VALID' and row['case_name'] == 'history10'
               and Decimal(row['audit']['total_fee']) < optimum for row in rows):
            raise StudyIntegrityError('A measured heuristic beats the independently proved oracle')
    rankings = {case: profile_ranking(rows, case) for case in cases}
    result = {'schema_version': 1, 'status': 'COMPLETED' if not skipped else 'COMPLETED_WITH_DEADLINE_SKIPS',
              'manifest': manifest, 'completed_runs': len(rows), 'runs': rows, 'skipped_runs': skipped,
              'ranking_by_case': rankings, 'best_by_case': {name: ranks[0]['best'] if ranks else None for name, ranks in rankings.items()},
              'lower_bound_by_case': {name: bound['cost_lower_bound'] for name, bound in bounds.items()},
              'history10_exact_proof': exact_proof, 'source_files_unchanged': validate_sources(expected) == expected,
              'source_business_database_written': False, 'external_calls': 0,
              'finished_at': int(time.time()), 'deadline_epoch': config['deadline_epoch'],
              'deployment_reserve_seconds_remaining': max(0, config['deadline_epoch'] - time.time()),
              'global_225_task_optimality_proven': False}
    if not result['best_by_case']['main225']:result['status'] = 'NO_VALID_COMPLETE_MAIN_RESULT'
    atomic_json(folder / 'study_summary.json', result)
    atomic_json(folder / 'study_progress.json', {'status': result['status'], 'stage': 'FINISHED',
                'completed_runs': len(rows), 'best_by_case': result['best_by_case'], 'updated_at': int(time.time()),
                'skipped_runs': skipped, 'summary_file': str(folder / 'study_summary.json')})


def manage(config):
    folder = Path(config['output']);folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (folder / 'study.lock').open('a');os.chmod(folder / 'study.lock', 0o600)
    try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:raise StudyIntegrityError('This study already has a running manager')
    stopping = [False]
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))
    state_path = folder / 'manager-status.json'
    while not stopping[0] and time.time() < config['cutoff_epoch']:
        gate_error = None
        try:gate = source_gate(config['source_ready'])
        except StudyIntegrityError as exc:gate = None;gate_error = str(exc)
        complete = graph_complete(config['graph_database']) if gate is not None else False
        state = {'status': 'READY_TO_RUN' if complete else 'WAITING_COMPLETE_GRAPH' if gate else 'WAITING_SOURCE_READY',
                 'manager_pid': os.getpid(), 'updated_at': int(time.time()), 'cutoff_epoch': config['cutoff_epoch'],
                 'deadline_epoch': config['deadline_epoch'], 'source_business_database_written': False, 'external_calls': 0}
        if gate_error:state.update(status='WAITING_SOURCE_HASHES_MATCH', error=gate_error)
        if gate is not None and not complete:
            collection = read_json(Path(config['collection_folder']) / 'collection-progress.json', {})
            if collection.get('status') == 'INCOMPLETE':
                state.update(status='NEEDS_ROAD_RETRY',
                             remaining_provider_keys=max(0, collection.get('expected_provider_keys', 0) - collection.get('completed_provider_keys', 0)),
                             collector_status='INCOMPLETE')
        atomic_json(state_path, state)
        if complete:break
        time.sleep(min(20, max(.1, config['cutoff_epoch'] - time.time())))
    else:
        atomic_json(state_path, {'status': 'STOPPED' if stopping[0] else 'NO_COMPLETE_GRAPH_BEFORE_STUDY_CUTOFF', 'updated_at': int(time.time())})
        return 2
    config_path = folder / 'study-config.json';atomic_json(config_path, config)
    with (folder / 'worker.log').open('a') as log:
        os.chmod(folder / 'worker.log', 0o600)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--execute', str(config_path)],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True,
                                   env={**os.environ, **EXPERIMENT_ENV})
        state.update(status='RUNNING', worker_pid=process.pid, started_at=int(time.time()))
        atomic_json(state_path, state)
        while process.poll() is None and not stopping[0] and time.time() < config['cutoff_epoch']:
            time.sleep(1)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:os.killpg(process.pid, signal.SIGKILL);process.wait(timeout=3)
            status = 'STOPPED' if stopping[0] else 'STUDY_CUTOFF_REACHED'
        else:status = 'FINISHED' if process.returncode == 0 else 'WORKER_FAILED'
        state.update(status=status, return_code=process.returncode, finished_at=int(time.time()))
        atomic_json(state_path, state)
        return 0 if status == 'FINISHED' else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', help=argparse.SUPPRESS)
    parser.add_argument('--run-one', help=argparse.SUPPRESS)
    parser.add_argument('--output')
    parser.add_argument('--source-ready', default=str(ROOT / 'data/algorithm-lab-20261113/source-ready.json'))
    parser.add_argument('--collection-folder', default=str(ROOT / 'data/algorithm-lab-20261113'))
    parser.add_argument('--graph-database', default=str(ROOT / 'data/algorithm-lab-20261113/full-roads.db'))
    parser.add_argument('--history-database', default=str(ROOT / 'data/retest-20261112/full-replay.db'))
    parser.add_argument('--deadline-epoch', type=int)
    parser.add_argument('--reserve-seconds', type=int, default=2700)
    parser.add_argument('--screen-seconds', type=int, default=60)
    parser.add_argument('--small-seconds', type=int, default=5)
    parser.add_argument('--refine-seconds', type=int, default=300)
    parser.add_argument('--long-seconds', type=int, default=900)
    parser.add_argument('--cold-seconds', type=int, default=180)
    parser.add_argument('--detach', action='store_true')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    if args.run_one:return run_one(args.run_one)
    if args.execute:
        try:execute_study(read_json(args.execute))
        except Exception as exc:
            config = read_json(args.execute) or {}
            if config.get('output'):atomic_json(Path(config['output']) / 'study-failure.json', {'exception_type': type(exc).__name__, 'message': str(exc), 'updated_at': int(time.time())})
            raise
        return 0
    if args.deadline_epoch is None:parser.error('--deadline-epoch is required')
    config = vars(args).copy()
    for key in ('execute', 'run_one', 'detach', 'plan_only'):config.pop(key)
    if config['output'] is None:
        suffix = datetime.now(BEIJING).strftime('%H%M%S')
        config['output'] = str(ROOT / 'data/algorithm-lab-20261113' / ('formal-study-' + suffix))
    config['output'] = str(Path(config['output']).resolve())
    config['cutoff_epoch'] = config['deadline_epoch'] - max(0, config['reserve_seconds'])
    if any(config[key] < 0 for key in ('screen_seconds', 'small_seconds', 'refine_seconds', 'long_seconds', 'cold_seconds')):
        parser.error('Experiment budgets must be nonnegative')
    if args.plan_only:
        plan = {'config': config, 'profiles': profiles(), 'planned_seconds': planned_seconds(config),
                'cutoff_beijing': datetime.fromtimestamp(config['cutoff_epoch'], BEIJING).isoformat()}
        print(json.dumps(plan, ensure_ascii=False, indent=2));return 0
    folder = Path(config['output']);folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.detach:
        command = [sys.executable, str(Path(__file__).resolve())]
        for key, value in config.items():
            if key == 'cutoff_epoch':continue
            command.extend(['--' + key.replace('_', '-'), str(value)])
        log_path = folder / 'manager.log';log_path.touch(mode=0o600, exist_ok=True)
        with log_path.open('a') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        atomic_json(folder / 'launcher.json', {'manager_pid': process.pid, 'output': str(folder), 'deadline_epoch': config['deadline_epoch']})
        print(json.dumps({'manager_pid': process.pid, 'output': str(folder)}, ensure_ascii=False));return 0
    return manage(config)


if __name__ == '__main__':
    sys.exit(main() or 0)
