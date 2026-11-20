"""Optional candidate expansion after finite-pool selection, on known roads."""
from collections import defaultdict
from decimal import Decimal
import math
import time

from app.route_ortools import generate_ortools_candidates
from app.routing_engine import StopSearch


def expand_candidate_cycles(search, *, quote_rotation=False, selection_loop=False,
                            gain_allocation=False, max_cycles=64):
    """Generate new orders before spending more time on an unchanged pool.

    Each switch can be measured independently. Group models are proposal
    generators; the host selects the whole physical fleet and real quotes.
    The fixed complete incumbent is never replaced by a partial group result.
    """
    started, compute_before = time.monotonic(), search.compute_elapsed()
    calls_before = search.road.stats().get('calls', 0)
    before_fee = search.best['total_fee']
    groups = defaultdict(list)
    for task_id, task in search.tasks.items():
        if task['signature']:
            groups[task['warehouse_id'], task['signature']].append(task_id)
    ordered = sorted(groups, key=lambda key: (-sum(search.common_cost[i] for i in groups[key]), key))
    group_actions = ordered if quote_rotation else [None]
    actions = [(group, variant) for group in group_actions
               for variant in (range(3) if gain_allocation else (0,))]
    diagnostics = {'method': 'CANDIDATE_EXPANSION_CYCLES', 'before_fee': before_fee,
                   'after_fee': before_fee, 'quote_group_rotation': quote_rotation,
                   'generation_selection_loop': selection_loop, 'gain_time_allocation': gain_allocation,
                   'cycles': [], 'global_optimality_proven': False,
                   'stop_reason': 'SEARCH_COMPLETED', 'new_network_calls': 0}
    if not callable(getattr(search.road, 'known_segment', None)) or not actions:
        diagnostics['stop_reason'] = 'NO_MEASURED_GROUPS'
        return diagnostics
    stats = {action: {'runs': 0, 'gain': 0., 'seconds': 0.} for action in actions}
    original_pack = search.pack
    # Native generation is charged to this host's compute deadline. Selection
    # only sees genuinely new identities; the same optimal pool is not solved
    # again in a loop merely to consume the remaining allowance.
    seen = set(search.memo)
    try:
        for index in range(max_cycles):
            search.control()
            if search.remaining() < .1:
                break
            if not selection_loop and not gain_allocation and index >= len(actions):
                break
            unexplored = [action for action in actions if stats[action]['runs'] == 0]
            if gain_allocation and not unexplored:
                total = sum(row['runs'] for row in stats.values())
                def score(action):
                    row = stats[action]
                    return row['gain'] / max(.001, row['seconds']) + math.sqrt(
                        2 * math.log(total + 1) / row['runs'])
                action = max(actions, key=score)
            else:
                action = unexplored[0] if unexplored else actions[index % len(actions)]
            group, variant = action
            data = search.data
            if group is not None:
                data = {**data, 'tasks': {i: search.tasks[i] for i in sorted(groups[group])},
                        'vehicles': {vid: v for vid, v in data['vehicles'].items()
                                     if v['available'] and v['warehouse_id'] == group[0]}}
            prior_fee, prior_compute = search.best['total_fee'], search.compute_elapsed()
            row = {'cycle': index + 1, 'group_task_count': len(data['tasks']),
                   'group_identity': repr(action), 'before_fee': prior_fee,
                   'new_orders': 0, 'changed_pool_columns': 0, 'selection_runs': 0,
                   'previous_pool_cost_optimal': bool(search.pack_diagnostics and
                       search.pack_diagnostics[-1].get('cost_candidate_pool_optimal'))}
            diagnostics['cycles'].append(row)
            def batch(specs):
                for spec in specs:
                    search.control()
                    key = spec['vehicle_id'], tuple(spec['task_ids'])
                    if key in seen:
                        continue
                    seen.add(key)
                    row['new_orders'] += 1
                    pool_key = key[0], sum(search.indices[i] for i in key[1])
                    prior = search.pool.get(pool_key)
                    candidate = search.evaluate(*key)
                    if candidate is not None:
                        after = search.pool.get(pool_key)
                        if after is not prior:
                            row['changed_pool_columns'] += 1
                        search.quick_exchange(candidate)
            try:
                duration = min(3., max(.05, search.remaining() * .5))
                assignment_options={}
                if getattr(getattr(search.road,'deep_experiments',None),'complete_assignment_pricing',False):
                    from app.route_assignment_pricing import assignment_callback
                    assignment_options={'complete_assignment_pricing':True,
                        'on_assignment':assignment_callback(search,scope_ids=data['tasks'])}
                specs, row['routing'] = generate_ortools_candidates(
                    data, lambda vehicle, a, b: search.road.known_segment(a, b), duration,
                    (search.seed + (index + 1) * 31337) % 2147483647, search.cancelled,
                    vehicle_independent=True,
                    strategy=('GUIDED_LOCAL_SEARCH', 'SIMULATED_ANNEALING', 'TABU_SEARCH')[
                        variant if gain_allocation else index % 3],
                    common_penalty_multiplier=(1, 2, .75, 4)[index % 4], on_batch=batch,
                    candidate_retention=getattr(getattr(search.road, 'deep_experiments', None),
                                                'native_candidate_retention', 'first'),
                    deadline_limited_candidates=getattr(search.road, 'deadline_limited_candidates', False),
                    **({'stage_trace':search.road.route_stage_trace} if getattr(search.road,'route_stage_trace',None) is not None else {}),
                    **({'candidate_stream_observer':search.road.native_candidate_stream_observer}
                       if getattr(search.road,'native_candidate_stream_observer',None) is not None else {}),
                    **assignment_options)
                if search.remaining() > .075:
                    batch(specs)
                if row['changed_pool_columns'] and search.remaining() > .075:
                    original_pack()
                    row['selection_runs'] += 1
            except StopSearch as exc:
                if isinstance(getattr(exc,'native_diagnostics',None),dict):
                    row['routing']=exc.native_diagnostics
                exc.candidate_expansion_diagnostics=diagnostics
                raise
            finally:
                row.update(after_fee=search.best['total_fee'],
                    fee_reduction=str(Decimal(prior_fee) - Decimal(search.best['total_fee'])),
                    compute_seconds=round(search.compute_elapsed() - prior_compute, 6))
                stats[action]['runs'] += 1
                stats[action]['gain'] += float(Decimal(prior_fee) - Decimal(search.best['total_fee']))
                stats[action]['seconds'] += search.compute_elapsed() - prior_compute
    except StopSearch as exc:
        diagnostics['stop_reason'] = exc.reason
        if exc.reason in {'CANCELLED', 'WALL_BUDGET'}:
            raise
    diagnostics.update(after_fee=search.best['total_fee'],
                       fee_reduction=str(Decimal(before_fee) - Decimal(search.best['total_fee'])),
                       elapsed_seconds=round(time.monotonic() - started, 6),
                       compute_seconds=round(search.compute_elapsed() - compute_before, 6),
                       new_network_calls=max(0, search.road.stats().get('calls', 0) - calls_before))
    return diagnostics
