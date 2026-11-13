"""Daily, bounded multi-seed search, route pooling and fresh-road verification.

All runs belong to the current frozen input. A job may reuse road measurements,
but never another group's demand quantities or a historical experiment's fee.
"""
from collections import OrderedDict
from copy import deepcopy
from math import ceil
import time

from app.api import ApiError
from app.road_service import DrivingRoad
from app.planning_inputs import content_hash
from app.route_metrics import evaluate_candidate
from app.route_ortools import optimize_ortools
from app.route_portfolio import optimize_alns, optimize_hybrid
from app.route_verification import (complete, recipe, replay, result_rank,
                                    validate_driving)
from app.routing_engine import StopSearch, assemble, check_result, normalize, optimize_legacy


class _Budget:
    def __init__(self, road, seconds, cancelled):
        self.road, self.seconds, self.cancelled = road, max(0., float(seconds)), cancelled
        self.started = time.monotonic()
        self.io_started = float(getattr(road, 'io_seconds', 0))
        self.wall_seconds = self.seconds + min(1800., max(120., self.seconds * 2))

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
                    if (not isinstance(received, int) or isinstance(received, bool)
                            or not 0 <= int(time.time()) - received < 7 * 86400
                            or not DrivingRoad.valid_reference(segment, allow_expired=True, allow_historical_status=True)):
                        return None
                    if int(time.time()) - received >= 3600:
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


