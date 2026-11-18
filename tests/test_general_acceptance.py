"""Audit logic fixtures only: no routing solver, real inputs or network."""
from copy import deepcopy
import itertools
from pathlib import Path
import subprocess

import pytest

from scripts.validate_general_acceptance import make_lock, source_switch_expectations, validate
from scripts.evaluate_delivery_stages import road_execution_policy


def sample():
    days, regimes, budgets, seeds = ['d1', 'd2', 'd3', 'd4'], ['with-hints', 'without-hints'], [300, 2000], [0, 1]
    profile = {'name': 'generic', 'baseline_options': {}, 'candidate_options': {'joint': True}}
    protocol = {'days': days, 'hint_regimes': regimes, 'budgets_seconds': budgets, 'seeds': seeds,
                'sides': ['baseline', 'candidate'], 'hash_seed': 0,
                'wait_policy': {'seconds_by_budget': {'300': 915, '2000': 3815}},
                'acceptance_thresholds': {'geometric_mean_day_median_ratio_at_most': 0.998,
                    'minimum_strictly_improved_day_medians': 2, 'maximum_day_median_ratio': 1.005,
                    'maximum_single_seed_ratio': 1.02, 'compute_overrun_tolerance_seconds': .05,
                    'wait_overrun_tolerance_seconds': .05}}
    cases = [{'day': d, 'regime': h, 'identity': {'case_file': d + h, 'input_hash': d,
              'graph_hash': 'same', 'historical_hints_hash': h}} for d, h in itertools.product(days, regimes)]
    manifests = {'baseline': {'sha256': 'old'}, 'candidate': {'sha256': 'new'}}
    lock = {'protocol': protocol, 'profile': profile, 'cases': cases,
            'production_search_policy': True, 'road_execution_policy': road_execution_policy(True),
            'source_manifests': manifests, 'expected_runs': 64,
            'expected_experiments': {side: {str(b): {d: ({} if side == 'baseline' else {'joint': True})
                                      for d in days} for b in budgets} for side in manifests}}
    rows = []
    ordinal = 0
    for case, budget, seed in itertools.product(cases, budgets, seeds):
        for side in (['baseline', 'candidate'] if ordinal % 2 == 0 else ['candidate', 'baseline']):
            row = {'identity': deepcopy(case['identity']), 'profile_name': 'generic', 'compute_seconds': budget,
                   'production_search_policy': True, 'road_execution_policy': road_execution_policy(True),
                   'road_production_planning': True, 'road_timer': 'time.monotonic',
                   'worker_launched_monotonic': 1000 + len(rows) * 10000,
                   'road_wall_deadline_monotonic': 1000 + len(rows) * 10000 + {300: 915, 2000: 3815}[budget],
                   'seed': seed, 'side': side, 'status': 'VALID', 'audit': {'valid': True,
                   'total_fee': '100.00' if side == 'baseline' else '99.80'},
                   'source_hash': 'old' if side == 'baseline' else 'new', 'source_unchanged': True,
                   'hash_seed': 0, 'wait_seconds': {300: 915, 2000: 3815}[budget],
                   'warm_start': False, 'timing_scope': 'NORMAL_COLD_START', 'external_calls': 0,
                   'source_business_database_written': False, 'compute_overrun_seconds': 0,
                   'wait_overrun_seconds': 0, 'entrypoint': 'app.route_deep:optimize_deep',
                   'options': {} if side == 'baseline' else {'joint': True},
                   'result_algorithm': {'experiments': {} if side == 'baseline' else {'joint': True}},
                   'runtime': {'environment': {name: '1' for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                        'MKL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS')}}}
            rows.append(row)
        ordinal += 1
    report = {'finished': True, 'manifest': {'source_manifests': manifests, 'warm_start': False,
              'production_search_policy': True, 'road_execution_policy': road_execution_policy(True),
              'config': {'profiles': [profile]},
              'execution_policy': 'STRICTLY_SERIAL_ALTERNATING_PAIRED_FRESH_PROCESSES'}, 'runs': rows}
    return lock, report


