"""A daily job's immutable, actually collected driving measurements.

Search may use historical measurements to propose routes. Final verification
collects a fresh batch once; subsequent repairs reuse that batch and collect
only missing bridge directions. Existing directions are never refreshed again
inside the batch, so a repaired plan is checked against one coherent set of
measurements rather than chasing changing traffic responses.
"""
from copy import deepcopy
from decimal import Decimal
from itertools import combinations, permutations
import time

from app.api import ApiError
from app.coordinates import provider_point
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, DRIVING_ENDPOINT, evidence, road_request
from app.routing_engine import (RoadError, StopSearch, assemble, check_result,
                                common_fee, evaluate_route, normalize)
from app.route_metrics import evaluate_candidate


def complete(data, result):
    try:
        proof = check_result(data, result)
        return (not result['unassigned'] and result['fees_complete']
                and all(issue['code'] == 'DRIVING_REFERENCE' for issue in proof['issues']))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def result_rank(result):
    return (len(result.get('unassigned', [])), Decimal(result['total_fee']),
            sum(route['meters'] for route in result['routes']), len(result['routes']))


def recipe(result):
    return {'routes': [{'vehicle_id': r['vehicle_id'], 'task_ids': list(r['task_ids'])}
                       for r in result['routes']],
            'common_ids': [r['task_id'] for r in result['common']]}


def replay(data, draft, road, control=None):
    routes = [evaluate_route(data, row['vehicle_id'], list(row['task_ids']), road, control)
              for row in draft['routes']]
    covered = {i for row in routes for i in row['task_ids']}
    common = set(draft['common_ids'])
    return assemble(data, routes, common, set(data['tasks']) - covered - common)


class CollectedDrivingRoad:
    """Read-only evidence rebinding on a fixed set of this job's receipts."""
    force_refresh = False
    allow_historical = False
    call_limit = 0
    io_seconds = 0.

    def __init__(self, receipts, started_at):
        self.receipts = receipts
        self.started_at = started_at
        self.hits = 0

    @staticmethod
    def key(origin, destination):
        return content_hash(DrivingRoad.reference_request(origin, destination))

    def known_segment(self, origin, destination):
        source = self.receipts.get(self.key(origin, destination))
        if source is None:
            return None
        if not DrivingRoad.valid_reference(source) or source['received_at'] < self.started_at:
            return None
        return {k: source[k] for k in ('meters', 'seconds', 'received_at')}

    def segment(self, vehicle, origin, destination, departure):
        key = self.key(origin, destination)
        source = self.receipts.get(key)
        if source is None or self.known_segment(origin, destination) is None:
            raise RoadError('ROAD_NOT_COLLECTED', '此连接路段尚未取得本轮高德道路回执')
        request = road_request(vehicle, origin, destination, departure, 'DRIVING', strategy=32)
        if origin.get('crs') != 'GCJ02' or destination.get('crs') != 'GCJ02':
            request.update(coordinate_policy='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02',
                           provider_origin=provider_point(origin), provider_destination=provider_point(destination),
                           coordinate_conversion='coordtransform-2.1.2-reference')
        # Geometry appears once. The original source receipt is reconstructible
        # by combining its immutable metadata with this unchanged polyline.
        receipt = {k: deepcopy(v) for k, v in source.items() if k != 'polyline'}
        item = evidence(request, source['meters'], source['seconds'], 'DRIVING', False,
                        deepcopy(source['polyline']), 'TRUCK_NOT_VERIFIED',
                        time_status='CURRENT_TRAFFIC_REFERENCE', received_at=source['received_at'],
                        provider_cache_key=key, provider_evidence_hash=source['evidence_hash'],
                        provider_received_at=source['received_at'], actual_source_receipt=receipt,
                        notice='本轮实际采集的普通驾车参考；未核验货车限行及未来发车路况')
        self.hits += 1
        return item

    def stats(self):
        return {'source': 'DRIVING', 'calls': 0, 'cache_hits': self.hits,
                'call_limit': 0, 'collected_reference_edges': len(self.receipts)}

    def verify(self, data, result, finished_at):
        if not complete(data, result):
            raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '道路复核后仍有配送冲突，未保存新方案。')
        selected = {}
        for route in result['routes']:
            origin = data['warehouses'][data['vehicles'][route['vehicle_id']]['warehouse_id']]
            for task_id, segment in zip(route['task_ids'], route['segments']):
                destination = data['tasks'][task_id]
                key = self.key(origin, destination)
                actual = self.receipts.get(key)
                receipt = {**segment['actual_source_receipt'], 'polyline': segment['polyline']}
                request = receipt.get('request', {})
                if (actual is None or receipt != actual or not DrivingRoad.valid_reference(receipt)
                        or not self.started_at <= receipt['received_at'] <= finished_at
                        or request.get('source') != 'DRIVING' or request.get('endpoint') != DRIVING_ENDPOINT
                        or request.get('strategy') != 32
                        or self.key(request['origin'], request['destination']) != key
                        or segment['provider_evidence_hash'] != receipt['evidence_hash']
                        or segment['meters'] != receipt['meters'] or segment['seconds'] != receipt['seconds']):
                    raise ApiError(422, 'DEEP_ROAD_RECEIPT', '选中道路与本轮真实回执不一致，未保存新方案。')
                selected[key] = {k: receipt[k] for k in ('received_at', 'meters', 'seconds', 'evidence_hash')}
                origin = destination
        return selected


