"""Prelock and audit complete real-day paired acceptance; never run a solver.

``lock`` pins final sources, profile, input, roads and hints before fee results.
``check`` consumes evaluate_delivery_stages reports, requires every registered
cell, and applies the cost gates separately at each budget and hint regime.
Private lock/report files belong in ignored data directories.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import asdict
from decimal import Decimal, localcontext
import hashlib
import itertools
import json
import math
import importlib.util
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x', encoding='utf-8') as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def budget_key(value):
    return format(float(value), '.17g')


def positive_finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _inspect_source_switches(source, requests):
    """Evaluate only the source's declarative switch factory, never its solver."""
    source = Path(source).resolve()
    path = source / 'app/route_deep.py'
    tree = ast.parse(path.read_text())
    optimizer = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'optimize_deep'), None)
    if optimizer is None:
        raise ValueError('No daily optimizer switch factory found')
    assignments = []
    for node in optimizer.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == 'switches' for t in targets):
            assignments.append(node.value)
    if len(assignments) != 1:
        raise ValueError('Expected one explicit switches expression; cannot infer actual defaults safely')
    expression = assignments[0]
    allowed = (ast.Expression, ast.IfExp, ast.Call, ast.Name, ast.Load, ast.Constant,
               ast.Compare, ast.Is, ast.IsNot, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq,
               ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Subscript, ast.Attribute,
               ast.keyword, ast.Dict, ast.List, ast.Tuple)
    names = {'DeepExperiments', 'experiments', 'budget', 'data', 'len'}
    for node in ast.walk(expression):
        if not isinstance(node, allowed):
            raise ValueError('Unsupported default-switch syntax: ' + type(node).__name__)
        if isinstance(node, ast.Name) and node.id not in names:
            raise ValueError('Unknown default-switch name: ' + node.id)
        if isinstance(node, ast.Attribute):
            permitted = (isinstance(node.value, ast.Name) and
                         (node.value.id, node.attr) in {('budget', 'seconds'), ('DeepExperiments', 'parse')})
            if not permitted:
                raise ValueError('Unsupported attribute in default-switch factory')
        if isinstance(node, ast.Call):
            permitted = (isinstance(node.func, ast.Name) and node.func.id in {'DeepExperiments', 'len'}
                         or isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                         and node.func.value.id == 'DeepExperiments' and node.func.attr == 'parse')
            if not permitted or any(k.arg is None for k in node.keywords):
                raise ValueError('Unsupported call in default-switch factory')
    spec = importlib.util.spec_from_file_location('locked_deep_experiments', source / 'app/route_experiments.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(source))
    spec.loader.exec_module(module)
    from types import SimpleNamespace
    code = compile(ast.Expression(expression), str(path), 'eval')
    entries = []
    for request in requests:
        value = eval(code, {'__builtins__': {}}, {
            'DeepExperiments': module.DeepExperiments,
            'experiments': request['options'].get('experiments'),
            'budget': SimpleNamespace(seconds=float(request['budget'])),
            'data': {'tasks': range(request['task_count'])}, 'len': len})
        entries.append({'budget': budget_key(request['budget']), 'input_hash': request['input_hash'],
                        'flags': asdict(value)})
    return {'factory_expression': ast.unparse(expression), 'factory_file_sha256': file_hash(path),
            'scope': 'RESTRICTED_SWITCH_FACTORY_EVALUATION_NO_OPTIMIZER_OR_SOLVER_EXECUTION',
            'expectations': entries}


def source_switch_expectations(source, requests):
    # Separate processes keep old/new class definitions and module caches apart.
    tool_root = str(Path(__file__).resolve().parents[1])
    code = "import json,sys;sys.path.insert(0,sys.argv[1]);from scripts.validate_general_acceptance import _inspect_source_switches;print(json.dumps(_inspect_source_switches(sys.argv[2],json.loads(sys.argv[3])),sort_keys=True))"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('SUKA_', 'TUNNEL_'))}
    env.update(PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', code, tool_root, str(Path(source).resolve()), json.dumps(requests)],
                            cwd=source, env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def make_lock(protocol_path, index_path, profiles_path, baseline_source, candidate_source,
              *, production_search_policy=False):
    # This import computes identities and manifests only. It never starts work.
    from scripts.evaluate_delivery_stages import case_identity, source_manifest, road_execution_policy
    protocol, index, profiles = read(protocol_path), read(index_path), read(profiles_path)
    if not isinstance(production_search_policy, bool):
        raise ValueError('Search policy must be an explicit boolean')
    if protocol.get('required_production_search_policy') and not production_search_policy:
        raise ValueError('This protocol requires the native production search policy')
    if len(profiles) != 1 or not profiles[0].get('name'):
        raise ValueError('Prelock one final named profile; separate ablations need separate locks')
    profile = profiles[0]
    if profile.get('capture_candidate_stream') or profile.get('capture_native_candidate_stream'):
        raise ValueError('Stream capture is diagnostic evidence, not acceptance timing')
    expected = {(row['day'], row['hint_regime']): row['input_hash'] for row in protocol['input_cases']}
    cases = []
    for row in index['cases']:
        key = row['day'], row['regime']
        if key not in expected:
            raise ValueError('Unregistered day or hint regime')
        identity = case_identity(row['case_file'])
        if identity['input_hash'] != expected[key]:
            raise ValueError('Changed registered full-day input')
        if identity['graph_hash'] != row['graph_hash'] or identity['historical_hints_hash'] != row['hint_hash']:
            raise ValueError('Case index does not identify its actual graph and hints')
        case_manifest = read(row['case_file'])
        body = read(Path(row['case_file']).parent / case_manifest['body_file'])
        task_count = len({t['store_id'] if 'stores' in body else t['id'] for t in body['tasks']})
        cases.append({'day': key[0], 'regime': key[1], 'identity': identity, 'task_count': task_count})
    if len(cases) != len(expected) or {(c['day'], c['regime']) for c in cases} != set(expected):
        raise ValueError('All locked cases and both hint regimes are required exactly once')
    if len({c['identity']['graph_hash'] for c in cases}) != 1:
        raise ValueError('One road batch must have one common measured graph, including both hint regimes')
    by_day = {}
    for c in cases:
        by_day.setdefault(c['day'], {})[c['regime']] = c
    for day, regimes in by_day.items():
        if regimes['with-hints']['identity']['input_hash'] != regimes['without-hints']['identity']['input_hash']:
            raise ValueError('Hint strata must have identical business input')
        # Empty object is the existing FrozenEvidenceRoad no-hint identity.
        empty = hashlib.sha256(b'{}').hexdigest()
        if regimes['without-hints']['identity']['historical_hints_hash'] != empty:
            raise ValueError('Hints-off case contains historical hints')
    sources = {'baseline': baseline_source, 'candidate': candidate_source}
    manifests = {side: source_manifest(root) for side, root in sources.items()}
    expected, factories = {}, {}
    for side, source in sources.items():
        entrypoint = profile.get(side + '_entrypoint', profile.get('entrypoint', 'app.route_deep:optimize_deep'))
        if entrypoint != 'app.route_deep:optimize_deep':
            raise ValueError('Actual-default flag locks require the daily optimize_deep entrypoint')
        options = profile.get('baseline_options', {}) if side == 'baseline' else profile.get('candidate_options', profile.get('options', {}))
        unique = {(budget_key(b), c['identity']['input_hash']): {'budget': b, 'input_hash': c['identity']['input_hash'],
                  'task_count': c['task_count'], 'options': options} for b in protocol['budgets_seconds'] for c in cases}
        resolved = source_switch_expectations(source, list(unique.values()))
        expected[side] = {}
        for item in resolved.pop('expectations'):
            expected[side].setdefault(item['budget'], {})[item['input_hash']] = item['flags']
        factories[side] = resolved
        if source_manifest(source) != manifests[side]:
            raise ValueError('Source changed while locking actual default flags')
    return {'schema_version': 3, 'scope': 'FINAL_PROFILE_PRE_RESULT_EXECUTION_LOCK',
            'production_search_policy': production_search_policy,
            'road_execution_policy': road_execution_policy(production_search_policy),
            'production_acceptance_eligible': production_search_policy,
            'locked_at_epoch': int(time.time()), 'fees_or_algorithm_results_read_for_lock': False,
            'protocol_file': str(Path(protocol_path).resolve()), 'protocol_sha256': file_hash(protocol_path),
            'protocol': protocol, 'case_index_file': str(Path(index_path).resolve()),
            'case_index_sha256': file_hash(index_path), 'cases': cases, 'profile': profile,
            'source_roots': {'baseline': str(Path(baseline_source).resolve()),
                             'candidate': str(Path(candidate_source).resolve())},
            'source_manifests': manifests, 'expected_experiments': expected,
            'expected_experiments_resolution': factories,
            'expected_runs': protocol['expected_runs_per_profile'],
            'execution_order': 'case-index order, budget order, seed order; AB/BA alternating by pair ordinal',
            'no_business_database_write': True, 'no_network': True}


def _ratio(old, new):
    if old == 0:
        return Decimal(1) if new == 0 else None
    return new / old


def _geomean(values):
    if not values or any(v is None or v <= 0 for v in values):
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        return (sum(v.ln() for v in values) / len(values)).exp()


def _string(value):
    return str(value) if value is not None else None


def validate(lock, reports):
    """Require every cell, reject duplicates/changed evidence, retain failures."""
    p, profile = lock['protocol'], lock['profile']['name']
    sources = {s: m['sha256'] for s, m in lock['source_manifests'].items()}
    identities = {c['identity']['case_file']: c for c in lock['cases']}
    expected = set(itertools.product(p['days'], p['hint_regimes'], p['budgets_seconds'], p['seeds'], p['sides']))
    rows, problems, all_rows = {}, [], []
    from scripts.evaluate_delivery_stages import road_execution_policy
    expected_road_policy = road_execution_policy(True)
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
    production_policy_locked = (lock.get('production_search_policy') is True
                                and canonical(lock.get('road_execution_policy')) == canonical(expected_road_policy))
    if not production_policy_locked:
        problems.append({'code': 'PRODUCTION_SEARCH_POLICY_NOT_LOCKED',
                         'reason': 'Research or unrecorded candidate-count policy cannot attest daily production performance'})
    strict_order = []
    for report_no, report in enumerate(reports):
        manifest = report.get('manifest', {})
        if (manifest.get('production_search_policy') is not True
                or canonical(manifest.get('road_execution_policy')) != canonical(expected_road_policy)):
            problems.append({'code': 'MANIFEST_PRODUCTION_SEARCH_POLICY_MISMATCH', 'report': report_no})
        if not report.get('finished'):
            problems.append({'code': 'REPORT_INCOMPLETE', 'report': report_no})
        if manifest.get('warm_start'):
            problems.append({'code': 'WARM_START_NOT_COLD', 'report': report_no})
        if manifest.get('config', {}).get('profiles') != [lock['profile']]:
            problems.append({'code': 'ALGORITHM_PROFILE_MISMATCH', 'report': report_no})
        if manifest.get('execution_policy') != 'STRICTLY_SERIAL_ALTERNATING_PAIRED_FRESH_PROCESSES':
            problems.append({'code': 'EXECUTION_POLICY_NOT_VERIFIED', 'report': report_no})
        for side, source_hash in sources.items():
            if manifest.get('source_manifests', {}).get(side, {}).get('sha256') != source_hash:
                problems.append({'code': 'SOURCE_MANIFEST_MISMATCH', 'report': report_no, 'side': side})
        for ordinal, row in enumerate(report.get('runs', [])):
            all_rows.append(row)
            identity = row.get('identity', {})
            pinned = identities.get(identity.get('case_file'))
            if pinned is None or identity != pinned['identity']:
                problems.append({'code': 'UNREGISTERED_OR_CHANGED_CASE', 'report': report_no, 'run': ordinal})
                continue
            day, regime = pinned['day'], pinned['regime']
            key = (day, regime, row.get('compute_seconds'), row.get('seed'), row.get('side'))
            if key not in expected or row.get('profile_name') != profile:
                problems.append({'code': 'UNREGISTERED_RUN', 'cell': list(key), 'report': report_no})
                continue
            if key in rows:
                problems.append({'code': 'DUPLICATE_RUN_NO_BEST_SELECTION', 'cell': list(key)})
                continue
            rows[key] = row
            strict_order.append(key)
            errors = []
            if (row.get('production_search_policy') is not True
                    or canonical(row.get('road_execution_policy')) != canonical(expected_road_policy)):
                errors.append('PRODUCTION_SEARCH_POLICY_MISMATCH')
            if row.get('road_production_planning') is not True or row.get('road_timer') != 'time.monotonic':
                errors.append('PRODUCTION_SEARCH_ADAPTER_NOT_ATTESTED')
            launched = row.get('worker_launched_monotonic')
            deadline = row.get('road_wall_deadline_monotonic')
            wait = row.get('wait_seconds')
            if (not all(positive_finite_number(value) for value in (launched, deadline, wait))
                    or abs(deadline - (launched + wait)) > .000001):
                errors.append('PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH')
            if row.get('status') != 'VALID' or not row.get('audit', {}).get('valid'):
                errors.append('NOT_INDEPENDENTLY_VALID')
            if row.get('deadline_exhausted'):
                errors.append('WAIT_DEADLINE_EXHAUSTED')
            if row.get('source_hash') != sources[key[-1]] or row.get('source_unchanged') is not True:
                errors.append('SOURCE_CHANGED_OR_UNVERIFIED')
            if row.get('hash_seed') != p['hash_seed']:
                errors.append('HASH_SEED_MISMATCH')
            environment = row.get('runtime', {}).get('environment', {})
            numeric_env = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                           'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS')
            if any(environment.get(k) != '1' for k in numeric_env):
                errors.append('NUMERIC_THREAD_ENV_NOT_PINNED')
            cfg = lock['profile']
            expected_options = cfg.get('baseline_options', {}) if key[-1] == 'baseline' else cfg.get('candidate_options', cfg.get('options', {}))
            expected_entrypoint = cfg.get(key[-1] + '_entrypoint', cfg.get('entrypoint', 'app.route_deep:optimize_deep'))
            if row.get('options') != expected_options or row.get('entrypoint') != expected_entrypoint:
                errors.append('ALGORITHM_OPTIONS_OR_ENTRYPOINT_MISMATCH')
            expected_flags = lock.get('expected_experiments', {}).get(key[-1], {}).get(budget_key(key[2]), {}).get(identity['input_hash'])
            actual_flags = row.get('result_algorithm', {}).get('experiments')
            if expected_flags is None:
                errors.append('ACTUAL_EXPERIMENT_FLAGS_NOT_LOCKED')
            elif json.dumps(actual_flags, sort_keys=True, separators=(',', ':')) != json.dumps(expected_flags, sort_keys=True, separators=(',', ':')):
                errors.append('ACTUAL_EXPERIMENT_FLAGS_MISMATCH')
            if row.get('wait_seconds') != p['wait_policy']['seconds_by_budget'][str(int(key[2]))]:
                errors.append('WAIT_BUDGET_MISMATCH')
            if row.get('warm_start') or row.get('timing_scope') != 'NORMAL_COLD_START':
                errors.append('NOT_NORMAL_COLD_START')
            if row.get('external_calls') != 0 or row.get('source_business_database_written') is not False:
                errors.append('NONFROZEN_OR_BUSINESS_WRITE')
            for field, tolerance in [('compute_overrun_seconds', p['acceptance_thresholds']['compute_overrun_tolerance_seconds']),
                                     ('wait_overrun_seconds', p['acceptance_thresholds']['wait_overrun_tolerance_seconds'])]:
                if row.get(field) is None:
                    errors.append(field.upper() + '_UNMEASURED')
                elif not math.isfinite(float(row[field])) or float(row[field]) > tolerance:
                    errors.append(field.upper())
            if errors:
                problems.append({'code': 'RUN_ACCEPTANCE_FAILURE', 'cell': list(key), 'errors': errors,
                                 'expected_experiments': expected_flags, 'actual_experiments': actual_flags,
                                 'run_folder': row.get('run_folder')})
    missing = sorted(expected - set(rows))
    if missing:
        problems.append({'code': 'MISSING_REGISTERED_RUNS', 'cells': [list(c) for c in missing]})
    # Within each supplied batch pairs must be adjacent; parity may restart for
    # a separately preregistered batch, but never run both complete versions first.
    for report_no, report in enumerate(reports):
        runlist = report.get('runs', [])
        for offset in range(0, len(runlist), 2):
            pair = runlist[offset:offset + 2]
            valid_pair = len(pair) == 2 and {r.get('side') for r in pair} == set(p['sides'])
            pair_fields = ('identity', 'compute_seconds', 'wait_seconds', 'seed', 'hash_seed', 'profile_name',
                           'production_search_policy', 'road_execution_policy')
            valid_pair = valid_pair and all(pair[0].get(k) == pair[1].get(k) for k in pair_fields)
            wanted = ['baseline', 'candidate'] if (offset // 2) % 2 == 0 else ['candidate', 'baseline']
            valid_pair = valid_pair and [r.get('side') for r in pair] == wanted
            if not valid_pair:
                problems.append({'code': 'NONALTERNATING_OR_UNPAIRED_RUN_ORDER', 'report': report_no, 'offset': offset})
    gates, regressions, first_complete, failures = [], [], [], []
    for key, row in rows.items():
        diag = row.get('result_algorithm', {}).get('diagnostics', {})
        first_complete.append({'day': key[0], 'regime': key[1], 'budget': key[2], 'seed': key[3], 'side': key[4],
                               'elapsed_seconds': diag.get('first_complete_elapsed_seconds'),
                               'compute_seconds': diag.get('first_complete_compute_seconds')})
        if row.get('status') != 'VALID' or row.get('deadline_exhausted') or not row.get('audit', {}).get('valid'):
            failures.append({'cell': list(key), 'status': row.get('status'), 'audit': row.get('audit'),
                             'exception': row.get('exception'), 'run_folder': row.get('run_folder')})
    thresholds = p['acceptance_thresholds']
    for budget, regime in itertools.product(p['budgets_seconds'], p['hint_regimes']):
        days, ratios = [], []
        for day in p['days']:
            values, seed_ratios, seed_fees = {'baseline': [], 'candidate': []}, [], []
            for seed in p['seeds']:
                pair = {side: rows.get((day, regime, budget, seed, side)) for side in p['sides']}
                if any(not r or r.get('status') != 'VALID' or not r.get('audit', {}).get('valid') for r in pair.values()):
                    continue
                fees = {s: Decimal(r['audit']['total_fee']) for s, r in pair.items()}
                for side, fee in fees.items():
                    values[side].append(fee)
                ratio = _ratio(fees['baseline'], fees['candidate'])
                seed_ratios.append(ratio)
                seed_fees.append({'seed': seed, 'baseline_fee': str(fees['baseline']),
                                  'candidate_fee': str(fees['candidate'])})
                if fees['candidate'] > fees['baseline']:
                    regressions.append({'day': day, 'regime': regime, 'budget': budget, 'seed': seed,
                                        'baseline_fee': str(fees['baseline']), 'candidate_fee': str(fees['candidate']),
                                        'ratio': _string(ratio)})
            complete = len(values['baseline']) == len(p['seeds'])
            medians = {s: statistics.median(fees) if complete else None for s, fees in values.items()}
            ratio = _ratio(medians['baseline'], medians['candidate']) if complete else None
            ratios.append(ratio)
            days.append({'day': day, 'complete_seed_pairs': len(values['baseline']),
                         'baseline_median_fee': _string(medians['baseline']), 'candidate_median_fee': _string(medians['candidate']),
                         'median_ratio': _string(ratio), 'seed_ratios': [_string(x) for x in seed_ratios],
                         'seed_fees': seed_fees})
        geometric = _geomean(ratios)
        seed_values = [Decimal(r) for d in days for r in d['seed_ratios'] if r is not None]
        worst_seed = max(seed_values) if len(seed_values) == len(p['days']) * len(p['seeds']) else None
        complete = all(r is not None for r in ratios) and worst_seed is not None
        improved = sum(r is not None and r < 1 for r in ratios)
        gate = {'budget': budget, 'regime': regime, 'days': days, 'complete': complete,
                'geometric_mean_day_median_ratio': _string(geometric), 'strictly_improved_days': improved,
                'worst_seed_ratio': _string(worst_seed),
                'cost_gate_passed': bool(complete and geometric is not None
                    and geometric <= Decimal(str(thresholds['geometric_mean_day_median_ratio_at_most']))
                    and improved >= thresholds['minimum_strictly_improved_day_medians']
                    and max(ratios) <= Decimal(str(thresholds['maximum_day_median_ratio']))
                    and worst_seed <= Decimal(str(thresholds['maximum_single_seed_ratio'])))}
        gates.append(gate)
    timing = []
    for budget, regime, side in itertools.product(p['budgets_seconds'], p['hint_regimes'], p['sides']):
        group = [r for k, r in rows.items() if (k[2], k[1], k[4]) == (budget, regime, side)]
        first = [r.get('result_algorithm', {}).get('diagnostics', {}).get('first_complete_elapsed_seconds') for r in group]
        first = [float(value) for value in first if value is not None and math.isfinite(float(value))]
        timeout = sum(r.get('deadline_exhausted', False) or r.get('status') in {'TIMEOUT', 'RETURNED_TIMEOUT'} for r in group)
        failed = sum(r.get('status') != 'VALID' or not r.get('audit', {}).get('valid') for r in group)
        timing.append({'budget': budget, 'regime': regime, 'side': side, 'run_count': len(group),
                       'expected_run_count': len(p['days']) * len(p['seeds']), 'timeouts': timeout,
                       'timeout_rate': timeout / len(group) if group else None,
                       'failures': failed, 'failure_rate': failed / len(group) if group else None,
                       'first_complete_measurement_count': len(first),
                       'median_first_complete_elapsed_seconds': statistics.median(first) if first else None})
    complete = not missing and len(rows) == lock['expected_runs']
    passed = complete and not problems and all(g['cost_gate_passed'] for g in gates)
    return {'schema_version': 1, 'status': 'ACCEPTED' if passed else 'INCOMPLETE' if not complete else 'NOT_ACCEPTED',
            'adoption_allowed': passed, 'registered_runs': lock['expected_runs'], 'received_registered_runs': len(rows),
            'production_search_policy_locked': production_policy_locked,
            'production_search_policy_attested': bool(complete and production_policy_locked and not problems),
            'all_supplied_runs': len(all_rows), 'conditions_or_run_failures': problems, 'strata': gates,
            'all_fee_regressions': regressions, 'all_failures_and_timeouts': failures,
            'first_complete_measurements': first_complete, 'timing_summaries': timing,
            'expected_experiments': lock.get('expected_experiments'),
            'coverage_gaps': p.get('coverage_gaps', []),
            'configuration_scope': p.get('configuration_scope'),
            'scope': 'FOUR_LOCKED_REAL_DEMAND_DAYS_ONLY; EXACT_ORACLE_SCALE_BOUNDARY_AND_UI_EVIDENCE_REQUIRED_SEPARATELY',
            'global_optimality_proven': False}


def main():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    lock = subs.add_parser('lock')
    for name in ('protocol', 'case-index', 'profiles', 'baseline-source', 'candidate-source', 'output'):
        lock.add_argument('--' + name, required=True)
    lock.add_argument('--production-search-policy', action='store_true',
                      help='Lock native production search controls while keeping frozen roads offline')
    check = subs.add_parser('check')
    check.add_argument('--lock', required=True)
    check.add_argument('--report', required=True, action='append')
    check.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'lock':
        result = make_lock(args.protocol, args.case_index, args.profiles, args.baseline_source, args.candidate_source,
                           production_search_policy=args.production_search_policy)
    else:
        result = validate(read(args.lock), [read(p) for p in args.report])
        result.update(execution_lock_sha256=file_hash(args.lock), report_files={str(Path(p).resolve()): file_hash(p) for p in args.report})
    save_new(args.output, result)
    print(json.dumps({'output': str(Path(args.output).resolve()), 'sha256': file_hash(args.output),
                      'status': result.get('status', 'LOCKED')}, ensure_ascii=False))
    return 0 if args.command == 'lock' or result['adoption_allowed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