def optimize_deep(body, road, budget_seconds=300, seed=0, cancelled=lambda: False,
                  *, initial_result=None, progress=None):
    """Saveable results are complete and independently verified, never estimates.

    The compute allowance covers all serial search/selection phases. Provider
    pacing/network wait is measured separately and bounded by a wall allowance.
    Small allowances still run the same stages with shorter slices; they do not
    claim the depth or proof of a long multi-hour experiment.
    """
    data = normalize(body)
    budget = _Budget(road, budget_seconds, cancelled)
    notify = progress or (lambda value: None)
    stages = []
    observed = OrderedDict()
    observation_count = dropped_observations = 0
    max_recipes = 10000
    previous_observer = getattr(road, 'candidate_observer', None)
    had_observer = hasattr(road, 'candidate_observer')
    current_progress = {'stage': 'MULTI_START', 'round_index': 0, 'round_total': 0,
                        'seed': seed, 'candidate_count': 0}
    baseline_common = {i for i, task in data['tasks'].items() if task['common_unit_price'] is not None}
    best = assemble(data, [], baseline_common, set(data['tasks']) - baseline_common)
    initial = _validated_without_io(data, initial_result, road) if initial_result is not None else None
    if initial is not None and result_rank(initial) < result_rank(best):
        best = initial
    best_complete = deepcopy(best) if complete(data, best) else None
    complete_allocations = []
    complete_seen = set()
    rounds = []
    stopped = 'SEARCH_COMPLETED'
    validation = None

    def interrupted():
        return cancelled() or budget.elapsed() >= budget.wall_seconds

    def emit(update):
        current_progress.update(update)
        current_progress.update(elapsed_seconds=round(budget.elapsed(), 3),
                                compute_seconds=round(budget.compute(), 3),
                                candidate_count=len(observed))
        if best_complete is None:
            current_progress['best_fee'] = None
        elif 'best_fee' not in update:
            current_progress['best_fee'] = best_complete['total_fee'] if best_complete is not None else None
        notify(deepcopy(current_progress))

    def observe(spec):
        nonlocal observation_count, dropped_observations
        if interrupted():
            raise StopSearch('CANCELLED' if cancelled() else 'WALL_BUDGET')
        observation_count += 1
        key = (spec['vehicle_id'], tuple(spec['task_ids']))
        if key not in observed:
            if len(observed) >= max_recipes:
                dropped_observations += 1
            else:
                observed[key] = {'vehicle_id': key[0], 'task_ids': list(key[1])}
        if callable(previous_observer):
            previous_observer(spec)

    def keep(result):
        nonlocal best, best_complete
        if not _partial_legal(data, result):
            return False
        if result_rank(result) < result_rank(best):
            best = deepcopy(result)
        if complete(data, result) and (best_complete is None or result_rank(result) < result_rank(best_complete)):
            best_complete = deepcopy(result)
        if complete(data, result):
            allocation = recipe(result)
            identity = content_hash(allocation)
            if identity not in complete_seen:
                complete_seen.add(identity)
                complete_allocations.append(allocation)
        for route in result['routes']:
            if not interrupted():
                observe(route)
        return True

    # Each source run uses the current job's isolated road object. No shared
    # Settings or module-global algorithm functions are changed between seeds.
    road.candidate_observer = observe
    try:
        search_allowance = budget.seconds * .64
        eligible = any(task['signature'] and any(car['available'] and car['warehouse_id'] == task['warehouse_id']
                       and car['max_pieces'] >= task['pieces'] for car in data['vehicles'].values())
                       for task in data['tasks'].values())
        planned_rounds = max(4, min(128, int(ceil(search_allowance / 45)))) if eligible else 0
        profiles = [('HYBRID', 1), ('GUIDED_LOCAL_SEARCH', 4), ('ALNS', 1),
                    ('HYBRID', 1), ('GUIDED_LOCAL_SEARCH', 1), ('SIMULATED_ANNEALING', 1.5)]
        emit({'stage': 'MULTI_START', 'round_total': planned_rounds})
        for index in range(planned_rounds):
            budget.control()
            available = search_allowance - budget.compute()
            if available <= .001:
                break
            profile, multiplier = profiles[index % len(profiles)]
            run_seed = (int(seed) + (index + 1) * 104729) % 2147483647
            seconds = max(.001, available / max(1, planned_rounds - index))
            if profile not in {'HYBRID', 'ALNS'}:
                seconds = min(30., seconds)
            started = time.monotonic()
            emit({'stage': 'MULTI_START', 'round_index': index + 1,
                  'seed': run_seed, 'strategy': profile})
            if profile == 'HYBRID':
                result = optimize_hybrid(body, road, seconds, run_seed, interrupted,
                                         initial_result=best_complete or best, legacy=optimize_legacy)
            elif profile == 'ALNS':
                result = optimize_alns(body, road, seconds, run_seed, interrupted,
                                       initial_result=best_complete or best, legacy=optimize_legacy)
            else:
                result = optimize_ortools(body, road, seconds, run_seed, interrupted,
                                         initial_result=best_complete or best, strategy=profile,
                                         common_penalty_multiplier=multiplier)
            accepted = keep(result)
            item = {'stage': 'MULTI_START', 'round_index': index + 1, 'seed': run_seed,
                    'strategy': profile, 'penalty_multiplier': multiplier,
                    'budget_seconds': round(seconds, 4), 'elapsed_seconds': round(time.monotonic() - started, 4),
                    'total_fee': result['total_fee'], 'complete': complete(data, result),
                    'accepted': accepted, 'stop_reason': result.get('algorithm', {}).get('stop_reason'),
                    'candidate_count': result.get('algorithm', {}).get('candidate_count', 0)}
            rounds.append(item)
            stages.append(item)
            budget.control()
        emit({'stage': 'COLUMN_SELECTION'})
        from app.route_columns import select_portfolio
        remaining = max(0., budget.seconds * .83 - budget.compute())
        before = time.monotonic()
        old_call_limit = getattr(road, 'call_limit', None)
        cap_extra_io = isinstance(road, DrivingRoad)
        if cap_extra_io:
            road.call_limit = road.calls
        try:
            pooled = select_portfolio(body, road, [best_complete or best],
                                      candidate_recipes=list(observed.values()),
                                      complete_recipes=complete_allocations,
                                      seconds=min(60., remaining), cancelled=interrupted,
                                      progress=emit)
        finally:
            if cap_extra_io:
                road.call_limit = old_call_limit
        keep(pooled)
        stages.append({'stage': 'COLUMN_SELECTION', 'elapsed_seconds': round(time.monotonic() - before, 4),
                       'total_fee': pooled['total_fee'], 'details': deepcopy(pooled.get('algorithm', {}))})
    except StopSearch as exc:
        stopped = exc.reason
    finally:
        if had_observer:
            road.candidate_observer = previous_observer
        else:
            delattr(road, 'candidate_observer')

    # No historical lower quote is silently returned as a fresh result. A user
    # cancellation may retain that complete reference with an explicit label.
    chosen = deepcopy(best_complete or best)
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
            emit({'stage': 'ROAD_VALIDATION'})
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
                    raise StopSearch('WALL_BUDGET')
            try:
                chosen, validation = validate_driving(body, road, chosen,
                    [{**deepcopy(best_complete or best), 'candidate_routes': list(observed.values())}],
                    cancelled=cancelled, control=lambda: budget.control(compute=False),
                    repair_control=optional_validation_control, progress=emit,
                    selection_seconds=lambda: min(15., max(0., min(budget.seconds - budget.compute(),
                        budget.wall_seconds - budget.elapsed() - proof_reserve))),
                    complete_recipes=complete_allocations, max_bridge_edges=bridge_limit)
            except StopSearch as exc:
                stopped = exc.reason
                if exc.reason != 'CANCELLED':
                    raise ApiError(422, 'DEEP_VALIDATION_TIMEOUT', '道路复核等待达到时限，原输入及历史方案保留，请重试。') from None
                chosen = deepcopy(best_complete or best)
                options = [_validated_without_io(data, item, road) for item in (chosen, baseline_result(data))]
                valid = [item for item in options if item is not None]
                if not valid:
                    raise ApiError(409, 'CANCELLED', '取消时尚未完成本轮道路校验，未保存新方案。') from None
                chosen = min(valid, key=result_rank)
                validation = _offline_validation(data, chosen, road)
            stages.append({'stage': 'ROAD_VALIDATION', 'elapsed_seconds': round(time.monotonic() - before, 4),
                           'total_fee': chosen['total_fee'], 'mode': validation['mode']})
        else:
            emit({'stage': 'FINAL_CHECK'})
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
    source_algorithm = deepcopy(chosen.get('algorithm', {}))
    validation['network_started_at'] = validation.get('started_at')
    validation['network_finished_at'] = validation.get('finished_at')
    chosen['algorithm'] = {'mode': 'DAILY_DEEP_PORTFOLIO', 'stop_reason': stopped,
        'budget_seconds': budget_seconds, 'elapsed_seconds': round(budget.elapsed(), 4),
        'compute_seconds': round(budget.compute(), 4), 'wall_budget_seconds': budget.wall_seconds,
        'seed': seed, 'round_count': len(rounds), 'rounds': rounds, 'stages': stages,
        'candidate_count': sum(r['candidate_count'] for r in rounds),
        'distinct_route_count': len(observed), 'candidate_pool_limit': max_recipes,
        'candidate_observations': observation_count, 'dropped_candidate_observations': dropped_observations,
        'actual_seeds': [r['seed'] for r in rounds], 'final_validation': validation,
        'complete_round_allocations': len(complete_allocations),
        'road_stats': road.stats(), 'selection_diagnostics': source_algorithm,
        'optimality_proven': False, 'global_225_task_optimality_proven': False,
        'candidate_pool_optimal': False, 'cost_candidate_pool_optimal': False,
        'cost_objective': 'min_unassigned_then_total_fee_then_distance_then_vehicles',
        'final_plan_independently_checked': True, 'same_frozen_input_all_rounds': True,
        'fleet_utilization': _utilization(data, chosen)}
    emit({'stage': 'COMPLETE', 'best_fee': chosen['total_fee'], 'round_total': len(rounds)})
    return chosen


def baseline_result(data):
    common = {i for i, task in data['tasks'].items() if task['common_unit_price'] is not None}
    return assemble(data, [], common, set(data['tasks']) - common)


def _utilization(data, result):
    pieces = sum(r['pieces'] for r in result['routes'])
    capacity = sum(data['vehicles'][r['vehicle_id']]['max_pieces'] for r in result['routes'])
    return {'dedicated_pieces': pieces, 'dedicated_capacity': capacity,
            'loaded_percent': round(100 * pieces / capacity, 2) if capacity else None,
            'dedicated_meters': sum(r['meters'] for r in result['routes'])}