def test_every_locked_stratum_at_preregistered_exact_threshold_passes():
    lock, report = sample()
    result = validate(lock, [report])
    assert result['status'] == 'ACCEPTED'
    assert all(s['strictly_improved_days'] == 4 for s in result['strata'])
    assert result['production_search_policy_attested'] is True


@pytest.mark.parametrize('policy', [None, False, 1])
def test_missing_research_or_integer_policy_cannot_attest_production_acceptance(policy):
    lock, report = sample()
    lock['production_search_policy'] = policy
    result = validate(lock, [report])
    assert result['adoption_allowed'] is False
    assert any(p['code'] == 'PRODUCTION_SEARCH_POLICY_NOT_LOCKED'
               for p in result['conditions_or_run_failures'])


def test_research_manifest_or_mixed_pair_policy_cannot_pass_on_low_fees():
    lock, report = sample()
    report['manifest']['road_execution_policy'] = road_execution_policy(False)
    report['runs'][0].update(production_search_policy=False,
                             road_execution_policy=road_execution_policy(False))
    result = validate(lock, [report])
    assert result['adoption_allowed'] is False
    codes = {p['code'] for p in result['conditions_or_run_failures']}
    assert {'MANIFEST_PRODUCTION_SEARCH_POLICY_MISMATCH', 'NONALTERNATING_OR_UNPAIRED_RUN_ORDER'} <= codes
    assert any('PRODUCTION_SEARCH_POLICY_MISMATCH' in p.get('errors', [])
               for p in result['conditions_or_run_failures'])


@pytest.mark.parametrize('changes,error', [
    ({'road_production_planning': False}, 'PRODUCTION_SEARCH_ADAPTER_NOT_ATTESTED'),
    ({'road_production_planning': 1}, 'PRODUCTION_SEARCH_ADAPTER_NOT_ATTESTED'),
    ({'road_timer': 'time.time'}, 'PRODUCTION_SEARCH_ADAPTER_NOT_ATTESTED'),
    ({'road_wall_deadline_monotonic': None}, 'PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH'),
    ({'road_wall_deadline_monotonic': float('inf')}, 'PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH'),
    ({'road_wall_deadline_monotonic': float('nan')}, 'PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH'),
    ({'road_wall_deadline_monotonic': True}, 'PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH'),
    ({'road_wall_deadline_monotonic': 1916}, 'PRODUCTION_SEARCH_ABSOLUTE_DEADLINE_MISMATCH'),
])
def test_reported_policy_without_actual_adapter_deadline_and_timer_is_rejected(changes, error):
    lock, report = sample()
    report['runs'][0].update(changes)
    result = validate(lock, [report])
    assert result['adoption_allowed'] is False
    assert any(error in p.get('errors', []) for p in result['conditions_or_run_failures'])


def test_protocol_requiring_production_policy_refuses_a_research_lock(tmp_path):
    import json
    protocol, index, profiles = [tmp_path / name for name in ('protocol.json', 'index.json', 'profiles.json')]
    protocol.write_text(json.dumps({'required_production_search_policy': True}))
    index.write_text('{}')
    profiles.write_text('[]')
    with pytest.raises(ValueError, match='requires the native production search policy'):
        make_lock(protocol, index, profiles, tmp_path, tmp_path)


def test_missing_seed_and_duplicate_seed_never_pass_even_if_report_finished():
    lock, report = sample()
    report['runs'][-1] = deepcopy(report['runs'][-3])
    result = validate(lock, [report])
    assert result['status'] == 'INCOMPLETE'
    codes = {p['code'] for p in result['conditions_or_run_failures']}
    assert {'DUPLICATE_RUN_NO_BEST_SELECTION', 'MISSING_REGISTERED_RUNS'} <= codes


def test_two_seed_median_is_mean_and_single_bad_seed_is_retained():
    lock, report = sample()
    for row in report['runs']:
        if row['side'] == 'candidate' and row['identity']['case_file'] == 'd1with-hints' and row['compute_seconds'] == 300:
            row['audit']['total_fee'] = '103.00' if row['seed'] == 0 else '90.00'
    result = validate(lock, [report])
    day = result['strata'][0]['days'][0]
    assert day['candidate_median_fee'] == '96.50'
    assert result['strata'][0]['worst_seed_ratio'] == '1.03'
    assert result['status'] == 'NOT_ACCEPTED'
    assert len(result['all_fee_regressions']) == 1


