"""Select mutually compatible evaluated routes from a finite candidate pool.

The integer solver can prove an optimum only for the supplied pool. It cannot
prove that an unexamined route would not be cheaper. Road evidence is deliberately
not retained here; the caller must materialize and check the selected routes.
"""
from collections import Counter
from decimal import Decimal
from functools import reduce
from math import gcd, isfinite
from time import monotonic

from app.routing_engine import RoadError, StopSearch, common_fee, line_fee, quote_signature


def _solver_api():
    # Optional at runtime: an installation/solver failure must not discard a
    # complete, independently evaluated incumbent.
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import csc_matrix
    return np, Bounds, LinearConstraint, milp, csc_matrix


def _cents(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value != value.quantize(Decimal('0.01')):
        raise ValueError('费用必须为非负的精确分金额')
    return int(value * 100)


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('件数、距离和时长必须为非负整数')
    return value


def _compact(data, candidate, signatures):
    vehicle_id = candidate['vehicle_id']
    vehicle = data['vehicles'][vehicle_id]
    ids = list(candidate['task_ids'])
    if not ids or len(set(ids)) != len(ids) or not vehicle['available'] or candidate.get('errors'):
        raise ValueError('候选线路为空、重复或车辆不可用')
    tasks = [data['tasks'][task_id] for task_id in ids]
    pieces = sum(_integer(task['pieces']) for task in tasks)
    if (not pieces or pieces > vehicle['max_pieces'] or
            any(task['warehouse_id'] != vehicle['warehouse_id'] for task in tasks) or
            signatures[ids[0]] is None or len({signatures[task_id] for task_id in ids}) != 1):
        raise ValueError('候选线路违反容量、同仓或报价兼容约束')
    if _integer(candidate['pieces']) != pieces:
        raise ValueError('候选线路件数不符')
    meters, seconds = _integer(candidate['meters']), _integer(candidate['seconds'])
    fee = _cents(candidate['fee'])
    if fee != _cents(line_fee(tasks[0]['quote'], meters, len(ids), tasks)):
        raise ValueError('候选线路报价复算不符')
    return {'vehicle_id': vehicle_id, 'task_ids': ids, 'fee': f'{Decimal(fee) / 100:.2f}',
            'meters': meters, 'seconds': seconds, 'pieces': pieces}


def _rank(data, routes, common_costs):
    seen, vehicles = Counter(), Counter()
    fee = 0
    for route in routes:
        seen.update(route['task_ids'])
        vehicles[route['vehicle_id']] += 1
        fee += _cents(route['fee'])
    if any(count != 1 for count in seen.values()) or any(count > 1 for count in vehicles.values()):
        raise ValueError('候选解重复分配门店或车辆')
    missing = 0
    for task_id in data['tasks']:
        if task_id not in seen:
            cost = common_costs[task_id]
            if cost is None:
                missing += 1
            else:
                fee += cost
    return missing, fee, sum(route['meters'] for route in routes), len(routes)


def select_candidate_routes(data, candidates, incumbent_candidates, time_limit, cancelled=None):
    """Return compact routes and finite-pool diagnostics, preserving a safe fallback.

    Objectives are lexicographic: missing tickets, integer-cent total cost,
    measured integer meters, then physical-car count. No epsilon can exchange
    money for utilization, distance or fewer cars. Common has no timing
    constraints. Every HiGHS invocation is limited to one second or less.
    """
    started = monotonic()
    diagnostics = {'solver': 'scipy.optimize.milp/HiGHS', 'candidate_pool_optimal': False,
                   'mip_gap': None, 'fallback': False, 'scope': 'CANDIDATE_POOL_ONLY',
                   'invalid_candidates': 0, 'coverage_optimal': False,
                   'cost_optimal_for_coverage': False,
                   'cost_candidate_pool_optimal': False, 'cost_mip_gap': None,
                   'distance_optimal_for_coverage_and_cost': False,
                   'vehicle_count_optimal_for_coverage_cost_distance': False,
                   'objective_order': ['unassigned', 'total_fee_cents', 'total_meters', 'vehicles']}

    def check_cancelled():
        if cancelled and cancelled():
            raise StopSearch('CANCELLED')

    def finish(routes, reason=None):
        if reason:
            diagnostics.update(fallback=True, fallback_reason=reason)
        rank = _rank(data, routes, common_costs)
        diagnostics.update(selected_count=len(routes), unassigned_count=rank[0],
                           total_fee=f'{Decimal(rank[1]) / 100:.2f}',
                           total_meters=rank[2],
                           improved_incumbent=rank < incumbent_rank)
        diagnostics['elapsed_seconds'] = round(monotonic() - started, 4)
        return routes, diagnostics

    check_cancelled()
    signatures = {task_id: quote_signature(task['quote']) if task.get('quote') else None
                  for task_id, task in data['tasks'].items()}
    common_costs = {task_id: (_cents(common_fee(task['pieces'], task.get('common_unit_price')))
                            if task.get('common_unit_price') is not None else None)
                    for task_id, task in data['tasks'].items()}
    incumbent = [_compact(data, route, signatures) for route in incumbent_candidates]
    incumbent_rank = _rank(data, incumbent, common_costs)
    pool = {}
    for index, candidate in enumerate([*incumbent, *candidates]):
        if index % 128 == 0:
            check_cancelled()
        try:
            route = _compact(data, candidate, signatures)
        except (KeyError, TypeError, ValueError, ArithmeticError, RoadError):
            diagnostics['invalid_candidates'] += 1
            continue
        # Task order affects fees through distance/timing, but not set coverage.
        # Keep the cheapest already verified order for the same car/task set.
        key = route['vehicle_id'], frozenset(route['task_ids'])
        score = (_cents(route['fee']), route['meters'], route['seconds'], tuple(route['task_ids']))
        previous = pool.get(key)
        if previous is None or score < previous[0]:
            pool[key] = score, route
    routes = [entry[1] for entry in pool.values()]
    diagnostics['candidate_count'] = len(routes)
    diagnostics['task_count'] = len(data['tasks'])
    if not routes:
        diagnostics.update(candidate_pool_optimal=True, mip_gap=0.0,
                           coverage_optimal=True, cost_optimal_for_coverage=True,
                           cost_candidate_pool_optimal=True,
                           distance_optimal_for_coverage_and_cost=True,
                           vehicle_count_optimal_for_coverage_cost_distance=True,
                           cost_mip_gap=0.0)
        return finish(incumbent)
    try:
        budget = float(time_limit)
    except (TypeError, ValueError, OverflowError):
        return finish(incumbent, 'INVALID_TIME_LIMIT')
    if not isfinite(budget) or budget <= 0 or monotonic() - started >= budget:
        return finish(incumbent, 'TIME_LIMIT')
    try:
        np, Bounds, LinearConstraint, milp, csc_matrix = _solver_api()
    except Exception:
        diagnostics['solver'] = 'incumbent-fallback'
        return finish(incumbent, 'SOLVER_UNAVAILABLE')
    check_cancelled()
    task_ids = list(data['tasks'])
    vehicle_ids = list({route['vehicle_id']: None for route in routes})
    task_rows = {task_id: index for index, task_id in enumerate(task_ids)}
    vehicle_rows = {vehicle_id: len(task_ids) + index for index, vehicle_id in enumerate(vehicle_ids)}
    common_ids = [task_id for task_id in task_ids if common_costs[task_id] is not None]
    route_count, common_count = len(routes), len(common_ids)
    missing_start = route_count + common_count
    variable_count = missing_start + len(task_ids)
    missing_row = len(task_ids) + len(vehicle_ids)
    rows, cols = [], []
    for column, route in enumerate(routes):
        for task_id in route['task_ids']:
            rows.append(task_rows[task_id]); cols.append(column)
        rows.append(vehicle_rows[route['vehicle_id']]); cols.append(column)
    for index, task_id in enumerate(common_ids):
        rows.append(task_rows[task_id]); cols.append(route_count + index)
    for index, task_id in enumerate(task_ids):
        rows.extend([task_rows[task_id], missing_row]); cols.extend([missing_start + index] * 2)
    costs = [_cents(route['fee']) for route in routes] + [common_costs[task_id] for task_id in common_ids] + [0] * len(task_ids)
    divisor = reduce(gcd, costs, 0) or 1
    costs = [cost // divisor for cost in costs]
    # Keep integer-cent comparisons exact after the solver's float conversion.
    if sum(costs) >= 2 ** 52:
        return finish(incumbent, 'NUMERIC_RANGE')
    cost_row = missing_row + 1
    coefficients = [1.0] * len(rows)
    for column, cost in enumerate(costs):
        if cost:
            rows.append(cost_row); cols.append(column); coefficients.append(float(cost))
    matrix = csc_matrix((coefficients, (rows, cols)), shape=(cost_row + 1, variable_count))
    lower = np.zeros(cost_row + 1); upper = np.ones(cost_row + 1)
    lower[:len(task_ids)] = 1
    upper[missing_row] = incumbent_rank[0]
    upper[cost_row] = np.inf
    best, best_rank = incumbent, incumbent_rank
    diagnostics['solver_slices'] = 0
    variable_upper = np.ones(variable_count)
    # An available common delivery always dominates leaving that ticket
    # unassigned. Disabling its artificial missing variable also ensures the
    # model's fixed monetary row equals the returned complete allocation.
    for index, task_id in enumerate(task_ids):
        if common_costs[task_id] is not None:
            variable_upper[missing_start + index] = 0

    def solve(objective, until=None):
        check_cancelled()
        remaining = budget - (monotonic() - started)
        if until is not None:
            remaining = min(remaining, until - monotonic())
        if remaining <= 0:
            return None
        diagnostics['solver_slices'] += 1
        result = milp(np.asarray(objective, dtype=float), integrality=np.ones(variable_count),
                      bounds=Bounds(np.zeros(variable_count), variable_upper),
                      constraints=LinearConstraint(matrix, lower, upper),
                      options={'time_limit': min(1.0, remaining), 'mip_rel_gap': 0.0, 'presolve': True})
        check_cancelled()
        return result

    def decode(result):
        if result is None or result.status not in {0, 1} or result.x is None:
            return None
        values = np.asarray(result.x)
        rounded = np.rint(values)
        if (values.shape != (variable_count,) or not np.isfinite(values).all() or
                np.max(np.abs(values - rounded), initial=0) > 1e-6 or
                (rounded < 0).any() or (rounded > variable_upper).any()):
            return None
        actual = matrix @ rounded
        if (actual < lower - 1e-8).any() or (actual > upper + 1e-8).any():
            return None
        selected = [_compact(data, routes[index], signatures) for index in range(route_count) if rounded[index] == 1]
        rank = _rank(data, selected, common_costs)
        # The compact return implies common for all uncovered eligible tickets.
        # Confirm it exactly agrees with the discrete model, including money.
        solved_missing = int(sum(rounded[missing_start:]))
        solved_cost = int(sum(cost * int(value) for cost, value in zip(costs, rounded))) * divisor
        if rank[0] != solved_missing or rank[1] != solved_cost:
            return None
        return selected, rank

    def keep(decoded):
        nonlocal best, best_rank
        if decoded is not None and decoded[1] < best_rank:
            best, best_rank = decoded
            return True
        return False

    def add_constraint_row(values):
        """Extend the sparse model between lexicographic stages, exactly."""
        nonlocal matrix, lower, upper
        index = matrix.shape[0]
        for column, value in enumerate(values):
            if value:
                rows.append(index); cols.append(column); coefficients.append(float(value))
        matrix = csc_matrix((coefficients, (rows, cols)), shape=(index + 1, variable_count))
        lower = np.append(lower, 0.0); upper = np.append(upper, np.inf)
        return index

    def minimize_tie(objective, objective_row, rank_index):
        """Optimize a fixed-prefix integer objective with short native slices."""
        while monotonic() - started < budget:
            check_cancelled()
            target = best_rank[rank_index]
            if target == 0:
                return True, None
            # Objective coefficients are exact nonnegative integers. Excluding
            # the retained value makes infeasibility a proof for that value.
            upper[objective_row] = target - 1
            result = solve(objective)
            if result is None:
                return False, 'TIME_LIMIT'
            if result.status == 2:
                return True, None
            decoded = decode(result)
            keep(decoded)
            if decoded is None and result.status != 1:
                return False, 'INVALID_SOLVER_RESULT'
            if result.status == 0:
                return True, None
            if best_rank[rank_index] >= target:
                return False, 'TIME_LIMIT'
        return False, 'TIME_LIMIT'

    try:
        coverage_optimal = best_rank[0] == 0
        if not coverage_optimal:
            coverage_objective = [0] * missing_start + [1] * len(task_ids)
            # Keep time for monetary selection even when minimum missing count
            # has not yet been proven. Every accepted improvement remains valid.
            coverage_until = monotonic() + max(0, (budget - (monotonic() - started)) * .4)
            while best_rank[0] and monotonic() < coverage_until:
                upper[missing_row] = best_rank[0] - 1
                previous_missing = best_rank[0]
                result = solve(coverage_objective, coverage_until)
                if result is None:
                    break
                if result.status == 2:
                    # No solution has strictly fewer missing tickets.
                    coverage_optimal = True
                    break
                decoded = decode(result)
                keep(decoded)
                if decoded is None and result.status != 1:
                    return finish(best, 'INVALID_SOLVER_RESULT')
                if result.status == 0:
                    coverage_optimal = True
                    break
                if best_rank[0] >= previous_missing:
                    break  # Identical restarted models cannot reuse a tree.
            coverage_optimal = coverage_optimal or best_rank[0] == 0
        money_optimal = False
        while monotonic() - started < budget:
            check_cancelled()
            lower[missing_row] = upper[missing_row] = best_rank[0]
            # Search strictly below the best *verified integer-cent* cost. An
            # infeasible restricted model therefore proves the retained cost.
            upper[cost_row] = best_rank[1] // divisor - 1
            if upper[cost_row] < 0:
                money_optimal = True
                break
            previous_rank = best_rank
            result = solve(costs)
            if result is None:
                break
            if result.status == 2:
                money_optimal = True
                break
            decoded = decode(result)
            keep(decoded)
            if decoded is None and result.status != 1:
                return finish(best, 'INVALID_SOLVER_RESULT')
            if best_rank[0] < previous_rank[0]:
                coverage_optimal = best_rank[0] == 0
                continue
            if result.status == 0:
                money_optimal = True
                break
            if best_rank[:2] >= previous_rank[:2]:
                break
        diagnostics.update(coverage_optimal=coverage_optimal,
                           cost_optimal_for_coverage=money_optimal,
                           cost_candidate_pool_optimal=coverage_optimal and money_optimal,
                           cost_mip_gap=0.0 if coverage_optimal and money_optimal else None)
        distance_optimal = vehicle_optimal = False
        secondary_reason = None
        # Tie optimization cannot spend one more cent or lose one more ticket,
        # even when the primary solve timed out without proving its optimum.
        # In that case the secondary proof is conditional on the retained cost.
        lower[missing_row] = upper[missing_row] = best_rank[0]
        lower[cost_row] = upper[cost_row] = best_rank[1] // divisor
        distances = [route['meters'] for route in routes] + [0] * (variable_count - route_count)
        if sum(distances) >= 2 ** 52:
            secondary_reason = 'DISTANCE_NUMERIC_RANGE'
        else:
            distance_row = add_constraint_row(distances)
            distance_optimal, secondary_reason = minimize_tie(distances, distance_row, 2)
            diagnostics['distance_optimal_for_coverage_and_cost'] = distance_optimal
            diagnostics['distance_proof_fee'] = f'{Decimal(best_rank[1]) / 100:.2f}'
            if secondary_reason == 'INVALID_SOLVER_RESULT':
                return finish(best, secondary_reason)
            # Car count is the final tie break only after exact cost and meters
            # have been fixed. High loading never outranks cheaper delivery.
            if monotonic() - started < budget:
                lower[distance_row] = upper[distance_row] = best_rank[2]
                counts = [1] * route_count + [0] * (variable_count - route_count)
                count_row = add_constraint_row(counts)
                vehicle_optimal, count_reason = minimize_tie(counts, count_row, 3)
                secondary_reason = secondary_reason or count_reason
        complete_proof = coverage_optimal and money_optimal and distance_optimal and vehicle_optimal
        diagnostics.update(candidate_pool_optimal=complete_proof,
                           distance_optimal_for_coverage_and_cost=distance_optimal,
                           vehicle_count_optimal_for_coverage_cost_distance=vehicle_optimal,
                           distance_proof_fee=f'{Decimal(best_rank[1]) / 100:.2f}',
                           mip_gap=0.0 if complete_proof else None)
        return finish(best, None if complete_proof else secondary_reason or 'TIME_LIMIT')
    except StopSearch:
        raise
    except Exception as exc:
        # No provider details or solver input are exposed in fallback diagnostics.
        diagnostics['solver_error'] = type(exc).__name__
        return finish(best, 'SOLVER_ERROR')
