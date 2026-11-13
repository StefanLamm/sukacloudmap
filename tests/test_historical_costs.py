from copy import deepcopy

import pytest

from scripts.reconcile_historical_costs import reconcile_expenses


def record(row, cost, *, day='2026-10-03', plate='TEST1', source='oct.xlsx', warehouse='HF', mode='拼车'):
    return {'source_file': source, 'source_sheet': '登记表', 'source_row': row,
            'delivery_date': day, 'plate': plate, 'pieces': 10,
            'registered_cost': cost, 'historical_mode': mode, 'warehouse_code': warehouse,
            'income_unit': '999999'}


def test_same_day_and_car_cost_is_counted_once_despite_different_routes_and_modes():
    rows = [record(3, '875'), record(4, '875', mode='包车'), record(5, '360')]
    rows[0]['line'] = 'first route'; rows[1]['line'] = 'second route'
    before = deepcopy(rows)
    report = reconcile_expenses(rows)
    assert report['summary']['total_cost'] == '875.00'
    assert report['summary']['repeated_cost_rows_not_added'] == 2
    assert report['groups'][0]['different_recorded_amounts']
    assert report['groups'][0]['pieces'] == 30
    assert rows == before  # No source mutation or quote writing, including income.


def test_dates_and_plates_each_start_a_separate_bill():
    rows = [record(3, '100'), record(4, '100'), record(5, '200', plate='TEST2'),
            record(6, '300', day='2026-10-04')]
    report = reconcile_expenses(rows)
    assert report['summary']['group_count'] == 3
    assert report['summary']['total_cost'] == '600.00'


def test_original_first_row_is_selected_even_if_input_is_reordered():
    a, b = record(3, '360'), record(4, '875')
    assert reconcile_expenses([b, a])['summary']['total_cost'] == '360.00'
    a['source_file'] = 'preferred.xlsx'; b['source_file'] = 'other.xlsx'
    report = reconcile_expenses([b, a], source_order=['preferred.xlsx', 'other.xlsx'])
    assert report['summary']['total_cost'] == '360.00'


@pytest.mark.parametrize('raw,expected', [('195,5', '195.50'), ('1317,5', '1317.50'), ('0', '0.00'), ('-10.5', '-10.50')])
def test_confirmed_decimal_comma_zero_and_signed_adjustment(raw, expected):
    report = reconcile_expenses([record(3, raw)], decimal_comma=True)
    assert report['summary']['total_cost'] == expected


@pytest.mark.parametrize('raw', [None, '', 'not money', 'NaN', 'Infinity', '1,234', '1,000.50'])
def test_missing_or_invalid_first_row_does_not_fall_forward_or_report_incomplete_total(raw):
    report = reconcile_expenses([record(3, raw), record(4, '900'), record(5, '100', plate='TEST2')], decimal_comma=True)
    assert report['groups'][0]['counted_cost'] is None
    assert report['summary']['total_cost'] is None
    assert report['summary']['counted_cost_subtotal'] == '100.00'
    assert report['summary']['unresolved_group_count'] == 1
    assert report['monthly'][0]['total_cost'] is None


def test_later_missing_or_invalid_amount_does_not_override_valid_first_row():
    report = reconcile_expenses([record(3, '0'), record(4, None), record(5, 'bad')])
    assert report['summary']['total_cost'] == '0.00'
    assert report['groups'][0]['observations'][2]['amount_status'] == 'INVALID'


def test_decimal_comma_is_not_enabled_without_explicit_policy():
    assert reconcile_expenses([record(3, '195,5')])['summary']['total_cost'] is None


def test_missing_car_identity_is_not_deduplicated_or_counted_as_one_car():
    report = reconcile_expenses([record(3, '100', plate=''), record(4, '100', plate='')])
    assert report['summary']['group_count'] == 2
    assert report['summary']['unresolved_group_count'] == 2
    assert report['summary']['total_cost'] is None


def test_partial_trial_population_never_receives_full_car_cost():
    report = reconcile_expenses([record(3, '100'), record(4, '100', warehouse=None),
                                 record(5, '200', plate='TEST2')])
    assert report['summary']['total_cost'] == '300.00'
    assert report['partial_trial_groups'] == 1
    assert report['matched_trial_whole_groups']['total_cost'] == '200.00'
    assert report['trial_full_population_total_cost'] is None


def test_empty_source_is_unavailable_not_a_zero_cost_history():
    assert reconcile_expenses([])['summary']['total_cost'] is None
