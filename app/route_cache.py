"""Bound compact search caches without discarding the checked incumbent.

Pruning is heuristic: optimality over the retained pool never proves optimality
over discarded columns or unexamined routes. Full incumbent geometry/evidence
belongs to the host and is deliberately neither read nor modified here.
"""
from collections import Counter, defaultdict, deque
from decimal import Decimal


def _limit(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Cache limits must be finite nonnegative integers') from None


def _target(limit):
    return max(1, limit * 4 // 5) if limit else 0


def _pool_key(host, route):
    mask = route.get('mask')
    if mask is None:
        mask = sum(host.indices[item] for item in route['task_ids'])
    return route['vehicle_id'], mask


def _costs(host, row):
    ids = row['task_ids']
    fee = Decimal(str(row['fee']))
    if not fee.is_finite() or fee < 0:
        raise ValueError('Invalid compact fee')
    meters = row['meters']
    if isinstance(meters, bool) or not isinstance(meters, int) or meters < 0:
        raise ValueError('Invalid compact distance')
    hard = sum(item in host.mandatory for item in ids)
    net = fee - sum((host.common_cost[item] for item in ids), Decimal(0))
    return fee, meters, hard, net


def trim_search_cache(host, memo_limit=100000, pool_limit=50000):
    """FIFO-trim memo and diversify the compact route pool after overflow.

    Complete incumbent routes are protected even when their count exceeds an
    explicitly smaller limit. All other entries are trimmed to about 80% of
    the limit, leaving headroom before the next linear pruning pass. Candidate
    diversity protects scarce task coverage and physical-vehicle alternatives.
    The caller may aggregate returned statistics; no plan/proof is rewritten.
    """
    memo_limit, pool_limit = _limit(memo_limit), _limit(pool_limit)
    before_memo, before_pool = len(host.memo), len(host.pool)
    old_memo, old_pool = host.memo, host.pool
    best_routes = list(host.best_routes)
    protected_pool = {_pool_key(host, row): host.pool.get(_pool_key(host, row), row) for row in best_routes}
    protected_memo = {(row['vehicle_id'], tuple(row['task_ids'])): row for row in best_routes}
    memo_pruned = pool_pruned = 0
    coverage_count = 0

    if before_memo > memo_limit:
        # Dict insertion order is a FIFO policy; OrderedDict hosts can update it
        # on cache hits for LRU. Reassignment keeps in-progress old iterators safe.
        target = max(len(protected_memo), _target(memo_limit))
        retained = dict(protected_memo)
        for key in reversed(host.memo):
            if len(retained) >= target:
                break
            if key not in retained:
                retained[key] = host.memo[key]
        # Preserve chronology of retained unprotected entries for future FIFO.
        ordered = [(key, retained[key]) for key in host.memo if key in retained]
        seen = {key for key, _ in ordered}
        ordered.extend((key, value) for key, value in protected_memo.items() if key not in seen)
        host.memo = type(host.memo)(ordered)
        memo_pruned = sum(key not in host.memo for key in old_memo)

    if before_pool > pool_limit:
        records = {}
        frequency = Counter()
        cheapest, strongest = {}, {}
        for key, row in host.pool.items():
            try:
                fee, meters, hard, net = _costs(host, row)
                ids = tuple(row['task_ids'])
                # Negative net is a larger saving versus paid common delivery.
                ranking = (-hard, net, meters, fee, bool(row.get('_estimated')),
                           str(row['vehicle_id']), ids)
                records[key] = row, ranking
                for item in ids:
                    frequency[item] += 1
                    cheap_score = (fee, net, meters, str(row['vehicle_id']), ids)
                    gain_score = (-hard, net, meters, fee, str(row['vehicle_id']), ids)
                    if item not in cheapest or cheap_score < cheapest[item][0]:
                        cheapest[item] = cheap_score, key
                    if item not in strongest or gain_score < strongest[item][0]:
                        strongest[item] = gain_score, key
            except (KeyError, TypeError, ValueError, ArithmeticError):
                # Malformed non-incumbent cache rows have no retention claim.
                continue
        retained = dict(protected_pool)
        rarity = sorted(frequency, key=lambda item: (item not in host.mandatory,
                                                     frequency[item], str(item)))
        coverage_keys = set()
        for item in rarity:
            coverage_keys.add(cheapest[item][1]); coverage_keys.add(strongest[item][1])
        # If task representatives fit the requested cap, keep both cheapest
        # and strongest opportunities even if they exceed the 80% headroom.
        all_essential = set(retained) | coverage_keys
        target = max(len(retained), _target(pool_limit))
        if len(all_essential) <= pool_limit:
            target = max(target, len(all_essential))
            for key in coverage_keys:
                if key not in retained:
                    retained[key] = records[key][0]
        else:
            target = max(target, len(retained))
            for item in rarity:
                for key in (strongest[item][1], cheapest[item][1]):
                    if len(retained) >= target:
                        break
                    if key not in retained:
                        retained[key] = records[key][0]
                if len(retained) >= target:
                    break
        # Round-robin high-quality columns from different physical vehicles.
        # One car's many orders cannot evict every alternative car's columns.
        by_car = defaultdict(list)
        for key, (row, ranking) in records.items():
            if key not in retained:
                by_car[row['vehicle_id']].append((ranking, key))
        queues = []
        for candidates in by_car.values():
            candidates.sort()
            queues.append(deque(candidates))
        queues.sort(key=lambda queue: queue[0][0])
        while queues and len(retained) < target:
            following = []
            for queue in queues:
                if len(retained) >= target:
                    break
                _, key = queue.popleft()
                retained[key] = records[key][0]
                if queue:
                    following.append(queue)
            queues = following
        # Keep original order where possible, with protected missing rows added.
        ordered = [(key, retained[key]) for key in host.pool if key in retained]
        seen = {key for key, _ in ordered}
        ordered.extend((key, value) for key, value in retained.items() if key not in seen)
        host.pool = type(host.pool)(ordered)
        pool_pruned = sum(key not in host.pool for key in old_pool)
        coverage_count = len({item for row in host.pool.values() for item in row['task_ids']})

    return {'memo_pruned': memo_pruned, 'pool_pruned': pool_pruned,
            'memo_size': len(host.memo), 'pool_size': len(host.pool),
            'memo_limit': memo_limit, 'pool_limit': pool_limit,
            'incumbent_memo_keys': len(protected_memo), 'incumbent_pool_keys': len(protected_pool),
            'memo_over_limit_for_incumbent': len(protected_memo) > memo_limit,
            'pool_over_limit_for_incumbent': len(protected_pool) > pool_limit,
            'retained_task_coverage': coverage_count if pool_pruned else None,
            'pool_pruned_changes_proof_scope': bool(pool_pruned)}
