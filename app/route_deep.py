"""Daily, bounded multi-seed search, route pooling and fresh-road verification.

All runs belong to the current frozen input. A job may reuse road measurements,
but never another group's demand quantities or a historical experiment's fee.
"""
from collections import OrderedDict
from copy import deepcopy
from math import ceil
from math import isfinite
from dataclasses import asdict
from decimal import Decimal
from itertools import chain
import time

from app.api import ApiError
from app.road_service import DrivingRoad
from app.planning_inputs import content_hash
from app.route_metrics import evaluate_candidate
from app.route_ortools import optimize_ortools
from app.route_diagnostics import SearchDiagnostics
from app.source_provenance import collect_source_provenance
from app.route_experiments import DeepExperiments
from app.route_portfolio import optimize_alns, optimize_hybrid
from app.route_verification import (complete, recipe, replay, result_rank,
                                    validate_driving)
from app.routing_engine import StopSearch, assemble, check_result, normalize, optimize_legacy

MAX_OBSERVED_RECIPES = 10000
PYVRP_GROUP_SECONDS = 20.


class _Budget:
    def __init__(self, road, seconds, cancelled, wall_seconds=None):
        if not isfinite(float(seconds)) or float(seconds) < 0:
            raise ValueError('Compute budget must be finite and nonnegative')
        self.road, self.seconds, self.cancelled = road, max(0., float(seconds)), cancelled
        self.started = time.monotonic()
        self.io_started = float(getattr(road, 'io_seconds', 0))
        self.wall_seconds = (self.seconds + min(1800., max(120., self.seconds * 2))
                             if wall_seconds is None else float(wall_seconds))
        if getattr(road, 'wall_deadline', None) is not None:
            self.wall_seconds = min(self.wall_seconds, max(0., road.wall_deadline - self.started))
        if not isfinite(self.wall_seconds) or self.wall_seconds < 0:
            raise ValueError('Wall budget must be finite and nonnegative')

    def elapsed(self):
        return time.monotonic() - self.started

    def compute(self):
        elapsed = self.elapsed()
        io = max(0., float(getattr(self.road, 'io_seconds', 0)) - self.io_started)
        return max(0., elapsed - min(elapsed, io))

    def control(self, *, compute=True):
        if self.cancelled():
            raise StopSearch('CANCELLED')
        if self.elapsed() >= self.wall_seconds:
            raise StopSearch('WALL_BUDGET')
        if compute and self.compute() >= self.seconds:
            raise StopSearch('TIME_BUDGET')


def _partial_legal(data, result):
    try:
        proof = check_result(data, result)
        return all(i['code'] in {'DRIVING_REFERENCE', 'UNASSIGNED'} for i in proof['issues'])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def _validated_without_io(data, result, road):
    """A cancelled job cannot call an old lower quote a current incumbent."""
    if not complete(data, result):
        return None
    copied = deepcopy(result)
    try:
        lookup = getattr(road, 'known_segment', None)
        for route in copied['routes']:
            if callable(lookup):
                metric = evaluate_candidate(data, route['vehicle_id'], route['task_ids'], road)
                if metric is None or metric['errors'] or any(metric[k] != route[k] for k in ('fee', 'meters', 'seconds', 'pieces')):
                    return None
            origin = data['warehouses'][data['vehicles'][route['vehicle_id']]['warehouse_id']]
            for task_id, segment in zip(route['task_ids'], route['segments']):
                destination = data['tasks'][task_id]
                if callable(lookup):
                    metric = lookup(origin, destination)
                    if metric is None or (metric['meters'], metric['seconds']) != (segment['meters'], segment['seconds']):
                        return None
                    if metric.get('received_at') is not None and metric['received_at'] != segment.get('received_at'):
                        return None
                elif segment['source'] == 'FIXTURE':
                    fixture = getattr(road, 'fixture', None)
                    fingerprint = getattr(road, 'input_hash', None)
                    if fixture is not None:
                        if segment['request'].get('fixture_hash') != content_hash(fixture['roads']):
                            return None
                    elif fingerprint is not None:
                        if segment['request'].get('input_hash') != fingerprint:
                            return None
                    else:
                        return None
                else:
                    return None
                if segment['source'] == 'DRIVING':
                    received = segment.get('received_at')
                    if getattr(road, 'fresh_only', False):
                        key = content_hash(DrivingRoad.reference_request(origin, destination))
                        actual = road.batch_receipt(key)
                        if (actual is None or actual['received_at'] < road.batch_started_at
                                or any(segment.get(k) != actual[k] for k in ('received_at','meters','seconds','polyline'))
                                or actual['evidence_hash'] not in (segment.get('evidence_hash'), segment.get('provider_evidence_hash'))):
                            return None
                    if (not isinstance(received, int) or isinstance(received, bool)
                            or not 0 <= int(time.time()) - received < 7 * 86400
                            or not DrivingRoad.valid_reference(segment, allow_expired=True, allow_historical_status=True)):
                        return None
                    if int(time.time()) - received >= 3600 and not getattr(road, 'fresh_only', False):
                        segment['time_status'] = 'HISTORICAL_TRAFFIC_REFERENCE'
                        segment['notice'] = '已有实路参考；保留原采集时间，本次未更新实时道路'
                        segment['evidence_hash'] = content_hash({k: v for k, v in segment.items() if k != 'evidence_hash'})
                origin = destination
        copied['check'] = check_result(data, copied)
        return copied if complete(data, copied) else None
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None


def _offline_validation(data, result, road):
    if not complete(data, result):
        raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '在时限内未找到覆盖全部门店的可行方案，请核对车辆、收货时间和报价资料后重试。')
    segments = [s for route in result['routes'] for s in route['segments']]
    driving = [s for s in segments if s['source'] == 'DRIVING']
    if not segments:
        mode = 'COMMON_ONLY_VALIDATED'
    elif driving:
        mode = 'OFFLINE_REFERENCE_VALIDATED'
    elif any(s['source'] == 'LIVE' for s in segments):
        mode = 'TRUCK_VALIDATED'
    else:
        mode = 'FIXTURE_VALIDATED'
    dates = [s['received_at'] for s in driving if isinstance(s.get('received_at'), int)]
    return {'mode': mode, 'fresh': False, 'selected_segments': len(segments),
            'actual_calls': 0, 'total_job_calls': road.stats().get('calls', 0),
            'source_count': len(segments), 'selected_subset_of_collected': False,
            'oldest_received_at': min(dates, default=None), 'newest_received_at': max(dates, default=None),
            'requires_truck_review': bool(driving)}


