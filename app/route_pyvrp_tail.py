"""Try one measured pure-kilometre group against a protected whole allocation."""
from collections import OrderedDict, defaultdict
from decimal import Decimal
from math import isfinite
from time import monotonic

from app.coordinates import provider_point
from app.planning_inputs import content_hash
from app.road_service import DRIVING_ENDPOINT, DrivingRoad, TruckRoad, provider_coordinate_key
from app.route_columns import select_portfolio
from app.route_verification import CollectedDrivingRoad, complete, recipe, replay
from app.routing_engine import RoadError, StopSearch, assemble, common_fee, normalize


def _checked_assembly(result):
    return (result['fees_complete'] and not result['unassigned']
            and all(issue['code'] == 'DRIVING_REFERENCE' for issue in result['check']['issues']))


def _compatible_groups(body, data, incumbent, group_facts, control):
    groups = defaultdict(set)
    for task_id, task in data['tasks'].items():
        quote = task['quote']
        if task['signature'] and quote and quote['mode'] == 'KM' and not quote.get('region_surcharge'):
            groups[task['warehouse_id'], task['signature']].add(task_id)

    def priority(key):
        cost = sum(Decimal(common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']) or '0')
                   for i in groups[key])
        return -cost, key

    common_ids = [row['task_id'] for row in incumbent['common']]
    task_key = 'store_id' if 'stores' in body else 'id'
    for key in sorted(groups, key=priority):
        control()
        members = groups[key]
        if len(members) < 2:
            continue
        group_routes = [row for row in incumbent['routes'] if row['task_ids'][0] in members]
        outside = [row for row in incumbent['routes'] if row['task_ids'][0] not in members]
        occupied = {row['vehicle_id'] for row in outside}
        minimum_pieces = min(data['tasks'][i]['pieces'] for i in members)
        allowed = {vid for vid, vehicle in data['vehicles'].items() if vehicle['available']
                   and vid not in occupied and vehicle['warehouse_id'] == key[0]
                   and vehicle['max_pieces'] >= minimum_pieces}
        if not allowed or any(row['vehicle_id'] not in allowed for row in group_routes):
            continue
        scoped_body = {**body, 'tasks': [row for row in body['tasks'] if row[task_key] in members],
                       'vehicles': [row for row in body['vehicles'] if row['id'] in allowed]}
        scoped = normalize(scoped_body)
        if (scoped['tasks'] != {i: data['tasks'][i] for i in members}
                or scoped['vehicles'] != {vid: data['vehicles'][vid] for vid in allowed}):
            continue
        try:
            group_facts(scoped)
        except ValueError:
            continue
        draft = recipe({'routes': group_routes, 'common': [row for row in incumbent['common']
                                                          if row['task_id'] in members]})
        return scoped_body, scoped, draft, outside, [i for i in common_ids if i not in members]
    return None


class _ReceiptGeometry:
    """Lazily read full sealed receipts only when actually materializing routes."""

    def __init__(self, source, matched):
        self.source = source
        self.matched = matched
        self.cache = OrderedDict()

    def __len__(self):
        return len(self.matched)

    def get(self, key):
        metrics = self.matched.get(key)
        if metrics is None:
            return None
        receipt = self.cache.get(key)
        if receipt is None:
            receipt = self.source.batch_receipt(key)
            if receipt is None or any(receipt.get(name) != value for name, value in metrics.items()):
                return None
            self.cache[key] = receipt
            DrivingRoad.trim_geometry(self.cache)
        else:
            self.cache.move_to_end(key)
        return receipt


class _MeasuredRoad:
    """The selector sees the same signed directional metrics as native search."""

    deadline_limited_candidates = True
    indexed_current_receipts = False

    def __init__(self, source, metrics, receipt_keys, fresh):
        self.metrics = metrics
        self.geometry = (CollectedDrivingRoad(_ReceiptGeometry(source, receipt_keys),
                                               source.batch_started_at, allow_expired=True)
                         if fresh else source)

    def known_segment(self, origin, destination):
        return self.metrics.get((origin['id'], destination['id']))

    def segment(self, vehicle, origin, destination, departure):
        if self.known_segment(origin, destination) is None:
            raise RoadError('ROAD_NOT_COLLECTED', '本轮可用道路中没有这条方向路段')
        return self.geometry.segment(vehicle, origin, destination, departure)


def _signed_metrics(row):
    return (isinstance(row, dict) and not row.get('heuristic_only')
            and isinstance(row.get('evidence_hash'), str) and len(row['evidence_hash']) == 64
            and all(type(row.get(field)) is int and row[field] >= 0
                    for field in ('meters', 'seconds', 'received_at')))


def _measure_indexed(road, origins, indexed, control):
    origin_aliases, destination_aliases = defaultdict(list), defaultdict(list)
    for task_id, point in origins:
        control()
        try:
            coords = provider_coordinate_key(provider_point(point))
        except (KeyError, TypeError, ValueError):
            continue
        origin_aliases[coords].append((task_id, point['id']))
        if task_id is not None:
            destination_aliases[coords].append(task_id)
    metrics, receipt_keys, outgoing = {}, {}, defaultdict(list)
    for key, row in indexed:
        control()
        destinations = destination_aliases.get(row.destination)
        possible_origins = origin_aliases.get(row.origin)
        if not destinations or not possible_origins or row.received_at < road.batch_started_at:
            continue
        metric = {'meters': row.meters, 'seconds': row.seconds, 'received_at': row.received_at,
                  'evidence_hash': row.evidence_hash}
        if not _signed_metrics(metric):
            continue
        for origin_id, point_id in possible_origins:
            for destination_id in destinations:
                if origin_id == destination_id:
                    continue
                control()
                metrics[point_id, destination_id] = metric
                receipt_keys[key] = metric
                outgoing[origin_id].append((destination_id, row.meters, row.seconds))
    rows = []
    for origin_id, _ in origins:
        control()
        for destination_id, meters, seconds in sorted(outgoing.get(origin_id, ())):
            rows.append((origin_id, destination_id, meters, seconds))
    control()
    measured = _MeasuredRoad(road, metrics, receipt_keys, fresh=True)
    measured.indexed_current_receipts = True
    return measured, rows


def _measure(road, data, fresh, control):
    tasks = data['tasks']
    warehouse = data['warehouses'][next(iter(tasks.values()))['warehouse_id']]
    ids = sorted(tasks)
    origins = [(None, warehouse), *((i, tasks[i]) for i in ids)]
    experiments = getattr(road, 'deep_experiments', None)
    if fresh and warehouse['id'] not in tasks and getattr(experiments, 'pyvrp_indexed_current_receipts', False):
        indexed = road.indexed_batch_receipts(control)
        if indexed is not None:
            return _measure_indexed(road, origins, indexed, control)
    metrics, receipt_keys, rows = {}, {}, []
    for origin_id, origin in origins:
        for destination_id in ids:
            if origin_id == destination_id:
                continue
            control()
            destination = tasks[destination_id]
            if fresh:
                try:
                    expected = DrivingRoad.reference_request(origin, destination)
                    key = content_hash(expected)
                except (ValueError, TypeError, KeyError):
                    continue
                # Absence costs a dictionary lookup only. In particular, do
                # not read nonbinding metric hints or call the live adapter.
                row = road.batch_receipt(key)
                if not _signed_metrics(row) or row['received_at'] < road.batch_started_at:
                    continue
                request = row.get('request', {})
                if (not isinstance(request, dict) or row.get('source') != 'DRIVING'
                        or row.get('time_status') != 'CURRENT_TRAFFIC_REFERENCE'):
                    continue
                if request != expected:
                    if (request.get('source') != 'DRIVING' or request.get('endpoint') != DRIVING_ENDPOINT
                            or request.get('strategy') != 32):
                        continue
                    try:
                        if DrivingRoad.reference_request(request['origin'], request['destination']) != expected:
                            continue
                    except (KeyError, TypeError, ValueError):
                        continue
            else:
                row = road.known_segment(origin, destination)
                if not _signed_metrics(row):
                    continue
            metric = {field: row[field] for field in ('meters', 'seconds', 'received_at', 'evidence_hash')}
            if fresh:
                receipt_keys[key] = metric
            metrics[origin['id'], destination_id] = metric
            rows.append((origin_id, destination_id, metric['meters'], metric['seconds']))
    control()
    return _MeasuredRoad(road, metrics, receipt_keys, fresh), rows


def improve(body, road, incumbent, *, seconds, seed, control, cancelled):
    """Return a strictly cheaper independently checked whole plan or its incumbent."""
    started = monotonic()
    allowance = min(20., max(0., float(seconds))) if isfinite(seconds) else 0.
    deadline = started + allowance
    generation_deadline = started + min(8., allowance * .4)
    details = {'before_fee': incumbent['total_fee'], 'after_fee': incumbent['total_fee'],
               'direct_fee_reduction': '0.00', 'skipped': True, 'candidate_count': 0,
               'measured_directions': 0, 'new_network_calls': 0, 'stop_reason': 'INSUFFICIENT_HEADROOM'}

    def hard_control():
        if cancelled():
            raise StopSearch('CANCELLED')
        control()

    def checkpoint():
        hard_control()
        if monotonic() >= deadline:
            raise StopSearch('PYVRP_STAGE_BUDGET')

    def generation_control():
        checkpoint()
        if monotonic() >= generation_deadline:
            raise StopSearch('PYVRP_GENERATION_BUDGET')

    try:
        hard_control()
        if allowance < .5:
            return incumbent, details
        fresh = isinstance(road, DrivingRoad) and getattr(road, 'fresh_only', False)
        if not fresh:
            if isinstance(road, DrivingRoad):
                details['stop_reason'] = 'CURRENT_MEASUREMENTS_REQUIRED'
                return incumbent, details
            details['stop_reason'] = 'UNSUPPORTED_ROAD'
            if isinstance(road, TruckRoad):
                return incumbent, details
            try:
                from scripts.benchmark_algorithms import FrozenEvidenceRoad
            except ImportError:
                return incumbent, details
            if not isinstance(road, FrozenEvidenceRoad):
                return incumbent, details
        from app.route_pyvrp_runtime import generate, group_facts

        data = normalize(body)
        if not complete(data, incumbent, generation_control):
            details['stop_reason'] = 'NO_COMPLETE_INCUMBENT'
            return incumbent, details
        group = _compatible_groups(body, data, incumbent, group_facts, generation_control)
        if group is None:
            details['stop_reason'] = 'NO_ELIGIBLE_KM_GROUP'
            return incumbent, details
        scoped_body, scoped, draft, outside, outside_common = group
        details.update(skipped=False, group_tasks=len(scoped['tasks']), group_vehicles=len(scoped['vehicles']),
                       frozen_outside_routes=len(outside))
        measured, rows = _measure(road, scoped, fresh, generation_control)
        details.update(measured_directions=len(rows), preparation_seconds=round(monotonic() - started, 6))
        if fresh and getattr(getattr(road, 'deep_experiments', None), 'pyvrp_indexed_current_receipts', False):
            details['indexed_current_receipts'] = measured.indexed_current_receipts
        if not rows:
            details['stop_reason'] = 'NO_MEASURED_DIRECTIONS'
            return incumbent, details
        generation_control()
        specs, native = generate(scoped, rows, seconds=generation_deadline - monotonic(), seed=seed,
                                 control=generation_control)
        details.update(native=native, generation_seconds=round(monotonic() - started, 6))
        checkpoint()
        proposals = {}
        for row in specs:
            checkpoint()
            vid, order = row['vehicle_id'], tuple(row['task_ids'])
            if (vid in scoped['vehicles'] and order and len(order) == len(set(order))
                    and all(i in scoped['tasks'] for i in order)):
                proposals.setdefault((vid, order), {'vehicle_id': vid, 'task_ids': list(order)})
        details['candidate_count'] = len(proposals)
        if not proposals:
            details['stop_reason'] = (native['stop_reason'] if native['stop_reason'] != 'SEARCH_COMPLETED'
                                      else 'NO_NATIVE_PROPOSALS')
            return incumbent, details

        before_selection = monotonic()
        try:
            local = replay(scoped, draft, measured, checkpoint)
        except RoadError as exc:
            if exc.code in {'CANCELLED', 'WALL_BUDGET'}:
                raise StopSearch(exc.code) from exc
            checkpoint()
            details['stop_reason'] = 'SCOPE_INCUMBENT_NOT_MEASURED'
            return incumbent, details
        checkpoint()
        if not _checked_assembly(local):
            details['stop_reason'] = 'SCOPE_INCUMBENT_NOT_MEASURED'
            return incumbent, details
        remaining = min(12., deadline - monotonic() - min(.5, allowance * .05))
        if remaining <= .075:
            details['stop_reason'] = 'NO_SELECTION_BUDGET'
            return incumbent, details
        stopped = None

        def selector_cancelled():
            nonlocal stopped
            if stopped is None:
                try:
                    checkpoint()
                except StopSearch as exc:
                    stopped = exc
            return stopped is not None

        selected = select_portfolio(scoped_body, measured, [local], seconds=remaining,
                                    cancelled=selector_cancelled, candidate_recipes=list(proposals.values()))
        if stopped is not None:
            raise stopped
        checkpoint()
        details.update(selection_seconds=round(monotonic() - before_selection, 6),
                       scope_before_fee=local['total_fee'], scope_after_fee=selected['total_fee'],
                       selector=selected['algorithm'])
        if Decimal(selected['total_fee']) >= Decimal(local['total_fee']):
            details['stop_reason'] = 'NO_STRICT_SCOPE_FEE_GAIN'
            return incumbent, details
        checkpoint()
        joined = assemble(data, [*outside, *selected['routes']],
                          [*outside_common, *[row['task_id'] for row in selected['common']]], [])
        if not _checked_assembly(joined):
            details['stop_reason'] = 'WHOLE_PROOF_REJECTED'
            return incumbent, details
        gain = Decimal(incumbent['total_fee']) - Decimal(joined['total_fee'])
        if gain <= 0:
            details['stop_reason'] = 'NO_STRICT_WHOLE_FEE_GAIN'
            return incumbent, details
        details.update(after_fee=joined['total_fee'], direct_fee_reduction=str(gain),
                       stop_reason='ACCEPTED_STRICT_WHOLE_FEE_GAIN')
        return joined, details
    except StopSearch as exc:
        details['stop_reason'] = exc.reason
        if exc.reason not in {'PYVRP_STAGE_BUDGET', 'PYVRP_GENERATION_BUDGET'}:
            exc.pyvrp_tail_diagnostics = details
            raise
        return incumbent, details
    finally:
        details['elapsed_seconds'] = round(monotonic() - started, 6)
