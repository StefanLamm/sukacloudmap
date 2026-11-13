"""Read-only historical expense control, separate from prospective carrier quotes.

User rule (2026-11-12): one expense per delivery date and plate, always take
the first source row even when later rows disagree. Decimal commas are decimal
points in these registration sheets. Missing first values never fall forward.
"""
import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.logistics_source_data import DEFAULT_SOURCE, ROOT, extract


def expense_amount(raw, *, decimal_comma=False):
    if raw is None or not str(raw).strip():
        return None, 'MISSING'
    value = str(raw).strip()
    if decimal_comma and ',' in value:
        # Only the explicitly confirmed decimal format; not thousands grouping.
        if value.count(',') != 1 or '.' in value:
            return None, 'INVALID'
        value = value.replace(',', '.')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount != amount.quantize(Decimal('0.01')):
            return None, 'INVALID'
    except InvalidOperation:
        return None, 'INVALID'
    return format(amount, '.2f'), 'VALID'


def totals(groups):
    ready = [g for g in groups if g['status'] == 'READY']
    subtotal = format(sum((Decimal(g['counted_cost']) for g in ready), Decimal(0)), '.2f')
    return {
        'group_count': len(groups), 'counted_group_count': len(ready),
        'unresolved_group_count': len(groups) - len(ready),
        'counted_cost_subtotal': subtotal,
        'total_cost': subtotal if groups and len(ready) == len(groups) else None,
    }


def reconcile_expenses(records, *, source_order=(), decimal_comma=False):
    priorities = {name: i for i, name in enumerate(source_order)}

    def source_key(row):
        return (priorities.get(row['source_file'], len(priorities)),
                row['source_file'], row['source_sheet'], row['source_row'])

    grouped = defaultdict(list)
    for row in records:
        plate = str(row.get('plate') or '').strip().upper()
        # Missing identities stay separate and unresolved, never one shared car.
        key = (row['delivery_date'], plate, () if plate else source_key(row))
        grouped[key].append(row)
    groups = []
    for (day, plate, _), rows in sorted(grouped.items()):
        rows = sorted(rows, key=source_key)
        observations = []
        for row in rows:
            amount, state = expense_amount(row.get('registered_cost'), decimal_comma=decimal_comma)
            observations.append({
                'source_file': row['source_file'], 'source_sheet': row['source_sheet'],
                'source_row': row['source_row'], 'raw_amount': row.get('registered_cost'),
                'amount': amount, 'amount_status': state,
            })
        selected = observations[0]
        status = ('MISSING_PLATE' if not plate else
                  'READY' if selected['amount_status'] == 'VALID' else
                  selected['amount_status'] + '_FIRST_AMOUNT')
        amounts = sorted({o['amount'] for o in observations if o['amount'] is not None}, key=Decimal)
        eligible = [r.get('warehouse_code') in ('HF', 'HA') for r in rows]
        scope = ('MATCHED_TRIAL_SCOPE' if all(eligible) else
                 'PARTIAL_TRIAL_SCOPE' if any(eligible) else 'OUTSIDE_TRIAL_SCOPE')
        groups.append({
            'delivery_date': day, 'plate': plate, 'status': status,
            'source_row_count': len(rows), 'pieces': sum(r['pieces'] for r in rows),
            'historical_modes': sorted({r['historical_mode'] for r in rows}),
            'warehouse_codes': sorted({r.get('warehouse_code') or 'UNASSIGNED' for r in rows}),
            'trial_scope': scope, 'selected_source': selected,
            'counted_cost': selected['amount'] if status == 'READY' else None,
            'different_recorded_amounts': len(amounts) > 1,
            'distinct_recorded_amounts': amounts,
            'observations': observations,
        })
    months = defaultdict(list)
    for group in groups:
        months[group['delivery_date'][:7]].append(group)
    partial_trial_groups = sum(g['trial_scope'] == 'PARTIAL_TRIAL_SCOPE' for g in groups)
    matched = [g for g in groups if g['trial_scope'] == 'MATCHED_TRIAL_SCOPE']
    return {
        'policy': {'group_by': ['delivery_date', 'plate'], 'amount_rule': 'FIRST_SOURCE_ROW',
                   'different_amounts': 'FIRST_SOURCE_ROW_AS_CONFIRMED',
                   'decimal_comma': decimal_comma, 'missing_first_amount': 'UNRESOLVED_NO_FALLBACK',
                   'source_order': list(source_order), 'within_source_order': 'sheet name, original row ascending',
                   'income_used_as_cost': False, 'writes_common_unit_price': False},
        'summary': {**totals(groups), 'source_row_count': len(records),
                    'repeated_cost_rows_not_added': len(records) - len(groups),
                    'different_amount_groups': sum(g['different_recorded_amounts'] for g in groups),
                    'decimal_comma_rows': sum(',' in str(r.get('registered_cost') or '') for r in records)},
        # Do not allocate an entire car expense to just its in-scope stores.
        'matched_trial_whole_groups': totals(matched),
        'partial_trial_groups': partial_trial_groups,
        'trial_full_population_total_cost': None if partial_trial_groups else totals(matched)['total_cost'],
        'monthly': [{'month': month, **totals(items)} for month, items in sorted(months.items())],
        'groups': groups,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=ROOT/'docs/hardening/historical-cost-review.json')
    args = parser.parse_args()
    records, source_report = extract(args.source)
    sources = source_report['sources']
    if args.output.resolve() in {Path(s['path']).resolve() for s in sources}:
        parser.error('output must not overwrite a source workbook')
    source_order = [str(Path(s['path']).relative_to(args.source.resolve())) for s in sources]
    report = reconcile_expenses(records, source_order=source_order, decimal_comma=True)
    report.update({'policy_confirmed_on': '2026-11-12', 'currency': 'CNY',
                   'sources': sources, 'usage': 'HISTORICAL_CONTROL_ONLY_NOT_FORWARD_PRICE',
                   'comparison_to_simulated_plan': 'NOT_COMPARABLE_NO_SAVINGS_CLAIM'})
    # Recheck inputs after reading. The script writes only its requested JSON.
    assert all(hashlib.sha256(Path(s['path']).read_bytes()).hexdigest() == s['sha256'] for s in sources)
    report['source_workbooks_unchanged'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'summary': report['summary'],
                      'matched_trial_whole_groups': report['matched_trial_whole_groups'],
                      'partial_trial_groups': report['partial_trial_groups'],
                      'monthly': report['monthly']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
