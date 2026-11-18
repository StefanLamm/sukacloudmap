"""OR-Tools candidate search on measured directed roads, followed by exact pricing.

The routing model is a candidate generator, never the source of a quotation or
road evidence. A dropped optional visit means *paid common delivery*, not a
cancelled order. Physical cars are shared by quote-specific virtual routes.
Open routes end at their final store; no fictitious return leg is charged.

Official model references:
https://developers.google.com/optimization/routing/penalties
https://developers.google.com/optimization/routing/vrptw
https://developers.google.com/optimization/routing/routing_options
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from functools import reduce
from math import gcd, lcm
from random import Random
from time import monotonic

from app.routing_engine import (RoadError, StopSearch, assemble, check_result,
                                common_fee, evaluate_route, normalize,
                                regional_extra)


STRATEGIES = {'GUIDED_LOCAL_SEARCH', 'SIMULATED_ANNEALING', 'TABU_SEARCH', 'GREEDY_DESCENT'}
FIRST_STRATEGIES = {'PARALLEL_CHEAPEST_INSERTION', 'LOCAL_CHEAPEST_INSERTION',
                    'PATH_CHEAPEST_ARC', 'SAVINGS', 'ALL_UNPERFORMED'}


class ModelUnavailable(Exception):
    pass


def _ortools_api():
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    return pywrapcp, routing_enums_pb2


def _penalty_factor(value):
    try:
        if isinstance(value,bool):raise ValueError()
        factor = value if isinstance(value,Fraction) else Fraction(Decimal(str(value)))
    except (ValueError,TypeError,ArithmeticError):
        raise ValueError('Common penalty multiplier must be a finite value from 0.1 to 10') from None
    if not Fraction(1,10) <= factor <= 10:
        raise ValueError('Common penalty multiplier must be a finite value from 0.1 to 10')
    return factor


def _rank(result):
    # Loading and vehicle count never outrank the complete delivery cost.
    return (len(result['unassigned']), Decimal(result['total_fee']),
            sum(r['meters'] for r in result['routes']), len(result['routes']))


def _valid_result(data, result):
    try:
        proof = check_result(data, result)
        return all(e['code'] in {'DRIVING_REFERENCE', 'UNASSIGNED'} for e in proof['issues'])
    except (KeyError, TypeError, ValueError, ArithmeticError, RoadError):
        return False


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**10


def _default_lookup(road):
    if not callable(getattr(road, 'known_segment', None)):
        raise ModelUnavailable('MATRIX_UNAVAILABLE')
    return lambda vehicle, origin, destination: road.known_segment(origin, destination)


class _MeasuredModel:
    """Measured routing proposals in the existing one-second native slices."""
    def __init__(self, data, edge_lookup, control, seed, *, vehicle_independent,
                 strategy, first_strategy, region_surrogate='PER_STORE', common_penalty_multiplier=Fraction(1),
                 candidate_retention='first', candidate_stream_observer=None,
                 stage_trace=None, trace_scope=None):
        pywrapcp, enums = _ortools_api()
        self.api, self.enums = pywrapcp, enums
        self.data, self.control = data, control
        self.strategy, self.first_strategy = strategy, first_strategy
        self.stage_trace, self.trace_scope = stage_trace, trace_scope
        self.candidate_specs = {}; self.solution_count = 0; self.native_slices = 0
        self.retention = None
        if candidate_retention != 'first':
            from app.route_retention import CandidateRetention
            self.retention = CandidateRetention(candidate_retention, data=data, control=control)
        self.candidate_stream_observer = candidate_stream_observer
        self.statuses = []; self.invalid_metrics = 0; self.unknown_edges = 0
        self.nonadditive_regions = set(); self.callback_cancelled = False
        self.callbacks = []
        self.common_penalties = {}
        for task_id,row in data['tasks'].items():
            price = common_fee(row['pieces'],row['common_unit_price'])
            self.common_penalties[task_id] = Fraction(Decimal(price))*common_penalty_multiplier if price is not None else None
        ids = sorted(data['tasks']); Random(seed).shuffle(ids)
        self.ids = ids
        self.groups = defaultdict(list)
        self.pricing = {}
        for task_id in ids:
            row = data['tasks'][task_id]
            if not row.get('quote') or not row.get('signature'):
                continue
            try:
                extra = regional_extra(row['quote'], [row])
            except RoadError:
                continue
            key = row['warehouse_id'], row['signature']
            self.groups[key].append(task_id)
            self.pricing[key] = row['quote']
            rule = row['quote'].get('region_surcharge')
            if rule and rule.get('billing_mode', 'ONCE') != 'PER_STORE':
                self.nonadditive_regions.add(rule.get('billing_mode', 'ONCE'))
        self.virtual = []
        for vehicle in sorted(data['vehicles'].values(), key=lambda v: v['id']):
            if not vehicle['available'] or vehicle['max_pieces'] <= 0:
                continue
            for key, tasks in sorted(self.groups.items()):
                if key[0] == vehicle['warehouse_id'] and any(data['tasks'][i]['pieces'] <= vehicle['max_pieces'] for i in tasks):
                    self.virtual.append((vehicle, key))
        if len(ids) > 1000 or len(self.virtual) > 500:
            raise ModelUnavailable('MODEL_SIZE_LIMIT')
        if not ids or not self.virtual:
            raise ModelUnavailable('NO_DEDICATED_MODEL')

        # Every objective component uses the same integer currency unit. KM
        # arc fees are not prematurely rounded per edge. Final route rounding
        # and every non-additive region charge still use line_fee in evaluation.
        scale = 100
        for quote in self.pricing.values():
            terms = ([Fraction(Decimal(quote['per_km'])) / 1000]
                     if quote['mode'].startswith('KM') else
                     [Fraction(Decimal(quote['base_price'])), Fraction(Decimal(quote['extra_stop_price']))])
            if quote.get('region_surcharge'):
                terms.append(Fraction(Decimal(quote['region_surcharge']['amount'])))
            for term in terms:
                scale = lcm(scale, term.denominator)
        for price in self.common_penalties.values():
            if price is not None:scale=lcm(scale,price.denominator)
        if scale > 10**9:
            raise ModelUnavailable('NUMERIC_RANGE')
        self.scale = scale
        def units(value):
            number = (value if isinstance(value,Fraction) else Fraction(Decimal(str(value)))) * scale
            if number.denominator != 1 or number < 0:
                raise ModelUnavailable('NUMERIC_RANGE')
            return int(number)
        self.units = units

        warehouse_ids = sorted({v['warehouse_id'] for v, _ in self.virtual})
        self.labels = warehouse_ids + ids + ['__OPEN_ROUTE_END__']
        self.node_of = {key: index for index, key in enumerate(self.labels)}
        self.task_nodes = {self.node_of[key]: key for key in ids}
        self.sink = len(self.labels) - 1
        self.points = {**data['warehouses'], **data['tasks']}
        metrics = {}; seen_physical_groups = set()
        max_arc_cost = 0; fixed_upper = 0; extra_stop_upper = 0
        self.node_surcharge = {}
        for index, (vehicle, group) in enumerate(self.virtual):
            control()
            quote = self.pricing[group]
            task_ids = self.groups[group]
            if not quote['mode'].startswith('KM'):
                fixed_upper += units(quote['base_price'])
                extra_stop_upper = max(extra_stop_upper, units(quote['extra_stop_price']))
            for task_id in task_ids:
                rule = quote.get('region_surcharge')
                extra = regional_extra(quote, [data['tasks'][task_id]]) if rule else Decimal(0)
                # PER_STORE is additive and exact. For non-additive policies,
                # use either the per-hit overestimate or omit that component;
                # exact whole-route pricing is compulsory before acceptance.
                if rule and rule.get('billing_mode', 'ONCE') != 'PER_STORE' and region_surrogate == 'IGNORE':
                    extra = Decimal(0)
                self.node_surcharge[group, task_id] = units(extra)
            physical_group = None if vehicle_independent else vehicle['id'], group
            if physical_group in seen_physical_groups:
                continue
            seen_physical_groups.add(physical_group)
            for from_id in [group[0], *task_ids]:
                control()
                for to_id in task_ids:
                    if from_id == to_id:
                        continue
                    cache_key = (None if vehicle_independent else vehicle['id'], from_id, to_id)
                    if cache_key in metrics:
                        continue
                    try:
                        item = edge_lookup(vehicle, self.points[from_id], self.points[to_id])
                        if item is None:
                            self.unknown_edges += 1; metrics[cache_key] = None; continue
                        m, s = item['meters'], item['seconds']
                        if not _integer(m) or not _integer(s):
                            self.invalid_metrics += 1; metrics[cache_key] = None; continue
                        metrics[cache_key] = m, s
                    except (KeyError, TypeError, ValueError, RoadError):
                        self.invalid_metrics += 1; metrics[cache_key] = None
            for a in [group[0], *task_ids]:
                for b in task_ids:
                    metric = metrics.get((None if vehicle_independent else vehicle['id'], a, b))
                    if metric is None:
                        continue
                    arc_cost = (units(Decimal(quote['per_km']) / 1000) * metric[0]
                                if quote['mode'].startswith('KM') else 0)
                    max_arc_cost = max(max_arc_cost, arc_cost + self.node_surcharge[group, b])
        self.metrics, self.vehicle_independent = metrics, vehicle_independent
        common_upper = sum(units(price) for price in self.common_penalties.values() if price is not None)
        # One fewer genuinely unassigned ticket dominates any possible change
        # in the bounded model's measured delivery cost.
        cost_upper = len(ids) * (max_arc_cost + extra_stop_upper) + fixed_upper + common_upper
        components = [units(price) for price in self.common_penalties.values() if price is not None]
        for quote in self.pricing.values():
            components.extend([units(Decimal(quote['per_km']) / 1000)] if quote['mode'].startswith('KM')
                              else [units(quote['base_price']), units(quote['extra_stop_price'])])
        components.extend(self.node_surcharge.values())
        divisor = reduce(gcd, components, 0) or 1
        self.money_divisor = divisor
        distance_upper = len(ids) * max((m[0] for m in metrics.values() if m is not None), default=0)
        missing_count = sum(t['common_unit_price'] is None for t in data['tasks'].values())
        # Integer lexicographic tie-breaking: even all possible route distance
        # savings cannot buy one unit of monetary cost. Reduce the shared
        # currency coefficients first to avoid unnecessary int64 growth.
        multiplier = distance_upper + 1
        combined_upper = (cost_upper // divisor) * multiplier + distance_upper
        self.distance_tie_break = (combined_upper + 1) * max(1, missing_count) < 2**62
        self.money_multiplier = multiplier if self.distance_tie_break else 1
        combined_upper = ((cost_upper // divisor) * self.money_multiplier
                          + (distance_upper if self.distance_tie_break else 0))
        self.mandatory_penalty = combined_upper + 1
        if self.mandatory_penalty * max(1, missing_count) >= 2**62:
            raise ModelUnavailable('NUMERIC_RANGE')
        def objective_money(value):
            return units(value) // divisor * self.money_multiplier
        self.objective_money = objective_money

        starts = [self.node_of[v['warehouse_id']] for v, _ in self.virtual]
        self.manager = pywrapcp.RoutingIndexManager(len(self.labels), len(self.virtual), starts, [self.sink] * len(self.virtual))
        self.routing = routing = pywrapcp.RoutingModel(self.manager)
        solver = routing.solver()
        by_physical = defaultdict(list)
        for index, (vehicle, _) in enumerate(self.virtual):
            by_physical[vehicle['id']].append(routing.ActiveVehicleVar(index))
        for variables in by_physical.values():
            if len(variables) > 1:
                solver.Add(solver.Sum(variables) <= 1)

        timestamps = [int(datetime.fromisoformat(w['earliest_departure_at']).timestamp()) for w in data['warehouses'].values()]
        timestamps += [int(datetime.fromisoformat(t['open_at']).timestamp()) for t in data['tasks'].values()]
        self.epoch = min(timestamps)
        latest = max([*timestamps, *[int(datetime.fromisoformat(t['close_at']).timestamp()) for t in data['tasks'].values()]])
        self.horizon = max(1, latest - self.epoch + max(int(t['unload_minutes'] * 60) for t in data['tasks'].values()) + 1)
        if self.horizon > 10**9:
            raise ModelUnavailable('TIME_RANGE')
        time_callbacks = []
        for vi, (vehicle, group) in enumerate(self.virtual):
            quote = self.pricing[group]
            km_rate = units(Decimal(quote['per_km']) / 1000) if quote['mode'].startswith('KM') else 0
            def cost(a, b, vehicle=vehicle, group=group, km_rate=km_rate):
                na, nb = self.manager.IndexToNode(a), self.manager.IndexToNode(b)
                if nb == self.sink or na == nb:
                    return 0
                if nb not in self.task_nodes:
                    return 0
                task_id = self.task_nodes[nb]
                item = self._metric(vehicle['id'], na, nb)
                if item is None:
                    return 0  # Forbidden by the time dimension, not free travel.
                price = km_rate * item[0] + self.node_surcharge.get((group, task_id), 0)
                return price // divisor * self.money_multiplier + (item[0] if self.distance_tie_break else 0)
            def transit(a, b, vehicle=vehicle):
                na, nb = self.manager.IndexToNode(a), self.manager.IndexToNode(b)
                service = int(data['tasks'][self.task_nodes[na]]['unload_minutes'] * 60) if na in self.task_nodes else 0
                if nb == self.sink or na == nb:
                    return service
                metric = self._metric(vehicle['id'], na, nb)
                return self.horizon + 1 if metric is None else metric[1] + service
            self.callbacks.extend([cost, transit])
            routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(cost), vi)
            time_callbacks.append(routing.RegisterTransitCallback(transit))
            if not quote['mode'].startswith('KM'):
                routing.SetFixedCostOfVehicle(objective_money(quote['base_price']), vi)
        routing.AddDimensionWithVehicleTransits(time_callbacks, self.horizon, self.horizon, False, 'Time')
        times = routing.GetDimensionOrDie('Time')
        for vi, (vehicle, _) in enumerate(self.virtual):
            departure = int(datetime.fromisoformat(data['warehouses'][vehicle['warehouse_id']]['earliest_departure_at']).timestamp()) - self.epoch
            times.CumulVar(routing.Start(vi)).SetRange(departure, departure)
        for task_node, task_id in self.task_nodes.items():
            row = data['tasks'][task_id]; index = self.manager.NodeToIndex(task_node)
            opens = int(datetime.fromisoformat(row['open_at']).timestamp()) - self.epoch
            closes = int(datetime.fromisoformat(row['close_at']).timestamp()) - self.epoch
            if row.get('latest_arrival_at'):
                deadline = int(datetime.fromisoformat(row['latest_arrival_at']).timestamp()) - self.epoch
                # Cumul denotes service start. Arrival before an earlier
                # deadline followed by waiting until opening is still legal.
                closes = min(closes, max(opens, deadline))
            if opens > closes or closes < 0:
                routing.VehicleVar(index).SetValues([-1])
            else:
                times.CumulVar(index).SetRange(max(0, opens), closes)
            key = row['warehouse_id'], row.get('signature')
            allowed = [vi for vi, (v, g) in enumerate(self.virtual) if g == key and row['pieces'] <= v['max_pieces']]
            if allowed:
                # SetAllowedVehiclesForIndex's Span binding is broken in the
                # official 9.15 Python wheel; the equivalent variable domain
                # keeps -1 for paid common delivery and exactly these cars.
                routing.VehicleVar(index).SetValues([-1, *allowed])
            else:
                routing.VehicleVar(index).SetValues([-1])
            price = self.common_penalties[task_id]
            routing.AddDisjunction([index], self.mandatory_penalty if price is None else objective_money(price))

        def demand(a):
            node = self.manager.IndexToNode(a)
            return data['tasks'][self.task_nodes[node]]['pieces'] if node in self.task_nodes else 0
        def stop(a):
            return int(self.manager.IndexToNode(a) in self.task_nodes)
        self.callbacks.extend([demand, stop])
        routing.AddDimensionWithVehicleCapacity(routing.RegisterUnaryTransitCallback(demand), 0,
                                               [v['max_pieces'] for v, _ in self.virtual], True, 'Pieces')
        routing.AddDimension(routing.RegisterUnaryTransitCallback(stop), 0, len(ids), True, 'Stops')
        stops = routing.GetDimensionOrDie('Stops')
        for vi, (_, group) in enumerate(self.virtual):
            quote = self.pricing[group]
            if not quote['mode'].startswith('KM'):
                stops.SetCumulVarSoftUpperBound(routing.End(vi), quote['included_stops'], objective_money(quote['extra_stop_price']))

        def solution_callback():
            # No network or quotation runs inside a native solver callback.
            self.solution_count += 1
            if (self.retention is None and self.candidate_stream_observer is None and self.stage_trace is None
                    and len(self.candidate_specs) >= 10000):
                return
            self._collect(lambda index: self.routing.NextVar(index).Value())
        self.callbacks.append(solution_callback)
        routing.AddAtSolutionCallback(solution_callback)

    def dispose(self):
        # Native transit callbacks retain Python closures. Explicitly release
        # their RoutingModel owner so repeated jobs do not form a C++/Python
        # ownership cycle and retain entire prior models indefinitely.
        self.callbacks.clear()
        self.routing = None
        self.manager = None
        self.objective_money = None

    def _metric(self, vid, from_node, to_node):
        return self.metrics.get((None if self.vehicle_independent else vid,
                                 self.labels[from_node], self.labels[to_node]))

    def _collect(self, next_value):
        specs = []
        for vi, (vehicle, _) in enumerate(self.virtual):
            index = self.routing.Start(vi); ids = []
            while not self.routing.IsEnd(index):
                node = self.manager.IndexToNode(index)
                if node in self.task_nodes:
                    ids.append(self.task_nodes[node])
                index = next_value(index)
                if len(ids) > len(self.ids):
                    return []
            if ids:
                key = vehicle['id'], tuple(ids)
                row = {'vehicle_id': vehicle['id'], 'task_ids': ids}
                if self.stage_trace is not None:
                    self.stage_trace.route('NATIVE_PROPOSED', row, scope=self.trace_scope)
                if self.candidate_stream_observer is not None:
                    self.candidate_stream_observer(row)
                if self.retention is not None:
                    self.retention.observe(row)
                elif key in self.candidate_specs or len(self.candidate_specs) < 10000:
                    self.candidate_specs.setdefault(key, row)
                specs.append(row)
        return specs

    def parameters(self, seconds):
        parameters = self.api.DefaultRoutingSearchParameters()
        parameters.first_solution_strategy = getattr(self.enums.FirstSolutionStrategy, self.first_strategy)
        parameters.local_search_metaheuristic = getattr(self.enums.LocalSearchMetaheuristic, self.strategy)
        parameters.time_limit.FromMilliseconds(max(1, int(min(1., seconds) * 1000)))
        parameters.lns_time_limit.FromMilliseconds(50)
        parameters.log_search = False
        return parameters

    def solve(self, seconds, previous=None):
        self.control(); self.native_slices += 1
        parameters = self.parameters(seconds)
        solution = (self.routing.SolveFromAssignmentWithParameters(previous, parameters)
                    if previous is not None else self.routing.SolveWithParameters(parameters))
        self.statuses.append(self.routing.status())
        specs = self._collect(lambda i: solution.Value(self.routing.NextVar(i))) if solution else []
        if self.candidate_stream_observer is not None:
            self.candidate_stream_observer({'kind': 'flush'})
        return solution, specs

    def warm_start(self, routes, seconds):
        self.routing.CloseModelWithParameters(self.parameters(seconds))
        virtual_of = {(v['id'], group): index for index, (v, group) in enumerate(self.virtual)}
        paths = [[] for _ in self.virtual]
        for route in routes:
            if not route['task_ids']:
                continue
            row = self.data['tasks'][route['task_ids'][0]]
            key = route['vehicle_id'], (row['warehouse_id'], row['signature'])
            if key not in virtual_of:
                continue
            paths[virtual_of[key]] = [self.manager.NodeToIndex(self.node_of[i]) for i in route['task_ids']]
        return self.routing.ReadAssignmentFromRoutes(paths, True)


def generate_ortools_candidates(data, edge_lookup, time_limit=5, seed=0, cancelled=lambda: False,
                                *, initial_routes=(), vehicle_independent=False,
                                strategy='GUIDED_LOCAL_SEARCH', first_strategy='PARALLEL_CHEAPEST_INSERTION',
                                region_surrogate='PER_STORE', common_penalty_multiplier=1, on_batch=None,
                                candidate_retention='first', candidate_stream_observer=None,
                                stage_trace=None,
                                complete_assignment_pricing=False, on_assignment=None):
    """Return route *specifications* and diagnostics; specs carry no quoted fee.

    ``edge_lookup(vehicle, origin, destination)`` must be a read-only lookup of
    measured meters/seconds. Unknown edges are forbidden. ``on_batch`` is called
    between native slices and may evaluate/pack candidates cooperatively.
    """
    factor = _penalty_factor(common_penalty_multiplier)
    started = monotonic(); deadline = started + max(0, time_limit)
    trace_scope = stage_trace.scope('NATIVE') if stage_trace is not None else None
    if stage_trace is not None:
        stage_trace.mark('NATIVE_PROPOSED', status='ENTERED', scope=trace_scope)
        stage_trace.mark('NATIVE_RETAINED', status='ENTERED', scope=trace_scope)
    def control():
        if cancelled():
            raise StopSearch('CANCELLED')
        if monotonic() >= deadline:
            raise StopSearch('TIME_BUDGET')
    diagnostics = {'solver': 'OR_TOOLS_ROUTING', 'strategy': strategy,
                   'first_strategy': first_strategy, 'scope': 'MEASURED_MATRIX_CANDIDATE_GENERATION',
                   'global_optimality_proven': False, 'quoted_fees_from_model': False,
                   'surrogate_common_penalty_multiplier':str(factor),
                   'common_penalty_is_real_quote':factor==1,
                   'candidate_stream_capture_enabled': candidate_stream_observer is not None,
                   'candidate_stream_capture_scope': 'NATIVE_SOLUTION_ROUTE_IDENTITIES',
                   'candidate_stream_capture_overhead_in_budget': True}
    if strategy not in STRATEGIES or first_strategy not in FIRST_STRATEGIES or region_surrogate not in {'PER_STORE', 'IGNORE'}:
        raise ValueError('Unsupported OR-Tools search variant')
    if candidate_retention not in {'first', 'batch_dedup', 'diversity'}:
        raise ValueError('Unsupported candidate retention policy')
    if not isinstance(complete_assignment_pricing,bool):
        raise ValueError('complete_assignment_pricing must be a boolean')
    assignment_enabled=complete_assignment_pricing and callable(on_assignment)
    assignment_reserve=min(.5,max(0.,time_limit*.15)) if assignment_enabled else 0.
    assignment_details={'enabled':complete_assignment_pricing,'host_callback_present':callable(on_assignment),
        'native_time_reserve_seconds':assignment_reserve,'assignment_count':0,
        'callback_seconds':0.,'direct_fee_reduction':'0.00','records':[],
        'quotation_inside_native_callback':False,'ordinary_pool_cap_applied':False}
    diagnostics['complete_assignment_pricing']=assignment_details
    if stage_trace is not None:
        stage_trace.mark('NATIVE_ASSIGNMENT',status='ENTERED' if assignment_enabled else 'SKIPPED',
            reason=None if assignment_enabled else 'SWITCH_DISABLED' if not complete_assignment_pricing else 'NO_HOST_CALLBACK',scope=trace_scope)
    diagnostics['candidate_retention_policy'] = candidate_retention
    model = None; stop = 'SEARCH_COMPLETED'; best_specs = []
    try:
        control()
        model = _MeasuredModel(data, edge_lookup, control, seed, vehicle_independent=vehicle_independent,
                               strategy=strategy, first_strategy=first_strategy, region_surrogate=region_surrogate,
                               common_penalty_multiplier=factor, candidate_retention=candidate_retention,
                               candidate_stream_observer=candidate_stream_observer,
                               stage_trace=stage_trace, trace_scope=trace_scope)
        remaining = deadline - monotonic()
        assignment = model.warm_start(initial_routes, remaining) if initial_routes else None
        emitted = set()
        solve_deadline=deadline-assignment_reserve
        while monotonic() < solve_deadline:
            solution, best_specs = model.solve(solve_deadline - monotonic(), assignment)
            if solution is not None:
                assignment = solution
                if assignment_enabled:
                    # A full returned assignment has its own channel, even if
                    # the ordinary candidate buffers are full. All pricing is
                    # after Solve* returned to Python, before ordinary batches.
                    record={'slice':model.native_slices,'input_routes':len(best_specs)}
                    assignment_details['records'].append(record)
                    assignment_details['assignment_count']+=1
                    if stage_trace is not None:
                        for row in best_specs:stage_trace.route('NATIVE_ASSIGNMENT',row,status='DELIVERED',scope=trace_scope)
                    if candidate_stream_observer is not None:
                        candidate_stream_observer({'kind':'native_assignment','slice':model.native_slices,
                                                   'routes':deepcopy(best_specs)})
                    callback_started=monotonic()
                    try:
                        details=on_assignment(best_specs)
                        if isinstance(details,dict):
                            record.update(details)
                            assignment_details['direct_fee_reduction']=str(Decimal(assignment_details['direct_fee_reduction'])+
                                Decimal(details.get('direct_fee_reduction','0.00')))
                    except StopSearch as exc:
                        # The host may have accepted and protected a complete
                        # actual win immediately before a local/global stop.
                        # Its callback then cannot return normally; preserve
                        # the final record without swallowing that stop or
                        # performing any additional pricing/road work here.
                        details=getattr(exc,'assignment_pricing_record',None)
                        if isinstance(details,dict):
                            record.update(details)
                            assignment_details['direct_fee_reduction']=str(Decimal(assignment_details['direct_fee_reduction'])+
                                Decimal(details.get('direct_fee_reduction','0.00')))
                        record['stop_reason']=exc.reason
                        raise
                    finally:
                        record['callback_seconds']=round(monotonic()-callback_started,6)
                        assignment_details['callback_seconds']+=record['callback_seconds']
            if model.retention is not None:
                # Preserve the latest complete native assignment above before
                # budgeted batch work: a timeout may abandon ordinary proposals.
                model.retention.flush()
                model.candidate_specs = dict(model.retention.entries)
            if on_batch:
                pending = [r for key, r in model.candidate_specs.items() if key not in emitted]
                # Best assignment is evaluated first, then useful distinct
                # alternative columns from the native search trace.
                ordered = { (r['vehicle_id'], tuple(r['task_ids'])):r for r in [*best_specs, *pending] }
                if ordered:
                    if stage_trace is not None:
                        for row in ordered.values():
                            stage_trace.route('NATIVE_RETAINED', row, status='TO_PRICING_BATCH', scope=trace_scope)
                    on_batch(list(ordered.values()))
                    emitted.update(ordered)
                    emitted.intersection_update(set(model.candidate_specs) | {(r['vehicle_id'], tuple(r['task_ids'])) for r in best_specs})
            control()
            if solution is None or strategy == 'GREEDY_DESCENT':
                break
    except StopSearch as exc:
        stop = exc.reason
        if exc.reason in {'CANCELLED','WALL_BUDGET'}:
            # Carry already available diagnostics to the host without another
            # route walk, metric evaluation or publication after a global stop.
            diagnostics.update(stop_reason=exc.reason,elapsed_seconds=round(monotonic()-started,4))
            if model is not None:
                diagnostics.update(native_slices=model.native_slices,
                    solution_count=model.solution_count,route_specifications=len(model.candidate_specs))
            exc.native_diagnostics=diagnostics
            if stage_trace is not None:
                for stage in ('NATIVE_PROPOSED', 'NATIVE_RETAINED'):
                    stage_trace.mark(stage, status='INTERRUPTED', reason=exc.reason, scope=trace_scope)
                if assignment_enabled:stage_trace.mark('NATIVE_ASSIGNMENT',status='INTERRUPTED',reason=exc.reason,scope=trace_scope)
            if model is not None:
                model.dispose()
            raise
    except ImportError:
        stop = 'SOLVER_UNAVAILABLE'
    except ModelUnavailable as exc:
        stop = str(exc)
    except (RuntimeError, OverflowError, AttributeError) as exc:
        diagnostics['solver_error'] = type(exc).__name__
        stop = 'SOLVER_ERROR'
    if model is not None:
        diagnostics.update(virtual_vehicles=len(model.virtual), physical_vehicles=len({v['id'] for v, _ in model.virtual}),
                           measured_edges=sum(v is not None for v in model.metrics.values()), unknown_edges=model.unknown_edges,
                           invalid_metrics=model.invalid_metrics, solution_count=model.solution_count,
                           native_slices=model.native_slices, native_statuses=model.statuses,
                           nonadditive_region_modes=sorted(model.nonadditive_regions),
                           regional_surrogate=region_surrogate, currency_scale=model.scale,
                           money_divisor=model.money_divisor, distance_tie_break=model.distance_tie_break,
                           money_multiplier=model.money_multiplier)
        if model.retention is not None:
            diagnostics['candidate_retention'] = model.retention.diagnostics()
    rows = list(model.candidate_specs.values()) if model is not None else []
    if model is not None:
        rows = list({(r['vehicle_id'], tuple(r['task_ids'])):r for r in [*rows, *best_specs]}.values())
    diagnostics.update(stop_reason=stop, route_specifications=len(rows), elapsed_seconds=round(monotonic() - started, 4))
    if stage_trace is not None:
        for row in rows:
            stage_trace.route('NATIVE_RETAINED', row, status='RETURNED', scope=trace_scope)
        status = 'COMPLETE' if stop == 'SEARCH_COMPLETED' else 'INTERRUPTED' if model is not None else 'SKIPPED'
        for stage in ('NATIVE_PROPOSED', 'NATIVE_RETAINED'):
            stage_trace.mark(stage, status=status, reason=stop, scope=trace_scope)
        if assignment_enabled:stage_trace.mark('NATIVE_ASSIGNMENT',status=status,reason=stop,scope=trace_scope)
    if model is not None:
        model.dispose()
    return rows, diagnostics


def optimize_ortools(body, road, budget_seconds=15, seed=0, cancelled=lambda: False,
                     *, initial_result=None, strategy='GUIDED_LOCAL_SEARCH',
                     first_strategy='PARALLEL_CHEAPEST_INSERTION', edge_lookup=None,
                     vehicle_independent=None, region_surrogate='PER_STORE', common_penalty_multiplier=1):
    """Comparable benchmark entry with compact columns and bounded raw geometry.

    The shared evaluator materializes evidence only for improving complete
    allocations. Thousands of alternative native routes never retain thousands
    of duplicate polylines in memory.
    """
    from app.route_search import JointSearch
    from app.route_selection import select_candidate_routes
    factor = _penalty_factor(common_penalty_multiplier)
    started = monotonic()
    data = normalize(body)
    search = JointSearch(data, road, budget_seconds, seed, cancelled, initial_result)
    native_diagnostics = {}
    def batch(rows):
        search.control()
        greedy = []; greedy_ids = set(); greedy_cars = set()
        def pack():
            search.control()
            selected, _ = select_candidate_routes(data, list(search.pool.values()), search.best_routes,
                                                   min(.25, search.remaining()), cancelled)
            search.consider(selected)
        for index, spec in enumerate(rows):
            candidate = search.evaluate(spec['vehicle_id'], spec['task_ids'])
            if candidate is None:
                continue
            if candidate['vehicle_id'] not in greedy_cars and not greedy_ids.intersection(candidate['task_ids']):
                greedy_cars.add(candidate['vehicle_id']); greedy_ids.update(candidate['task_ids']); greedy.append(candidate)
                search.consider(greedy)
            search.quick_exchange(candidate)
            if index % 32 == 31:
                pack()
        pack()
    assignment_options={}
    if getattr(getattr(road,'deep_experiments',None),'complete_assignment_pricing',False):
        from app.route_assignment_pricing import assignment_callback
        assignment_options={'complete_assignment_pricing':True,
                            'on_assignment':assignment_callback(search,scope_ids=data['tasks'])}
    stop = 'SEARCH_COMPLETED'
    try:
        search.control()
        if edge_lookup is None:
            edge_lookup = _default_lookup(road)
            independent = True
        else:
            independent = bool(vehicle_independent)
        _, native_diagnostics = generate_ortools_candidates(data, edge_lookup,
                    max(0, search.remaining() - min(.5, budget_seconds * .1)), seed, cancelled,
                    initial_routes=search.best_routes, vehicle_independent=independent,
                    strategy=strategy, first_strategy=first_strategy, region_surrogate=region_surrogate,
                    common_penalty_multiplier=factor, on_batch=batch,
                    candidate_retention=getattr(getattr(road, 'deep_experiments', None), 'native_candidate_retention', 'first'),
                    candidate_stream_observer=getattr(road, 'native_candidate_stream_observer', None),
                    **({'stage_trace': road.route_stage_trace} if getattr(road, 'route_stage_trace', None) is not None else {}),
                    **assignment_options)
        stop = native_diagnostics['stop_reason']
    except StopSearch as exc:
        stop = exc.reason
        if isinstance(getattr(exc,'native_diagnostics',None),dict):
            native_diagnostics=exc.native_diagnostics
    except ModelUnavailable as exc:
        stop = str(exc)
    best = search.best
    best['algorithm'] = {'mode': 'ORTOOLS_MEASURED_HYBRID', 'seed': seed,
                         'stop_reason': stop, 'strategy': strategy,
                         'surrogate_common_penalty_multiplier':str(factor),
                         'elapsed_seconds': round(monotonic() - started, 4),
                         'budget_seconds': budget_seconds, 'candidate_count': search.evaluations,
                         'distinct_route_count': len(search.pool), 'rejected_candidates': search.failures,
                         'raw_geometry_cache_size': len(search.raw),
                         'optimality_proven': False, 'infeasibility_proven': False,
                         'cost_objective': 'min_unassigned_then_total_fee_then_distance',
                         'initial_plan_retained_or_improved': search.initial_accepted,
                         'road_stats': road.stats(), 'routing_diagnostics': native_diagnostics}
    return best
