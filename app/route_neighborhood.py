"""Adaptive, road-priced large neighborhoods for joint dedicated/common delivery.

The host owns evidence, the incumbent and the global deadline. This module
only proposes physical-car routes through ``host.evaluate`` and complete
allocations through ``host.consider``. Coordinates/known edge lengths order
probes; neither can substitute for independently checked route evidence.

Method references: Ropke & Pisinger, Transportation Science 40(4), 2006
(adaptive destroy/repair and regret insertion); Vidal, COR 140, 2022
(cross-route removal followed by reinsertion rather than position-only swap).
This is a purpose-built rich delivery adaptation, not either paper's solver.
"""
from collections import Counter
from decimal import Decimal
import math
import random
import time


class _LocalBudget(Exception):
    pass


class AdaptiveNeighborhood:
    """Reusable ALNS worker over a ``JointSearch``-compatible host.

    Work solutions may be temporarily more expensive under simulated
    annealing. The externally visible incumbent remains monotone: every
    publication still goes through the host's independent evidence check.
    """
    DESTROY = ('related', 'random', 'route', 'worst')
    REPAIR = ('regret2', 'regret3', 'load', 'trunk')

    def __init__(self, host, *, seconds=None, iteration_limit=None, seed=None):
        self.host = host
        self.data, self.tasks = host.data, host.tasks
        self.vehicles = {v['id']: v for v in host.cars}
        self.rng = random.Random(host.seed if seed is None else seed)
        self.started = time.monotonic()
        compute_clock = getattr(host, 'compute_elapsed', None)
        self._clock = compute_clock if callable(compute_clock) else time.monotonic
        self.compute_started = self._clock()
        allowance = host.remaining() if seconds is None else max(0, float(seconds))
        self.deadline = self.compute_started + allowance
        self.iteration_limit = iteration_limit
        self.iterations = self.accepted = self.improvements = self.restarts = 0
        self.before_evaluations = host.evaluations
        self.before_pool = len(host.pool)
        self.operator_counts = Counter()
        self.operator_improvements = Counter()
        self.weights = {('destroy', op): 1.0 for op in self.DESTROY}
        self.weights.update({('repair', op): 1.0 for op in self.REPAIR})
        self.segment_scores = Counter()
        self.segment_uses = Counter()
        self.points = {**self.data['warehouses'], **self.tasks}
        self.eligible = [i for i, row in self.tasks.items()
                         if row['signature'] and any(v['warehouse_id'] == row['warehouse_id']
                         and v['max_pieces'] >= row['pieces'] for v in self.vehicles.values())]
        self.curve = []
        self.initial_rank = host.best_rank
        self._record('initial')

    def tick(self):
        self.host.control()
        if self._clock() >= self.deadline:
            raise _LocalBudget()

    def _record(self, kind):
        row = {'iteration': self.iterations,
               'elapsed_seconds': round(time.monotonic() - self.started, 4),
               'compute_elapsed_seconds': round(self._clock() - self.compute_started, 4),
               'unassigned': self.host.best_rank[0],
               'total_fee': str(self.host.best_rank[1]),
               'dedicated_vehicles': len(self.host.best_routes), 'kind': kind}
        if len(self.curve) >= 512:
            # Preserve the start and the most recent progress without allowing
            # a 24,000-second run to grow an unbounded result document.
            self.curve = [self.curve[0], *self.curve[2::2]]
        self.curve.append(row)

    def choose(self, family, names):
        return self.rng.choices(names, weights=[self.weights[(family, n)] for n in names], k=1)[0]

    def evaluate(self, vehicle_id, ids):
        self.tick()
        if not ids:
            return None
        return self.host.evaluate(vehicle_id, list(ids))

    def compatible(self, vehicle_id, ids):
        vehicle = self.vehicles[vehicle_id]
        if not ids or len(ids) != len(set(ids)):
            return False
        first = self.tasks[ids[0]]
        return bool(first['signature'] and
                    all(self.tasks[i]['warehouse_id'] == vehicle['warehouse_id']
                        and self.tasks[i]['signature'] == first['signature'] for i in ids) and
                    sum(self.tasks[i]['pieces'] for i in ids) <= vehicle['max_pieces'])

    def distance_hint(self, left, right):
        """Ranking signal only; unknown edges never become priced road data."""
        a, b = self.points[left], self.points[right]
        try:
            known = getattr(self.host.road, 'known_segment', None)
            edge = known(a, b) if known else None
            if edge is not None:
                meters = edge.get('meters') if isinstance(edge, dict) else edge[0]
                if isinstance(meters, (int, float)) and math.isfinite(meters) and meters >= 0:
                    return float(meters)
        except (KeyError, TypeError, ValueError, IndexError):
            pass
        try:
            # A scale-free coordinate ordering is sufficient for choosing which
            # insertions to ask the real evaluator about. It is never a quote.
            latitude = (float(a['latitude']) + float(b['latitude'])) / 2
            dx = (float(a['longitude']) - float(b['longitude'])) * math.cos(math.radians(latitude))
            dy = float(a['latitude']) - float(b['latitude'])
            return math.hypot(dx, dy) * 100000
        except (KeyError, TypeError, ValueError):
            return 0.0

    def positions(self, vehicle_id, ids, item):
        if len(ids) <= 9:
            return range(len(ids) + 1)
        warehouse = self.vehicles[vehicle_id]['warehouse_id']
        scored = []
        for position in range(len(ids) + 1):
            before = ids[position - 1] if position else warehouse
            after = ids[position] if position < len(ids) else None
            extra = self.distance_hint(before, item)
            if after is not None:
                extra += self.distance_hint(item, after) - self.distance_hint(before, after)
            scored.append((extra, position))
        # All positions are considered on short lines. On larger lines retain
        # the strongest measured/geographic probes plus end/mid alternatives.
        picked = [p for _, p in sorted(scored)[:5]]
        return list(dict.fromkeys(picked + [0, len(ids), len(ids) // 2]))

    def nearest_routes(self, state, item):
        task = self.tasks[item]
        existing, empty = [], []
        for vid, vehicle in self.vehicles.items():
            route = state.get(vid)
            ids = route['task_ids'] if route else []
            if not self.compatible(vid, ids + [item]):
                continue
            if ids:
                near = min(self.distance_hint(i, item) for i in ids)
                slack = vehicle['max_pieces'] - route['pieces'] - task['pieces']
                existing.append((near, slack, vid))
            else:
                empty.append((vehicle['max_pieces'], vid))
        # Probe physical vehicles with several capacities, not just one globally
        # largest truck; every selected car will still be fully evaluated.
        result = [vid for _, _, vid in sorted(existing)[:5]]
        empty.sort()
        representatives = []
        by_capacity = {}
        for _, vid in empty:
            by_capacity.setdefault(self.vehicles[vid]['max_pieces'], []).append(vid)
            if not representatives or self.vehicles[representatives[-1]]['max_pieces'] != self.vehicles[vid]['max_pieces']:
                representatives.append(vid)
        if representatives:
            selected = [representatives[0], representatives[-1]]
            if len(representatives) > 2:
                selected.append(representatives[self.rng.randrange(1, len(representatives) - 1)])
            for vid in dict.fromkeys(selected):
                peers = by_capacity[self.vehicles[vid]['max_pieces']]
                result.append(vid)
                if len(peers) > 1:
                    # Equal capacities do not prove equal physical-road
                    # feasibility. Rotate alternatives across ALNS iterations.
                    result.append(self.rng.choice(peers[1:]))
        return result

    def insertions(self, state, item):
        """Return checked alternatives with exact total-cost opportunity deltas."""
        options = []
        for vid in self.nearest_routes(state, item):
            self.tick()
            before = state.get(vid)
            ids = before['task_ids'] if before else []
            old_fee = Decimal(before['fee']) if before else Decimal(0)
            local = None
            for position in self.positions(vid, ids, item):
                route = self.evaluate(vid, ids[:position] + [item] + ids[position:])
                if route is None:
                    continue
                delta = Decimal(route['fee']) - old_fee - self.host.common_cost[item]
                score = (delta, route['meters'], route['seconds'], tuple(route['task_ids']))
                if local is None or score < local[0]:
                    local = score, route
            if local:
                options.append((local[0][0], local[1]))
        return sorted(options, key=lambda pair: (pair[0], pair[1]['meters'], pair[1]['vehicle_id']))

    def removal_count(self):
        # Large changes free capacity and alter dedicated/common proportions;
        # smaller changes polish already useful schedules.
        n = max(1, len(self.eligible))
        upper = min(40, max(3, int(math.sqrt(n)) * 2))
        return self.rng.randint(min(2, n), min(n, upper))

    def destroy(self, state, operator):
        assigned = [i for row in state.values() for i in row['task_ids']]
        count = self.removal_count()
        common = [i for i in self.eligible if i not in set(assigned)]
        selected = []
        if operator == 'route' and state:
            # Low utilization and expensive net routes are promising to merge
            # or outsource, but this only chooses a neighborhood, not a result.
            routes = sorted(state.values(), key=lambda r: (
                r['pieces'] / self.vehicles[r['vehicle_id']]['max_pieces'],
                -float(self.host.decorate(r)['_net']), r['vehicle_id']))
            first = routes[self.rng.randrange(min(3, len(routes)))]
            selected.extend(first['task_ids'])
            for row in routes:
                if row is first or len(selected) >= count:
                    continue
                if self.rng.random() < .4:
                    selected.extend(row['task_ids'])
        elif operator == 'worst' and assigned:
            probes = self.rng.sample(assigned, min(12, len(assigned)))
            ranked = []
            lookup = {i: row for row in state.values() for i in row['task_ids']}
            for item in probes:
                self.tick()
                row = lookup[item]
                remaining = [i for i in row['task_ids'] if i != item]
                replacement = self.evaluate(row['vehicle_id'], remaining) if remaining else None
                if remaining and replacement is None:
                    continue
                marginal = Decimal(row['fee']) - (Decimal(replacement['fee']) if replacement else Decimal(0))
                ranked.append((marginal - self.host.common_cost[item], item))
            selected.extend(i for _, i in sorted(ranked, reverse=True)[:count])
        elif operator == 'related' and self.eligible:
            # Include unserved common tickets around the anchor. That is the
            # dedicated/common ratio dimension absent from route-only LNS.
            anchor = self.rng.choice(common if common and self.rng.random() < .65 else self.eligible)
            a = self.tasks[anchor]
            related = [i for i in self.eligible if self.tasks[i]['warehouse_id'] == a['warehouse_id']
                       and self.tasks[i]['signature'] == a['signature']]
            related.sort(key=lambda i: (self.distance_hint(anchor, i), -self.host.common_cost[i], i))
            selected.extend(related[:count])
        else:
            selected.extend(self.rng.sample(assigned, min(count, len(assigned))))
        # Every operator periodically adds expensive common jobs, so repair can
        # increase loading and create another profitable dedicated route.
        external = sorted(common, key=lambda i: (-self.host.common_cost[i], -self.tasks[i]['pieces'], i))
        if external:
            width = min(len(external), max(4, count * 3))
            selected.extend(self.rng.sample(external[:width], min(count // 2 + 1, width)))
        removed = list(dict.fromkeys(selected))
        affected = set(removed)
        partial = {}
        for vid, row in state.items():
            ids = [i for i in row['task_ids'] if i not in affected]
            if ids == row['task_ids']:
                partial[vid] = row
            elif ids:
                route = self.evaluate(vid, ids)
                if route is None:
                    # If a shortened directed path is unknown, reopen the whole
                    # line. Never relabel or splice unverified road evidence.
                    removed.extend(i for i in ids if i not in affected)
                else:
                    partial[vid] = route
        return partial, list(dict.fromkeys(removed))

    def repair(self, state, removed, policy):
        pending = list(removed)
        if policy == 'load':
            pending.sort(key=lambda i: (i not in self.host.mandatory, -self.tasks[i]['pieces'], -self.host.common_cost[i], i))
        else:
            self.rng.shuffle(pending)
            pending.sort(key=lambda i: i not in self.host.mandatory)
        while pending:
            self.tick()
            # Evaluate a rolling subset rather than all N tickets against all
            # vehicles; later iterations and seeds revisit the entire domain.
            window = pending[:min(8, len(pending))]
            choices = []
            for item in window:
                options = self.insertions(state, item)
                mandatory = item in self.host.mandatory
                if not options:
                    continue
                best_delta, best_route = options[0]
                if policy == 'load':
                    best_delta, best_route = min(options, key=lambda pair: (
                        pair[1]['vehicle_id'] not in state,
                        self.vehicles[pair[1]['vehicle_id']]['max_pieces'] - pair[1]['pieces'],
                        pair[0], pair[1]['meters']))
                # A trunk repair can take an initially loss-making first stop;
                # additional common deliveries can share its fixed/trunk cost.
                if not mandatory and best_delta >= 0 and policy != 'trunk':
                    continue
                alternatives = [delta for delta, _ in options]
                if not mandatory:
                    alternatives.append(Decimal(0))  # retain common delivery
                alternatives.sort()
                k = 3 if policy == 'regret3' else 2
                best = alternatives[0]
                regret = sum((alternatives[min(j, len(alternatives) - 1)] - best
                              for j in range(1, k)), Decimal(0))
                if len(options) == 1 and mandatory:
                    regret += Decimal('1000000000')
                if policy == 'load':
                    order = (not mandatory, -self.tasks[item]['pieces'], best_delta, item)
                elif policy == 'trunk':
                    order = (not mandatory, best_delta, -self.host.common_cost[item], item)
                else:
                    order = (not mandatory, -regret, best_delta, item)
                choices.append((order, item, best_route))
            if not choices:
                # These jobs stay common (or explicitly unassigned if mandatory)
                # until another neighborhood makes a feasible placement.
                pending = pending[len(window):]
                continue
            _, item, route = min(choices, key=lambda row: row[0])
            state[route['vehicle_id']] = route
            pending.remove(item)
        # Dropping a wholly optional, unprofitable route is a legitimate move
        # back to common. Pricing nonlinear regional fees still comes from the
        # complete evaluator; this never uses per-stop allocated route costs.
        return {vid: row for vid, row in state.items()
                if any(i in self.host.mandatory for i in row['task_ids']) or
                Decimal(row['fee']) <= sum((self.host.common_cost[i] for i in row['task_ids']), Decimal(0))}

    def best_order(self, vehicle_id, ids, incumbent=None):
        """Directed 2-opt and Or-opt probes; keep exact priced feasible order."""
        best = incumbent or self.evaluate(vehicle_id, ids)
        if best is None:
            return None
        n = len(ids)
        if n < 2:
            return best
        cuts = range(n) if n <= 8 else sorted(set([0, n - 1, *self.rng.sample(range(n), min(6, n))]))
        for left in cuts:
            for right in cuts:
                if left >= right:
                    continue
                candidate_ids = ids[:left] + list(reversed(ids[left:right + 1])) + ids[right + 1:]
                route = self.evaluate(vehicle_id, candidate_ids)
                if route is not None and (Decimal(route['fee']), route['meters'], route['seconds']) < (Decimal(best['fee']), best['meters'], best['seconds']):
                    best = route
        # Relocating a short block can improve an asymmetric road route where
        # reversal is expensive. Never derive a symmetric distance assumption.
        for start in cuts:
            length = min(1 + self.rng.randrange(3), n - start)
            block = ids[start:start + length]
            remaining = ids[:start] + ids[start + length:]
            positions = range(len(remaining) + 1) if n <= 8 else sorted(set([0, len(remaining), len(remaining) // 2]))
            for position in positions:
                route = self.evaluate(vehicle_id, remaining[:position] + block + remaining[position:])
                if route is not None and (Decimal(route['fee']), route['meters'], route['seconds']) < (Decimal(best['fee']), best['meters'], best['seconds']):
                    best = route
        return best

    def cross_routes(self, state):
        """Whole-pair tail exchange, SWAP* style reinsertion and merge probes."""
        rows = list(state.values())
        self.rng.shuffle(rows)
        for left in rows[:min(4, len(rows))]:
            compatible = [r for r in rows if r['vehicle_id'] != left['vehicle_id'] and
                          self.tasks[r['task_ids'][0]]['warehouse_id'] == self.tasks[left['task_ids'][0]]['warehouse_id'] and
                          self.tasks[r['task_ids'][0]]['signature'] == self.tasks[left['task_ids'][0]]['signature']]
            if not compatible:
                continue
            right = min(compatible, key=lambda r: min(self.distance_hint(i, j) for i in left['task_ids'] for j in r['task_ids']))
            a, b = list(left['task_ids']), list(right['task_ids'])
            pair = {left['vehicle_id'], right['vehicle_id']}
            kept = [r for vid, r in state.items() if vid not in pair]
            # Two-opt-star swaps complete suffixes, escaping one-store moves.
            acuts = sorted(set([0, len(a), len(a) // 2, self.rng.randrange(len(a) + 1)]))
            bcuts = sorted(set([0, len(b), len(b) // 2, self.rng.randrange(len(b) + 1)]))
            for ai in acuts:
                for bi in bcuts:
                    left_ids, right_ids = a[:ai] + b[bi:], b[:bi] + a[ai:]
                    if not left_ids or not right_ids:
                        continue
                    if not self.compatible(left['vehicle_id'], left_ids) or not self.compatible(right['vehicle_id'], right_ids):
                        continue
                    l = self.evaluate(left['vehicle_id'], left_ids)
                    r = self.evaluate(right['vehicle_id'], right_ids)
                    if l is not None and r is not None:
                        self.host.consider(kept + [l, r])
            # A larger-capacity unused car can combine two lightly loaded lines.
            combined = a + b
            cars = [vid for vid in self.vehicles if vid in pair or vid not in state]
            for vid in cars:
                if not self.compatible(vid, combined):
                    continue
                for order in (combined, b + a):
                    route = self.evaluate(vid, order)
                    if route is not None:
                        self.host.consider(kept + [route])
            # Remove two jobs first, then choose their insertion positions anew.
            item_a, item_b = self.rng.choice(a), self.rng.choice(b)
            a0, b0 = [i for i in a if i != item_a], [i for i in b if i != item_b]
            candidates_a, candidates_b = [], []
            if self.compatible(left['vehicle_id'], a0 + [item_b]) and self.compatible(right['vehicle_id'], b0 + [item_a]):
                for position in self.positions(left['vehicle_id'], a0, item_b):
                    row = self.evaluate(left['vehicle_id'], a0[:position] + [item_b] + a0[position:])
                    if row is not None:
                        candidates_a.append(row)
                for position in self.positions(right['vehicle_id'], b0, item_a):
                    row = self.evaluate(right['vehicle_id'], b0[:position] + [item_a] + b0[position:])
                    if row is not None:
                        candidates_b.append(row)
                if candidates_a and candidates_b:
                    key = lambda r: (Decimal(r['fee']), r['meters'], r['seconds'])
                    self.host.consider(kept + [min(candidates_a, key=key), min(candidates_b, key=key)])

    def update_weights(self):
        for key in self.weights:
            uses = self.segment_uses[key]
            if uses:
                performance = self.segment_scores[key] / uses
                self.weights[key] = max(.15, .75 * self.weights[key] + .25 * performance)
        self.segment_scores.clear()
        self.segment_uses.clear()

    def run(self):
        work = {r['vehicle_id']: r for r in self.host.best_routes}
        current_rank = self.host.rank(list(work.values()))
        temperature = max(1.0, float(current_rank[1]) * .025)
        stagnant = 0
        stop = 'ITERATION_LIMIT'
        if not self.vehicles or not self.eligible:
            self.iteration_limit = 0
            stop = 'NO_ELIGIBLE_DEDICATED_TASKS'
        try:
            while self.iteration_limit is None or self.iterations < self.iteration_limit:
                self.tick()
                self.iterations += 1
                self.host.phase = 'adaptive_large_neighborhood'
                destroy = self.choose('destroy', self.DESTROY)
                repair = self.choose('repair', self.REPAIR)
                names = (('destroy', destroy), ('repair', repair))
                for name in names:
                    self.operator_counts['/'.join(name)] += 1
                    self.segment_uses[name] += 1
                before = self.host.best_rank
                partial, removed = self.destroy(work, destroy)
                proposal = self.repair(partial, removed, repair)
                if proposal and self.iterations % 3 == 0:
                    chosen = self.rng.choice(list(proposal))
                    improved = self.best_order(chosen, proposal[chosen]['task_ids'], proposal[chosen])
                    if improved is not None:
                        proposal[chosen] = improved
                rows = list(proposal.values())
                proposed_rank = self.host.rank(rows)
                self.host.consider(rows)
                if self.iterations % 5 == 0:
                    self.cross_routes(proposal)
                # Recombine columns from independent work solutions: accepting
                # only one allocation at a time misses simultaneous replacements.
                if self.iterations % 8 == 0 and len(self.host.pool) > self.before_pool:
                    self.host.pack()
                is_best = self.host.best_rank < before
                reward = 1.0
                accept = proposed_rank <= current_rank
                if not accept and proposed_rank[0] <= current_rank[0]:
                    delta = float(proposed_rank[1] - current_rank[1])
                    accept = delta <= 0 or self.rng.random() < math.exp(-min(700, delta / max(.01, temperature)))
                work_improved = proposed_rank < current_rank
                if accept:
                    work, current_rank = proposal, proposed_rank
                    self.accepted += 1
                    reward = 3.0 if work_improved else 1.5
                if is_best:
                    self.improvements += 1
                    stagnant = 0
                    reward = 8.0
                    self._record('improvement')
                    for name in names:
                        self.operator_improvements['/'.join(name)] += 1
                else:
                    stagnant += 1
                for name in names:
                    self.segment_scores[name] += reward
                if self.iterations % 16 == 0:
                    self.update_weights()
                temperature = max(.01, temperature * .995)
                if stagnant >= 40:
                    work = {r['vehicle_id']: r for r in self.host.best_routes}
                    current_rank = self.host.best_rank
                    temperature = max(1.0, float(current_rank[1]) * .02)
                    self.restarts += 1
                    stagnant = 0
                if self.iterations % 32 == 0:
                    self._record('progress')
        except _LocalBudget:
            stop = 'NEIGHBORHOOD_TIME_BUDGET'
        self._record('final')
        return {'method': 'ADAPTIVE_LARGE_NEIGHBORHOOD', 'stop_reason': stop,
                'iterations': self.iterations, 'accepted_work_solutions': self.accepted,
                'incumbent_improvements': self.improvements, 'restarts': self.restarts,
                'candidate_evaluations': self.host.evaluations - self.before_evaluations,
                'new_candidate_routes': len(self.host.pool) - self.before_pool,
                'operator_attempts': dict(self.operator_counts),
                'operator_improvements': dict(self.operator_improvements),
                'operator_weights': {'/'.join(k): round(v, 5) for k, v in self.weights.items()},
                'elapsed_seconds': round(time.monotonic() - self.started, 4),
                'compute_elapsed_seconds': round(self._clock() - self.compute_started, 4),
                'initial_fee': str(self.initial_rank[1]), 'best_fee': str(self.host.best_rank[1]),
                'cost_curve': self.curve, 'optimality_proven': False,
                'route_evidence_policy': 'HOST_EVALUATE_AND_INDEPENDENT_CHECK'}


def improve_neighborhood(search, *, seconds=None, iteration_limit=None, seed=None):
    """Run bounded ALNS and return diagnostics; cancellation propagates unchanged.

    ``iteration_limit=None`` keeps computing until the supplied/global deadline,
    so long user budgets are not exhausted after a fixed four seed schedules.
    A local time slice returns normally, allowing a portfolio scheduler to run
    another method. Global cancellation or budget exceptions remain the host's
    responsibility and never publish an unchecked partial work solution.
    """
    return AdaptiveNeighborhood(search, seconds=seconds,
                                iteration_limit=iteration_limit, seed=seed).run()