def _pre_selection_macros(data, road, incumbent, budget, seed, cancelled):
    """Add long economic bundles within the caller-owned compute allowance.

    A remote trunk may lose money for its first ten stops yet beat common for
    nineteen. New-car activation and full compatible-group proposals use only
    measured metrics. The one global column selector below performs any joint
    fleet repartition; this generator never starts a native selector itself.
    """
    from app.route_macro import seed_long_routes
    from app.route_cost_cleanup import polish_route_orders
    from app.route_portfolio import _phase
    from app.route_search import JointSearch

    started = time.monotonic()
    compute_before = budget.compute()
    ceiling = budget.seconds * .83
    seconds = min(3., budget.seconds * .06, max(0., ceiling - compute_before) * .4)
    fragment_policy = getattr(getattr(road, 'deep_experiments', None), 'fragment_merging', False)
    details = {'seed_generator_policy':'OPEN_FRAGMENT_MERGING' if fragment_policy else 'LONG_TRUNK_MACROS',
               'fragment_merging_skipped_reason':None if fragment_policy else 'SWITCH_DISABLED',
               'before_complete':complete(data,incumbent),
               'budget_seconds': seconds, 'before_fee': incumbent['total_fee'],
               'after_fee': incumbent['total_fee'], 'candidate_count': 0,
               'new_network_calls': 0, 'methods': [], 'global_optimality_proven': False}
    if cancelled():
        raise StopSearch('CANCELLED')
    if (seconds < .05 or not callable(getattr(road, 'known_segment', None))
            or (not complete(data, incumbent) and not fragment_policy)):
        details.update(skipped=True, stop_reason='NO_UNUSED_MACRO_BUDGET_OR_COMPLETE_START',
                       elapsed_seconds=round(time.monotonic() - started, 4))
        return incumbent, details, []
    capped = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    original_limit = getattr(road, 'call_limit', None)
    calls_before = getattr(road, 'calls', 0)
    if capped:
        road.call_limit = min(road.calls, original_limit) if original_limit is not None else road.calls
    search = None
    stopped = 'SEARCH_COMPLETED'
    try:
        search = JointSearch(data, road, seconds, seed, cancelled, incumbent)
        local_control, local_remaining = search.control, search.remaining
        def control():
            budget.control()
            if budget.compute() >= ceiling:
                raise StopSearch('MACRO_BUDGET')
            local_control()
        search.control = control
        search.remaining = lambda: max(0., min(local_remaining(), ceiling - budget.compute()))
        for car in sorted(data['vehicles'].values(), key=lambda v: (v['warehouse_id'], -v['max_pieces'], v['id'])):
            search.control()
            if car['available']:
                search.cars.append(car)

        def activate():
            original_cars = {r['vehicle_id'] for r in search.best_routes}
            before = search.best['total_fee']
            methods = [_phase(search, 'grow_common_trunks', seconds * .5 * .55,
                              lambda: seed_long_routes(search, scope='common'))]
            added = {r['vehicle_id'] for r in search.best_routes} - original_cars
            if added and search.remaining() > .005:
                methods.append(_phase(search, 'polish_new_trunks', search.remaining(),
                                      lambda: polish_route_orders(search, added)))
            return {'before_fee': before, 'after_fee': search.best['total_fee'], 'methods': methods}

        if fragment_policy:
            from app.route_fragment_merging import generate_fragment_routes, greedy_fragment_pack
            def run_phase(name, allowance, action, phase_details=None):
                # Record entry before work: a global/local stop can arrive
                # after a complete improvement, before _phase returns its row.
                phase_started, phase_compute = time.monotonic(), search.compute_elapsed()
                before_fee = search.best['total_fee']
                row = {'method':name, 'action_entered':False,
                       'complete_before':complete(data,search.best)}
                if phase_details is not None:
                    row['details'] = phase_details
                details['methods'].append(row)
                def entered():
                    row['action_entered'] = True
                    return action()
                try:
                    row.update(_phase(search,name,allowance,entered))
                except StopSearch as exc:
                    row.update(stop_reason=exc.reason, recorded_on_interruption=True,
                        elapsed_seconds=round(time.monotonic()-phase_started,4),
                        compute_seconds=round(search.compute_elapsed()-phase_compute,4),
                        before_fee=before_fee, best_fee=search.best['total_fee'],
                        fee_reduction=str(Decimal(before_fee)-Decimal(search.best['total_fee'])),
                        dedicated_vehicles=len(search.best_routes),
                        unassigned=len(search.best['unassigned']), candidate_count=len(search.pool))
                    raise
                finally:
                    row['complete_after'] = complete(data,search.best)

            run_phase('fragment_merge_orders',seconds*.65,
                lambda:generate_fragment_routes(search,enabled=True,seconds=search.remaining(),seed=seed))
            selection_details = {}
            def choose_fragments():
                proposal=greedy_fragment_pack(search,seconds=search.remaining()*.5)
                public=selection_details
                public.update({key:value for key,value in proposal.items() if key!='routes'})
                public['complete_proposal_considered']=False
                if proposal['complete_proposal']:
                    search.consider(proposal['routes'])
                    public['complete_proposal_considered']=True
                public['physical_pack_attempted']=False
                if search.remaining()>=.075:
                    public['physical_pack_attempted']=True
                    search.pack()
                return public
            run_phase('fragment_physical_selection',search.remaining(),choose_fragments,selection_details)
        else:
            details['methods'].append(_phase(search, 'activate_unused_cars', seconds * .5, activate))
            details['methods'].append(_phase(search, 'compatible_long_columns', seconds * .5,
                                            lambda: seed_long_routes(search, scope='compatible')))
    except StopSearch as exc:
        # Return a newly proved complete win before Deep propagates the stop.
        # Otherwise retain the existing immediate cancellation semantics.
        if exc.reason in {'CANCELLED','WALL_BUDGET'} and not (search is not None and
                complete(data,search.best) and result_rank(search.best)<result_rank(incumbent)):
            raise
        stopped = exc.reason
    finally:
        if capped:
            road.call_limit = original_limit
    result, recipes = incumbent, []
    if search is not None:
        if complete(data, search.best) and result_rank(search.best) < result_rank(incumbent):
            result = search.best
        # Protect late macros from a full source buffer. Nonprofitable optional
        # prefixes must keep growing, but need not inflate the final model.
        useful = [row for row in search.pool.values() if row['_hard'] or row['_net'] < 0]
        useful.sort(key=lambda row: (-row['_hard'], row['_net'], row['meters'], tuple(row['task_ids'])))
        seen = set()
        for row in useful:
            identity = tuple(row['task_ids'])
            if identity not in seen:
                seen.add(identity)
                recipes.append({'vehicle_id': row['vehicle_id'], 'task_ids': list(identity)})
                if (not getattr(road, 'deadline_limited_candidates', False)
                        and len(recipes) >= MAX_OBSERVED_RECIPES):
                    break
        details.update(candidate_count=search.evaluations, macro_order_count=len(recipes),
                       after_complete=complete(data,search.best))
    details.update(after_fee=result['total_fee'], skipped=False, stop_reason=stopped,
                   elapsed_seconds=round(time.monotonic() - started, 4),
                   compute_seconds=round(max(0., budget.compute() - compute_before), 4),
                   new_network_calls=max(0, getattr(road, 'calls', 0) - calls_before))
    return result, details, recipes