class DrivingCollector:
    """Only this wrapper may extend a final measurement batch."""
    def __init__(self, road, cancelled, control, max_bridge_edges=64, optional_control=None):
        self.road = road
        self.cancelled = cancelled
        self.control = control
        self.optional_control = optional_control or control
        self.started_at = int(time.time())
        self.receipts = {}
        self.failed_directions = {}
        self.frozen = CollectedDrivingRoad(self.receipts, self.started_at)
        self.calls_before = road.stats().get('calls', 0)
        self.failures_before = road.stats().get('failures', 0)
        self.max_bridge_edges = max_bridge_edges
        self.bridge_edges = 0
        self.bridge_budget_exhausted = 0
        self.initial_pass = True
        self._original_flags = (road.force_refresh, road.allow_historical,
                                road._freshened_keys, road._fresh_bound_keys)
        road.force_refresh, road.allow_historical = True, False
        road._freshened_keys, road._fresh_bound_keys = set(), set()

    def close(self):
        (self.road.force_refresh, self.road.allow_historical,
         self.road._freshened_keys, self.road._fresh_bound_keys) = self._original_flags

    def known_segment(self, origin, destination):
        return self.frozen.known_segment(origin, destination)

    def segment(self, vehicle, origin, destination, departure):
        self.control()
        if self.cancelled():
            raise StopSearch('CANCELLED')
        key = self.frozen.key(origin, destination)
        if key not in self.receipts:
            if key in self.failed_directions:
                code, message = self.failed_directions[key]
                raise RoadError(code, message)
            if not self.initial_pass:
                self.optional_control()
                if self.bridge_edges >= self.max_bridge_edges:
                    self.bridge_budget_exhausted += 1
                    raise RoadError('DEEP_BRIDGE_BUDGET', '本轮新增连接路段复核已达到时限内的搜索上限')
                self.bridge_edges += 1
            try:
                raw = self.road.segment(vehicle, origin, destination, departure)
            except RoadError as exc:
                # A permutation search must not retry the same failed provider
                # direction thousands of times in one immutable validation batch.
                self.failed_directions[key] = exc.code, exc.message
                raise
            if (not DrivingRoad.valid_reference(raw) or raw['received_at'] < self.started_at
                    or key not in self.road._freshened_keys
                    or raw['request'].get('source') != 'DRIVING'
                    or raw['request'].get('endpoint') != DRIVING_ENDPOINT
                    or raw['request'].get('strategy') != 32
                    or self.frozen.key(raw['request']['origin'], raw['request']['destination']) != key):
                raise RoadError('DEEP_NOT_FRESH', '该道路未取得本轮实时回执')
            self.receipts[key] = deepcopy(raw)
        return self.frozen.segment(vehicle, origin, destination, departure)

    def stats(self):
        return self.road.stats()


class _ProposalRoad:
    """Historical bridge metrics only rank proposals; never final evidence."""
    def __init__(self, frozen, source):
        self.frozen, self.source = frozen, source

    def known_segment(self, a, b):
        return self.frozen.known_segment(a, b) or self.source.known_segment(a, b)


