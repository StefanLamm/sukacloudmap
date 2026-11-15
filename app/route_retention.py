"""Bounded identity-only route retention, with all work charged to the caller.

These policies decide which *proposals* to price. They never decide a fee or
declare a route feasible. ``first`` reproduces the original first-unique rule;
``batch_dedup`` keeps recent unique identities; ``diversity`` rotates across
quote/warehouse, compulsory coverage and route-length strata, interleaving
different first stops within a stratum. Sorting happens only in ``flush``.
Complete allocations have an independent, validated protection channel.
"""
from collections import Counter, OrderedDict, deque
from copy import deepcopy
from math import log2
from time import perf_counter


POLICIES = frozenset({'first', 'batch_dedup', 'diversity'})


def identity(spec):
    vehicle_id, task_ids = spec['vehicle_id'], spec['task_ids']
    if (not isinstance(vehicle_id, str) or not vehicle_id or not task_ids
            or isinstance(task_ids, (str, bytes))):
        raise ValueError('A candidate needs a physical vehicle and a nonempty task order')
    order = tuple(task_ids)
    if any(not isinstance(task, str) or not task for task in order) or len(set(order)) != len(order):
        raise ValueError('Candidate task identifiers must be distinct nonempty strings')
    return vehicle_id, order


def compact(key):
    return {'vehicle_id': key[0], 'task_ids': list(key[1])}


class CandidateRetention:
    """Call ``flush`` between native slices/stages, never inside a callback.

    The ordinary callback adds an immutable identity to a bounded list. The
    buffer cap is explicit: overload drops arrivals rather than silently doing
    expensive pruning in a native callback. Counts describe observations, so
    an evicted identity seen again is a new admission, not a global-unique
    count. No unbounded set of all observed identities is kept.
    """
    def __init__(self, policy='first', limit=10000, batch_size=512, data=None, control=None):
        if policy not in POLICIES:
            raise ValueError('Unsupported candidate retention policy')
        if (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
                or isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1):
            raise ValueError('Candidate and batch limits must be positive integers')
        self.policy, self.limit, self.batch_size, self.data = policy, limit, batch_size, data
        self.control = control
        self.entries = OrderedDict()
        self.pending = []
        self.pending_limit = max(batch_size, 4 * limit)
        self.counts = Counter()
        self.flush_seconds = 0.
        self.protected_allocations = []
        self._protected_seen = set()

    def observe(self, spec):
        self.counts['generated_observations'] += 1
        # The live observer only receives evaluator/model-generated identities.
        # Validation of external frozen streams is done by replay_frozen_stream.
        key = spec['vehicle_id'], tuple(spec['task_ids'])
        if self.policy == 'first':
            if key in self.entries:
                self.counts['duplicate_observations'] += 1
            elif len(self.entries) < self.limit:
                self.entries[key] = compact(key)
                self.counts['admitted_candidates'] += 1
            else:
                self.counts['capacity_rejected_observations'] += 1
        elif len(self.pending) < self.pending_limit:
            self.pending.append(key)
        else:
            self.counts['buffer_rejected_observations'] += 1

    def _stratum(self, key):
        tasks = self.data['tasks'] if self.data else {}
        first = tasks.get(key[1][0], {})
        group = first.get('warehouse_id', ''), str(first.get('signature', ''))
        mandatory = any(tasks.get(task, {}).get('common_unit_price', 'OPTIONAL') is None for task in key[1])
        # Separate physical cars: a route cannot occupy another car by alias.
        return group, mandatory, int(log2(len(key[1]))), key[0]

    def _diverse(self, candidates):
        strata = {}
        # Recency is only a tie-break in proposal retention, never a fee claim.
        for index, key in enumerate(reversed(candidates)):
            if self.control and index % 64 == 0:
                self.control()
            strata.setdefault(self._stratum(key), []).append(key)
        buckets = []
        for stratum in sorted(strata, key=lambda item: (not item[1], item[0], item[2], item[3])):
            if self.control:
                self.control()
            # Interleave sequences with different starts, preserving alternate
            # stop orders instead of collapsing all permutations of a subset.
            by_start = {}
            for key in strata[stratum]:
                by_start.setdefault(key[1][0], deque()).append(key)
            starts = deque(by_start.values())
            ordered = deque()
            while starts:
                queue = starts.popleft()
                ordered.append(queue.popleft())
                if queue:
                    starts.append(queue)
            buckets.append(ordered)
        chosen = []
        rounds = deque(buckets)
        while rounds and len(chosen) < self.limit:
            bucket = rounds.popleft()
            chosen.append(bucket.popleft())
            if bucket:
                rounds.append(bucket)
        return chosen

    def flush(self):
        if not self.pending:
            return
        started = perf_counter()
        try:
            self._flush()
        finally:
            self.flush_seconds += perf_counter() - started

    def _flush(self):
        self.counts['flushes'] += 1
        candidates = OrderedDict.fromkeys(self.entries)
        for index, key in enumerate(self.pending):
            if self.control and index % 64 == 0:
                self.control()
            if key in candidates:
                self.counts['duplicate_observations'] += 1
                candidates.move_to_end(key)
            else:
                self.counts['admitted_candidates'] += 1
                candidates[key] = None
        if len(candidates) > self.limit:
            self.counts['evicted_candidates'] += len(candidates) - self.limit
            keys = (list(candidates)[-self.limit:] if self.policy == 'batch_dedup'
                    else self._diverse(candidates))
        else:
            keys = list(candidates)
        self.entries.clear()
        self.entries.update((key, compact(key)) for key in keys)
        self.pending.clear()

    def values(self):
        self.flush()
        return list(self.entries.values())

    def protect_allocation(self, result):
        from app.route_verification import complete, recipe
        if self.data is None or not complete(self.data, result):
            raise ValueError('Only independently checked complete allocations can be protected')
        allocation = recipe(result)
        key = (tuple((row['vehicle_id'], tuple(row['task_ids'])) for row in allocation['routes']),
               tuple(sorted(allocation['common_ids'])))
        if key not in self._protected_seen:
            self._protected_seen.add(key)
            self.protected_allocations.append(deepcopy(allocation))

    def diagnostics(self):
        return {'policy': self.policy, 'limit': self.limit, 'batch_size': self.batch_size,
                'pending_limit': self.pending_limit, 'pending_observations': len(self.pending),
                'retained_candidates': len(self.entries),
                'protected_complete_allocations': len(self.protected_allocations),
                'flush_seconds': round(self.flush_seconds, 6),
                **{key: self.counts[key] for key in (
                    'generated_observations', 'duplicate_observations', 'admitted_candidates',
                    'capacity_rejected_observations', 'buffer_rejected_observations',
                    'evicted_candidates', 'flushes')},
                'fees_evaluated_in_observer': 0, 'network_calls_in_observer': 0,
                'global_unique_count_available': False}