def _post_selection_small_groups(data, road, incumbent, budget, seed, cancelled):
    """Use only unused column-selection time, once, without new provider calls.

    Repeated early enumeration competes with the main heuristics for their
    fixed allowance. This optional phase starts after those runs and pooling,
    keeps their complete incumbent, and leaves the final 17% verification
    reserve intact. The caller's observer also retains its route recipes.
    """
    from app.route_portfolio import _phase
    from app.route_search import JointSearch
    from app.route_small_groups import seed_small_groups

    started = time.monotonic()
    compute_before = budget.compute()
    ceiling = budget.seconds * .83
    seconds = min(3., budget.seconds * .05, max(0., ceiling - compute_before))
    diagnostics = {'budget_seconds': seconds, 'compute_before': compute_before,
                   'pre_validation_compute_limit': ceiling, 'before_fee': incumbent['total_fee'],
                   'after_fee': incumbent['total_fee'], 'candidate_count': 0,
                   'new_network_calls': 0, 'methods': [], 'global_optimality_proven': False}
    if cancelled():
        raise StopSearch('CANCELLED')
    if seconds < .05 or not callable(getattr(road, 'known_segment', None)):
        diagnostics.update(stop_reason='NO_UNUSED_SELECTION_BUDGET' if seconds < .05 else 'MATRIX_UNAVAILABLE',
                           skipped=True, elapsed_seconds=round(time.monotonic() - started, 4), compute_seconds=0.)
        return incumbent, diagnostics
    original_limit = getattr(road, 'call_limit', None)
    cap_io = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    calls_before = road.stats().get('calls', 0)
    if cap_io:
        road.call_limit = min(road.calls, original_limit) if original_limit is not None else road.calls
    search = None
    stopped = 'SEARCH_COMPLETED'
    try:
        search = JointSearch(data, road, seconds, seed, cancelled, incumbent)
        local_control, local_remaining = search.control, search.remaining
        def control():
            budget.control()
            if budget.compute() >= ceiling:
                raise StopSearch('SMALL_GROUP_BUDGET')
            local_control()
        search.control = control
        search.remaining = lambda: max(0., min(local_remaining(), ceiling - budget.compute()))
        native_pack = search.pack
        def bounded_pack():
            # JointSearch's native selector has a 50 ms minimum allowance.
            # Do not start it with only a few milliseconds before the reserve.
            if search.remaining() >= .075:
                return native_pack()
        search.pack = bounded_pack
        # This read-only phase needs no provider-key preflight. Every proposed
        # physical car still passes capacity/warehouse/evidence checks when the
        # host materializes it; no credentials are needed to price known roads.
        for car in sorted(data['vehicles'].values(), key=lambda v: (v['warehouse_id'], -v['max_pieces'], v['id'])):
            search.control()
            if car['available']:
                search.cars.append(car)
        from app.route_cost_cleanup import improve_cost_cleanup, polish_route_orders
        before_polish = search.best['total_fee']
        polished = _phase(search, 'polish_complete_route_orders', seconds * .35,
                          lambda: polish_route_orders(search, {r['vehicle_id'] for r in search.best_routes}))
        polished.update(before_fee=before_polish, after_fee=search.best['total_fee'])
        diagnostics['methods'].append(polished)
        before_cleanup = search.best['total_fee']
        cleanup = _phase(search, 'deterministic_cost_cleanup', seconds * .25,
                         lambda: improve_cost_cleanup(search))
        cleanup.update(before_fee=before_cleanup, after_fee=search.best['total_fee'])
        diagnostics['methods'].append(cleanup)
        # Reuse time the preceding optional passes did not spend. The host's
        # remaining allowance already respects both the post phase and 83%
        # ceiling; leave its final quarter for joint selection.
        diagnostics['methods'].append(_phase(search, 'small_compatible_groups', search.remaining() * .75,
                                              lambda: seed_small_groups(search)))
        if search.evaluations and search.remaining() >= .075:
            diagnostics['methods'].append(_phase(search, 'small_group_joint_selection', search.remaining(), search.pack))
    except StopSearch as exc:
        # Return a newly proved complete win before Deep propagates the stop.
        # Otherwise retain the existing immediate cancellation semantics.
        if exc.reason in {'CANCELLED','WALL_BUDGET'} and not (search is not None and
                complete(data,search.best) and result_rank(search.best)<result_rank(incumbent)):
            raise
        stopped = exc.reason
    finally:
        if cap_io:
            road.call_limit = original_limit
    result = incumbent
    if search is not None:
        # A local deadline, missing evidence or refreshed-start policy must not
        # discard the complete pre-existing allocation already held by deep.
        if _partial_legal(data, search.best) and result_rank(search.best) < result_rank(incumbent):
            result = search.best
        diagnostics.update(candidate_count=search.evaluations, distinct_route_count=len(search.pool))
    diagnostics.update(after_fee=result['total_fee'], skipped=False, stop_reason=stopped,
                       elapsed_seconds=round(time.monotonic() - started, 4),
                       compute_seconds=round(max(0., budget.compute() - compute_before), 4),
                       new_network_calls=road.stats().get('calls', 0) - calls_before)
    return result, diagnostics


