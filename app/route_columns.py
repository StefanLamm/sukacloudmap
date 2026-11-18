"""Current-input, integer-price selection across independently generated routes.

Only identities and order are taken from proposals.  Existing complete plans
are independently checked, every retained column is priced again for its real
physical car, and a model solution is materialized through the road adapter.
An optimal CP-SAT status proves a cost minimum for this finite retained pool;
it never proves that ungenerated routes or delivery combinations are optimal.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal
from itertools import chain
from math import isfinite
import threading
from time import monotonic

from app.route_metrics import evaluate_candidate
from app.routing_engine import (RoadError, StopSearch, assemble, check_result,
                                common_fee, evaluate_route, normalize)

MAX_ORDERS = 20000
MAX_COLUMNS = 100000
PROOF_RESERVE_FRACTION = .15


def _cents(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
        raise ValueError('费用必须为非负的精确分金额')
    integer = int(amount * 100)
    if integer >= 2 ** 60:
        raise ValueError('费用超出整数求解范围')
    return integer


def _legal(result):
    return (not result['unassigned'] and result['fees_complete'] and
            all(item['code'] == 'DRIVING_REFERENCE' for item in result['check']['issues']))


def _rank(result):
    return (len(result['unassigned']), _cents(result['total_fee']),
            sum(row['meters'] for row in result['routes']), len(result['routes']))


def _compact(row):
    return {key: deepcopy(row[key]) for key in
            ('vehicle_id', 'task_ids', 'fee', 'meters', 'seconds', 'pieces')}


def _pattern(row):
    return (tuple(row['task_ids']), _cents(row['fee']), row['meters'],
            row['seconds'], row['pieces'])


def _order(data, value):
    if not isinstance(value, (tuple, list)) or not value:
        return None
    if any(not isinstance(item, str) or item not in data['tasks'] for item in value):
        return None
    ids = tuple(value)
    return ids if len(set(ids)) == len(ids) else None


def _pricing_fleet(data, ids, vehicle_ids, control=None):
    """Precheck the exact eligibility used by the metric-only evaluator.

    A metric proposal depends on the car's warehouse and capacity, not its
    plate/identity. This grouping applies to compact proposals only: every
    selected physical car still gets its own bound evidence below.
    """
    selected = []
    for index, task_id in enumerate(ids):
        if index and index % 64 == 0 and control:
            control()
        selected.append(data['tasks'][task_id])
    warehouse = selected[0]['warehouse_id']
    signature = selected[0]['signature']
    if signature is None or any(t['warehouse_id'] != warehouse or t['signature'] != signature
                                for t in selected):
        return []
    pieces = sum(t['pieces'] for t in selected)
    groups = {}
    for index, vehicle_id in enumerate(vehicle_ids):
        if index and index % 64 == 0 and control:
            control()
        vehicle = data['vehicles'][vehicle_id]
        if (vehicle['available'] and vehicle['warehouse_id'] == warehouse
                and vehicle['max_pieces'] >= pieces):
            groups.setdefault((warehouse, vehicle['max_pieces']), []).append(vehicle_id)
    return list(groups.values())


def _check_complete(data, result):
    """Validate caller evidence and actual current prices, never its check flag."""
    try:
        checked = check_result(data, result)
        return (not result.get('unassigned') and result.get('fees_complete') is True and
                all(item['code'] == 'DRIVING_REFERENCE' for item in checked['issues']))
    except (KeyError, ValueError, TypeError, ArithmeticError, RoadError):
        return False


def _current_metrics_match(data, result, road):
    """A zero-I/O gate for retaining an already materialized same-job incumbent.

    No old result may be retained just because its recorded check says valid.
    Its entire evidence must bind this input and its measured metrics/arrival
    schedule must still agree with the adapter's current read-only matrix.
    """
    if not _check_complete(data, result):
        return False
    if not result['routes']:
        return True
    if not callable(getattr(road, 'known_segment', None)):
        return False
    try:
        for route in result['routes']:
            current = evaluate_candidate(data, route['vehicle_id'], route['task_ids'], road)
            if current is None or current['errors'] or any(
                    current[key] != route[key] for key in ('fee', 'meters', 'seconds', 'pieces')):
                return False
            origin = data['warehouses'][data['vehicles'][route['vehicle_id']]['warehouse_id']]
            for task_id, segment in zip(route['task_ids'], route['segments']):
                destination = data['tasks'][task_id]
                metric = road.known_segment(origin, destination)
                if metric is None or (metric.get('meters'), metric.get('seconds')) != (segment['meters'], segment['seconds']):
                    return False
                if 'received_at' in metric and metric['received_at'] != segment.get('received_at'):
                    return False
                origin = destination
        return True
    except (KeyError, ValueError, TypeError, ArithmeticError, RoadError):
        return False


def _vehicle_classes(data, columns, control=None):
    """Equivalent means the complete exact *available* choice sets agree."""
    groups = {}
    for vehicle_id, choices in columns.items():
        if control:
            control()
        signatures = tuple(sorted(_pattern(row) for row in choices.values()))
        key = data['vehicles'][vehicle_id]['warehouse_id'], signatures
        groups.setdefault(key, []).append(vehicle_id)
    result = []
    for number, ((warehouse, signatures), members) in enumerate(sorted(groups.items())):
        members.sort()
        by_order = {tuple(row['task_ids']): row for row in columns[members[0]].values()}
        result.append({'id': number, 'warehouse_id': warehouse, 'members': members,
                       'patterns': [by_order[item[0]] for item in signatures]})
    return result


def _solver_api():
    from ortools.sat.python import cp_model
    return cp_model


def _solve(data, classes, incumbent, seconds, cancelled, seed=0):
    """Solve exact cents once, with a joined cancellation watcher."""
    until = monotonic() + seconds
    def control():
        if cancelled():
            raise StopSearch('CANCELLED')
        if monotonic() >= until:
            raise StopSearch('TIME_BUDGET')
    control()
    cp_model = _solver_api()
    model = cp_model.CpModel()
    choices, common_vars, objective = {}, {}, []
    coverage = {key: [] for key in data['tasks']}
    hints = set()
    incumbent_complete = _legal(incumbent)
    if incumbent_complete:
        class_by_vehicle = {v: group for group in classes for v in group['members']}
        for route in incumbent['routes']:
            group = class_by_vehicle.get(route['vehicle_id'])
            if group is None:
                raise ValueError('已验证方案缺少物理车辆')
            # Per-car equal-coverage columns retain their cheapest valid order.
            # That order can dominate the incumbent's old order while preserving
            # its allocation and providing a feasible (no dearer) model hint.
            matches = [i for i, row in enumerate(group['patterns'])
                       if frozenset(row['task_ids']) == frozenset(route['task_ids'])]
            if len(matches) != 1:
                raise ValueError('已验证方案未包含在有限列池中')
            hints.add((group['id'], matches[0]))
    for group in classes:
        control()
        local = []
        for index, row in enumerate(group['patterns']):
            if index % 64 == 0:
                control()
            variable = model.new_bool_var('route_%s_%s' % (group['id'], index))
            choices[group['id'], index] = variable
            local.append(variable)
            for task_id in row['task_ids']:
                coverage[task_id].append(variable)
            objective.append(_cents(row['fee']) * variable)
            if incumbent_complete:
                model.add_hint(variable, int((group['id'], index) in hints))
        model.add(sum(local) <= len(group['members']))
    common_hint = {row['task_id'] for row in incumbent['common']}
    for task_id, task in data['tasks'].items():
        control()
        if task['common_unit_price'] is not None:
            variable = model.new_bool_var('common_' + task_id)
            common_vars[task_id] = variable
            coverage[task_id].append(variable)
            objective.append(_cents(common_fee(task['pieces'], task['common_unit_price'])) * variable)
            if incumbent_complete:
                model.add_hint(variable, int(task_id in common_hint))
        model.add(sum(coverage[task_id]) == 1)
    cost = sum(objective)
    if incumbent_complete:
        model.add(cost <= _cents(incumbent['total_fee']))
    model.minimize(cost)
    if model.validate():
        raise ValueError('有限列池整数模型无效')
    def native():
        control()
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.max_time_in_seconds = max(.001, min(60., until - monotonic()))
        solver.parameters.random_seed = seed
        stopped, interrupted = threading.Event(), []
        def watch():
            while not stopped.wait(.025):
                try:
                    if cancelled():
                        interrupted.append('CANCELLED')
                        solver.stop_search()
                        return
                    if monotonic() >= until:
                        solver.stop_search()
                        return
                except Exception:
                    interrupted.append('CANCEL_CHECK_FAILED')
                    solver.stop_search()
                    return
        watcher = threading.Thread(target=watch, name='suka-column-cancel', daemon=True)
        watcher.start()
        try:
            status = solver.solve(model)
        finally:
            stopped.set()
            watcher.join()
        return solver, status, interrupted

    def decode(solver):
        selected = []
        for group in classes:
            rows = [row for index, row in enumerate(group['patterns'])
                    if solver.boolean_value(choices[group['id'], index])]
            if len(rows) > len(group['members']):
                raise ValueError('整数解超过实际车辆数量')
            selected.extend({**row, 'vehicle_id': vehicle_id}
                            for vehicle_id, row in zip(group['members'], rows))
        common_ids = {task_id for task_id, variable in common_vars.items() if solver.boolean_value(variable)}
        assigned = [task_id for row in selected for task_id in row['task_ids']]
        if len(set(assigned)) != len(assigned) or set(assigned) & common_ids or set(assigned) | common_ids != set(data['tasks']):
            raise ValueError('整数解违反完整分配约束')
        cents = sum(_cents(row['fee']) for row in selected) + sum(
            _cents(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price'])) for i in common_ids)
        if incumbent_complete and cents > _cents(incumbent['total_fee']):
            raise ValueError('整数解费用高于已验证完整方案')
        return selected, common_ids, (cents, sum(row['meters'] for row in selected), len(selected))

    solver, status, interrupted = native()
    diagnostics = {'solver': 'OR_TOOLS_CP_SAT', 'solver_status': solver.status_name(status),
                   'solver_wall_seconds': solver.wall_time, 'native_worker_count': 1,
                   'finite_pool_cost_optimal': False, 'distance_tie_optimality_proven': False,
                   'vehicle_tie_optimality_proven': False,
                   'class_count': len(classes), 'solver_stages': [],
                   'pattern_variables': len(choices), 'common_variables': len(common_vars)}
    diagnostics['solver_stages'].append({'objective': 'total_fee_cents', 'status': solver.status_name(status),
                                         'wall_seconds': solver.wall_time})
    if interrupted:
        diagnostics['interrupted'] = interrupted[0]
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, diagnostics
    selected, common_ids, best_score = decode(solver)
    diagnostics.update(integer_objective_cents=best_score[0], solver_best_bound_cents=solver.best_objective_bound,
                       finite_pool_cost_optimal=status == cp_model.OPTIMAL and not interrupted)
    # Only a proven monetary minimum permits a secondary objective. We fix the
    # exact integer cents first; no distance/car trade can increase expenditure.
    if diagnostics['finite_pool_cost_optimal']:
        coefficients = [row['meters'] for group in classes for row in group['patterns']]
        safe_distance = (all(isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 60
                             for value in coefficients) and sum(coefficients) < 2 ** 62)
        if not safe_distance:
            diagnostics['tie_skipped_reason'] = 'DISTANCE_INTEGER_RANGE'
        else:
            meters = sum(row['meters'] * choices[group['id'], index]
                         for group in classes for index, row in enumerate(group['patterns']))
            vehicles = sum(choices.values())
            prior_expression, fixed_value = cost, best_score[0]
            for name, objective, score_index, proof_key in [
                    ('total_meters_at_fixed_fee', meters, 1, 'distance_tie_optimality_proven'),
                    ('vehicles_at_fixed_fee_and_meters', vehicles, 2, 'vehicle_tie_optimality_proven')]:
                if until - monotonic() <= .005:
                    diagnostics['tie_skipped_reason'] = 'TIME_BUDGET'
                    break
                try:
                    control()
                    model.add(prior_expression == fixed_value)
                    model.minimize(objective)
                    if model.validate():
                        diagnostics['tie_skipped_reason'] = 'INVALID_TIE_MODEL'
                        break
                    tie_solver, tie_status, tie_interrupted = native()
                    diagnostics['solver_wall_seconds'] += tie_solver.wall_time
                    diagnostics['solver_stages'].append({'objective': name, 'status': tie_solver.status_name(tie_status),
                                                         'wall_seconds': tie_solver.wall_time})
                    if tie_interrupted:
                        diagnostics['interrupted'] = tie_interrupted[0]
                    if tie_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                        break
                    proposed, proposed_common, score = decode(tie_solver)
                    if score[0] != best_score[0] or (score_index == 2 and score[1] != best_score[1]):
                        diagnostics['tie_skipped_reason'] = 'INVALID_TIE_RESULT'
                        break
                    if score < best_score:
                        selected, common_ids, best_score = proposed, proposed_common, score
                    if tie_status != cp_model.OPTIMAL or tie_interrupted or score[score_index] > best_score[score_index]:
                        break
                    diagnostics[proof_key] = True
                    prior_expression, fixed_value = objective, best_score[score_index]
                except StopSearch as exc:
                    diagnostics['tie_skipped_reason'] = exc.reason
                    if exc.reason == 'CANCELLED':
                        diagnostics['interrupted'] = 'CANCELLED'
                    break
                except (RuntimeError, ValueError, TypeError, ArithmeticError):
                    # The monetary solution is already decoded and remains safe.
                    diagnostics['tie_skipped_reason'] = 'TIE_SOLVER_ERROR'
                    break
    diagnostics.update(selected_meters=best_score[1], selected_vehicles=best_score[2])
    return (selected, common_ids), diagnostics


def select_portfolio(body, road, results, *, seconds, cancelled=lambda: False, progress=None,
                     candidate_recipes=(), complete_recipes=()):
    """Return the best independently verified current-input portfolio result.

    ``results`` contains full plans plus optional ``candidate_routes`` or
    ``algorithm._portfolio_recipes`` recipes. ``complete_recipes`` contains all
    completed rounds as compact ``routes`` / ``common_ids`` allocations, avoiding
    duplicated polylines. They are priced before the general recipe pool and a
    cheaper complete plan is materialized before CP-SAT can time out. Unknown proposal metrics are
    never queried here.  A normal cancellation/budget/solver failure returns a
    retained proved plan rather than raising or promoting a compact estimate.
    A mandatory-delivery task with no feasible route remains visibly unassigned
    if no complete incumbent exists; the caller must not save it as completed.
    """
    started = monotonic()
    try:
        budget = float(seconds)
    except (TypeError, ValueError, OverflowError):
        budget = 0.
    if not isfinite(budget) or budget < 0:
        budget = 0.
    deadline = started + budget
    # Reserve a bounded part of the caller's allowance for full evidence proof.
    work_deadline = deadline - min(2., budget * PROOF_RESERVE_FRACTION)
    column_deadline = started + budget * .6
    data = normalize(body)
    stage_trace = getattr(road, 'route_stage_trace', None)
    trace_scope = stage_trace.scope('PORTFOLIO') if stage_trace is not None else None
    traced_stages = set()
    def mark(stage):
        if stage_trace is not None:
            traced_stages.add(stage)
            stage_trace.mark(stage, status='ENTERED', scope=trace_scope)

    def trace_route(stage, row, *, status='SEEN', reason=None):
        stage_trace.route(stage, row, status=status, reason=reason, fee=row.get('fee'), scope=trace_scope)

    def materialize(proposals):
        if stage_trace is None:
            return [evaluate_route(data, row['vehicle_id'], list(row['task_ids']), road,
                                   lambda: control(final=True)) for row in proposals]
        mark('MATERIALIZED')
        routes = []
        for row in proposals:
            actual = evaluate_route(data, row['vehicle_id'], list(row['task_ids']), road,
                                    lambda: control(final=True))
            trace_route('MATERIALIZED', actual, status='VALID' if not actual['errors'] and actual['fee'] is not None else 'REJECTED',
                        reason='|'.join(sorted({error['code'] for error in actual['errors']})) or None)
            routes.append(actual)
        return routes
    priced = {i for i, task in data['tasks'].items() if task['common_unit_price'] is not None}
    common_cents = {i: _cents(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']))
                    for i in priced}
    best = assemble(data, [], priced, set(data['tasks']) - priced)
    diagnostics = {'scope': 'CURRENT_INPUT_FINITE_ROUTE_POOL_ONLY', 'solver': 'not_started',
                   'finite_pool_cost_optimal': False, 'optimality_proven': False,
                   'global_optimality_proven': False, 'objective_units': 'ACTUAL_INTEGER_CENTS',
                   'source_plan_count': 0, 'current_complete_plans': 0, 'metric_evaluations': 0,
                   'eligible_vehicle_options': 0, 'ineligible_vehicle_options': 0,
                   'shared_metric_proposals': 0,
                   'common_dominated_metric_groups': 0, 'common_dominated_vehicle_options': 0,
                   'complete_recipe_count': 0, 'complete_recipes_priced': 0,
                   'complete_recipes_missing_metrics': 0,
                   'invalid_source_plans': 0, 'invalid_recipes': 0, 'unknown_metric_recipes': 0,
                   'candidate_pool_truncated': False, 'fallback': False}
    columns = {vid: {} for vid, vehicle in data['vehicles'].items() if vehicle['available']}
    orders, sources = {}, []
    column_count = 0
    stop_reason = 'SEARCH_COMPLETED'

    def report(stage):
        if progress:
            progress({'phase': stage, 'column_count': sum(len(v) for v in columns.values()),
                      'best_fee': best['total_fee'], 'unassigned': len(best['unassigned'])})

    def control(*, final=False, pricing=False):
        if cancelled():
            raise StopSearch('CANCELLED')
        if monotonic() >= (deadline if final else column_deadline if pricing else work_deadline):
            raise StopSearch('TIME_BUDGET')

    def remember(row):
        nonlocal column_count
        if row['vehicle_id'] not in columns or row['errors'] or row['fee'] is None:
            return
        value = _compact(row)
        _cents(value['fee'])
        key = frozenset(value['task_ids'])
        previous = columns[value['vehicle_id']].get(key)
        if previous is None or (_cents(value['fee']), value['meters'], value['seconds'], tuple(value['task_ids'])) < (
                _cents(previous['fee']), previous['meters'], previous['seconds'], tuple(previous['task_ids'])):
            columns[value['vehicle_id']][key] = value
            if stage_trace is not None:
                mark('COLUMN')
                trace_route('COLUMN', value, status='RETAINED')
                if previous is not None:
                    trace_route('COLUMN', previous, status='REPLACED', reason='CHEAPER_SAME_CAR_COVERAGE_OR_TIEBREAK')
            if previous is None:
                column_count += 1
        elif stage_trace is not None:
            trace_route('COLUMN', value, status='DOMINATED', reason='SAME_CAR_COVERAGE')

    def retain(result):
        nonlocal best
        if _legal(result) and _rank(result) < _rank(best):
            best = result
        for row in result['routes']:
            remember(row)

    # Preserve materialized same-job incumbents even when cancellation is already
    # set. This gate has no network side effects and verifies current metrics.
    def rows(value):
        return value if isinstance(value, (list, tuple)) else []

    def source_fee(result):
        try:
            return _cents(result.get('total_fee'))
        except (ValueError, TypeError, ArithmeticError):
            return 2 ** 60

    for result in results:
        if not isinstance(result, dict):
            diagnostics['invalid_source_plans'] += 1
            continue
        sources.append(result)
    # Verify the cheapest matching source first; a later matched source with a
    # larger exact original total cannot improve it. Other sources remain recipes.
    matched_source = None
    for number, result in enumerate(sorted(sources, key=source_fee)):
        # One non-network safety proof may be needed even at budget zero. Do
        # not scan every other old plan after that safety allowance expires.
        if number and (cancelled() or monotonic() >= column_deadline):
            break
        if _current_metrics_match(data, result, road):
            copied = assemble(data, deepcopy(result['routes']),
                              [r['task_id'] for r in result['common']], [])
            if stage_trace is not None:
                mark('POOL_ORDER');mark('POOL_PRICE')
                for row in copied['routes']:
                    trace_route('POOL_ORDER', row, status='PROTECTED_CURRENT_SOURCE')
                    trace_route('POOL_PRICE', row, status='CURRENT_SOURCE_MATCHES_METRICS')
            retain(copied)
            diagnostics['current_complete_plans'] += 1
            matched_source = result
            break
        if budget and monotonic() >= column_deadline:
            break
    diagnostics['source_plan_count'] = len(sources)
    recipe_sources = [recipe for recipe in complete_recipes if isinstance(recipe, dict)]
    whole_sources = [(result, True) for result in sorted(sources, key=source_fee)]
    whole_sources.extend((recipe, False) for recipe in recipe_sources)
    diagnostics['complete_recipe_count'] = len(whole_sources)
    try:
        control()
        # Completed allocations are a protected first stage, never reduced to a
        # few columns and then forgotten if the ordinary pool exhausts its time.
        whole_candidates = []
        for source, materialized_source in whole_sources:
            try:
                control()
            except StopSearch as exc:
                if exc.reason == 'CANCELLED':
                    raise
                diagnostics['candidate_pool_truncated'] = True
                break
            if source is matched_source:
                continue
            raw_routes = rows(source.get('routes'))
            if any(not isinstance(row, dict) for row in raw_routes) or source.get('unassigned'):
                continue
            common_ids = (source.get('common_ids') if not materialized_source else
                          [row.get('task_id') for row in rows(source.get('common')) if isinstance(row, dict)])
            if not isinstance(common_ids, (list, tuple, set)) or any(not isinstance(i, str) or i not in priced for i in common_ids):
                continue
            seen, cars = Counter(common_ids), Counter()
            identities_valid = True
            for row in raw_routes:
                ids = _order(data, row.get('task_ids'))
                if ids is None or not isinstance(row.get('vehicle_id'), str) or row['vehicle_id'] not in columns:
                    identities_valid = False
                    break
                seen.update(ids)
                cars[row['vehicle_id']] += 1
            if (not identities_valid or set(seen) != set(data['tasks']) or any(n != 1 for n in seen.values())
                    or any(n != 1 for n in cars.values())):
                continue
            compact_routes, complete = [], True
            for row in raw_routes:
                try:
                    control()
                    if stage_trace is not None:
                        mark('POOL_ORDER');mark('POOL_PRICE')
                        trace_route('POOL_ORDER', row, status='COMPLETE_RECIPE')
                    current = evaluate_candidate(data, row['vehicle_id'], list(row['task_ids']), road, control)
                    # A road without a metric interface can still prove an actual
                    # materialized source, but cannot invent missing compact edges.
                    if current is None and materialized_source and not callable(getattr(road, 'known_segment', None)):
                        current = evaluate_route(data, row['vehicle_id'], list(row['task_ids']), road, control)
                    if current is None:
                        if stage_trace is not None:
                            trace_route('POOL_PRICE', row, status='MISSING_METRICS')
                        diagnostics['complete_recipes_missing_metrics'] += 1
                        complete = False
                        break
                    if current['errors'] or current['fee'] is None:
                        if stage_trace is not None:
                            trace_route('POOL_PRICE', current, status='REJECTED',
                                reason='|'.join(sorted({error['code'] for error in current['errors']})))
                        complete = False
                        break
                    if stage_trace is not None:
                        trace_route('POOL_PRICE', current, status='VALID_COMPLETE_RECIPE')
                    compact_routes.append(_compact(current))
                    remember(current)
                except StopSearch as exc:
                    if exc.reason == 'CANCELLED':
                        raise
                    diagnostics['candidate_pool_truncated'] = True
                    complete = False
                    break
            if not complete:
                continue
            diagnostics['complete_recipes_priced'] += 1
            cost = sum(_cents(row['fee']) for row in compact_routes) + sum(
                _cents(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price'])) for i in common_ids)
            score = (0, cost, sum(row['meters'] for row in compact_routes), len(compact_routes))
            whole_candidates.append((score, compact_routes, list(common_ids)))
        # The best whole recipe gets full evidence before ordinary pool work.
        # Proof uses the reserved final allowance, so a long pricing phase cannot
        # knowingly replace this already priced improvement with a dearer result.
        for score, compact_routes, common_ids in sorted(whole_candidates, key=lambda item: item[0]):
            if score >= _rank(best):
                break
            try:
                routes = materialize(compact_routes)
                complete = assemble(data, routes, common_ids, [])
                if _legal(complete):
                    retain(complete)
                    diagnostics['current_complete_plans'] += 1
            except StopSearch as exc:
                if exc.reason == 'CANCELLED':
                    raise
                diagnostics['candidate_pool_truncated'] = True
                break
        control()
        if stage_trace is not None:
            mark('POOL_ORDER')
        for result in [*sources, *recipe_sources, {'candidate_routes': candidate_recipes}]:
            algorithm = result.get('algorithm')
            extra = algorithm.get('_portfolio_recipes') if isinstance(algorithm, dict) else None
            for row in chain(rows(result.get('routes')), rows(result.get('candidate_routes')), rows(extra)):
                if monotonic() >= column_deadline:
                    diagnostics['candidate_pool_truncated'] = True
                    break
                if cancelled():
                    raise StopSearch('CANCELLED')
                ids = _order(data, row.get('task_ids') if isinstance(row, dict) else None)
                if ids is None:
                    diagnostics['invalid_recipes'] += 1
                    continue
                if len(orders) >= MAX_ORDERS and ids not in orders:
                    if stage_trace is not None:
                        trace_route('POOL_ORDER', row, status='REJECTED', reason='MAX_ORDERS')
                    diagnostics['candidate_pool_truncated'] = True
                    break
                orders.setdefault(ids, None)
                if stage_trace is not None:
                    trace_route('POOL_ORDER', row, status='ADMITTED')
        diagnostics['unique_orders'] = len(orders)
        report('REPRICE_COLUMNS')
        # Price warehouse/capacity-equivalent compact proposals once. The metric
        # evaluator uses a vehicle-independent known_segment(origin, destination)
        # lookup; grouping never copies a receipt or materialized route. Full
        # evidence and all physical-car constraints are rechecked after selection.
        # If interrupted, model classes reflect exactly the retained choice sets.
        pricing_finished = False
        if stage_trace is not None:
            mark('POOL_PRICE')
        for ids in orders:
            # Even an entirely ineligible order must honor cancellation and
            # yield the remaining allowance to model construction/solving.
            control()
            if monotonic() >= column_deadline or column_count >= MAX_COLUMNS:
                diagnostics['candidate_pool_truncated'] = True
                break
            fleet = _pricing_fleet(data, ids, columns, control)
            eligible = sum(map(len, fleet))
            diagnostics['eligible_vehicle_options'] += eligible
            diagnostics['ineligible_vehicle_options'] += len(columns) - eligible
            if not fleet and stage_trace is not None:
                trace_route('POOL_PRICE', {'vehicle_id':'','task_ids':ids}, status='NO_ELIGIBLE_CAR')
            for group in fleet:
                control()
                if monotonic() >= column_deadline or column_count >= MAX_COLUMNS:
                    diagnostics['candidate_pool_truncated'] = True
                    pricing_finished = True
                    break
                diagnostics['metric_evaluations'] += 1
                row = evaluate_candidate(data, group[0], list(ids), road, control)
                if row is None:
                    if stage_trace is not None:
                        trace_route('POOL_PRICE', {'vehicle_id':group[0],'task_ids':ids}, status='MISSING_METRICS')
                    diagnostics['unknown_metric_recipes'] += 1
                    continue
                if row['errors'] or row['fee'] is None:
                    if stage_trace is not None:
                        trace_route('POOL_PRICE', row, status='REJECTED',
                            reason='|'.join(sorted({error['code'] for error in row['errors']})))
                    continue
                if stage_trace is not None:
                    trace_route('POOL_PRICE', row, status='VALID_METRIC_REPRESENTATIVE')
                # An all-optional route that costs at least its tickets'
                # configured common delivery cannot improve any allocation:
                # outsourcing that entire route keeps coverage and frees a
                # vehicle. Equal cents also favors common's zero dedicated
                # meters/cars. Do not apply this to mandatory tasks or to the
                # separately protected complete-source columns/hints.
                if (all(i in common_cents for i in ids)
                        and _cents(row['fee']) >= sum(common_cents[i] for i in ids)):
                    diagnostics['common_dominated_metric_groups'] += 1
                    diagnostics['common_dominated_vehicle_options'] += len(group)
                    if stage_trace is not None:
                        trace_route('POOL_PRICE', row, status='COMMON_DOMINATED')
                    continue
                for index, vehicle_id in enumerate(group):
                    control()
                    if column_count >= MAX_COLUMNS:
                        diagnostics['candidate_pool_truncated'] = True
                        pricing_finished = True
                        break
                    remember({**row, 'vehicle_id': vehicle_id})
                    diagnostics['shared_metric_proposals'] += int(index > 0)
                if pricing_finished:
                    break
            if pricing_finished:
                break
        control()
        classes = _vehicle_classes(data, columns, control)
        if stage_trace is not None:
            mark('MODEL_INCLUDED');mark('MODEL_SELECTED')
            for group in classes:
                for vehicle_id in group['members']:
                    for row in group['patterns']:
                        control()
                        trace_route('MODEL_INCLUDED', {**row,'vehicle_id':vehicle_id}, status='IN_MODEL')
        diagnostics.update(column_count=column_count, class_count=len(classes),
                           physical_vehicle_count=len(columns))
        report('JOINT_COLUMN_SELECTION')
        remaining = work_deadline - monotonic()
        try:
            selection, solved = _solve(data, classes, best, remaining, cancelled)
            diagnostics.update(solved)
        except (ImportError, RuntimeError, ValueError, TypeError, ArithmeticError) as exc:
            selection = None
            diagnostics.update(fallback=True, fallback_reason='SOLVER_UNAVAILABLE' if isinstance(exc, ImportError) else 'SOLVER_ERROR')
        if selection is not None:
            selected, common_ids = selection
            if stage_trace is not None:
                for row in selected:
                    trace_route('MODEL_SELECTED', row, status='SELECTED')
            compact_fee = sum(_cents(row['fee']) for row in selected) + sum(
                _cents(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price'])) for i in common_ids)
            try:
                routes = materialize(selected)
                materialized = assemble(data, routes, common_ids, [])
                if not _legal(materialized) or _cents(materialized['total_fee']) != compact_fee:
                    diagnostics.update(fallback=True, fallback_reason='MATERIALIZATION_MISMATCH',
                                       materialization_mismatch=True, finite_pool_cost_optimal=False)
                    if _legal(materialized):
                        retain(materialized)
                else:
                    retain(materialized)
                    if _cents(best['total_fee']) != compact_fee:
                        diagnostics['finite_pool_cost_optimal'] = False
            except (RoadError, ValueError, TypeError, KeyError, ArithmeticError):
                diagnostics.update(fallback=True, fallback_reason='MATERIALIZATION_FAILED', finite_pool_cost_optimal=False)
        elif not diagnostics.get('fallback'):
            diagnostics.update(fallback=True, fallback_reason=diagnostics.get('solver_status', 'NO_INTEGER_SOLUTION'))
        if cancelled():
            stop_reason = 'CANCELLED'
            diagnostics['finite_pool_cost_optimal'] = False
    except StopSearch as exc:
        stop_reason = exc.reason
        diagnostics.update(fallback=True, fallback_reason=exc.reason, finite_pool_cost_optimal=False)
    # Never keep a proof after truncation, a changed materialization or incomplete
    # allocation. A global claim is false even for an OPTIMAL finite model.
    if diagnostics['candidate_pool_truncated'] or not _legal(best):
        diagnostics['finite_pool_cost_optimal'] = False
    # A live adapter can refresh a shared metric during materialization. Its old
    # incumbent may consequently no longer match the now-current measured matrix.
    # Rebind that plan rather than retaining an obsolete lower quotation.
    if best['routes'] and callable(getattr(road, 'known_segment', None)) and not _current_metrics_match(data, best, road):
        diagnostics.update(finite_pool_cost_optimal=False, fallback=True,
                           fallback_reason='INCUMBENT_METRICS_CHANGED')
        try:
            routes = [evaluate_route(data, row['vehicle_id'], list(row['task_ids']), road,
                                     lambda: control(final=True)) for row in best['routes']]
            rebound = assemble(data, routes, [r['task_id'] for r in best['common']], [])
            if not _legal(rebound):
                raise ValueError('更新道路后原方案不再可行')
            best = rebound
        except (StopSearch, RoadError, KeyError, TypeError, ValueError, ArithmeticError):
            best = assemble(data, [], priced, set(data['tasks']) - priced)
            diagnostics['fallback_reason'] = 'INCUMBENT_REVALIDATION_FAILED'
    if not diagnostics['finite_pool_cost_optimal']:
        diagnostics['distance_tie_optimality_proven'] = False
        diagnostics['vehicle_tie_optimality_proven'] = False
    elif diagnostics.get('selected_meters') != sum(row['meters'] for row in best['routes']):
        diagnostics['distance_tie_optimality_proven'] = False
        diagnostics['vehicle_tie_optimality_proven'] = False
    elif diagnostics.get('selected_vehicles') != len(best['routes']):
        diagnostics['vehicle_tie_optimality_proven'] = False
    diagnostics.update(mode='JOINT_COLUMN_PORTFOLIO', stop_reason=stop_reason,
                       elapsed_seconds=round(monotonic() - started, 4), budget_seconds=budget,
                       column_count=column_count,
                       complete_plan_verified=_legal(best), optimality_proven=False, global_optimality_proven=False,
                       total_fee=best['total_fee'], objective_order=['unassigned', 'total_fee_cents', 'meters', 'vehicles'])
    best['algorithm'] = diagnostics
    if stage_trace is not None:
        for stage in ('POOL_ORDER','POOL_PRICE','COLUMN','MODEL_INCLUDED','MODEL_SELECTED','MATERIALIZED'):
            stage_trace.mark(stage, status=('COMPLETE' if stop_reason=='SEARCH_COMPLETED' and not diagnostics['candidate_pool_truncated']
                else 'INTERRUPTED') if stage in traced_stages else 'SKIPPED', reason=stop_reason, scope=trace_scope)
        stage_trace.mark('PORTFOLIO_FINAL', status='COMPLETE' if _legal(best) else 'INTERRUPTED', scope=trace_scope)
        for row in best['routes']:
            trace_route('PORTFOLIO_FINAL', row, status='SELECTED_COMPLETE' if _legal(best) else 'PARTIAL')
    return best
