"""Separate real-day holdout validation, after the formal study has finished.

Uses the explicitly supplied corrected template and a separate frozen graph.
It never changes the formal study rankings, source files, business DBs or roads.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0, str(ROOT))
from scripts.run_algorithm_study import (EXPERIMENT_ENV, atomic_json, json_digest, profiles,
                                         read_json, source_gate, validate_sources)

DATES = ('2026-08-02', '2026-09-13', '2026-10-18', '2026-10-25')


def source_plan(template_path, database):
    template = read_json(template_path)
    names = {}
    for row in template['stores']:names.setdefault(row['name'], []).append(row['id'])
    days = []
    with sqlite3.connect(f'{Path(database).resolve().as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for day in DATES:
            rows = [dict(row) for row in connection.execute(
                'SELECT d.id,d.store_id,d.import_id,d.pieces,d.source_rows,s.name FROM daily_demands d '
                'JOIN stores s ON s.id=d.store_id WHERE d.delivery_date=? ORDER BY s.name,d.id', (day,))]
            if not rows or any(len(names.get(row['name'], [])) != 1 for row in rows):
                raise ValueError('Every holdout source row must map unambiguously')
            days.append({'day': day, 'source_demand_rows': len(rows), 'pieces': sum(row['pieces'] for row in rows),
                         'source_requirement_hash': json_digest(rows), 'source_rows_discarded': 0})
    return {'scope': 'REAL_HOLDOUT_DAYS_WITH_EXPLICIT_CURRENT_CORRECTED_TEMPLATE',
            'template_input_hash': json_digest(template), 'template_input': str(Path(template_path).resolve()),
            'history_database_read_only': str(Path(database).resolve()), 'days': days,
            'planned_profiles': profiles(), 'planned_algorithm_seconds': len(DATES) * len(profiles()) * 5,
            'formal_study_rankings_unchanged': True, 'source_business_database_written': False, 'external_calls': 0}


def validate(args):
    from scripts.benchmark_algorithms import FrozenEvidenceRoad, compatible_pairs, freeze_historical_case, read_only
    from scripts.exact_reference_oracle import exact_reference_oracle
    from app.routing_engine import normalize
    output = Path(args.output).resolve();output.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = source_plan(args.template_input, args.history_database)
    if args.prepare_only:
        atomic_json(output / 'holdout-plan.json', plan)
        return {'status': 'PREPARED_NOT_RUN', 'plan': plan}
    study = Path(args.formal_study)
    manager = read_json(study / 'manager-status.json', {})
    summary = read_json(study / 'study_summary.json', {})
    if manager.get('status') != 'FINISHED' or not summary.get('best_by_case', {}).get('main225'):
        raise ValueError('Formal study must finish successfully before holdout CPU experiments')
    gate = source_gate(args.source_ready)
    if gate is None:raise ValueError('Source-ready gate is not valid')
    expected = {**gate['source_hashes'], 'scripts/run_real_holdouts.py': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    validate_sources(expected)
    old_plan = read_json(output / 'holdout-plan.json')
    if old_plan is not None and old_plan != plan:raise ValueError('Prepared holdout source quantities changed')
    atomic_json(output / 'holdout-plan.json', plan)
    source_manifest_hash = json_digest(expected)
    cutoff = args.deadline_epoch - 2700
    graph = output / 'corrected-frozen-roads.db'
    if not graph.exists():
        graph.touch(mode=0o600)
        with read_only(args.graph_database) as source, sqlite3.connect(graph) as destination:source.backup(destination)
    road = FrozenEvidenceRoad(graph);graph_hash = road.graph_hash;road.close()
    template = read_json(args.template_input)
    results = []
    for day in DATES:
        case_folder = output / day
        case = freeze_historical_case(template, args.history_database, day, case_folder, road_database=graph)
        if case['scenario']['source_requirement_hash'] != next(row['source_requirement_hash'] for row in plan['days'] if row['day'] == day):
            raise ValueError('Historical quantities changed while freezing')
        case['baseline_hash'] = json_digest(read_json(case_folder / case['baseline_file']))
        atomic_json(case_folder / 'case.json', case)
        body = read_json(case_folder / case['body_file']);road = FrozenEvidenceRoad(graph)
        if any(road.known_segment(a, b) is None for a, b in compatible_pairs(normalize(body))):
            road.close();raise ValueError('Corrected holdout graph lacks required measured roads')
        try:exact_result, proof = exact_reference_oracle(body, road, budget_seconds=120)
        finally:road.close()
        atomic_json(case_folder / 'exact-result.json', exact_result);atomic_json(case_folder / 'exact-proof.json', proof)
        if not proof['frozen_model_optimality_proven']:raise ValueError('Holdout exact proof did not complete')
        day_rows = []
        for profile in profiles():
            if time.time() + 35 >= cutoff:raise TimeoutError('Preserving the deployment reserve')
            validate_sources(expected)
            run = case_folder / 'profiles' / profile['name'];run.mkdir(mode=0o700, parents=True, exist_ok=True)
            spec = {'source_hashes': expected, 'source_manifest_hash': source_manifest_hash, 'profile': profile,
                    'case_file': str(case_folder / 'case.json'), 'case_name': day, 'stage': 'REAL_HELD_OUT_VALIDATION',
                    'budget_seconds': 5, 'seed': 0, 'run_output': str(run), 'warm_start': False, 'hard_seconds': 30,
                    'expected_case_identity': {key: case.get(key) for key in ('input_hash', 'graph_hash', 'baseline_hash')}}
            atomic_json(run / 'run-spec.json', spec)
            log_path = run / 'process.log';log_path.touch(mode=0o600, exist_ok=True)
            with log_path.open('a') as log:
                completed = subprocess.run([sys.executable, str(ROOT / 'scripts/run_algorithm_study.py'), '--run-one', str(run / 'run-spec.json')],
                                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=30, check=False, env={**os.environ, **EXPERIMENT_ENV})
            row = read_json(run / 'completed-run.json')
            if completed.returncode != 0 or row is None:raise RuntimeError('A holdout worker failed; inspect its private process log')
            validate_sources(expected)
            row['oracle_fee'] = proof['audit']['total_fee']
            row['matches_exact_frozen_model_fee'] = row.get('audit', {}).get('total_fee') == row['oracle_fee']
            if row['status'] == 'VALID':
                row['oracle_gap_fee'] = str(Decimal(row['audit']['total_fee']) - Decimal(row['oracle_fee']))
                if Decimal(row['oracle_gap_fee']) < 0:raise ValueError('A heuristic beat the independent exact oracle')
            day_rows.append(row)
            atomic_json(case_folder / 'progress.json', {'day': day, 'exact_proof': proof, 'runs': day_rows})
        results.append({'day': day, 'task_count': len(body['tasks']), 'pieces': body['totals']['total_pieces'],
                        'input_hash': case['input_hash'], 'graph_hash': graph_hash, 'exact_proof': proof, 'runs': day_rows})
        atomic_json(output / 'holdout-progress.json', {'completed_days': results, 'external_calls': 0})
    report = {'status': 'COMPLETED', 'scope': plan['scope'], 'plan': plan, 'source_hashes': expected,
              'graph_hash': graph_hash, 'formal_graph_hash': summary['manifest']['cases']['main225']['graph_hash'],
              'corrected_graph_is_explicitly_separate': True, 'days': results, 'runs': sum(len(d['runs']) for d in results),
              'all_deliveries_complete': all(r['status'] == 'VALID' and not r['audit']['unassigned_tasks'] for day in results for r in day['runs']),
              'used_for_hyperparameter_selection': False, 'formal_study_rankings_unchanged': True,
              'source_business_database_written': False, 'external_calls': 0, 'finished_at': int(time.time())}
    atomic_json(output / 'holdout-summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template-input', required=True)
    parser.add_argument('--graph-database', required=True)
    parser.add_argument('--history-database', default=str(ROOT / 'data/retest-20261112/full-replay.db'))
    parser.add_argument('--formal-study', default=str(ROOT / 'data/algorithm-lab-20261113/formal-study'))
    parser.add_argument('--source-ready', default=str(ROOT / 'data/algorithm-lab-20261113/source-ready.json'))
    parser.add_argument('--output', default=str(ROOT / 'data/algorithm-lab-20261113/real-holdouts'))
    parser.add_argument('--deadline-epoch', type=int, default=1794542875)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    result = validate(args)
    print(json.dumps({'status': result['status'], 'runs': result.get('runs'),
                      'all_deliveries_complete': result.get('all_deliveries_complete')}, ensure_ascii=False))


if __name__ == '__main__':main()
