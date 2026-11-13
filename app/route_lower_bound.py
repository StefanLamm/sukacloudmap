"""Certified lower bounds for the frozen delivery-cost problem.

These are bounds, not delivery plans. Unknown roads cost zero in this relaxation.
Time windows, route connectivity and non-additive regional fees are relaxed.
The LP permits fractional visits and disconnected cycles, and its native primal
objective is NEVER used as a lower bound. Instead arbitrary finite dual values
are checked with exact rational Lagrangian arithmetic over finite variable
bounds. Thus numerical dual residuals cannot silently raise the certificate.

SciPy's dual fields/sign convention:
https://docs.scipy.org/doc/scipy/reference/optimize.linprog-highs.html
"""
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
import hashlib
from math import isfinite
from time import monotonic

from app.planning_inputs import content_hash
from app.routing_engine import RoadError, StopSearch, common_fee, normalize, regional_extra


def _fraction(value):
    return Fraction(Decimal(str(value)))


def _floor_money(value):
    cents = max(0, value.numerator * 100 // value.denominator)
    return f'{cents // 100}.{cents % 100:02d}'


def _per_store_extra(row):
    quote = row.get('quote')
    rule = quote.get('region_surcharge') if quote else None
    if not rule or rule.get('billing_mode', 'ONCE') != 'PER_STORE':
        return Fraction(0)
    try:
        return _fraction(regional_extra(quote, [row]))
    except RoadError:
        # Unresolved geography must weaken a bound, never add assumed charges.
        return Fraction(0)


def _minimum_base_average(quote, count):
    base, extra = _fraction(quote['base_price']), _fraction(quote['extra_stop_price'])
    included = quote['included_stops']
    return min((base + max(n - included, 0) * extra) / n for n in range(1, count + 1))


def _lp_api():
    import numpy as np
    from scipy.optimize import linprog
    from scipy.sparse import csc_matrix
    return np, linprog, csc_matrix


def _lagrangian_certificate(costs, bounds, equalities, inequalities, eq_dual,
                            ub_dual, constant=Fraction(0), cancelled=lambda: False,
                            expired=lambda: False):
    """Exact valid dual bound even if every supplied marginal is inaccurate.

    For Gx<=h, use mu<=0. For arbitrary lambda and mu, minimize the reduced
    objective independently over each finite variable interval. This produces
    a genuine lower bound without trusting floating-point dual feasibility.
    """
    if len(eq_dual) != len(equalities) or len(ub_dual) != len(inequalities):
        raise ValueError('Dual dimensions do not match the relaxation')
    if any(not isfinite(float(v)) for v in [*eq_dual, *ub_dual]):
        raise ValueError('Dual values must be finite')
    lambdas = [_fraction(float(v)) for v in eq_dual]
    mus = [min(Fraction(0), _fraction(float(v))) for v in ub_dual]
    reduced = list(costs); value = constant
    for rows, multipliers in [(equalities, lambdas), (inequalities, mus)]:
        for index, ((terms, rhs), dual) in enumerate(zip(rows, multipliers)):
            if index % 128 == 0 and cancelled():
                raise StopSearch('CANCELLED')
            if index % 128 == 0 and expired():raise TimeoutError()
            value += dual * rhs
            for column, coefficient in terms.items():
                reduced[column] -= dual * coefficient
    residual_correction = Fraction(0)
    for index, (residual, (lower, upper)) in enumerate(zip(reduced, bounds)):
        if index % 128 == 0 and cancelled():
            raise StopSearch('CANCELLED')
        if index % 128 == 0 and expired():raise TimeoutError()
        if upper is None:
            raise ValueError('Finite upper bounds are required for certificate repair')
        residual_correction += residual * (lower if residual >= 0 else upper)
    value += residual_correction
    witness = {'equality_multipliers': [str(v) for v in lambdas],
               'nonpositive_inequality_multipliers': [str(v) for v in mus],
               'reduced_cost_box_correction': str(residual_correction),
               'exact_lagrangian_value': str(value)}
    return value, witness


def compute_lower_bound(body, road, budget_seconds=3, cancelled=lambda: False,
                        *, edge_lookup=None, vehicle_independent=None, use_lp=True,
                        include_certificate=False):
    """Return a downward-rounded cost bound; never calls ``road.segment``.

    The coverage requirement is delivery of every task. Any gap comparison must
    use a fully delivered, independently checked plan on this same frozen input
    and graph. The result is not a real-world global-optimum claim.
    """
    started = monotonic(); deadline = started + max(0, budget_seconds)
    data = normalize(body); tasks = data['tasks']; ids = sorted(tasks)
    def check_cancelled():
        if cancelled():raise StopSearch('CANCELLED')
    def has_time():
        check_cancelled()
        return monotonic() < deadline
    check_cancelled()
    vehicles = defaultdict(list)
    for vehicle in data['vehicles'].values():
        if vehicle['available'] and vehicle['max_pieces'] >= 0:
            vehicles[vehicle['warehouse_id']].append(vehicle)
    groups = defaultdict(list); quotes = {}
    common = {}; extras = {}; minimum_average = {}
    for task_id in ids:
        row = tasks[task_id]
        if row['pieces'] < 0:
            raise ValueError('A nonnegative-cost certificate requires nonnegative quantities')
        price = common_fee(row['pieces'], row['common_unit_price'])
        common[task_id] = _fraction(price) if price is not None else None
        if common[task_id] is not None and common[task_id] < 0:
            raise ValueError('A nonnegative-cost certificate requires nonnegative common fees')
        extras[task_id] = _per_store_extra(row)
        if row.get('quote') and row.get('signature'):
            key = row['warehouse_id'], row['signature']
            groups[key].append(task_id); quotes[key] = row['quote']
    for quote in quotes.values():
        amounts = ([quote['per_km']] if quote['mode'].startswith('KM')
                   else [quote['base_price'],quote['extra_stop_price']])
        if quote.get('region_surcharge'):amounts.append(quote['region_surcharge']['amount'])
        if any(_fraction(value)<0 for value in amounts):
            raise ValueError('A nonnegative-cost certificate requires nonnegative quote parameters')
    # Frozen production input has one task per store. Keep the certificate
    # conservative for external fixtures containing several task IDs for the
    # same physical store too: one route charges that store only once.
    for group_ids in groups.values():
        identities = defaultdict(int)
        for task_id in group_ids:
            identities[tasks[task_id].get('store_id') or task_id] += 1
        for task_id in group_ids:
            extras[task_id] /= identities[tasks[task_id].get('store_id') or task_id]
    for key, group_ids in groups.items():
        if not quotes[key]['mode'].startswith('KM'):
            minimum_average[key] = _minimum_base_average(quotes[key], len(group_ids))
    if edge_lookup is None:
        known = getattr(road, 'known_segment', None)
        edge_lookup = (lambda v,a,b:known(a,b)) if callable(known) else (lambda v,a,b:None)
        independent = True
    else:
        independent = bool(vehicle_independent)
    points = {**data['warehouses'], **tasks}; measured = {}; meters = {}; incoming_lower = {}
    expected_edges = 0; known_edges = 0; unknown_edges = 0
    graph_digest = hashlib.sha256()
    for key, group_ids in sorted(groups.items()):
        if not quotes[key]['mode'].startswith('KM') or not vehicles[key[0]]:
            continue
        cars = vehicles[key[0]][:1] if independent else vehicles[key[0]]
        expected_edges += len(group_ids)**2 * len(cars)
        for destination in group_ids:
            if not has_time():
                break
            complete_row = True; incoming_min = None
            for origin in [key[0], *group_ids]:
                if origin == destination:
                    continue
                if not has_time():complete_row=False;break
                values = []; complete = True
                for vehicle in cars:
                    lookup_key = (None if independent else vehicle['id'], origin, destination)
                    if lookup_key not in measured:
                        value = None
                        if has_time():
                            try:
                                item = edge_lookup(vehicle, points[origin], points[destination])
                                amount = item['meters'] if item is not None else None
                                if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                                    value = amount
                            except (KeyError, TypeError, ValueError, RoadError):
                                pass
                        measured[lookup_key] = value
                        if value is None:unknown_edges += 1
                        else:known_edges += 1
                        graph_digest.update(repr((lookup_key,value)).encode())
                    value = measured[lookup_key]
                    if value is None:complete = False
                    else:values.append(value)
                # If any admissible vehicle/edge is unknown, zero is the only
                # justified general lower estimate for that directed edge.
                meters[origin,destination] = min(values) if complete and values else 0
                incoming_min = min(incoming_min,meters[origin,destination]) if incoming_min is not None else meters[origin,destination]
            incoming_lower[destination] = incoming_min if complete_row and incoming_min is not None else 0
    task_counts = defaultdict(int)
    for row in tasks.values():task_counts[row['warehouse_id']] += 1
    route_count_upper = sum(min(len(cars), task_counts[wid]) for wid,cars in vehicles.items())
    rounding_allowance = Fraction(route_count_upper, 200)
    analytical = Fraction(0); unpriced_mandatory = 0
    for task_id in ids:
        row = tasks[task_id]; group = row['warehouse_id'], row.get('signature')
        dedicated = None
        if group in groups and vehicles[group[0]]:
            quote = quotes[group]
            if quote['mode'].startswith('KM'):
                incoming = incoming_lower.get(task_id,0)
                dedicated = _fraction(quote['per_km']) * incoming / 1000 + extras[task_id]
            else:
                dedicated = minimum_average[group] + extras[task_id]
        options = [v for v in [common[task_id],dedicated] if v is not None]
        if not options:
            unpriced_mandatory += 1
        else:
            analytical += min(options)
    analytical_floor = _floor_money(analytical - rounding_allowance)
    report = {'schema_version':1, 'scope':'FROZEN_MEASURED_GRAPH_RELAXATION',
              'coverage_requirement':'ALL_TASKS_DELIVERED',
              'input_hash':content_hash(body), 'graph_hash':getattr(road,'graph_hash',None),
              'queried_cost_digest':graph_digest.hexdigest(),
              'cost_lower_bound':analytical_floor, 'analytical_lower_bound':analytical_floor,
              'lp_lower_bound':None, 'method':'ANALYTICAL_RELAXATION',
              'global_optimality_proven':False, 'feasibility_proven':False,
              'rounding_allowance':str(Decimal(route_count_upper)/Decimal(200)),
              'rounding_route_count_upper':route_count_upper,
              'round_down_to_cents':True, 'unknown_roads_cost_zero':True,
              'known_cost_edges':known_edges, 'unknown_cost_edges':unknown_edges,
              'unqueried_cost_edges':max(0,expected_edges-len(measured)),
              'unpriced_mandatory_tasks':unpriced_mandatory,
              'lp':{'status':'NOT_REQUESTED' if not use_lp else 'TIME_BUDGET', 'certified':False},
              'relaxed_constraints':['TIME_WINDOWS','PER_ROUTE_CAPACITY','CONNECTED_ROUTES',
                                      'INTEGRAL_ALLOCATION','NONADDITIVE_REGIONAL_SURCHARGES']}
    def finish():
        report['elapsed_seconds'] = round(monotonic()-started,4)
        return report
    if not use_lp or not ids or not has_time():
        return finish()
    variable_estimate = len(ids) + sum(len(g)**2 for g in groups.values()) + len(minimum_average)
    if variable_estimate > 200000:
        report['lp']['status']='MODEL_SIZE_LIMIT';return finish()
    try:
        np, linprog, csc_matrix = _lp_api()
    except ImportError:
        report['lp']['status']='SOLVER_UNAVAILABLE';return finish()
    if not has_time():return finish()
    costs = []; bounds = []; incoming = defaultdict(list); outgoing = defaultdict(list)
    departures = defaultdict(list); group_departures = defaultdict(list); y = {}; fee = {}
    constant = sum((v for v in common.values() if v is not None), Fraction(0))
    for task_id in ids:
        y[task_id] = len(costs)
        costs.append(extras[task_id] - (common[task_id] or Fraction(0)))
        bounds.append((Fraction(1) if common[task_id] is None else Fraction(0), Fraction(1)))
    for group, group_ids in sorted(groups.items()):
        if not has_time():return finish()
        quote = quotes[group]
        for origin in [group[0],*group_ids]:
            if not has_time():return finish()
            for destination in group_ids:
                if origin == destination:continue
                index = len(costs)
                costs.append(_fraction(quote['per_km'])*meters.get((origin,destination),0)/1000
                             if quote['mode'].startswith('KM') else Fraction(0))
                bounds.append((Fraction(0),Fraction(1)))
                incoming[destination].append(index)
                if origin == group[0]:
                    departures[group[0]].append(index);group_departures[group].append(index)
                else:outgoing[origin].append(index)
        if group in minimum_average:
            fee[group] = len(costs);costs.append(Fraction(1))
            upper = len(group_ids)*(_fraction(quote['base_price'])+_fraction(quote['extra_stop_price']))
            bounds.append((Fraction(0),upper))
    equalities = []; inequalities = []
    for task_id in ids:
        equalities.append(({**{i:Fraction(1) for i in incoming[task_id]},y[task_id]:Fraction(-1)},Fraction(0)))
        inequalities.append(({**{i:Fraction(1) for i in outgoing[task_id]},y[task_id]:Fraction(-1)},Fraction(0)))
    for warehouse_id in data['warehouses']:
        inequalities.append(({i:Fraction(1) for i in departures[warehouse_id]},Fraction(len(vehicles[warehouse_id]))))
        capacity = sum(v['max_pieces'] for v in vehicles[warehouse_id])
        inequalities.append(({y[i]:Fraction(tasks[i]['pieces']) for i in ids if tasks[i]['warehouse_id']==warehouse_id},Fraction(capacity)))
    for group, index in fee.items():
        quote = quotes[group];base = _fraction(quote['base_price']);extra = _fraction(quote['extra_stop_price'])
        group_y = [y[i] for i in groups[group]];starts = group_departures[group]
        inequalities.append(({**{i:minimum_average[group] for i in group_y},index:Fraction(-1)},Fraction(0)))
        inequalities.append(({**{i:base for i in starts},index:Fraction(-1)},Fraction(0)))
        inequalities.append(({**{i:extra for i in group_y},
                              **{i:base-extra*quote['included_stops'] for i in starts},index:Fraction(-1)},Fraction(0)))
    report['lp'].update(variables=len(costs), equalities=len(equalities), inequalities=len(inequalities),
                        route_connectivity_relaxed=True, fractional_visits_allowed=True)
    if not has_time():return finish()
    def matrix(rows):
        rr=[];cc=[];vv=[]
        for row_id,(terms,_) in enumerate(rows):
            for column,coefficient in terms.items():
                if coefficient:rr.append(row_id);cc.append(column);vv.append(float(coefficient))
        return csc_matrix((vv,(rr,cc)),shape=(len(rows),len(costs)))
    try:
        ub_matrix=matrix(inequalities);eq_matrix=matrix(equalities)
        native_cost=np.asarray([float(v) for v in costs])
        native_bounds=[(float(a),float(b)) for a,b in bounds]
        remaining = deadline-monotonic()
        if remaining <= 0:return finish()
        result = linprog(native_cost,
                A_ub=ub_matrix, b_ub=np.asarray([float(rhs) for _,rhs in inequalities]),
                A_eq=eq_matrix, b_eq=np.asarray([float(rhs) for _,rhs in equalities]),
                bounds=native_bounds, method='highs',
                options={'time_limit':min(1.0,remaining), 'presolve':True})
        check_cancelled()
        report['lp']['status'] = {0:'SOLVED',1:'TIME_LIMIT',2:'RELAXATION_INFEASIBLE',3:'UNBOUNDED',4:'SOLVER_ERROR'}.get(result.status,'UNKNOWN')
        # Neither a feasible primal point nor res.fun is a lower certificate.
        eq_dual = result.eqlin.marginals;ub_dual = result.ineqlin.marginals
        if eq_dual is None or ub_dual is None:return finish()
        raw, witness = _lagrangian_certificate(costs,bounds,equalities,inequalities,eq_dual,ub_dual,constant,cancelled,lambda:monotonic()>=deadline)
        certified_floor = _floor_money(raw-rounding_allowance)
        report['lp_lower_bound'] = certified_floor
        report['lp']['certified'] = True
        report['lp']['certificate_method'] = 'EXACT_RATIONAL_LAGRANGIAN_WITH_BOUNDED_REDUCED_COST_REPAIR'
        report['lp']['certificate_hash'] = content_hash(witness)
        if include_certificate:report['lp']['certificate'] = witness
        if _fraction(certified_floor) > _fraction(report['cost_lower_bound']):
            report['cost_lower_bound'] = certified_floor;report['method'] = 'CERTIFIED_LP_RELAXATION'
    except StopSearch:
        raise
    except TimeoutError:
        report['lp'].update(status='CERTIFICATE_TIME_BUDGET',certified=False)
    except (ValueError, TypeError, OverflowError, ArithmeticError, AttributeError, RuntimeError) as exc:
        report['lp'].update(status='SOLVER_OR_CERTIFICATE_ERROR',error_type=type(exc).__name__)
    return finish()