def test_timeout_audited_fallback_cannot_be_counted_as_fee_success():
    lock, report = sample()
    report['runs'][0].update(status='TIMEOUT', deadline_exhausted=True)
    result = validate(lock, [report])
    assert result['status'] == 'NOT_ACCEPTED'
    assert len(result['all_failures_and_timeouts']) == 1
    assert not result['strata'][0]['complete']


def test_changed_road_or_profile_and_nonpaired_execution_rejected():
    lock, report = sample()
    report['runs'][0]['identity']['graph_hash'] = 'changed'
    report['runs'][2]['options'] = {}
    result = validate(lock, [report])
    assert not result['adoption_allowed']
    assert any(p['code'] == 'UNREGISTERED_OR_CHANGED_CASE' for p in result['conditions_or_run_failures'])
    assert any(p['code'] == 'NONALTERNATING_OR_UNPAIRED_RUN_ORDER' for p in result['conditions_or_run_failures'])


def test_unchanged_kwargs_cannot_hide_changed_actual_default_flags():
    lock, report = sample()
    # All kwargs/manifest conditions remain unchanged; the actual result differs.
    report['runs'][1]['result_algorithm']['experiments'] = {'joint': False}
    result = validate(lock, [report])
    assert not result['adoption_allowed']
    assert any('ACTUAL_EXPERIMENT_FLAGS_MISMATCH' in p.get('errors', [])
               for p in result['conditions_or_run_failures'])


@pytest.mark.parametrize('actual', [None, {'joint': 1}])
def test_missing_actual_flags_and_integer_boolean_substitution_fail(actual):
    lock, report = sample()
    report['runs'][1]['result_algorithm']['experiments'] = actual
    assert not validate(lock, [report])['adoption_allowed']


def source_fixture(tmp_path, expression=None):
    application = tmp_path / 'app'
    application.mkdir()
    (application / 'route_experiments.py').write_text('''from dataclasses import dataclass
@dataclass(frozen=True)
class DeepExperiments:
    core: bool = False
    generation_selection_loop: bool = False
    @classmethod
    def parse(cls,value):
        return cls(**(value or {}))
''')
    expression = expression or "DeepExperiments(core=True, generation_selection_loop=(budget.seconds >= 600 and len(data['tasks']) > 8)) if experiments is None else DeepExperiments.parse(experiments)"
    (application / 'route_deep.py').write_text("raise AssertionError('Optimizer module must never execute')\ndef optimize_deep():\n    switches = " + expression + '\n')
    return tmp_path


def test_actual_default_factory_is_budget_and_task_aware_without_loading_optimizer(tmp_path):
    root = source_fixture(tmp_path)
    requests = [{'budget': 300, 'task_count': 10, 'options': {}, 'input_hash': 'large'},
                {'budget': 2000, 'task_count': 10, 'options': {}, 'input_hash': 'large'},
                {'budget': 2000, 'task_count': 7, 'options': {}, 'input_hash': 'small'},
                {'budget': 2000, 'task_count': 10, 'options': {'experiments': {}}, 'input_hash': 'explicit'}]
    result = source_switch_expectations(root, requests)
    flags = [row['flags'] for row in result['expectations']]
    assert flags == [{'core': True, 'generation_selection_loop': False},
                     {'core': True, 'generation_selection_loop': True},
                     {'core': True, 'generation_selection_loop': False},
                     {'core': False, 'generation_selection_loop': False}]
    assert result['scope'].endswith('NO_OPTIMIZER_OR_SOLVER_EXECUTION')


def test_default_factory_refuses_arbitrary_executable_calls(tmp_path):
    root = source_fixture(tmp_path, "open('unexpected-side-effect', 'w')")
    with pytest.raises(subprocess.CalledProcessError):
        source_switch_expectations(root, [{'budget': 300, 'task_count': 10, 'options': {}, 'input_hash': 'I'}])
    assert not (Path(root) / 'unexpected-side-effect').exists()