class _SmallRouteRoad:
    """A per-neighborhood share of the job's fresh bridge allowance."""
    def __init__(self, collector, allowance):
        self.collector = collector
        self.started = collector.bridge_edges
        self.allowance = max(0, allowance)

    def segment(self, vehicle, origin, destination, departure):
        if (self.collector.frozen.known_segment(origin, destination) is None and
                self.collector.bridge_edges - self.started >= self.allowance):
            raise RoadError('SMALL_ROUTE_BRIDGE_BUDGET', '小线路重排的新增连接路段预算已用完')
        return self.collector.segment(vehicle, origin, destination, departure)


class _FreshMetricView:
    """Cache checked scalar hints locally; full proof still checks each receipt.

    The collector only appends immutable directions. Failed/missing lookups are
    not cached because a later proposal can collect that direction. Avoid hashing
    the same large provider polyline for every step of every permutation.
    """
    def __init__(self, frozen):
        self.frozen = frozen
        self.metrics = {}

    def known_segment(self, origin, destination):
        key = self.frozen.key(origin, destination)
        if key not in self.metrics:
            metric = self.frozen.known_segment(origin, destination)
            if metric is None:
                return None
            self.metrics[key] = metric
        return self.metrics[key]


def _small_orders(data, ids):
    """Cheap useful starts first, then every ordering of this bounded set."""
    seeds = [ids, list(reversed(ids)),
             sorted(ids, key=lambda i: (data['tasks'][i].get('latest_arrival_at') or data['tasks'][i]['close_at'], i)),
             sorted(ids, key=lambda i: (data['tasks'][i]['open_at'], i))]
    seeds.extend(ids[n:] + ids[:n] for n in range(1, len(ids)))
    seen = set()
    for order in seeds:
        key = tuple(order)
        if key not in seen:
            seen.add(key)
            yield key
    for order in permutations(ids):
        if order not in seen:
            yield order


def _best_small_route(data, vehicle_id, ids, collector, repair_control, bridge_allowance,
                      *, first_feasible=False):
    """Search <=8 stops against fresh receipts, retaining each verified win.

    Old metrics never reject an order. Missing directions are collected within
    both the job and neighborhood limits. Known orders are priced without
    geometry; only improving candidates are fully materialized and retained.
    This is not a global proof: bridge/time limits can leave orders unexplored.
    """
    diagnostics = {'orders_examined': 0, 'new_bridge_edges': 0, 'unmeasured_orders': 0,
                   'search_completed': False, 'stop_reason': 'INELIGIBLE'}
    if not 2 <= len(ids) <= 8 or len(set(ids)) != len(ids):
        return None, diagnostics
    car = data['vehicles'].get(vehicle_id)
    first = data['tasks'][ids[0]]
    if (car is None or not car['available'] or not first['signature'] or
            any(data['tasks'][i]['warehouse_id'] != car['warehouse_id'] or
                data['tasks'][i]['signature'] != first['signature'] for i in ids) or
            sum(data['tasks'][i]['pieces'] for i in ids) > car['max_pieces']):
        return None, diagnostics
    bounded = _SmallRouteRoad(collector, bridge_allowance)
    measured = _FreshMetricView(collector.frozen)
    best = None
    best_score = None
    try:
        for order in _small_orders(data, list(ids)):
            repair_control()
            diagnostics['orders_examined'] += 1
            metric = evaluate_candidate(data, vehicle_id, list(order), measured, repair_control)
            if metric is None:
                if collector.bridge_edges - bounded.started >= bounded.allowance:
                    diagnostics['unmeasured_orders'] += 1
                    continue
                actual = evaluate_route(data, vehicle_id, list(order), bounded, repair_control)
                metric = actual
            else:
                actual = None
            if metric['errors'] or metric['fee'] is None:
                if any(error['code'].startswith(('ROAD_', 'DEEP_', 'SMALL_')) for error in metric['errors']):
                    diagnostics['unmeasured_orders'] += 1
                continue
            score = Decimal(metric['fee']), metric['meters'], metric['seconds'], tuple(order)
            if best_score is not None and score >= best_score:
                continue
            if actual is None:
                actual = evaluate_route(data, vehicle_id, list(order), collector.frozen, repair_control)
            if not actual['errors'] and actual['fee'] is not None:
                best, best_score = actual, score
                if first_feasible:
                    diagnostics['stop_reason'] = 'FIRST_FEASIBLE_ORDER'
                    break
        else:
            diagnostics.update(search_completed=not diagnostics['unmeasured_orders'], stop_reason='ORDERS_EXAMINED')
    except StopSearch as exc:
        if collector.cancelled():
            raise StopSearch('CANCELLED')
        diagnostics['stop_reason'] = exc.reason
    diagnostics['new_bridge_edges'] = collector.bridge_edges - bounded.started
    diagnostics['bridge_allowance'] = bounded.allowance
    return best, diagnostics


