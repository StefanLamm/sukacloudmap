"""Freeze development sources, then explicitly run single-flag paired screens.

No solver starts during ``prepare``. ``run`` uses the copied evaluator and both
copied solver trees, so edits to a live development tree cannot contaminate a
running screen. The 9/22 development day and known November regression cases are
allowed; the four locked unseen days are deliberately excluded.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DATES = {'2026-09-22', '2026-11-13', '2026-11-15'}
UNSEEN_DATES = {'2026-10-16', '2026-10-02', '2026-08-09', '2026-08-22'}
PHASE_FLAGS = {
    'retention': 'candidate_retention',
    'atomic': 'complete_assignment_pricing',
    'fragment': 'fragment_merging',
    'joint': 'joint_reoptimization',
    'joint_impl': 'joint_reoptimization',
    'schedule_rotation': 'quote_group_rotation',
    'schedule_loop': 'generation_selection_loop',
    'schedule_gain': 'gain_time_allocation',
    'reservation': 'stage_budget_reservation',
}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def save_new(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def source_listing(root):
    root = Path(root)
    paths = sorted([*(root / 'app').rglob('*.py'), *(root / 'scripts').rglob('*.py')])
    paths += [root / n for n in ('requirements.lock', 'pyproject.toml') if (root / n).is_file()]
    if not (root / 'app/routing_engine.py').is_file() or not (root / 'scripts/evaluate_delivery_stages.py').is_file():
        raise ValueError('Source lacks routing engine or pinned evaluator')
    for path in paths:
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('Source snapshot must copy regular files, never links')
    return {str(path.relative_to(root)): sha(path) for path in paths}


def snapshot_source(source, target):
    source, target = Path(source).resolve(), Path(target)
    files = source_listing(source)
    target.mkdir(parents=True, mode=0o700)
    for name, checksum in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, path)
        if sha(path) != checksum:
            raise ValueError('Source changed during snapshot; use a new output and retry')
    if source_listing(source) != files or source_listing(target) != files:
        raise ValueError('Source changed during snapshot; use a new output and retry')
    revision = None
    if (source / '.git').exists():
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    # The harness verifies these content identities again before/after each run.
    for name in files:
        (target / name).chmod(0o444)
    return {'origin': str(source), 'snapshot': str(target.resolve()), 'git_revision': revision,
            'sha256': digest(files), 'files': files, 'copy_not_link': True}


def freeze_case(source, target):
    source = Path(source).resolve()
    case = load(source)
    body_path = (source.parent / case['body_file']).resolve()
    body = load(body_path)
    day = body.get('delivery_date')
    if day in UNSEEN_DATES or day not in ALLOWED_DATES:
        raise ValueError('Development screen forbids unseen/unregistered day: ' + str(day))
    if digest(body) != case['input_hash']:
        raise ValueError('Development frozen input hash differs')
    target.mkdir(mode=0o700, parents=True)
    frozen = deepcopy(case)
    frozen['body_file'] = 'input.json'
    frozen['road_database'] = str(Path(case['road_database']).resolve())
    # Known complete plans are diagnosis-only. The cold screen never carries
    # those references into its copied case or passes an injected initial plan.
    frozen.pop('plans', None)
    if case.get('baseline_file'):
        baseline_path = (source.parent / case['baseline_file']).resolve()
        baseline = load(baseline_path)
        if case.get('baseline_hash') is not None and digest(baseline) != case['baseline_hash']:
            raise ValueError('Frozen baseline identity differs')
        frozen['baseline_file'] = 'baseline.json'
        shutil.copy2(baseline_path, target / 'baseline.json')
    shutil.copy2(body_path, target / 'input.json')
    save_new(target / 'case.json', frozen)
    return {'origin_case': str(source), 'case_file': str((target / 'case.json').resolve()),
            'delivery_date': day, 'source_group_name':case.get('group_name'), 'input_hash': case['input_hash'], 'injected_plan': False,
            'warm_start': False, 'source_manifest_sha256': sha(source)}


def profiles(phases, retention_policy):
    result = []
    expanded = []
    for phase in phases:
        expanded.extend(('schedule_rotation', 'schedule_loop', 'schedule_gain') if phase == 'schedule' else (phase,))
    for phase in dict.fromkeys(expanded):
        flag = PHASE_FLAGS[phase]
        value = retention_policy if phase == 'retention' else True
        before, after = {}, {flag: value}
        if phase == 'joint_impl':
            before = {'joint_reoptimization': True, 'stage_budget_reservation': True,
                      'scalable_joint_search': False}
            after = {'joint_reoptimization': True, 'stage_budget_reservation': True,
                     'scalable_joint_search': True}
        elif phase == 'reservation':
            before = {'joint_reoptimization': True, 'scalable_joint_search': True,
                      'stage_budget_reservation': False}
            after = {'joint_reoptimization': True, 'scalable_joint_search': True,
                     'stage_budget_reservation': True}
        elif phase == 'schedule_rotation':
            before = {'stage_budget_reservation': True, 'generation_selection_loop': True,
                      'quote_group_rotation': False}
            after = {**before, 'quote_group_rotation': True}
        elif phase == 'schedule_loop':
            before = {'stage_budget_reservation': True, 'quote_group_rotation': True,
                      'generation_selection_loop': False}
            after = {**before, 'generation_selection_loop': True}
        elif phase == 'schedule_gain':
            before = {'stage_budget_reservation': True, 'quote_group_rotation': True,
                      'generation_selection_loop': True, 'gain_time_allocation': False}
            after = {**before, 'gain_time_allocation': True}
        result.append({'name': phase + '_only', 'baseline_options': {'experiments': before},
                       'candidate_options': {'experiments': after}})
    return result


def snapshot_inspection(snapshot, case_files, selected_profiles, python, *, side='candidate'):
    # Imports/road identity reads only, in the copied source. No optimizer is
    # called and no credentials or mutable business database are loaded.
    code = '''import json,sys
sys.path.insert(0,sys.argv[1])
from scripts.evaluate_delivery_stages import case_identity,source_manifest
from app.route_experiments import DeepExperiments
spec=json.loads(sys.argv[2])
for p in spec['profiles']: DeepExperiments.parse(p[spec['side']+'_options']['experiments'])
print(json.dumps({'source_manifest':source_manifest(sys.argv[1]),'identities':{p:case_identity(p) for p in spec['cases']}},sort_keys=True))
'''
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('SUKA_', 'TUNNEL_'))}
    env.update(PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1')
    request = json.dumps({'cases': case_files, 'profiles': selected_profiles, 'side': side})
    result = subprocess.run([python, '-c', code, str(snapshot), request], cwd=snapshot,
                            env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def prepare(args):
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700)  # No overwrite, reuse or result-based retry selection.
    selected_profiles = profiles(args.phase, args.retention_policy)
    trees = {side: snapshot_source(getattr(args, side + '_source'), output / 'sources' / side)
             for side in ('baseline', 'candidate')}
    same_source_phases = ('reservation', 'activation', 'atomic', 'fragment', 'schedule',
                          'schedule_rotation', 'schedule_loop', 'schedule_gain')
    if any(phase in args.phase for phase in same_source_phases) and trees['baseline']['sha256'] != trees['candidate']['sha256']:
        raise ValueError('Controller/reservation/activation screens require the same implementation on both sides')
    cases = [freeze_case(case, output / 'cases' / str(i)) for i, case in enumerate(args.case)]
    if len({c['input_hash'] for c in cases}) != len(cases):
        raise ValueError('Duplicate development inputs are not extra independent samples')
    if len(cases) != 2 or not any(c['source_group_name'] == 'september:9.22' and c['input_hash'] == '87663da7b0f8f3f965c03fdc98cee226770871e1c2169ed417daaf81505c5607' for c in cases) or not any(c['delivery_date'] in {'2026-11-13', '2026-11-15'} for c in cases):
        raise ValueError('Prelocked screen requires exactly 9/22 and one known regression input')
    inspected = snapshot_inspection(trees['candidate']['snapshot'], [c['case_file'] for c in cases],
                                    selected_profiles, args.python)
    if inspected['source_manifest']['sha256'] != trees['candidate']['sha256']:
        raise ValueError('Pinned evaluator source identity differs')
    baseline_inspected = snapshot_inspection(trees['baseline']['snapshot'], [c['case_file'] for c in cases],
                                            selected_profiles, args.python, side='baseline')
    if baseline_inspected['source_manifest']['sha256'] != trees['baseline']['sha256'] or baseline_inspected['identities'] != inspected['identities']:
        raise ValueError('Baseline source/case inspection differs')
    profile_file = output / 'profiles.json'
    save_new(profile_file, selected_profiles)
    lock = {'schema_version': 1, 'scope': 'DEVELOPMENT_SINGLE_FLAG_SCREEN_NOT_UNSEEN_ACCEPTANCE',
            'created_at': int(time.time()), 'status': 'PREPARED_NO_SOLVER_STARTED', 'sources': trees,
            'cases': cases, 'identities': inspected['identities'], 'profiles': selected_profiles,
            'profile_file': str(profile_file), 'profile_file_sha256': sha(profile_file),
            'budgets': args.budgets, 'seeds': args.seeds, 'hash_seed': 0, 'python': args.python,
            'wait_seconds': args.wait_seconds, 'warm_start': False, 'injected_plan': False,
            'baseline_revision_label': args.baseline_revision,
            'stage_policy': 'One independent variable per profile. joint_impl holds joint/reservation=True and toggles scalable. reservation holds joint/scalable=True and toggles reservation. Controllers hold reservation=True and joint off: rotation holds loop=True; loop holds rotation=True; gain holds rotation/loop=True. Only the named controller varies False/True. Controller/reservation/activation comparisons require the same source.',
            'controller_screen_budget_seconds': 120,
            'stage_budget_design': 'Reservation preserves original 64% primary search and 83% selection allowances; optional refinement may use previously unspent compute up to95%, leaving5% for final verification. Never shorten the main search to fund the new phase.',
            'stage_execution_evidence': 'Flags alone never prove execution. Report JOINT_REOPTIMIZATION stage details including skipped/reason, neighborhoods, scalable_methods, scalable_order_count and generated_orders; candidate scalable profiles need a nonzero method/order observation to support an executed claim.',
            'require_actual_scalable_candidate_execution': True,
            'require_actual_controller_candidate_execution': True,
            'screen_gate': {'scope': 'EACH_BUDGET_AND_SEED_SEPARATELY', 'complete_cases_required': 2,
                            'all_cases_nonincreasing_fee': True, 'minimum_strict_improvements': 1,
                            'zero_failures_or_timeouts': True, 'independent_constraints_required': True,
                            'maximum_compute_overrun_seconds': .05,
                            'maximum_wait_overrun_seconds': .05,
                            'keep_all_failures_and_regressions': True,
                            'development_only_not_proof_of_unseen_general_improvement': True},
            'execution_policy': 'STRICTLY_SERIAL_ALTERNATING_PAIRED_FRESH_PROCESSES',
            'frozen_road_policy': 'same case and measured graph for both sides; worker rejects graph mutation',
            'solver_runs_started_during_prepare': 0, 'network_calls': 0, 'source_business_database_written': False}
    save_new(output / 'screen-lock.json', lock)
    return {'status': lock['status'], 'lock_file': str(output / 'screen-lock.json'),
            'lock_sha256': sha(output / 'screen-lock.json'), 'profiles': [p['name'] for p in selected_profiles],
            'expected_runs': len(cases) * len(args.budgets) * len(args.seeds) * len(selected_profiles) * 2}


def run(lock_path):
    lock_path = Path(lock_path).resolve()
    lock = load(lock_path)
    if lock.get('scope') != 'DEVELOPMENT_SINGLE_FLAG_SCREEN_NOT_UNSEEN_ACCEPTANCE' or lock.get('warm_start'):
        raise ValueError('Expected a cold development screen lock')
    for manifest in lock['sources'].values():
        actual = source_listing(Path(manifest['snapshot']))
        if actual != manifest['files'] or digest(actual) != manifest['sha256']:
            raise ValueError('Frozen source snapshot changed')
    if sha(lock['profile_file']) != lock['profile_file_sha256'] or load(lock['profile_file']) != lock['profiles']:
        raise ValueError('Frozen feature profiles changed')
    candidate = lock['sources']['candidate']['snapshot']
    inspected = snapshot_inspection(Path(candidate), [c['case_file'] for c in lock['cases']], lock['profiles'], lock['python'])
    if inspected['identities'] != lock['identities']:
        raise ValueError('Development cases or measured graph changed since prelock')
    command = [lock['python'], str(Path(candidate) / 'scripts/evaluate_delivery_stages.py'),
               '--baseline-source', lock['sources']['baseline']['snapshot'], '--candidate-source', candidate,
               '--profiles', lock['profile_file'], '--hash-seed', '0', '--python', lock['python'],
               '--output', str(lock_path.parent / 'results'), '--budgets', *map(str, lock['budgets']),
               '--seeds', *map(str, lock['seeds'])]
    for case in lock['cases']:
        command.extend(['--case', case['case_file']])
    if lock['wait_seconds'] is not None:
        command.extend(['--wait-seconds', str(lock['wait_seconds'])])
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('SUKA_', 'TUNNEL_'))}
    env.update(PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run(command, cwd=candidate, env=env, stdin=subprocess.DEVNULL, check=False).returncode


def joint_execution_evidence(row):
    algorithm = row.get('result_algorithm', {})
    stages = [s for s in algorithm.get('stages', []) if s.get('stage') == 'JOINT_REOPTIMIZATION']
    evidence = {'stage_present': bool(stages), 'scalable_methods_count': 0,
                'scalable_method_generated_orders': 0, 'scalable_order_count': 0,
                'generated_orders': 0, 'candidate_count': 0, 'skip_reasons': [],
                'actual_experiments': algorithm.get('experiments')}
    def visit(value):
        if isinstance(value, dict):
            methods = value.get('scalable_methods')
            if isinstance(methods, list):
                evidence['scalable_methods_count'] += len(methods)
                evidence['scalable_method_generated_orders'] += sum(int(m.get('generated_orders') or 0) for m in methods)
            for key in ('scalable_order_count', 'generated_orders', 'candidate_count'):
                if isinstance(value.get(key), (int, float)):
                    evidence[key] += int(value[key])
            if value.get('skipped'):
                evidence['skip_reasons'].append(value.get('reason') or value.get('stop_reason') or 'SKIPPED')
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    for stage in stages:
        visit(stage.get('details', {}))
    evidence['scalable_stage_executed'] = bool(evidence['stage_present']
        and evidence['scalable_methods_count'] > 0
        and (evidence['scalable_order_count'] > 0 or evidence['scalable_method_generated_orders'] > 0))
    return evidence


def controller_execution_evidence(row):
    algorithm = row.get('result_algorithm', {})
    stages = [s for s in algorithm.get('stages', []) if s.get('stage') == 'CANDIDATE_EXPANSION']
    cycles, groups, skips = [], set(), []
    direct_gain = Decimal(0)
    for stage in stages:
        details = stage.get('details', {})
        if details.get('skipped'):
            skips.append(details.get('reason') or details.get('stop_reason') or 'SKIPPED')
        direct_gain += Decimal(str(details.get('fee_reduction') or '0'))
        for cycle in details.get('cycles', []):
            identity = cycle.get('group_identity')
            try:
                group = ast.literal_eval(identity)[0] if identity else None
                groups.add(repr(group))
            except (ValueError, SyntaxError, TypeError, IndexError):
                groups.add(str(identity))
            fields = ('cycle', 'group_identity', 'group_task_count', 'new_orders', 'changed_pool_columns',
                      'selection_runs', 'previous_pool_cost_optimal', 'before_fee', 'after_fee',
                      'fee_reduction', 'compute_seconds')
            cycles.append({key: cycle.get(key) for key in fields})
    return {'stage_present': bool(stages), 'controller_stage_executed': bool(cycles),
            'cycles': cycles, 'cycle_count': len(cycles), 'group_count': len(groups), 'groups': sorted(groups),
            'selection_runs': sum(int(c.get('selection_runs') or 0) for c in cycles),
            'new_orders': sum(int(c.get('new_orders') or 0) for c in cycles),
            'changed_pool_columns': sum(int(c.get('changed_pool_columns') or 0) for c in cycles),
            'direct_fee_reduction': str(direct_gain), 'skip_reasons': skips,
            'actual_experiments': algorithm.get('experiments')}


def summarize(lock_path, report_path):
    lock, report = load(lock_path), load(report_path)
    problems = []
    manifest = report.get('manifest', {})
    if not report.get('finished') or manifest.get('warm_start'):
        problems.append('REPORT_INCOMPLETE_OR_WARM')
    if manifest.get('config', {}).get('profiles') != lock['profiles']:
        problems.append('FEATURE_PROFILE_CHANGED')
    for side in ('baseline', 'candidate'):
        if manifest.get('source_manifests', {}).get(side, {}).get('sha256') != lock['sources'][side]['sha256']:
            problems.append('SOURCE_CHANGED_' + side)
    identities = lock['identities']
    expected_paths = set(identities)
    failures = list(report.get('failures_and_timeouts', []))
    overruns = [row for row in report.get('runs', []) if (row.get('compute_overrun_seconds') is None
                or row.get('wait_overrun_seconds') is None
                or row['compute_overrun_seconds'] > lock['screen_gate']['maximum_compute_overrun_seconds']
                or row['wait_overrun_seconds'] > lock['screen_gate']['maximum_wait_overrun_seconds'])]
    strata, seen = [], set()
    for profile in lock['profiles']:
        for budget in lock['budgets']:
            for seed in lock['seeds']:
                pairs = [row for row in report.get('comparisons', [])
                         if (row['profile_name'], row['compute_seconds'], row['seed']) == (profile['name'], budget, seed)]
                paths = [row['identity']['case_file'] for row in pairs]
                valid = len(paths) == 2 and set(paths) == expected_paths
                for row in pairs:
                    key = profile['name'], budget, seed, row['identity']['case_file']
                    if key in seen or row['identity'] != identities.get(row['identity']['case_file']):
                        valid = False
                    seen.add(key)
                    if (row['hash_seed'] != 0 or row['outcome'] == 'INCOMPLETE_COMPARISON'
                            or row['baseline_status'] != 'VALID' or row['candidate_status'] != 'VALID'):
                        valid = False
                complete = valid
                improving = sum(row['outcome'] == 'IMPROVED' for row in pairs)
                nonincreasing = all(row['outcome'] in {'IMPROVED', 'EQUAL'} for row in pairs)
                scoped_failures = [r for r in failures + overruns if
                                   (r.get('profile_name'), r.get('compute_seconds'), r.get('seed')) == (profile['name'], budget, seed)]
                stage_evidence = []
                required = bool(lock.get('require_actual_scalable_candidate_execution')
                                and profile['candidate_options']['experiments'].get('scalable_joint_search'))
                for row in report.get('runs', []):
                    if (row.get('profile_name'), row.get('compute_seconds'), row.get('seed')) != (profile['name'], budget, seed):
                        continue
                    stage_evidence.append({'side': row.get('side'), 'case_file': row.get('identity', {}).get('case_file'),
                                           **joint_execution_evidence(row)})
                candidate_evidence = [e for e in stage_evidence if e['side'] == 'candidate']
                executed = (len(candidate_evidence) == 2 and all(e['scalable_stage_executed'] for e in candidate_evidence))
                controller_required = bool(lock.get('require_actual_controller_candidate_execution')
                                           and profile['name'].startswith('schedule_'))
                controller_evidence = [{'side': row.get('side'), 'case_file': row.get('identity', {}).get('case_file'),
                                        **controller_execution_evidence(row)} for row in report.get('runs', [])
                                       if (row.get('profile_name'), row.get('compute_seconds'), row.get('seed'))
                                       == (profile['name'], budget, seed)]
                controller_candidates = [e for e in controller_evidence if e['side'] == 'candidate']
                controller_executed = (len(controller_candidates) == 2
                                       and all(e['controller_stage_executed'] for e in controller_candidates))
                strata.append({'profile': profile['name'], 'budget': budget, 'seed': seed,
                               'complete_cases': len(pairs), 'all_cases_present_and_valid': complete,
                               'strictly_improved_cases': improving, 'all_cases_nonincreasing': nonincreasing,
                               'failed_or_overrun_runs': len(scoped_failures), 'pairs': pairs,
                               'actual_joint_stage_evidence': stage_evidence,
                               'actual_scalable_candidate_execution_required': required,
                               'actual_scalable_candidate_execution_observed': executed,
                               'actual_controller_stage_evidence': controller_evidence,
                               'actual_controller_execution_required': controller_required,
                               'actual_controller_execution_observed': controller_executed,
                               'screen_gate_passed': bool(complete and nonincreasing and improving >= 1
                                    and not scoped_failures and (not required or executed)
                                    and (not controller_required or controller_executed))})
    wanted_pairs = len(lock['profiles']) * len(lock['budgets']) * len(lock['seeds']) * 2
    if len(report.get('comparisons', [])) != wanted_pairs or len(report.get('runs', [])) != wanted_pairs * 2:
        problems.append('UNREGISTERED_OR_MISSING_RUNS')
    return {'schema_version': 1, 'scope': 'DEVELOPMENT_SCREEN_ONLY_NOT_UNSEEN_ACCEPTANCE',
            'lock_sha256': sha(lock_path), 'report_sha256': sha(report_path), 'problems': problems,
            'strata': strata, 'all_fee_regressions': report.get('regressions', []),
            'all_failures_and_timeouts': failures, 'all_compute_or_wait_overruns': overruns,
            'all_profiles_passed_screen': not problems and bool(strata) and all(r['screen_gate_passed'] for r in strata),
            'global_optimality_proven': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--baseline-source', required=True)
    prep.add_argument('--candidate-source', required=True)
    prep.add_argument('--baseline-revision', default='4bcdbcb1dbde8c12bef1fc698d7a2e7599a03405')
    prep.add_argument('--case', required=True, action='append')
    prep.add_argument('--phase', required=True, action='append', choices=[*PHASE_FLAGS, 'schedule'])
    prep.add_argument('--retention-policy', choices=['first', 'batch_dedup', 'diversity'], default='diversity')
    prep.add_argument('--budgets', type=float, nargs='+')
    prep.add_argument('--seeds', type=int, nargs='+', default=[0])
    prep.add_argument('--wait-seconds', type=float)
    prep.add_argument('--python', default=sys.executable)
    prep.add_argument('--output', required=True)
    launch = sub.add_parser('run')
    launch.add_argument('--lock', required=True)
    audit = sub.add_parser('check')
    audit.add_argument('--lock', required=True)
    audit.add_argument('--report', required=True)
    audit.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        if args.budgets is None:
            args.budgets = [120] if any(p == 'schedule' or p.startswith('schedule_') for p in args.phase) else [60]
        if any(not math.isfinite(b) or b <= 0 for b in args.budgets):
            parser.error('Budgets must be finite and positive')
        if args.wait_seconds is not None and (not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0):
            parser.error('Wait cutoff must be finite and positive')
        if len(set(args.seeds)) != len(args.seeds) or len(set(args.budgets)) != len(args.budgets):
            parser.error('No duplicated seeds or budgets')
        print(json.dumps(prepare(args), ensure_ascii=False), flush=True)
        return 0
    if args.command == 'run':
        return run(args.lock)
    result = summarize(args.lock, args.report)
    save_new(args.output, result)
    print(json.dumps({'output': args.output, 'all_profiles_passed_screen': result['all_profiles_passed_screen']}, ensure_ascii=False))
    return 0 if result['all_profiles_passed_screen'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