def optimize_deep(body, road, budget_seconds=300, seed=0, cancelled=lambda: False,
                  *, initial_result=None, progress=None, experiments=None,
                  wall_budget_seconds=None, deadline_limited_candidates=None):
    """Saveable results are complete and independently verified, never estimates.

    The compute allowance covers all serial search/selection phases. Provider
    pacing/network wait is measured separately and bounded by a wall allowance.
    Small allowances still run the same stages with shorter slices; they do not
    claim the depth or proof of a long multi-hour experiment.
    """
    data = normalize(body)
    budget = _Budget(road, budget_seconds, cancelled, wall_budget_seconds)
    use_live_batch = isinstance(road, DrivingRoad) and getattr(road, 'fresh_only', False)
    # Daily planning uses the measured quote-group phase alongside diagnostics
    # and road discovery. An explicit experiment configuration keeps each
    # phase independently controllable in research comparisons.
    switches = (DeepExperiments(fragment_merging=False, complete_assignment_pricing=False,
                               joint_reoptimization=False, scalable_joint_search=False,
                               stage_budget_reservation=False, critical_edge_collection=True,
                               pyvrp_group_generation=True,
                               pyvrp_indexed_current_receipts=bool(use_live_batch
                                   and getattr(road, 'production_planning', False)
                                   and getattr(getattr(road, 'settings', None), 'road_calls_enabled', False)),
                               generation_selection_loop=(budget.seconds >= 600 and len(data['tasks']) > 8))
                if experiments is None else DeepExperiments.parse(experiments))
    diagnostics = SearchDiagnostics(budget, len(data['tasks'])) if switches.diagnostics else None
    notify = progress or (lambda value: None)
    stages = []
    trace = getattr(road, 'route_stage_trace', None)
    observed = OrderedDict()
    observation_count = dropped_observations = 0
    duplicate_observations = 0
    if deadline_limited_candidates is None:
        deadline_limited_candidates = experiments is None
    max_recipes = None if deadline_limited_candidates else MAX_OBSERVED_RECIPES
    retention = None
    if max_recipes is not None and switches.candidate_retention != 'first':
        from app.route_retention import CandidateRetention
        retention = CandidateRetention(switches.candidate_retention, max_recipes, data=data,
                                       control=budget.control)
    previous_observer = getattr(road, 'candidate_observer', None)
    had_observer = hasattr(road, 'candidate_observer')
    current_progress = {'stage': 'MULTI_START', 'round_index': 0, 'round_total': 0,
                        'seed': seed, 'candidate_count': 0}
    baseline_common = {i for i, task in data['tasks'].items() if task['common_unit_price'] is not None}
    best = assemble(data, [], baseline_common, set(data['tasks']) - baseline_common)
    # A prior job supplies route structure only. Fresh replay prices it later.
    initial = (_validated_without_io(data, initial_result, road) if initial_result is not None
               and not (getattr(road, 'fresh_only', False) and initial_result.get('routes')) else None)
    if initial is not None and result_rank(initial) < result_rank(best):
        best = initial
    best_complete = deepcopy(best) if complete(data, best) else None
    if diagnostics is not None:
        diagnostics.plan(best, 'CURRENT_INPUT_BASELINE_OR_CHECKED_START')
    complete_allocations = []
    complete_seen = set()
    rounds = []
    stopped = 'SEARCH_COMPLETED'
    validation = None
    small_details = None
    macro_details = None
    macro_recipes = []
    optional_candidate_count = 0
    previous_job_attributes = {name: (hasattr(road, name), getattr(road, name, None))
                               for name in ('search_diagnostics', 'deep_experiments',
                                            'priced_candidate_observer', 'deadline_limited_candidates')}
    eligible_count = sum(bool(task['signature']) and any(
        car['available'] and car['warehouse_id'] == task['warehouse_id']
        and car['max_pieces'] >= task['pieces'] for car in data['vehicles'].values())
        for task in data['tasks'].values())
    original_call_limit = getattr(road, 'call_limit', None)
    calls_at_entry = road.stats().get('calls', 0)
    reserve_calls = (isinstance(road, DrivingRoad) and original_call_limit is not None
                     and original_call_limit > 0
                     and bool(getattr(road.settings, 'road_calls_enabled', False)))
    remaining_calls = (max(0, original_call_limit - calls_at_entry)
                       if original_call_limit is not None else None)
    reserved_calls = min(remaining_calls, eligible_count) if reserve_calls else 0
    search_call_limit = original_call_limit - reserved_calls if reserve_calls else original_call_limit
    call_budget = {'enabled': reserve_calls, 'total_limit': original_call_limit,
                   'eligible_task_count': eligible_count, 'calls_at_entry': calls_at_entry,
                   'search_call_limit': search_call_limit, 'reserved_for_validation': reserved_calls,
                   'reservation_policy': 'ONE_ATTEMPT_PER_ELIGIBLE_TASK'}

    def interrupted():
        return cancelled() or budget.elapsed() >= budget.wall_seconds

    prior_network_control = getattr(road, 'network_control', None)
    final_wall_reserve = min(180., budget.wall_seconds * .20) if use_live_batch else 0.

    def search_time_guard():
        if use_live_batch and budget.elapsed() >= budget.wall_seconds - final_wall_reserve:
            raise StopSearch('SEARCH_WALL_RESERVE')

    def search_network_control():
        if prior_network_control is not None:
            prior_network_control()
        search_time_guard()

    def emit(update):
        if 'stage' in update and 'optimization_phase' not in update:
            current_progress.pop('optimization_phase', None)
        current_progress.update(update)
        if diagnostics is not None:
            diagnostics.stage = current_progress.get('optimization_phase', current_progress['stage'])
        current_progress.update(elapsed_seconds=round(budget.elapsed(), 3),
                                compute_seconds=round(budget.compute(), 3),
                                candidate_count=len(observed))
        if best_complete is None:
            current_progress['best_fee'] = None
        elif 'best_fee' not in update:
            current_progress['best_fee'] = best_complete['total_fee'] if best_complete is not None else None
        notify(deepcopy(current_progress))

    def observe(spec):
        nonlocal observation_count, dropped_observations, duplicate_observations
        if interrupted():
            raise StopSearch('CANCELLED' if cancelled() else 'WALL_BUDGET')
        observation_count += 1
        if trace is not None:
            trace.route('DEEP_OBSERVED', spec, fee=spec.get('fee'), compute_seconds=budget.compute())
        key = (spec['vehicle_id'], tuple(spec['task_ids']))
        if retention is not None:
            retention.observe(spec)
        elif key not in observed:
            if max_recipes is not None and len(observed) >= max_recipes:
                dropped_observations += 1
            else:
                observed[key] = {'vehicle_id': key[0], 'task_ids': list(key[1])}
        else:
            duplicate_observations += 1
        stream_observer = getattr(road, 'candidate_stream_observer', None)
        if callable(stream_observer):
            stream_observer({'kind': 'candidate', 'vehicle_id': key[0], 'task_ids': list(key[1]),
                             'retention_fee':spec.get('retention_fee', spec.get('fee'))})
        if callable(previous_observer):
            previous_observer(spec)

    def observe_priced(spec):
        stream_observer = getattr(road, 'candidate_stream_observer', None)
        if callable(stream_observer):
            stream_observer({'kind':'priced_candidate', **spec})
        prior = previous_job_attributes['priced_candidate_observer'][1]
        if callable(prior):
            prior(spec)

    def flush_observations():
        if retention is not None:
            retention.flush()
            observed.clear()
            observed.update(retention.entries)
        if trace is not None:
            for spec in observed.values():
                trace.route('DEEP_RETAINED', spec, compute_seconds=budget.compute())
        stream_observer = getattr(road, 'candidate_stream_observer', None)
        if callable(stream_observer):
            stream_observer({'kind': 'flush'})

    def keep(result):
        nonlocal best, best_complete
        if not _partial_legal(data, result):
            return False
        if result_rank(result) < result_rank(best):
            best = deepcopy(result)
        if complete(data, result) and (best_complete is None or result_rank(result) < result_rank(best_complete)):
            best_complete = deepcopy(result)
        if complete(data, result):
            if diagnostics is not None:
                diagnostics.plan(result, 'DEEP_KEEP')
            allocation = recipe(result)
            identity = content_hash(allocation)
            if identity not in complete_seen:
                complete_seen.add(identity)
                complete_allocations.append(allocation)
                if trace is not None:
                    for spec in allocation['routes']:
                        trace.route('COMPLETE_PROTECTED', spec, compute_seconds=budget.compute())
                stream_observer = getattr(road, 'candidate_stream_observer', None)
                if callable(stream_observer):
                    stream_observer({'kind': 'allocation', 'allocation': deepcopy(allocation)})
        for route in result['routes']:
            if not interrupted():
                observe(route)
        return True

    def run_optional_refinement(method, stage):
        nonlocal optional_candidate_count
        try:
            strengthened, details = _optional_refinement(data, road, best_complete or best,
                budget, seed, interrupted, method, recipes=chain(macro_recipes, observed.values()))
        except StopSearch as exc:
            details = getattr(exc, 'optional_refinement_diagnostics', None)
            if details is not None:
                optional_candidate_count += details.get('candidate_count', 0)
                stages.append({'stage': stage, 'total_fee': (best_complete or best)['total_fee'],
                               'elapsed_seconds': details['elapsed_seconds'], 'details': details})
            raise
        keep(strengthened)
        optional_candidate_count += details.get('candidate_count', 0)
        stages.append({'stage': stage, 'total_fee': strengthened['total_fee'],
                       'elapsed_seconds': details['elapsed_seconds'], 'details': details})
        if details.get('stop_reason') in {'CANCELLED', 'WALL_BUDGET'}:
            # Protect the already checked whole-day improvement before obeying
            # the original stop. Logging must never resume a cancelled phase.
            budget.control()
            raise StopSearch(details['stop_reason'])

    def run_pyvrp_group():
        nonlocal optional_candidate_count
        stage = 'PYVRP_GROUP_GENERATION'
        before = best_complete or best
        compute_started = budget.compute()
        started = budget.elapsed()
        compute_ceiling = budget.seconds * .92
        remaining_wall = budget.wall_seconds - started - final_wall_reserve
        details = {'skipped': True, 'candidate_count': 0, 'budget_seconds': PYVRP_GROUP_SECONDS}
        try:
            if best_complete is None:
                details['stop_reason'] = 'NO_COMPLETE_INCUMBENT'
                return
            if min(compute_ceiling - compute_started, remaining_wall) < PYVRP_GROUP_SECONDS:
                details['stop_reason'] = 'INSUFFICIENT_PHASE_BUDGET'
                return

            def phase_control():
                budget.control()
                search_time_guard()
                if budget.compute() >= compute_ceiling:
                    raise StopSearch('PYVRP_PHASE_BUDGET')

            from app.route_pyvrp_tail import improve
            emit({'stage': 'COLUMN_SELECTION', 'optimization_phase': stage,
                  'message': '正在按相容报价组生成线路并复核整单费用'})
            proposal, details = improve(body, road, before,
                seconds=PYVRP_GROUP_SECONDS, seed=seed, control=phase_control, cancelled=cancelled)
            # Commit a checked improvement to the in-memory incumbent before
            # polling again. Worker publication still enforces the hard wall.
            if Decimal(proposal['total_fee']) < Decimal(before['total_fee']) and complete(data, proposal):
                keep(proposal)
            phase_control()
        except StopSearch as exc:
            details = getattr(exc, 'pyvrp_tail_diagnostics', details)
            details.update(skipped=False, stop_reason=exc.reason)
            if exc.reason != 'PYVRP_PHASE_BUDGET':
                raise
        finally:
            optional_candidate_count += details.get('candidate_count', 0)
            after = best_complete or best
            stages.append({'stage': stage, 'before_fee': before['total_fee'],
                           'after_fee': after['total_fee'], 'total_fee': after['total_fee'],
                           'fee_reduction': str(Decimal(before['total_fee']) - Decimal(after['total_fee'])),
                           'budget_seconds': PYVRP_GROUP_SECONDS,
                           'elapsed_seconds': round(budget.elapsed() - started, 6),
                           'compute_seconds': round(budget.compute() - compute_started, 6),
                           'details': details})

    # Each source run uses the current job's isolated road object. No shared
    # Settings or module-global algorithm functions are changed between seeds.
    road.candidate_observer = observe
    needs_prices = (callable(getattr(road, 'candidate_stream_observer', None)) or
                    callable(previous_job_attributes['priced_candidate_observer'][1]))
    road.priced_candidate_observer = observe_priced if needs_prices else None
    road.search_diagnostics = diagnostics
    road.deep_experiments = switches
    road.deadline_limited_candidates = deadline_limited_candidates
    try:
        if use_live_batch:
            road.network_control = search_network_control
        if reserve_calls:
            # Fresh validation cannot reuse search receipts. Keep one attempt
            # per eligible task; retries and repair bridges may still need more.
            # With a smaller total allowance, search can still use known roads.
            road.call_limit = search_call_limit
        if switches.critical_edge_collection:
            from app.route_edge_discovery import collect_promising_edges
            emit({'stage':'CRITICAL_EDGE_COLLECTION', 'message':'正在补采有潜力线路的必要道路'})
            edge_details = {}
            edge_stage = {'stage':'CRITICAL_EDGE_COLLECTION', 'elapsed_seconds':0., 'details':edge_details}
            stages.append(edge_stage)
            edge_started = time.monotonic()
            try:
                prior = initial_result if (getattr(road, 'fresh_only', False)
                    and initial_result is not None and complete(data, initial_result)) else best_complete
                edge_details.update(collect_promising_edges(data, road, budget.control,
                    incumbent=prior, phase_wall_seconds=max(0., budget.wall_seconds*.25-budget.elapsed()),
                    diagnostics=edge_details))
                if prior is initial_result and prior is not None:
                    replay_started = budget.elapsed()
                    replay_limit = max(1., budget.wall_seconds * .035)
                    def replay_control():
                        budget.control()
                        if budget.elapsed() - replay_started >= replay_limit:
                            raise StopSearch('INITIAL_REPLAY_BUDGET')
                    try:
                        refreshed = replay(data, recipe(prior), road, replay_control)
                        if complete(data, refreshed):
                            keep(refreshed)
                            edge_details['initial_plan_repriced_fee'] = refreshed['total_fee']
                    except StopSearch as exc:
                        if exc.reason != 'INITIAL_REPLAY_BUDGET':
                            raise
                        edge_details['initial_replay_stop_reason'] = exc.reason
            finally:
                edge_stage['elapsed_seconds'] = round(time.monotonic()-edge_started,6)
        # Construct the first search seed from the current input, without an
        # external saved plan. Its real compute belongs to the 64% allowance.
        emit({'stage': 'MULTI_START', 'message': '构建初始配送方案',
              'round_index': 0, 'round_total': 0})
        before_seed_rank = result_rank(best_complete or best)
        seed_before_fee = (best_complete or best)['total_fee']
        seed_before_complete = best_complete is not None
        strengthened, macro_details, macro_recipes = _pre_selection_macros(
            data, road, baseline_result(data), budget, seed, interrupted)
        seed_complete = complete(data, strengthened)
        seed_rank = result_rank(strengthened) if seed_complete else None
        keep(strengthened)
        flush_observations()
        actual = best_complete or best
        macro_details = {**macro_details, 'seed_origin': 'CURRENT_INPUT',
                         'seed_complete': seed_complete,
                         'seed_accepted': (seed_complete and result_rank(actual) < before_seed_rank
                                           and result_rank(actual) == seed_rank),
                         'seed_fee': strengthened['total_fee'] if seed_complete else None,
                         'global_before_fee':seed_before_fee, 'global_after_fee':actual['total_fee'],
                         'direct_fee_reduction': (str(Decimal(seed_before_fee)-Decimal(actual['total_fee']))
                             if seed_before_complete and best_complete is not None else None)}
        stages.append({'stage': 'CURRENT_INPUT_COLD_SEED', 'total_fee': actual['total_fee'],
                       'before_fee':seed_before_fee, 'after_fee':actual['total_fee'],
                       'fee_reduction':macro_details['direct_fee_reduction'],
                       'complete': best_complete is not None,
                       'elapsed_seconds': macro_details['elapsed_seconds'], 'details': macro_details})
        search_allowance = budget.seconds * .64
        planned_rounds = max(4, min(128, int(ceil(search_allowance / 45)))) if eligible_count else 0
        profiles = [('HYBRID', 1), ('GUIDED_LOCAL_SEARCH', 4), ('ALNS', 1),
                    ('HYBRID', 1), ('GUIDED_LOCAL_SEARCH', 1), ('SIMULATED_ANNEALING', 1.5)]
        emit({'stage': 'MULTI_START', 'round_total': planned_rounds,
              'message': '正在比较不同专车与共配组合'})
        for index in range(planned_rounds):
            budget.control()
            search_time_guard()
            available = search_allowance - budget.compute()
            if available <= .001:
                break
            profile, multiplier = profiles[index % len(profiles)]
            run_seed = (int(seed) + (index + 1) * 104729) % 2147483647
            seconds = max(.001, available / max(1, planned_rounds - index))
            if profile not in {'HYBRID', 'ALNS'}:
                seconds = min(30., seconds)
            # One cold native first solution can explore beyond the checked
            # current-input seed. The daily incumbent remains protected by keep.
            independent_native_start = (initial_result is None and index == 1
                                        and profile == 'GUIDED_LOCAL_SEARCH')
            round_initial = None if independent_native_start else best_complete or best
            initialization_source = ('INDEPENDENT_NATIVE_FIRST_SOLUTION' if independent_native_start
                                     else 'CURRENT_BEST')
            started = time.monotonic()
            compute_before = budget.compute()
            before_fee = (best_complete or best)['total_fee']
            emit({'stage': 'MULTI_START', 'round_index': index + 1,
                  'seed': run_seed, 'strategy': profile,
                  'message': '正在比较不同专车与共配组合'})
            if profile == 'HYBRID':
                result = optimize_hybrid(body, road, seconds, run_seed, interrupted,
                                         initial_result=round_initial, legacy=optimize_legacy)
            elif profile == 'ALNS':
                result = optimize_alns(body, road, seconds, run_seed, interrupted,
                                       initial_result=round_initial, legacy=optimize_legacy)
            else:
                result = optimize_ortools(body, road, seconds, run_seed, interrupted,
                                         initial_result=round_initial, strategy=profile,
                                         common_penalty_multiplier=multiplier)
            accepted = keep(result)
            flush_observations()
            item = {'stage': 'MULTI_START', 'round_index': index + 1, 'seed': run_seed,
                    'strategy': profile, 'penalty_multiplier': multiplier,
                    'initialization_source': initialization_source,
                    'initial_plan_supplied_to_round': round_initial is not None,
                    'budget_seconds': round(seconds, 4), 'elapsed_seconds': round(time.monotonic() - started, 4),
                    'compute_seconds': round(budget.compute() - compute_before, 4),
                    'before_fee': before_fee, 'after_fee': (best_complete or best)['total_fee'],
                    'fee_reduction': str(Decimal(before_fee) - Decimal((best_complete or best)['total_fee'])),
                    'methods': result.get('algorithm', {}).get('methods', []),
                    'routing_diagnostics': result.get('algorithm', {}).get('routing_diagnostics', {}),
                    'interrupted_native_diagnostics': result.get('algorithm', {}).get('interrupted_native_diagnostics', []),
                    'total_fee': result['total_fee'], 'complete': complete(data, result),
                    'accepted': accepted, 'stop_reason': result.get('algorithm', {}).get('stop_reason'),
                    'candidate_count': result.get('algorithm', {}).get('candidate_count', 0)}
            rounds.append(item)
            stages.append(item)
            budget.control()
        emit({'stage': 'COLUMN_SELECTION', 'message': '正在比较配送方案总费用'})
        search_time_guard()
        flush_observations()
        stream_observer = getattr(road, 'candidate_stream_observer', None)
        if callable(stream_observer):
            stream_observer({'kind': 'checkpoint', 'stage': 'PRE_COLUMN_SELECTION'})
        if trace is not None:
            trace.mark('DEEP_OBSERVED', status='COMPLETE', scope='PRE_COLUMN_SELECTION_CURRENT_RUN')
            trace.mark('DEEP_RETAINED', status='COMPLETE', scope='PRE_COLUMN_SELECTION_CURRENT_RUN')
            trace.mark('COMPLETE_PROTECTED', status='COMPLETE', scope='PRE_COLUMN_SELECTION_CURRENT_RUN')
        from app.route_columns import select_portfolio
        remaining = max(0., budget.seconds * .83 - budget.compute())
        before = time.monotonic()
        selection_compute_before = budget.compute()
        selection_before_fee = (best_complete or best)['total_fee']
        old_call_limit = getattr(road, 'call_limit', None)
        cap_extra_io = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
        if cap_extra_io:
            road.call_limit = min(road.calls, old_call_limit) if old_call_limit is not None else road.calls
        try:
            pooled = select_portfolio(body, road, [best_complete or best],
                                      candidate_recipes=chain(macro_recipes, observed.values()),
                                      complete_recipes=complete_allocations,
                                      seconds=min(60., remaining), cancelled=interrupted,
                                      progress=emit)
        finally:
            if cap_extra_io:
                road.call_limit = old_call_limit
        keep(pooled)
        stages.append({'stage': 'COLUMN_SELECTION', 'elapsed_seconds': round(time.monotonic() - before, 4),
                       'compute_seconds': round(budget.compute() - selection_compute_before, 4),
                       'before_fee': selection_before_fee, 'after_fee': (best_complete or best)['total_fee'],
                       'fee_reduction': str(Decimal(selection_before_fee) - Decimal((best_complete or best)['total_fee'])),
                       'reserved_refinement_seconds': 0.,
                       'total_fee': pooled['total_fee'], 'details': deepcopy(pooled.get('algorithm', {}))})
        strengthened, small_details = _post_selection_small_groups(
            data, road, best_complete or best, budget, seed, interrupted)
        keep(strengthened)
        stages.append({'stage': 'SMALL_GROUP_INTENSIFICATION', 'total_fee': strengthened['total_fee'],
                       'elapsed_seconds': small_details['elapsed_seconds'], 'details': small_details})
        budget.control()
        if switches.joint_reoptimization:
            emit({'stage': 'COLUMN_SELECTION', 'optimization_phase': 'JOINT_REOPTIMIZATION',
                  'message': '正在联合比较相关线路与共配费用'})
            run_optional_refinement('joint', 'JOINT_REOPTIMIZATION')
            budget.control()
        if switches.quote_group_rotation or switches.generation_selection_loop or switches.gain_time_allocation:
            emit({'stage': 'COLUMN_SELECTION', 'optimization_phase': 'CANDIDATE_EXPANSION',
                  'message': '正在生成并比较新的线路组合'})
            run_optional_refinement('cycles', 'CANDIDATE_EXPANSION')
        if switches.pyvrp_group_generation:
            run_pyvrp_group()
    except StopSearch as exc:
        stopped = exc.reason
        if trace is not None:
            for stage in ('DEEP_OBSERVED','DEEP_RETAINED','COMPLETE_PROTECTED'):
                trace.mark(stage, status='INTERRUPTED', reason=exc.reason, scope='CURRENT_RUN')
    finally:
        if use_live_batch:
            road.network_control = prior_network_control
        if reserve_calls:
            road.call_limit = original_call_limit
        calls_after_search = road.stats().get('calls', 0)
        call_budget.update(calls_after_search=calls_after_search,
                           search_calls=max(0, calls_after_search - calls_at_entry),
                           remaining_before_validation=(max(0, original_call_limit - calls_after_search)
                               if original_call_limit is not None else None))
        if had_observer:
            road.candidate_observer = previous_observer
        else:
            delattr(road, 'candidate_observer')
        for name, (existed, value) in previous_job_attributes.items():
            if existed:
                setattr(road, name, value)
            else:
                delattr(road, name)

    # No historical lower quote is silently returned as a fresh result. A user
    # cancellation may retain that complete reference with an explicit label.
    chosen = deepcopy(best_complete or best)
    before_validation_fee = chosen['total_fee']
    validation_started, validation_compute = budget.elapsed(), budget.compute()
    if cancelled():
        stopped = 'CANCELLED'
        checked = [_validated_without_io(data, item, road) for item in (chosen, baseline_result(data))]
        valid = [item for item in checked if item is not None]
        if not valid:
            raise ApiError(409, 'CANCELLED', '取消时尚未得到完整可行方案，未保存新方案；历史方案及输入已保留。')
        chosen = min(valid, key=result_rank)
        validation = _offline_validation(data, chosen, road)
    else:
        if not complete(data, chosen):
            raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '在时限内未找到覆盖全部门店的完整可行方案，未保存新方案。')
        live_driving = (isinstance(road, DrivingRoad) and road.call_limit != 0
                        and bool(getattr(road.settings, 'road_calls_enabled', False)))
        if live_driving and chosen['routes']:
            emit({'stage': 'ROAD_VALIDATION', 'message': '正在复核所选线路与道路'})
            before = time.monotonic()
            qps = max(1, min(3, int(getattr(road.settings, 'amap_web_qps', 3))))
            proof_reserve = max(5., min(20., len(data['tasks']) * .05))
            initial_directions = sum(len(r['task_ids']) for r in chosen['routes'])
            remaining_wall = max(0., budget.wall_seconds - budget.elapsed())
            bridge_limit = min(1024, max(0, int((remaining_wall - proof_reserve
                                               - initial_directions / qps) * qps * .75)))
            def optional_validation_control():
                budget.control()
                if budget.wall_seconds - budget.elapsed() <= proof_reserve:
                    raise StopSearch('VALIDATION_PROOF_RESERVE')
            try:
                chosen, validation = validate_driving(body, road, chosen,
                    [{**deepcopy(best_complete or best), 'candidate_routes': chain(macro_recipes, observed.values())}],
                    cancelled=cancelled, control=lambda: budget.control(compute=False),
                    repair_control=optional_validation_control, progress=emit,
                    selection_seconds=lambda: min(15., max(0., min(budget.seconds - budget.compute(),
                        budget.wall_seconds - budget.elapsed() - proof_reserve))),
                    complete_recipes=complete_allocations,
                    max_bridge_edges=None if getattr(road, 'production_planning', False) else bridge_limit,
                    deadline_limited_candidates=deadline_limited_candidates)
            except StopSearch as exc:
                stopped = exc.reason
                if exc.reason == 'WALL_BUDGET':
                    raise
                if exc.reason != 'CANCELLED':
                    raise ApiError(422, 'DEEP_VALIDATION_TIMEOUT', '道路复核等待达到时限，原输入及历史方案保留，请重试。') from None
                chosen = deepcopy(best_complete or best)
                options = [_validated_without_io(data, item, road) for item in (chosen, baseline_result(data))]
                valid = [item for item in options if item is not None]
                if not valid:
                    raise ApiError(409, 'CANCELLED', '取消时尚未完成本轮道路校验，未保存新方案。') from None
                chosen = min(valid, key=result_rank)
                validation = _offline_validation(data, chosen, road)
            if validation.get('optional_stop_reason') == 'CANCELLED':
                stopped = 'CANCELLED'
            stages.append({'stage': 'ROAD_VALIDATION', 'elapsed_seconds': round(time.monotonic() - before, 4),
                           'total_fee': chosen['total_fee'], 'mode': validation['mode']})
        else:
            emit({'stage': 'FINAL_CHECK', 'message': '正在复核全部门店与配送费用'})
            # Actual road rebinding rechecks current prices, geometry and time;
            # a historical reference remains explicitly historical/offline.
            options = [recipe(chosen)] + complete_allocations
            baseline = baseline_result(data)
            known = _validated_without_io(data, chosen, road)
            valid = ([baseline] if complete(data, baseline) else []) + ([known] if known is not None else [])
            for allocation in options:
                try:
                    budget.control(compute=False)
                    candidate = replay(data, allocation, road, lambda: budget.control(compute=False))
                except StopSearch as exc:
                    stopped = exc.reason
                    break
                if complete(data, candidate):
                    valid.append(candidate)
            if not valid:
                raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '现有道路参考下未找到完整可行方案，未保存新方案。')
            chosen = min(valid, key=result_rank)
            validation = _offline_validation(data, chosen, road)
    if not complete(data, chosen):
        raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '深度规划未找到覆盖全部门店的完整可行方案，未保存新方案。')
    for stage_name, enabled in [('JOINT_REOPTIMIZATION', switches.joint_reoptimization),
                                ('CANDIDATE_EXPANSION', switches.quote_group_rotation or switches.generation_selection_loop or switches.gain_time_allocation),
                                ('CRITICAL_EDGE_COLLECTION', switches.critical_edge_collection),
                                ('PYVRP_GROUP_GENERATION', switches.pyvrp_group_generation)]:
        if not any(row['stage'] == stage_name for row in stages):
            stages.append({'stage':stage_name, 'elapsed_seconds':0., 'details':{
                'skipped':True, 'stop_reason':'SWITCH_DISABLED' if not enabled else 'NOT_REACHED_' + stopped,
                'candidate_count':0, 'fee_reduction':'0.00'}})
    if trace is not None:
        for route in chosen['routes']:
            trace.route('FINAL', route, fee=route['fee'], compute_seconds=budget.compute())
        trace.mark('FINAL',status='COMPLETE',scope='INDEPENDENTLY_VERIFIED_FINAL_ALLOCATION')
    source_algorithm = deepcopy(chosen.get('algorithm', {}))
    retention_details = (retention.diagnostics() if retention is not None else
        {'policy': 'deadline_dedup' if deadline_limited_candidates else 'first',
         'limit': max_recipes, 'generated_observations': observation_count,
         'duplicate_observations': duplicate_observations, 'retained_candidates': len(observed),
         'capacity_rejected_observations': dropped_observations, 'flush_seconds': 0.})
    retention_details['protected_complete_allocations'] = len(complete_allocations)
    if diagnostics is not None:
        diagnostics.validation = {'before_fee': before_validation_fee, 'after_fee': chosen['total_fee'],
            'fee_increase': str(Decimal(chosen['total_fee']) - Decimal(before_validation_fee)),
            'elapsed_seconds': round(budget.elapsed() - validation_started, 6),
            'compute_seconds': round(budget.compute() - validation_compute, 6),
            'mode': validation['mode'], 'fresh': validation.get('fresh', False)}
        diagnostics.counts.update(candidate_observations=observation_count,
                                  duplicate_observations=retention_details['duplicate_observations'],
                                  dropped_observations=(retention_details.get('capacity_rejected_observations', 0)
                                      + retention_details.get('buffer_rejected_observations', 0)),
                                  retained_observations=len(observed),
                                  protected_complete_allocations=len(complete_allocations))
    validation['network_started_at'] = validation.get('started_at')
    validation['network_finished_at'] = validation.get('finished_at')
    chosen['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'stop_reason': stopped,
        'budget_seconds': budget_seconds, 'elapsed_seconds': round(budget.elapsed(), 4),
        'compute_seconds': round(budget.compute(), 4), 'wall_budget_seconds': budget.wall_seconds,
        'source_provenance': collect_source_provenance(),
        'experiments': asdict(switches),
        'diagnostics': diagnostics.snapshot() if diagnostics is not None else {'enabled': False},
        'seed': seed, 'round_count': len(rounds), 'rounds': rounds, 'stages': stages,
        'initial_plan_supplied': initial_result is not None,
        'initial_plan_accepted': initial is not None,
        'candidate_count': (sum(r['candidate_count'] for r in rounds)
                            + optional_candidate_count
                            + (small_details or {}).get('candidate_count', 0)
                            + (macro_details or {}).get('candidate_count', 0)),
        'distinct_route_count': len(observed), 'candidate_pool_limit': max_recipes,
        'candidate_observations': observation_count,
        'dropped_candidate_observations': (retention_details.get('capacity_rejected_observations', 0)
            + retention_details.get('buffer_rejected_observations', 0)),
        'duplicate_candidate_observations': retention_details['duplicate_observations'],
        'candidate_retention': retention_details,
        'actual_seeds': [r['seed'] for r in rounds], 'final_validation': validation,
        'complete_round_allocations': len(complete_allocations),
        'road_stats': road.stats(), 'road_call_budget': call_budget,
        'selection_diagnostics': source_algorithm,
        'optimality_proven': False, 'global_demand_optimality_proven': False,
        'candidate_pool_optimal': False, 'cost_candidate_pool_optimal': False,
        'cost_objective': 'min_unassigned_then_total_fee_then_distance_then_vehicles',
        'final_plan_independently_checked': True, 'same_frozen_input_all_rounds': True,
        'fleet_utilization': _utilization(data, chosen)}
    emit({'stage': 'COMPLETE', 'message': '配送方案已完成核算',
          'best_fee': chosen['total_fee'], 'round_total': len(rounds)})
    return chosen