def replay_frozen_stream(events, *, policies=('first', 'batch_dedup', 'diversity'),
                         limit=10000, batch_size=512, data=None, explicit_boundaries=False):
    """Replay one frozen identity stream without search, network, or selection.

    Events are candidate dictionaries, optionally interspersed with
    ``{'kind': 'flush'}``. Set ``explicit_boundaries`` to reproduce recorded
    slices; otherwise periodic flushes keep memory bounded and identical across
    all policies. This comparison isolates retention, not total-budget
    search performance. The caller can price/select each returned pool against
    the same frozen graph and protected complete allocations.
    """
    pools = {policy: CandidateRetention(policy, limit, batch_size, data) for policy in policies}
    stream_count, seen, stream_duplicates = 0, set(), 0
    allocation_keys, complete_recipes = set(), []
    for event in events:
        if event.get('kind') == 'flush':
            for pool in pools.values():
                pool.flush()
            continue
        if event.get('kind') == 'allocation':
            # This trace contains identities, not checked road evidence. Keep
            # whole recipes outside ordinary limits; select_portfolio must
            # independently reprice/materialize them before accepting one.
            allocation = event['allocation']
            rows = allocation['routes']
            common_ids = allocation['common_ids']
            keys = tuple(identity(row) for row in rows)
            if (any(not isinstance(task, str) for task in common_ids)
                    or len(set(common_ids)) != len(common_ids)):
                raise ValueError('Invalid complete-allocation common identities')
            allocation_key = keys, tuple(sorted(common_ids))
            if allocation_key not in allocation_keys:
                allocation_keys.add(allocation_key)
                complete_recipes.append({'routes': [compact(key) for key in keys],
                                          'common_ids': list(common_ids)})
            continue
        key = identity(event)
        stream_count += 1
        if key in seen:
            stream_duplicates += 1
        seen.add(key)
        for pool in pools.values():
            pool.observe(event)
        if not explicit_boundaries and stream_count % batch_size == 0:
            for pool in pools.values():
                pool.flush()
    results = {}
    for policy, pool in pools.items():
        rows = pool.values()
        tasks = {task for row in rows for task in row['task_ids']}
        groups = {pool._stratum(key)[0] for key in pool.entries}
        results[policy] = {'candidates': rows, 'diagnostics': pool.diagnostics(),
                           'represented_tasks': len(tasks), 'represented_quote_groups': len(groups),
                           'represented_physical_vehicles': len({row['vehicle_id'] for row in rows})}
    return {'stream_observations': stream_count, 'stream_unique_identities': len(seen),
            'stream_duplicate_observations': stream_duplicates,
            'complete_recipes': complete_recipes, 'complete_recipe_count': len(complete_recipes),
            'trace_allocations_independently_validated': False,
            'same_frozen_stream': True, 'cold_start_performance_claimed': False, 'policies': results}
