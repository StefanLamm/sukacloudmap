"""Summarize every paired delivery experiment without leaking task identities.

Only JSON report files are read; no solver, network or business database is
opened. Source hashes, profile, graph/hints, budgets and timing mode define
separate strata. Cold-start differences and direct within-stage reductions
remain separate. Partial reports and all unsuccessful runs stay visible.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import statistics
import time


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def cash(value):
    result = number(value)
    return str(result) if result is not None else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def _select(mapping, fields):
    return {key: mapping[key] for key in fields if key in mapping and isinstance(mapping[key], (str, int, float, bool, type(None)))}


NATIVE_FIELDS = ('solver', 'strategy', 'first_strategy', 'stop_reason', 'unknown_edges', 'measured_edges',
                 'invalid_metrics', 'solution_count', 'native_slices', 'route_specifications',
                 'elapsed_seconds', 'candidate_retention_policy', 'virtual_vehicles', 'physical_vehicles')


def native_rows(value):
    if isinstance(value, list):
        return [row for item in value for row in native_rows(item)]
    if not isinstance(value, dict):
        return []
    if value.get('solver') == 'OR_TOOLS_ROUTING':
        return [_select(value, NATIVE_FIELDS)]
    # Never traverse arbitrary route/evidence objects or copy free-form text.
    return [row for key in ('routing_diagnostics', 'methods', 'details')
            for row in native_rows(value.get(key))]


def stage_rows(stages):
    rows = []
    def append(value, path, *, depth=0):
        if not isinstance(value, dict):
            return
        detail = value.get('details') if isinstance(value.get('details'), dict) else {}
        before = value.get('before_fee', detail.get('before_fee'))
        after = value.get('after_fee', detail.get('after_fee', value.get('best_fee')))
        reduction = cash(value.get('fee_reduction', detail.get('fee_reduction')))
        basis = 'EXPLICIT_RECORDED_FEE_REDUCTION' if reduction is not None else 'NOT_RECORDED'
        if reduction is None and number(before) is not None and number(after) is not None:
            reduction = str(number(before) - number(after))
            basis = 'SAME_STAGE_RECORDED_BEFORE_MINUS_AFTER'
        row = {'stage_path': path, 'depth': depth, 'before_fee': cash(before), 'after_fee': cash(after),
               'direct_fee_reduction': reduction, 'reduction_basis': basis,
               'additivity': 'NESTED_ROWS_OVERLAP_PARENT_DO_NOT_SUM_ACROSS_DEPTHS'}
        for field in ('elapsed_seconds', 'compute_seconds', 'budget_seconds', 'candidate_count', 'stop_reason', 'skipped'):
            source = value if field in value else detail
            if field in source and isinstance(source[field], (str, int, float, bool, type(None))):
                row[field] = source[field]
        rows.append(row)
        methods = value.get('methods') if isinstance(value.get('methods'), list) else detail.get('methods', [])
        if isinstance(methods, list):
            for index, method in enumerate(methods):
                if isinstance(method, dict):
                    append(method, path + '/' + str(index + 1) + ':' + str(method.get('method', 'METHOD')), depth=depth + 1)
    for index, stage in enumerate(stages or []):
        if isinstance(stage, dict):
            suffix = ':round' + str(stage['round_index']) if stage.get('round_index') is not None else ''
            append(stage, str(index + 1) + ':' + str(stage.get('stage', 'STAGE')) + suffix)
    return rows


def _run_id(report_label, raw, index):
    slug = Path(raw.get('run_folder') or str(index)).name
    # The generated slug contains only sequence/profile/case-index/budget/seed.
    # Do not emit a user-provided path or arbitrary folder basename.
    ordinal = slug.split('-', 1)[0]
    return report_label + ':run-' + (ordinal if ordinal.isdigit() else str(index)) + ':' + str(raw.get('side', 'UNKNOWN'))


def anonymize_run(raw, manifest, report_label, index):
    algorithm = raw.get('result_algorithm') if isinstance(raw.get('result_algorithm'), dict) else {}
    diagnostics = algorithm.get('diagnostics') if isinstance(algorithm.get('diagnostics'), dict) else {}
    identity = raw.get('identity') if isinstance(raw.get('identity'), dict) else {}
    side = raw.get('side', 'UNKNOWN')
    profile = next((value for value in manifest.get('config', {}).get('profiles', [])
                    if value.get('name') == raw.get('profile_name')), {})
    profile_hash = hashlib.sha256(canonical(profile).encode()).hexdigest()
    manifest_source = manifest.get('source_manifests', {}).get(side, {}).get('sha256')
    source = raw.get('source_hash', manifest_source)
    status = raw.get('status', 'MISSING_STATUS')
    if source is not None and manifest_source is not None and source != manifest_source:
        status = 'SOURCE_IDENTITY_CONFLICT'
    if raw.get('deadline_exhausted') or status in {'RETURNED_TIMEOUT', 'TIMEOUT'}:
        status = 'TIMEOUT'
    audit = raw.get('audit') if isinstance(raw.get('audit'), dict) else {}
    case_file = raw.get('case_file', identity.get('case_file', 'UNKNOWN/UNKNOWN'))
    row = {'run_id': _run_id(report_label, raw, index), 'report_label': report_label,
           'side': side, 'profile_name': raw.get('profile_name'), 'profile_definition_hash': profile_hash,
           'case_label': Path(case_file).parent.name,
           'source_hash': source, 'evaluator_source_hash': raw.get('independent_evaluator_source_hash', manifest.get('evaluator_source_hash')),
           'input_hash': identity.get('input_hash'), 'graph_hash': identity.get('graph_hash'),
           'graph_file_sha256': identity.get('graph_file_sha256'), 'historical_hints_hash': identity.get('historical_hints_hash'),
           'baseline_hash': identity.get('baseline_hash'), 'compute_budget_seconds': raw.get('compute_seconds'),
           'wait_limit_seconds': raw.get('wait_seconds'), 'seed': raw.get('seed'), 'hash_seed': raw.get('hash_seed'),
           'timing_scope': raw.get('timing_scope', 'UNRECORDED'), 'warm_start': raw.get('warm_start', False),
           'status': status, 'original_status': raw.get('status'), 'audit_valid': audit.get('valid'),
           'audited_fee': cash(audit.get('total_fee')), 'unassigned_tasks': audit.get('unassigned_tasks'),
           'solver_stop_reason': raw.get('solver_stop_reason', algorithm.get('stop_reason')),
           'exception_type': raw.get('exception_type'),
           'first_complete_elapsed_seconds': diagnostics.get('first_complete_elapsed_seconds'),
           'first_complete_compute_seconds': diagnostics.get('first_complete_compute_seconds'),
           'first_complete_fee': cash((diagnostics.get('first_complete') or {}).get('total_fee')),
           'stage_reductions': stage_rows(algorithm.get('stages', [])),
           'compute_overrun_seconds': raw.get('compute_overrun_seconds'),
           'wait_overrun_seconds': raw.get('wait_overrun_seconds'),
           'solve_seconds': raw.get('solve_seconds'), 'reported_compute_seconds': raw.get('reported_compute_seconds'),
           'worker_seconds': raw.get('worker_seconds'), 'supervisor_seconds': raw.get('supervisor_seconds'),
           'peak_rss_bytes': raw.get('lifetime_peak_rss_bytes')}
    row['candidate_counts'] = _select(algorithm, ('candidate_count', 'distinct_route_count', 'candidate_pool_limit',
        'candidate_observations', 'dropped_candidate_observations', 'duplicate_candidate_observations', 'complete_round_allocations'))
    retention = algorithm.get('candidate_retention')
    row['candidate_retention'] = _select(retention, ('policy', 'limit', 'generated_observations', 'duplicate_observations',
        'retained_candidates', 'capacity_rejected_observations', 'buffer_rejected_observations', 'flush_seconds',
        'flush_count', 'protected_complete_allocations')) if isinstance(retention, dict) else {}
    row['diagnostic_counts'] = _select(diagnostics.get('counts', {}), ('evaluated_candidates', 'evaluation_cache_hits',
        'rejected_candidates', 'road_rejected_candidates', 'missing_metric_lookups', 'candidate_observations',
        'duplicate_observations', 'dropped_observations', 'retained_observations', 'protected_complete_allocations'))
    curve = diagnostics.get('fee_curve', [])
    row['fee_curve'] = [_select(point, ('elapsed_seconds', 'compute_seconds', 'total_fee', 'stage', 'source'))
                        for point in curve if isinstance(point, dict)]
    validation = diagnostics.get('final_road_fee_change')
    row['final_road_fee_change'] = _select(validation, ('before_fee', 'after_fee', 'fee_increase', 'elapsed_seconds',
        'compute_seconds', 'mode', 'fresh')) if isinstance(validation, dict) else None
    row['native_by_round'] = []
    for round_row in algorithm.get('rounds', []):
        if not isinstance(round_row, dict):
            continue
        native = native_rows(round_row)
        unknown = [entry['unknown_edges'] for entry in native if isinstance(entry.get('unknown_edges'), int)]
        row['native_by_round'].append({'round_index': round_row.get('round_index'), 'strategy': round_row.get('strategy'),
            'round_seed': round_row.get('seed'), 'models': native,
            'unknown_edges_per_model': unknown, 'unknown_sum_across_models_not_distinct_edges': sum(unknown) if unknown else None})
    return row


def _conditions(row):
    return {key: row.get(key) for key in ('profile_name', 'profile_definition_hash', 'case_label', 'input_hash', 'graph_hash', 'graph_file_sha256',
        'historical_hints_hash', 'baseline_hash', 'compute_budget_seconds', 'wait_limit_seconds', 'seed',
        'hash_seed', 'warm_start', 'timing_scope')}


def compare(old, new):
    same = _conditions(old) == _conditions(new)
    row = {**_conditions(new), 'report_label': new['report_label'], 'baseline_run': old['run_id'],
           'candidate_run': new['run_id'], 'baseline_source_hash': old['source_hash'],
           'candidate_source_hash': new['source_hash'], 'baseline_status': old['status'],
           'candidate_status': new['status'], 'baseline_fee': old['audited_fee'], 'candidate_fee': new['audited_fee'],
           'same_conditions': same, 'cold_start_saving': None, 'outcome': 'INCOMPLETE_COMPARISON',
           'attribution': 'COLD_START_TOTAL_DIFFERENCE_NOT_DIRECT_STAGE_CAUSAL_EFFECT'}
    if not same:
        row['outcome'] = 'CONDITION_MISMATCH'
    elif old['status'] == new['status'] == 'VALID' and old['audit_valid'] and new['audit_valid']:
        saving = number(old['audited_fee']) - number(new['audited_fee'])
        row.update(cold_start_saving=str(saving), outcome='IMPROVED' if saving > 0 else 'REGRESSED' if saving < 0 else 'EQUAL')
    return row


def _group(rows, fields):
    groups = defaultdict(list)
    for row in rows:
        key = canonical({field: row.get(field) for field in fields})
        groups[key].append(row)
    return groups


def build_summary(report_paths, dispositions=None):
    dispositions = dispositions or {}
    reports, runs, pairs, pending = [], [], [], []
    seen_paths = set()
    for report_number, supplied_path in enumerate(report_paths):
        path = Path(supplied_path).resolve()
        if path in seen_paths:
            continue
        seen_paths.add(path)
        content = path.read_bytes()
        report = json.loads(content)
        label = path.parent.name
        if any(row['report_label'] == label for row in reports):
            label += '-' + str(report_number + 1)
        manifest = report.get('manifest', {})
        raw_runs = report.get('runs', [])
        converted = [anonymize_run(raw, manifest, label, index) for index, raw in enumerate(raw_runs)]
        by_folder = {raw.get('run_folder'): row for raw, row in zip(raw_runs, converted) if raw.get('run_folder')}
        used = set()
        for comparison in report.get('comparisons', []):
            old = by_folder.get(comparison.get('baseline_run'))
            new = by_folder.get(comparison.get('candidate_run'))
            if old is not None and new is not None:
                pairs.append(compare(old, new))
                used.update((old['run_id'], new['run_id']))
        # A report written between its second completed worker and pair-summary
        # write still contains a valid pair. Match only within this report's
        # generated run ordinal, and retain one-sided progress as pending.
        buckets = defaultdict(list)
        for raw, row in zip(raw_runs, converted):
            if row['run_id'] not in used:
                ordinal = Path(raw.get('run_folder') or '').name.split('-', 1)[0]
                key = (ordinal if ordinal.isdigit() else str(len(buckets)), canonical(_conditions(row)))
                buckets[key].append(row)
        for values in buckets.values():
            old = [row for row in values if row['side'] == 'baseline']
            new = [row for row in values if row['side'] == 'candidate']
            if len(old) == len(new) == 1:
                pairs.append(compare(old[0], new[0]))
            else:
                pending.extend({'run_id': row['run_id'], 'report_label': label, 'case_label': row['case_label'],
                                'profile_name': row['profile_name'], 'side': row['side'],
                                'status': 'PAIR_NOT_FINISHED_IN_REPORT_SNAPSHOT'} for row in values)
        runs.extend(converted)
        reports.append({'report_label': label, 'file_sha256': hashlib.sha256(content).hexdigest(),
                        'finished': report.get('finished', False), 'snapshot_run_count': len(converted),
                        'disposition': dispositions.get(label, 'UNDECIDED'),
                        'candidate_source_hash': manifest.get('source_manifests', {}).get('candidate', {}).get('sha256'),
                        'baseline_source_hash': manifest.get('source_manifests', {}).get('baseline', {}).get('sha256')})
    run_fields = ('source_hash', 'side', 'profile_name', 'profile_definition_hash', 'case_label', 'input_hash', 'graph_hash', 'graph_file_sha256',
                  'historical_hints_hash', 'compute_budget_seconds', 'wait_limit_seconds', 'hash_seed', 'timing_scope', 'warm_start')
    run_strata = []
    for key, values in _group(runs, run_fields).items():
        valid_fees = [number(row['audited_fee']) for row in values if row['status'] == 'VALID' and row['audit_valid']]
        valid_fees = [fee for fee in valid_fees if fee is not None]
        run_strata.append({**json.loads(key), 'runs': len(values), 'status_counts': dict(Counter(row['status'] for row in values)),
                           'all_valid_fee_sum': str(sum(valid_fees, Decimal(0))),
                           'median_valid_fee': str(statistics.median(valid_fees)) if valid_fees else None,
                           'seeds': [row['seed'] for row in values], 'run_ids': [row['run_id'] for row in values]})
    pair_fields = ('baseline_source_hash', 'candidate_source_hash', 'profile_name', 'profile_definition_hash', 'graph_hash', 'graph_file_sha256',
                   'historical_hints_hash', 'compute_budget_seconds', 'wait_limit_seconds', 'hash_seed', 'timing_scope', 'warm_start')
    pair_strata = []
    for key, values in _group(pairs, pair_fields).items():
        savings = [number(row['cold_start_saving']) for row in values if row['cold_start_saving'] is not None]
        pair_strata.append({**json.loads(key), 'pairs': len(values), 'outcome_counts': dict(Counter(row['outcome'] for row in values)),
                            'all_valid_pair_saving_sum': str(sum(savings, Decimal(0))),
                            'median_pair_saving': str(statistics.median(savings)) if savings else None,
                            'scope': 'ALL_PAIRED_EXPERIMENTS_NOT_ACTUAL_TRANSPORT_SPEND'})
    return {'schema_version': 1, 'created_at': int(time.time()), 'reports': reports, 'runs': runs,
            'run_strata': run_strata, 'comparisons': pairs, 'pair_strata': pair_strata,
            'all_regressions': [row for row in pairs if row['outcome'] == 'REGRESSED'],
            'all_failures_and_timeouts': [row for row in runs if row['status'] != 'VALID'],
            'all_compute_overruns': [row for row in runs if (number(row.get('compute_overrun_seconds')) or 0) > Decimal('.05')],
            'condition_mismatches': [row for row in pairs if row['outcome'] == 'CONDITION_MISMATCH'],
            'pending_pairs': pending,
            'notes': ['Fees are independent final audits, including valid fallback fees for timed-out runs; timeouts are excluded from valid comparisons.',
                      'Cold-start differences are not attributed to a particular feature. Direct stage reductions use that run\'s recorded before/after fees.',
                      'Source hashes, graphs, graph-file hashes, hints, profiles, compute budgets and wait limits remain separate.',
                      'Nested method reductions overlap their parent stage and must not be added to parent reductions.',
                      'All seeds and adverse runs are retained; no best-of-seed result is selected.',
                      'Partial reports are point-in-time snapshots, not completed experiments.',
                      'No task ID, vehicle ID, coordinates, route order, private path or source exception message is included.']}


def markdown(summary):
    lines = ['费用目标为完整覆盖且满足全部硬约束后的整单实际费用。新旧固定输入、道路、历史提示、seed、哈希环境、计算预算和总等待截止，串行交替运行。以下保留全部样本；冷启动差额与单阶段直接降费分别报告，不按最低 seed 挑选。', '',
             '| 报告 | 完成 | 已记录运行 | 处置 | 基线源码 | 候选源码 |', '| --- | --- | ---: | --- | --- | --- |']
    def cell(value):
        return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')
    def short(value):
        return value[:12] if isinstance(value, str) else '—'
    for row in summary['reports']:
        lines.append('| ' + ' | '.join(map(cell, (row['report_label'], row['finished'], row['snapshot_run_count'],
                     row['disposition'], short(row['baseline_source_hash']), short(row['candidate_source_hash'])))) + ' |')
    lines += ['', '| 报告 | 日标签 | 单项开关 | 预算秒 | seed | 基线费用 | 候选费用 | 旧减新 | 结果 |',
              '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |']
    for row in summary['comparisons']:
        lines.append('| ' + ' | '.join(map(cell, (row['report_label'], row['case_label'], row['profile_name'],
                     row['compute_budget_seconds'], row['seed'], row['baseline_fee'], row['candidate_fee'],
                     row['cold_start_saving'], row['outcome']))) + ' |')
    lines += ['', '候选阶段的直接降费（父阶段与嵌套方法重复覆盖，不能叠加求和）：', '',
              '| 报告/日/seed | 开关 | 阶段 | 计算秒 | 阶段直接降费 | 依据 |', '| --- | --- | --- | ---: | ---: | --- |']
    for row in summary['runs']:
        if row['side'] != 'candidate':
            continue
        for stage in row['stage_reductions']:
            if stage['depth'] != 0:
                continue
            lines.append('| ' + ' | '.join(map(cell, (row['report_label'] + '/' + row['case_label'] + '/s' + str(row['seed']),
                         row['profile_name'], stage['stage_path'], stage.get('compute_seconds'),
                         stage['direct_fee_reduction'], stage['reduction_basis']))) + ' |')
    lines += ['', '全部失败及超时样本：', '', '| 报告 | 日 | 开关 | 侧 | seed | 状态 | 异常类型 |',
              '| --- | --- | --- | --- | ---: | --- | --- |']
    for row in summary['all_failures_and_timeouts']:
        lines.append('| ' + ' | '.join(map(cell, (row['report_label'], row['case_label'], row['profile_name'],
                     row['side'], row['seed'], row['status'], row['exception_type']))) + ' |')
    if not summary['all_failures_and_timeouts']:
        lines.append('| — | — | — | — | — | 无 | — |')
    lines += ['', f"全部升费样本 {len(summary['all_regressions'])} 对，全部超时/失败 {len(summary['all_failures_and_timeouts'])} 次，尚未成对 {len(summary['pending_pairs'])} 次。首次完整可行时间、每轮原生缺边、候选计数、费用轨迹及最终道路复核价变见同名匿名 JSON。",
              '', '上述重复试验费用不能解释为实际运输支出。未记录的基线诊断显示为空，不能按零处理。不同源码版本的结果分开保留；处置为拒绝的试验仍完整列出。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='append', required=True)
    parser.add_argument('--output', required=True, help='Anonymized JSON summary')
    parser.add_argument('--markdown', help='Optional docs-ready Markdown table')
    parser.add_argument('--disposition', action='append', default=[], help='REPORT_FOLDER=REJECTED/VALIDATION_ONLY/UNDECIDED')
    args = parser.parse_args()
    disposition = {}
    for value in args.disposition:
        if '=' not in value:
            parser.error('--disposition requires REPORT_FOLDER=LABEL')
        key, label = value.split('=', 1)
        disposition[key] = label
    summary = build_summary(args.report, disposition)
    write(args.output, summary)
    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(markdown(summary))
        path.chmod(0o600)
    print(json.dumps({'reports': len(summary['reports']), 'runs': len(summary['runs']),
                      'pairs': len(summary['comparisons']), 'regressions': len(summary['all_regressions']),
                      'failures_and_timeouts': len(summary['all_failures_and_timeouts']),
                      'pending_pairs': len(summary['pending_pairs'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