def baseline_result(data):
    common = {i for i, task in data['tasks'].items() if task['common_unit_price'] is not None}
    return assemble(data, [], common, set(data['tasks']) - common)


def _optional_refinement(data, road, incumbent, budget, seed, cancelled, method, *, recipes=()):
    from app.route_search import JointSearch
    started, compute_before = time.monotonic(), budget.compute()
    raw_switches = getattr(road, 'deep_experiments', None)
    switches = DeepExperiments.parse(vars(raw_switches) if raw_switches is not None and not isinstance(raw_switches, (dict, DeepExperiments)) else raw_switches)
    reserved = switches.stage_budget_reservation and (
        switches.joint_reoptimization or switches.quote_group_rotation or
        switches.generation_selection_loop or switches.gain_time_allocation)
    ceiling = .95 if reserved else .83
    available = max(0., budget.seconds * ceiling - budget.compute())
    seconds = (available if reserved and method == 'cycles' else
               min(240. if reserved else 60., budget.seconds * .15, available))
    details = {'before_fee': incumbent['total_fee'], 'after_fee': incumbent['total_fee'],
               'budget_seconds': seconds, 'skipped': seconds < .1, 'candidate_count': 0,
               'stop_reason': 'NO_UNUSED_REFINEMENT_BUDGET', 'elapsed_seconds': 0.}
    if not complete(data, incumbent):
        details.update(skipped=True, stop_reason='NO_COMPLETE_INCUMBENT')
        return incumbent, details
    if seconds < .1:
        return incumbent, details
    original_limit = getattr(road, 'call_limit', None)
    capped = isinstance(road, DrivingRoad) and not getattr(road, 'production_planning', False)
    if capped:
        road.call_limit = road.calls if original_limit is None else min(road.calls, original_limit)
    search = None
    global_stop = None
    try:
        search = JointSearch(data, road, seconds, seed, cancelled, incumbent)
        local_control = search.control
        def control():
            budget.control()
            if budget.compute() >= budget.seconds * ceiling:
                raise StopSearch('REFINEMENT_BUDGET')
            local_control()
        search.control = control
        search.cars = sorted((v for v in data['vehicles'].values() if v['available']), key=lambda v: v['id'])
        # Reuse measured proposals from this same job. New columns can combine
        # with earlier alternatives instead of rebuilding a pool containing
        # only the incumbent. Importing has its own small, charged allowance;
        # it never starts a selector on the unchanged pool or queries a road.
        import_until = search.compute_elapsed() + min(2., seconds * .15)
        imported = 0
        for spec in recipes:
            control()
            if search.compute_elapsed() >= import_until:
                break
            key = spec['vehicle_id'], tuple(spec['task_ids'])
            if key in search.memo:
                continue
            candidate = evaluate_candidate(data, key[0], key[1], road, control)
            if candidate is not None and search.route_ok(candidate):
                search.remember(candidate)
                imported += 1
        details['imported_previous_orders'] = imported
        diagnostics = getattr(road, 'search_diagnostics', None)
        if diagnostics is not None:
            diagnostics.counts['imported_previous_orders'] += imported
        if method == 'joint':
            from app.route_joint_reoptimization import reoptimize_joint_routes
            details.update(reoptimize_joint_routes(search, scalable=switches.scalable_joint_search))
        else:
            from app.route_adaptive_cycles import expand_candidate_cycles
            switches = road.deep_experiments
            details.update(expand_candidate_cycles(search, quote_rotation=switches.quote_group_rotation,
                selection_loop=switches.generation_selection_loop, gain_allocation=switches.gain_time_allocation))
        details['skipped'] = False
    except StopSearch as exc:
        attribute = ('joint_reoptimization_diagnostics' if method == 'joint'
                     else 'candidate_expansion_diagnostics')
        # These dictionaries have already been finalized by the stopped helper.
        # Preserve nested native routing evidence and actual entered-stage work.
        attached = getattr(exc, attribute, None)
        if isinstance(attached, dict):
            details.update(attached)
        details.update(skipped=False, stop_reason=exc.reason)
        if exc.reason in {'CANCELLED', 'WALL_BUDGET'}:
            global_stop = exc
    finally:
        if capped:
            road.call_limit = original_limit
    result = incumbent
    if search is not None:
        if complete(data, search.best) and result_rank(search.best) < result_rank(incumbent):
            result = search.best
        details['candidate_count'] = search.evaluations
    details.update(after_fee=result['total_fee'],
                   fee_reduction=str(Decimal(incumbent['total_fee']) - Decimal(result['total_fee'])),
                   elapsed_seconds=round(time.monotonic() - started, 6),
                   compute_seconds=round(budget.compute() - compute_before, 6))
    if global_stop is not None and result is incumbent:
        global_stop.optional_refinement_diagnostics = details
        raise global_stop
    return result, details


def _utilization(data, result):
    pieces = sum(r['pieces'] for r in result['routes'])
    capacity = sum(data['vehicles'][r['vehicle_id']]['max_pieces'] for r in result['routes'])
    return {'dedicated_pieces': pieces, 'dedicated_capacity': capacity,
            'loaded_percent': round(100 * pieces / capacity, 2) if capacity else None,
            'dedicated_meters': sum(r['meters'] for r in result['routes'])}