def _repair_proposals(data, routes, common, collector, progress, repair_control):
    """Compare cheap local repairs, materializing only their new bridges."""
    fixed = []
    priced_common = set(common)
    used = {r['vehicle_id'] for r in routes}
    proposal_road = _ProposalRoad(collector.frozen, collector.road)
    repair_log = []
    repair_recipes = []
    remaining_bad = sum(bool(route.get('errors')) or route.get('fee') is None for route in routes)
    def optional_open():
        try:
            repair_control()
            return True
        except StopSearch:
            if collector.cancelled():
                raise StopSearch('CANCELLED')
            return False
    for original in routes:
        vid, ids = original['vehicle_id'], list(original['task_ids'])
        reevaluated = evaluate_route(data, vid, ids, collector.frozen)
        if not reevaluated['errors'] and reevaluated['fee'] is not None:
            fixed.append(reevaluated)
            continue
        reorder_search = None
        bad_ids = {e.get('task_id') for e in reevaluated['errors']}
        first_bad = next((n for n, i in enumerate(ids) if i in bad_ids), 0)
        # This verified-prefix fallback never invents an unmeasured bridge.
        fallback_prefix = ids[:first_bad]
        fallback_common = ids[first_bad:]
        offers = []

        def offer(order, dropped=(), second=None, label='local'):
            if any(data['tasks'][i]['common_unit_price'] is None for i in dropped):
                return
            specs = ([{'vehicle_id': vid, 'task_ids': list(order)}] if order else [])
            if second:
                specs.append({'vehicle_id': second[0], 'task_ids': list(second[1])})
            estimated = []
            for spec in specs:
                metric = evaluate_candidate(data, spec['vehicle_id'], spec['task_ids'], proposal_road)
                if metric is None or metric['errors'] or metric['fee'] is None:
                    return
                estimated.append(metric)
            fee = sum((Decimal(r['fee']) for r in estimated), Decimal(0))
            fee += sum((Decimal(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']))
                        for i in dropped), Decimal(0))
            offers.append((fee, label, specs, tuple(dropped)))

        offer(fallback_prefix, fallback_common, label='verified_prefix_and_common')
        # Keep the fallback separately; candidate pruning may not discard it.
        prefix_fallback = list(offers)
        optional = [i for i in ids if data['tasks'][i]['common_unit_price'] is not None]
        # Large routes compare suspicious/slow stops first, with a bounded set
        # of two-store combinations instead of materializing every permutation.
        priority = sorted(optional, key=lambda i: (i not in bad_ids, -data['tasks'][i]['unload_minutes'], i))[:32]
        for count in (1, 2):
            for dropped in combinations(priority, count):
                if not optional_open():
                    break
                offer([i for i in ids if i not in dropped], dropped, label='priced_common_repair')
        for a in range(min(len(ids), 48)):
            for b in range(a + 1, min(len(ids), 48)):
                if not optional_open():
                    break
                offer(ids[:a] + list(reversed(ids[a:b+1])) + ids[b+1:], label='reorder_repair')
            if not optional_open():
                break
        spare = [v for v, car in data['vehicles'].items() if v not in used and car['available']
                 and car['warehouse_id'] == data['vehicles'][vid]['warehouse_id']]
        for split in range(1, len(ids)):
            for car in spare[:4]:
                if not optional_open():
                    break
                offer(ids[:split], second=(car, ids[split:]), label='unused_car_split')
            if not optional_open():
                break
        candidates = sorted(offers, key=lambda x: (x[0], x[1]))[:12]
        candidates += [item for item in prefix_fallback if item not in candidates]
        accepted = []
        for _, label, specs, dropped in candidates:
            try:
                repair_control()
                actual = [evaluate_route(data, spec['vehicle_id'], spec['task_ids'], collector)
                          for spec in specs]
                if any(r['errors'] or r['fee'] is None for r in actual):
                    continue
                price = sum((Decimal(r['fee']) for r in actual), Decimal(0))
                price += sum((Decimal(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']))
                              for i in dropped), Decimal(0))
                accepted.append((price, label, actual, dropped))
                repair_recipes.extend({'vehicle_id': r['vehicle_id'], 'task_ids': list(r['task_ids'])} for r in actual)
            except (StopSearch, RoadError):
                if collector.cancelled():
                    raise StopSearch('CANCELLED')
                break
        # A prefix is a subset of already measured data, so it is safe even
        # when the optional repair search ran out of compute or bridge calls.
        for _, label, specs, dropped in prefix_fallback:
            actual = [evaluate_route(data, s['vehicle_id'], s['task_ids'], collector.frozen) for s in specs]
            if not any(r['errors'] or r['fee'] is None for r in actual):
                price = sum((Decimal(r['fee']) for r in actual), Decimal(0))
                price += sum((Decimal(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']))
                              for i in dropped), Decimal(0))
                accepted.append((price, label, actual, dropped))
                repair_recipes.extend({'vehicle_id': r['vehicle_id'], 'task_ids': list(r['task_ids'])} for r in actual)
        if not accepted:
            # A complete baseline takes priority over optional exhaustive cost
            # search. If cheap repairs fail, stop at the first fresh feasible
            # order, leaving remaining bad lines their compute/bridge allowance.
            remaining_bridges = max(0, collector.max_bridge_edges - collector.bridge_edges)
            fresh_reorder, reorder_search = _best_small_route(
                data, vid, ids, collector, repair_control,
                min(32, remaining_bridges // max(1, remaining_bad)), first_feasible=True)
            if fresh_reorder is not None:
                accepted.append((Decimal(fresh_reorder['fee']), 'complete_small_reorder', [fresh_reorder], ()))
                repair_recipes.append({'vehicle_id': vid, 'task_ids': list(fresh_reorder['task_ids'])})
        remaining_bad = max(0, remaining_bad - 1)
        if not accepted:
            raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '最新道路下未找到完整可行线路；请核对收货时间、车辆及共配资料后重试。')
        _, label, selected, dropped = min(accepted, key=lambda x: (x[0], len(x[2]), x[1]))
        fixed.extend(selected)
        priced_common.update(dropped)
        used.update(r['vehicle_id'] for r in selected)
        repair_log.append({'vehicle_id': vid, 'method': label, 'common_store_count': len(dropped),
                           'candidate_count': len(offers), 'small_reorder': reorder_search})
        progress({'stage': 'FEASIBILITY_REPAIR', 'repaired_routes': len(repair_log)})
    return assemble(data, fixed, priced_common, []), repair_log, repair_recipes


def _refine_repaired_groups(data, current, originals, repair_log, collector, repair_control):
    """Improve original small groups only after every delivery is feasible.

    A cheap fallback may have split the group or moved some stores to common.
    Preserve the original set so a complete reorder can absorb those stores
    again. Replace only routes wholly belonging to that original group.
    """
    recipes = []
    by_vehicle = {row['vehicle_id']: row for row in originals}
    for record in repair_log:
        try:
            repair_control()
        except StopSearch:
            if collector.cancelled():
                raise StopSearch('CANCELLED')
            break
        original = by_vehicle[record['vehicle_id']]
        ids = list(original['task_ids'])
        members = set(ids)
        affected = [r for r in current['routes'] if members.intersection(r['task_ids'])]
        if (any(not set(r['task_ids']).issubset(members) for r in affected) or
                any(r['vehicle_id'] == original['vehicle_id'] and r not in affected for r in current['routes'])):
            continue
        route, details = _best_small_route(data, original['vehicle_id'], ids, collector, repair_control,
            min(32, max(0, collector.max_bridge_edges - collector.bridge_edges)))
        record['small_reorder'] = details
        if route is None:
            continue
        recipes.append({'vehicle_id': route['vehicle_id'], 'task_ids': list(route['task_ids'])})
        kept = [r for r in current['routes'] if r not in affected]
        common = [r['task_id'] for r in current['common'] if r['task_id'] not in members]
        trial = assemble(data, kept + [route], common, [])
        if complete(data, trial) and result_rank(trial) < result_rank(current):
            current = trial
            record.update(initial_method=record['method'], method='complete_small_reorder', common_store_count=0)
    return current, recipes


def _consolidate_small_routes(data, current, collector, repair_control, progress):
    """Compare complete pair unions after repair, never force fewer vehicles.

    The accepted whole allocation is the fallback at every optional deadline.
    Up to 32 small neighborhoods are visited; shared immutable receipts bound
    provider calls independently of the number of permutations or vehicles.
    """
    tried = set()
    recipes = []
    log = []
    searches = 0
    while searches < 32:
        try:
            repair_control()
        except StopSearch:
            if collector.cancelled():
                raise StopSearch('CANCELLED')
            break
        used = {r['vehicle_id'] for r in current['routes']}
        pairs = []
        for left, right in combinations(current['routes'], 2):
            ids = list(left['task_ids']) + list(right['task_ids'])
            key = tuple(sorted(ids))
            if key in tried or not 2 <= len(ids) <= 8 or len(set(ids)) != len(ids):
                continue
            first = data['tasks'][ids[0]]
            if not first['signature'] or any(data['tasks'][i]['warehouse_id'] != first['warehouse_id'] or
                    data['tasks'][i]['signature'] != first['signature'] for i in ids):
                continue
            pair_cars = {left['vehicle_id'], right['vehicle_id']}
            pieces = sum(data['tasks'][i]['pieces'] for i in ids)
            cars = [v for v in data['vehicles'].values() if v['available'] and
                    v['warehouse_id'] == first['warehouse_id'] and v['max_pieces'] >= pieces and
                    (v['id'] in pair_cars or v['id'] not in used)]
            if not cars:
                continue
            # Ordinary-driving metrics/quote are vehicle independent after
            # warehouse and capacity checks, but proof binds the chosen car.
            car = min(cars, key=lambda v: (v['id'] not in pair_cars, v['max_pieces'], v['id']))
            pairs.append((-(Decimal(left['fee']) + Decimal(right['fee'])), key, ids, pair_cars, car))
        if not pairs:
            break
        _, key, ids, pair_cars, car = min(pairs, key=lambda row: (row[0], row[1]))
        tried.add(key)
        searches += 1
        before = current['total_fee']
        route, details = _best_small_route(data, car['id'], ids, collector, repair_control,
            min(32, max(0, collector.max_bridge_edges - collector.bridge_edges)))
        improved = False
        if route is not None:
            recipes.append({'vehicle_id': car['id'], 'task_ids': list(route['task_ids'])})
            kept = [r for r in current['routes'] if r['vehicle_id'] not in pair_cars]
            trial = assemble(data, kept + [route], [r['task_id'] for r in current['common']], [])
            if complete(data, trial) and result_rank(trial) < result_rank(current):
                current = trial
                improved = True
        log.append({'task_count': len(ids), 'vehicle_id': car['id'], 'improved': improved,
                    'before_fee': before, 'after_fee': current['total_fee'], **details})
        progress({'stage': 'FINAL_SELECTION', 'message': '正在比较小线路合并费用', 'best_fee': current['total_fee'],
                  'consolidation_searches': searches})
    return current, log, recipes


def _whole_price(data, draft, road):
    """Rank a whole proposal using current metrics; this is not evidence."""
    try:
        if len(set(draft['common_ids'])) != len(draft['common_ids']):
            return None
        used = set(draft['common_ids'])
        if any(i not in data['tasks'] or data['tasks'][i]['common_unit_price'] is None for i in used):
            return None
        price = sum((Decimal(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']))
                     for i in used), Decimal(0))
        cars = set()
        for row in draft['routes']:
            if row['vehicle_id'] in cars or used.intersection(row['task_ids']):
                return None
            current = evaluate_candidate(data, row['vehicle_id'], list(row['task_ids']), road)
            if current is None or current['errors'] or current['fee'] is None:
                return None
            cars.add(row['vehicle_id'])
            used.update(row['task_ids'])
            price += Decimal(current['fee'])
        return price if used == set(data['tasks']) else None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _allocation_key(draft):
    return (tuple(sorted((r['vehicle_id'], tuple(r['task_ids'])) for r in draft['routes'])),
            tuple(sorted(draft['common_ids'])))


def validate_driving(body, road, candidate, alternatives, *, cancelled, control,
                     repair_control, progress, selection_seconds=5, complete_recipes=(),
                     max_bridge_edges=64):
    """Return the cheapest verified allocation found on the fresh batch."""
    data = normalize(body)
    collector = DrivingCollector(road, cancelled, control, max_bridge_edges=max_bridge_edges,
                                 optional_control=repair_control)
    repair_log = []
    repair_recipes = []
    try:
        progress({'stage': 'ROAD_VALIDATION', 'best_fee': candidate['total_fee']})
        draft = recipe(candidate)
        routes = [evaluate_route(data, s['vehicle_id'], s['task_ids'], collector, control)
                  for s in draft['routes']]
        if cancelled():
            raise StopSearch('CANCELLED')
        collector.initial_pass = False
        current = assemble(data, routes, draft['common_ids'], [])
        if not complete(data, current):
            progress({'stage': 'FEASIBILITY_REPAIR'})
            current, repair_log, repair_recipes = _repair_proposals(data, routes, draft['common_ids'], collector,
                                                                   progress, repair_control)
        if not complete(data, current):
            raise ApiError(422, 'DEEP_NO_FEASIBLE_PLAN', '道路复核后仍有未完成配送，未保存新方案。')
        # Establish the complete safety incumbent before spending any remaining
        # optional budget on full permutations of the originally broken groups.
        current, refined_recipes = _refine_repaired_groups(
            data, current, routes, repair_log, collector, repair_control)
        repair_recipes.extend(refined_recipes)
        # A historically cheapest complete plan need not remain cheapest after
        # traffic changes. Compare the promising other *whole* rounds too;
        # only their missing bridges may extend the immutable current batch.
        proposals = []
        seen = {_allocation_key(recipe(current))}
        proposal_road = _ProposalRoad(collector.frozen, collector.road)
        for allocation in complete_recipes:
            identity = _allocation_key(allocation)
            if identity in seen:
                continue
            seen.add(identity)
            price = _whole_price(data, allocation, proposal_road)
            if price is not None and price <= Decimal(current['total_fee']):
                proposals.append((price, allocation))
        whole_comparisons = 0
        for _, allocation in sorted(proposals, key=lambda row: row[0])[:8]:
            try:
                repair_control()
                trial = replay(data, allocation, collector, control)
                whole_comparisons += 1
                repair_recipes.extend({'vehicle_id': r['vehicle_id'], 'task_ids': list(r['task_ids'])}
                                      for r in trial['routes'] if not r['errors'] and r['fee'] is not None)
                if complete(data, trial) and result_rank(trial) < result_rank(current):
                    current = trial
            except StopSearch:
                if cancelled():
                    raise StopSearch('CANCELLED')
                break
        current, consolidations, consolidated_recipes = _consolidate_small_routes(
            data, current, collector, repair_control, progress)
        repair_recipes.extend(consolidated_recipes)
        # Recombine all known route recipes on the immutable collected matrix.
        # Missing historical directions cannot enter this final selection.
        progress({'stage': 'FINAL_SELECTION', 'best_fee': current['total_fee']})
        from app.route_columns import select_portfolio
        seconds = selection_seconds() if callable(selection_seconds) else selection_seconds
        joined = select_portfolio(body, collector.frozen, [current] + list(alternatives),
                                  candidate_recipes=repair_recipes, complete_recipes=complete_recipes,
                                  seconds=max(0, seconds), cancelled=cancelled)
        if complete(data, joined) and result_rank(joined) < result_rank(current):
            current = joined
        finished_at = int(time.time())
        selected = collector.frozen.verify(data, current, finished_at)
        stats = road.stats()
        ages = [row['received_at'] for row in selected.values()]
        validation = {'mode': 'FRESH_DRIVING_VALIDATED' if selected else 'COMMON_ONLY_VALIDATED',
                      'fresh': bool(selected), 'started_at': collector.started_at, 'finished_at': finished_at,
                      'selected_segments': sum(len(r['segments']) for r in current['routes']),
                      'selected_provider_count': len(selected), 'source_count': len(collector.receipts),
                      'actual_calls': stats.get('calls', 0) - collector.calls_before,
                      'total_job_calls': stats.get('calls', 0),
                      'failures': stats.get('failures', 0) - collector.failures_before,
                      'oldest_received_at': min(ages, default=None), 'newest_received_at': max(ages, default=None),
                      'selected_subset_of_collected': set(selected).issubset(collector.receipts),
                      'selected_provider_sources': selected, 'new_bridge_edges': collector.bridge_edges,
                      'bridge_edge_limit': collector.max_bridge_edges,
                      'bridge_budget_exhausted': collector.bridge_budget_exhausted,
                      'whole_plan_comparisons': whole_comparisons,
                      'repairs': repair_log, 'consolidations': consolidations,
                      'failed_provider_directions': len(collector.failed_directions),
                      'requires_truck_review': True,
                      'global_optimality_proven': False}
        return current, validation
    finally:
        collector.close()
