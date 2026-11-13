"""Reproducible cost-first routing experiments on a frozen measured road graph.

No command in this module sends network requests or writes a business database.
Historical ordinary-driving measurements stay reference-only, with their real
timestamps and geometry. Missing directed edges remain missing. A frozen graph
can be built from existing cache evidence, or supplied by the separate collector.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import gzip
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.coordinates import provider_point
from app.planning_inputs import content_hash, encode
from app.road_service import DrivingRoad, DRIVING_ENDPOINT, evidence, road_request
from app.routing_engine import (RoadError, assemble, check_result, common_fee,
                                evaluate_route, normalize, regional_extra)


GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
 provider_key TEXT PRIMARY KEY, from_id TEXT, to_id TEXT,
 meters INTEGER NOT NULL, seconds INTEGER NOT NULL, received_at INTEGER NOT NULL,
 evidence_hash TEXT NOT NULL, content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS edges (
 from_id TEXT NOT NULL, to_id TEXT NOT NULL, provider_key TEXT NOT NULL,
 PRIMARY KEY(from_id,to_id));
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hints (
 evidence_hash TEXT PRIMARY KEY,request TEXT NOT NULL,seconds INTEGER NOT NULL);
"""


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def read_only(path):
    connection = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def graph_digest(connection):
    rows = connection.execute('SELECT provider_key,meters,seconds,received_at,evidence_hash FROM metrics ORDER BY provider_key')
    metrics_hash = content_hash([list(row) for row in rows])
    aliases_hash = content_hash([list(row) for row in connection.execute('SELECT from_id,to_id,provider_key FROM edges ORDER BY from_id,to_id')])
    return content_hash({'metrics_hash': metrics_hash, 'aliases_hash': aliases_hash})


def decode_evidence(content):
    if isinstance(content, str):
        return json.loads(content)
    if isinstance(content, memoryview):
        content = content.tobytes()
    return json.loads(gzip.decompress(content) if bytes(content[:2]) == b'\x1f\x8b' else content)


def compatible_pairs(data):
    """Exact directed edges relevant to the current open-route quote policy."""
    groups = {}
    for task in data['tasks'].values():
        if task['signature']:
            groups.setdefault((task['warehouse_id'], task['signature']), []).append(task['id'])
    pairs = set()
    for (warehouse, _), ids in groups.items():
        pairs.update((warehouse, item) for item in ids)
        pairs.update((a, b) for a in ids for b in ids if a != b)
    return pairs


def write_graph(path, body, items):
    """Freeze existing, hash-valid measured DRIVING edges; never invent an edge."""
    path = Path(path)
    if path.exists():
        raise ValueError('Refusing to replace a frozen graph')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600)
    data = normalize(body)
    points = {**data['tasks'], **data['warehouses']}
    coordinates = {}
    for item in points.values():
        try:
            coordinates.setdefault(encode(provider_point(item)), []).append(item['id'])
        except (KeyError, TypeError, ValueError):
            pass
    wanted = compatible_pairs(data)
    rejected = accepted = 0
    with sqlite3.connect(path) as connection:
        connection.executescript(GRAPH_SCHEMA)
        for item in items:
            try:
                if not DrivingRoad.valid_reference(item, allow_expired=True):
                    rejected += 1
                    continue
                raw = item['request']
                if raw.get('source') == 'DRIVING_PROVIDER_REFERENCE':
                    request = raw
                elif raw.get('source') == 'DRIVING' and raw.get('endpoint') == DRIVING_ENDPOINT and raw.get('strategy') == 32:
                    request = DrivingRoad.reference_request(raw['origin'], raw['destination'])
                    if all(raw.get(k) for k in ('vehicle_id', 'from_id', 'to_id', 'departure_at')):
                        connection.execute('INSERT OR IGNORE INTO hints VALUES (?,?,?)',
                                           (item['evidence_hash'], encode(raw), item['seconds']))
                else:
                    rejected += 1
                    continue
                origins = coordinates.get(encode(request['origin']), [])
                destinations = coordinates.get(encode(request['destination']), [])
                aliases = [(a, b) for a in origins for b in destinations if (a, b) in wanted]
                if not aliases:
                    continue
                key = content_hash(request)
                before = connection.execute('SELECT received_at FROM metrics WHERE provider_key=?', (key,)).fetchone()
                if before is None or before[0] < item['received_at']:
                    a, b = aliases[0]
                    connection.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',
                                       (key, a, b, item['meters'], item['seconds'], item['received_at'],
                                        item['evidence_hash'], gzip.compress(encode(item).encode(), mtime=0)))
                    accepted += 1
                connection.executemany('INSERT OR REPLACE INTO edges VALUES (?,?,?)', [(a, b, key) for a, b in aliases])
            except (KeyError, ValueError, TypeError, OverflowError):
                rejected += 1
        frozen_at = int(time.time())
        metadata = {'frozen_at': frozen_at, 'scope': 'OFFLINE_FROZEN_DRIVING_REFERENCE',
                    'graph_hash': graph_digest(connection), 'input_hash': content_hash(body),
                    'accepted_measurements': accepted, 'rejected_evidence': rejected,
                    'required_directed_edges': len(wanted),
                    'known_directed_edges': connection.execute('SELECT COUNT(*) FROM edges').fetchone()[0]}
        connection.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', [(k, encode(v)) for k, v in metadata.items()])
        connection.commit()
    return metadata


class FrozenEvidenceRoad:
    """Read-only metric metadata in RAM, real geometry loaded only on demand.

    Measurements may be old, so this object must never replace a live app road
    adapter. It deliberately provides no HTTP client or credentials. Metric
    lookup can guide search; every accepted route receives full bound evidence.
    """
    def __init__(self, path, *, cancelled=lambda: False, geometry_cache_size=96):
        self.path = Path(path).resolve()
        self.connection = read_only(self.path)
        self.cancelled = cancelled
        self.metrics = {row['provider_key']: dict(row) for row in self.connection.execute(
            'SELECT provider_key,meters,seconds,received_at,evidence_hash FROM metrics')}
        self.edge_keys = {(row['from_id'], row['to_id']): row['provider_key']
                          for row in self.connection.execute('SELECT * FROM edges')}
        self.matrix_by_ids = {pair: self.metrics[key] for pair, key in self.edge_keys.items() if key in self.metrics}
        self.graph_hash = graph_digest(self.connection)
        self.metadata = {}
        try:
            for row in self.connection.execute('SELECT key,value FROM metadata'):
                try:self.metadata[row['key']] = json.loads(row['value'])
                except (ValueError, TypeError):self.metadata[row['key']] = row['value']
        except sqlite3.OperationalError:
            pass
        self.frozen_at = self.metadata.get('frozen_at', int(time.time()))
        self.route_seed_samples = {}
        try:
            for row in self.connection.execute('SELECT * FROM hints'):
                self.route_seed_samples[row['evidence_hash']] = {'request': json.loads(row['request']), 'seconds': row['seconds']}
        except sqlite3.OperationalError:
            pass
        self.geometry = OrderedDict()
        self._provider_lookup = {}
        self.geometry_cache_size = max(1, int(geometry_cache_size))
        self.hits = self.misses = self.reads = 0

    def close(self):
        self.connection.close()

    def _key(self, origin, destination):
        if isinstance(origin, str) and isinstance(destination, str):
            return self.edge_keys.get((origin, destination))
        tag = tuple(str(point.get(name)) for point in (origin, destination)
                    for name in ('id', 'longitude', 'latitude', 'crs'))
        if tag in self._provider_lookup:
            return self._provider_lookup[tag]
        by_id = self.edge_keys.get((origin.get('id'), destination.get('id')))
        # An id alias is tied to one immutable input. Coordinate fallback makes
        # the provider binding explicit, and rejects a changed-location lookup.
        key = content_hash(DrivingRoad.reference_request(origin, destination))
        found = key if key in self.metrics else by_id if by_id == key else None
        self._provider_lookup[tag] = found
        return found

    def known_segment(self, origin, destination):
        try:
            key = self._key(origin, destination)
            row = self.metrics.get(key)
            return None if row is None else {'meters': row['meters'], 'seconds': row['seconds'],
                                             'received_at': row['received_at'], 'evidence_hash': row['evidence_hash']}
        except (KeyError, TypeError, ValueError):
            return None

    def preflight(self, vehicle, origin):
        provider_point(origin)

    def segment(self, vehicle, origin, destination, departure):
        if self.cancelled():
            raise RoadError('CANCELLED', 'Frozen experiment cancelled')
        key = self._key(origin, destination)
        if key not in self.metrics:
            self.misses += 1
            raise RoadError('ROAD_EDGE_NOT_FROZEN', '固定实路图中没有此方向路段，未使用估算道路')
        if key in self.geometry:
            self.hits += 1
            original = self.geometry[key]
            self.geometry.move_to_end(key)
        else:
            row = self.connection.execute('SELECT content FROM metrics WHERE provider_key=?', (key,)).fetchone()
            original = decode_evidence(row[0])
            if not DrivingRoad.valid_reference(original, allow_expired=True):
                raise RoadError('ROAD_INTEGRITY', '冻结实路证据完整性不符')
            metric = self.metrics[key]
            if (original['meters'], original['seconds'], original['evidence_hash']) != (metric['meters'], metric['seconds'], metric['evidence_hash']):
                raise RoadError('ROAD_INTEGRITY', '冻结路段指标与原始证据不符')
            self.reads += 1
            self.geometry[key] = original
            while len(self.geometry) > self.geometry_cache_size:
                self.geometry.popitem(last=False)
        request = road_request(vehicle, origin, destination, departure, 'DRIVING', strategy=32)
        if origin['crs'] != 'GCJ02' or destination['crs'] != 'GCJ02':
            request.update(coordinate_policy='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02',
                           provider_origin=provider_point(origin), provider_destination=provider_point(destination),
                           coordinate_conversion='coordtransform-2.1.2-reference')
        return evidence(request, original['meters'], original['seconds'], 'DRIVING', False,
                        original['polyline'], 'TRUCK_NOT_VERIFIED',
                        time_status='CURRENT_TRAFFIC_REFERENCE', received_at=original['received_at'],
                        frozen_graph_hash=self.graph_hash, frozen_at=self.frozen_at,
                        provider_evidence_hash=original['evidence_hash'],
                        notice='离线冻结的真实普通驾车参考；未核验货车及计划发车时刻，不得当作当前实时交通')

    def stats(self):
        ages = [int(time.time()) - row['received_at'] for row in self.metrics.values()]
        return {'source': 'DRIVING', 'calls': 0, 'call_limit': 0, 'cache_hits': self.hits,
                'frozen_geometry_reads': self.reads, 'missing_edges': self.misses,
                'known_directed_edges': len(self.matrix_by_ids), 'planned_directed_edges': len(self.edge_keys), 'provider_edges': len(self.metrics),
                'graph_hash': self.graph_hash, 'scope': 'OFFLINE_FROZEN_DRIVING_REFERENCE',
                'oldest_measurement_age_seconds': max(ages, default=None),
                'newest_measurement_age_seconds': min(ages, default=None)}


def snapshot_input(database, group_id):
    with read_only(database) as connection:
        group = connection.execute('SELECT * FROM demand_groups WHERE id=?', (group_id,)).fetchone()
        if group is None:
            raise ValueError('Unknown demand group')
        snapshot = connection.execute('SELECT * FROM input_snapshots WHERE group_id=? ORDER BY version DESC LIMIT 1', (group_id,)).fetchone()
        if snapshot is None:
            raise ValueError('Demand group has no frozen input')
        body = json.loads(snapshot['content'])
        if content_hash(body) != snapshot['content_hash']:
            raise ValueError('Input snapshot hash mismatch')
        plan = connection.execute('SELECT * FROM plan_versions WHERE group_id=? AND snapshot_id=? ORDER BY version DESC LIMIT 1', (group_id, snapshot['id'])).fetchone()
        previous = None
        if plan:
            saved = json.loads(plan['content'])
            if content_hash(saved) != plan['content_hash']:
                raise ValueError('Plan hash mismatch')
            previous = {'id': plan['id'], 'version': plan['version'], 'content_hash': plan['content_hash'], 'result': saved['result']}
        return body, previous, {'group_id': group_id, 'group_name': group['name'], 'delivery_date': group['delivery_date'],
                                'snapshot_id': snapshot['id'], 'snapshot_version': snapshot['version'], 'input_hash': snapshot['content_hash']}


def freeze_case(database, output, group_id, *, road_database=None, road_json=None):
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    body, previous, origin = snapshot_input(database, group_id)
    graph = Path(road_database).resolve() if road_database else output / 'frozen-roads.db'
    if not road_database:
        def cache_items():
            with read_only(database) as connection:
                for row in connection.execute('SELECT content FROM road_cache'):
                    try:yield json.loads(row['content'])
                    except (ValueError, TypeError):continue
            if road_json:
                value = json.loads(Path(road_json).read_text())
                yield from value.values() if isinstance(value, dict) else value
        write_graph(graph, body, cache_items())
    road = FrozenEvidenceRoad(graph)
    summary = {**origin, 'schema_version': 1, 'source_database_read_only': str(Path(database).resolve()),
               'body_file': 'input.json', 'baseline_file': 'baseline.json' if previous else None,
               'road_database': str(graph), 'graph_hash': road.graph_hash, 'road_stats': road.stats(),
               'required_directed_edges': len(compatible_pairs(normalize(body))),
               'scope': 'FIXED_INPUT_AND_FROZEN_MEASURED_ROADS_ONLY'}
    road.close()
    private_json(output / 'input.json', body)
    if previous:private_json(output / 'baseline.json', previous)
    private_json(output / 'case.json', summary)
    return summary


def freeze_historical_case(template_body, history_database, delivery_date, output, *, road_database):
    """Replay an entire historical day using the current frozen constraints.

    Exact names must map every historical store to one current point. No row is
    discarded to fit the graph. These are historical *quantities* under current
    warehouse, vehicle, price and time settings, not historical expense claims.
    """
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = normalize(template_body)
    by_name = {}
    for task in data['tasks'].values():by_name.setdefault(task['name'], []).append(task['id'])
    with read_only(history_database) as connection:
        source = [dict(row) for row in connection.execute(
            'SELECT d.id,d.store_id,d.import_id,d.pieces,d.source_rows,s.name FROM daily_demands d '
            'JOIN stores s ON s.id=d.store_id WHERE d.delivery_date=? ORDER BY s.name,d.id', (delivery_date,))]
    if not source:raise ValueError('Historical day has no demands')
    unmapped = sorted({row['name'] for row in source if len(by_name.get(row['name'], [])) != 1})
    if unmapped:raise ValueError('Historical rows must all map unambiguously: ' + ','.join(unmapped))
    original_rows = {row['store_id']: row for row in template_body['tasks']}
    grouped = {}
    for row in source:
        sid = by_name[row['name']][0]
        item = grouped.setdefault(sid, {'pieces': 0, 'source_demand_ids': [], 'source_import_ids': [], 'source_rows': []})
        item['pieces'] += row['pieces']
        item['source_demand_ids'].append(row['id'])
        item['source_import_ids'].append(row['import_id'])
        item['source_rows'].extend(json.loads(row['source_rows']))
    body = deepcopy(template_body)
    difference = datetime.fromisoformat(delivery_date) - datetime.fromisoformat(body['delivery_date'])
    def shifted(row):
        row = deepcopy(row)
        for key in ('earliest_departure_at', 'business_open_at', 'business_close_at', 'latest_arrival_at'):
            if row.get(key):row[key] = (datetime.fromisoformat(row[key]) + difference).isoformat()
        return row
    body['delivery_date'] = delivery_date
    body.pop('group_id', None)
    body['source'] = 'BENCHMARK_HISTORICAL_QUANTITIES_CURRENT_CONFIGURATION'
    body['stores'] = [shifted(row) for row in body['stores'] if row['id'] in grouped]
    body['warehouses'] = [shifted(row) for row in body['warehouses']]
    body['tasks'] = []
    for sid, source_rows in sorted(grouped.items()):
        row = shifted(original_rows[sid])
        row.update(pieces=source_rows['pieces'], source_rows=source_rows['source_rows'],
                   historical_source_demand_ids=source_rows['source_demand_ids'],
                   historical_source_import_ids=sorted(set(source_rows['source_import_ids'])))
        body['tasks'].append(row)
    quantities = Counter()
    pieces = Counter()
    for row in body['tasks']:
        quantities[row['warehouse_id']] += 1
        pieces[row['warehouse_id']] += row['pieces']
    body['totals'] = {'task_count': len(body['tasks']), 'total_pieces': sum(pieces.values()),
                      'active_warehouse_count': len(quantities),
                      'warehouses': [{'warehouse_id': row['id'], 'warehouse_code': row.get('code'),
                                      'warehouse_name': row.get('name'), 'task_count': quantities[row['id']],
                                      'total_pieces': pieces[row['id']]} for row in body['warehouses']]}
    scenario = {'policy': 'HISTORICAL_REQUIREMENT_QUANTITIES_WITH_CURRENT_FROZEN_CONFIGURATION',
                'history_database_read_only': str(Path(history_database).resolve()),
                'historical_delivery_date': delivery_date, 'configuration_template_date': template_body['delivery_date'],
                'configuration_template_hash': content_hash(template_body), 'source_demand_rows': len(source),
                'source_rows_all_mapped': True, 'source_rows_discarded': 0,
                'source_requirement_hash': content_hash(source)}
    body['benchmark_scenario'] = scenario
    normalized = normalize(body)
    common = [i for i, row in normalized['tasks'].items() if row['common_unit_price'] is not None]
    baseline = assemble(normalized, [], common, set(normalized['tasks']) - set(common))
    road = FrozenEvidenceRoad(road_database)
    summary = {'schema_version': 1, 'group_id': None, 'group_name': 'historical-' + delivery_date,
               'delivery_date': delivery_date, 'snapshot_id': None, 'snapshot_version': None,
               'input_hash': content_hash(body), 'body_file': 'input.json', 'baseline_file': 'baseline.json',
               'road_database': str(Path(road_database).resolve()), 'graph_hash': road.graph_hash,
               'road_stats': road.stats(), 'required_directed_edges': len(compatible_pairs(normalized)),
               'scope': 'ALL_HISTORICAL_DEMANDS_WITH_CURRENT_CONFIGURATION', 'scenario': scenario}
    road.close()
    private_json(output / 'input.json', body)
    private_json(output / 'baseline.json', {'id': 'ALL_COMMON_REFERENCE', 'version': 0,
                                           'content_hash': content_hash(baseline), 'result': baseline})
    private_json(output / 'case.json', summary)
    return summary


def replay_baseline(data, saved, road):
    if not saved:
        return None
    routes = [evaluate_route(data, row['vehicle_id'], row['task_ids'], road) for row in saved['result']['routes']]
    result = assemble(data, routes, [row['task_id'] for row in saved['result']['common']],
                      [row['task_id'] for row in saved['result']['unassigned']])
    if any(row['errors'] or row['fee'] is None for row in routes):
        return None
    if any(issue['code'] != 'DRIVING_REFERENCE' for issue in result['check']['issues']):
        return None
    return result


def independent_audit(data, result, road):
    """Recount coverage, money, capacities and times directly from frozen inputs.

    Base quote arithmetic and complete allocation are independent of the search.
    Region matching intentionally delegates to the established policy evaluator;
    its own dedicated regression suite covers aliases and regional boundaries.
    """
    errors = []
    seen = Counter()
    used = Counter()
    dedicated = Decimal(0)
    common = Decimal(0)
    meters = 0
    dedicated_pieces = 0
    capacities = 0
    utilization = []
    cent = Decimal('.01')
    def cents(amount):return Decimal(amount).quantize(cent, rounding=ROUND_HALF_UP)
    for route in result.get('routes', []):
        vid = route['vehicle_id']
        used[vid] += 1
        vehicle = data['vehicles'].get(vid)
        ids = route['task_ids']
        seen.update(ids)
        if vehicle is None or not vehicle['available']:
            errors.append('UNAVAILABLE_VEHICLE')
            continue
        tasks = [data['tasks'].get(item) for item in ids]
        if not ids or any(item is None for item in tasks):
            errors.append('UNKNOWN_OR_EMPTY_ROUTE')
            continue
        pieces = sum(item['pieces'] for item in tasks)
        if pieces != route['pieces'] or pieces > vehicle['max_pieces']:
            errors.append('CAPACITY_OR_PIECES')
        if any(item['warehouse_id'] != vehicle['warehouse_id'] for item in tasks):errors.append('WAREHOUSE_MISMATCH')
        if tasks[0]['signature'] is None or len({item['signature'] for item in tasks}) != 1:errors.append('QUOTE_INCOMPATIBLE')
        segments = route.get('segments', [])
        if len(segments) != len(ids):
            errors.append('MISSING_ROAD_SEGMENTS')
            continue
        previous = data['warehouses'][vehicle['warehouse_id']]
        leave = datetime.fromisoformat(previous['earliest_departure_at'])
        distance = 0
        for task, segment in zip(tasks, segments):
            metric = road.known_segment(previous, task)
            if metric is None or (segment['meters'], segment['seconds']) != (metric['meters'], metric['seconds']):
                errors.append('ROAD_GRAPH_MISMATCH')
            arrival = leave + timedelta(seconds=segment['seconds'])
            start = max(arrival, datetime.fromisoformat(task['open_at']))
            if start > datetime.fromisoformat(task['close_at']) or (task.get('latest_arrival_at') and arrival > datetime.fromisoformat(task['latest_arrival_at'])):
                errors.append('TIME_WINDOW')
            leave = start + timedelta(minutes=task['unload_minutes'])
            distance += segment['meters']
            previous = task
        quote = tasks[0]['quote']
        if quote['mode'] in ('KM', 'KM_REGION'):
            base = Decimal(distance) / 1000 * Decimal(quote['per_km'])
        else:
            base = Decimal(quote['base_price']) + max(len(ids) - quote['included_stops'], 0) * Decimal(quote['extra_stop_price'])
        expected = cents(base + regional_extra(quote, tasks))
        if route['fee'] is None or cents(route['fee']) != expected:errors.append('DEDICATED_FEE')
        if distance != route['meters']:errors.append('ROUTE_DISTANCE')
        dedicated += expected
        meters += distance
        dedicated_pieces += pieces
        capacities += vehicle['max_pieces']
        utilization.append({'vehicle_id': vid, 'pieces': pieces, 'capacity': vehicle['max_pieces'],
                            'load_percent': str(cents(Decimal(pieces) / vehicle['max_pieces'] * 100)),
                            'meters': distance, 'fee': str(expected), 'stops': len(ids)})
    for row in result.get('common', []):
        task = data['tasks'].get(row['task_id'])
        seen[row['task_id']] += 1
        if task is None or task['common_unit_price'] is None:
            errors.append('COMMON_PRICE_MISSING')
            continue
        expected = cents(Decimal(task['pieces']) * Decimal(task['common_unit_price']))
        if row.get('pieces') != task['pieces'] or row.get('fee') is None or cents(row['fee']) != expected:
            errors.append('COMMON_FEE')
        common += expected
    for row in result.get('unassigned', []):seen[row['task_id']] += 1
    if set(seen) != set(data['tasks']) or any(count != 1 for count in seen.values()):errors.append('COVERAGE')
    if any(count > 1 for count in used.values()):errors.append('VEHICLE_REUSED')
    total = cents(dedicated + common)
    if cents(result['total_fee']) != total:errors.append('TOTAL_FEE')
    if result.get('unassigned') or not result.get('fees_complete'):errors.append('INCOMPLETE_DELIVERY')
    proof = check_result(data, result)
    errors.extend(issue['code'] for issue in proof['issues'] if issue['code'] != 'DRIVING_REFERENCE')
    return {'valid': not errors, 'errors': sorted(set(errors)), 'total_fee': str(total),
            'dedicated_fee': str(cents(dedicated)), 'common_fee': str(cents(common)),
            'road_meters': meters, 'used_vehicles': len(used), 'dedicated_tasks': sum(len(row['task_ids']) for row in result.get('routes', [])),
            'common_tasks': len(result.get('common', [])), 'unassigned_tasks': len(result.get('unassigned', [])),
            'dedicated_pieces': dedicated_pieces, 'fleet_capacity_used': capacities,
            'weighted_load_percent': str(cents(Decimal(dedicated_pieces) / capacities * 100)) if capacities else None,
            'per_route': utilization, 'global_optimality_proven': result.get('algorithm', {}).get('optimality_proven', False)}


@dataclass
class Algorithm:
    name: str
    entrypoint: str
    options: dict = field(default_factory=dict)

    def function(self):
        module, name = self.entrypoint.split(':', 1)
        return getattr(importlib.import_module(module), name)


def run_benchmark(case_path, algorithms, *, seeds=(0,), budgets=(30,), output=None, warm_start=True, cancel_after=None):
    case_path = Path(case_path).resolve()
    case = json.loads(case_path.read_text())
    folder = case_path.parent
    body = json.loads((folder / case['body_file']).read_text())
    if content_hash(body) != case['input_hash']:
        raise ValueError('Frozen input changed')
    data = normalize(body)
    saved = json.loads((folder / case['baseline_file']).read_text()) if case.get('baseline_file') else None
    output = Path(output).resolve() if output else folder / 'runs'
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    baseline_road = FrozenEvidenceRoad(case['road_database'])
    if baseline_road.graph_hash != case['graph_hash']:
        baseline_road.close()
        raise ValueError('Frozen road graph changed')
    incumbent = replay_baseline(data, saved, baseline_road)
    baseline_audit = independent_audit(data, incumbent, baseline_road) if incumbent else None
    baseline_road.close()
    rows = []
    winner = None
    for spec in algorithms:
        function = spec.function()
        for budget in budgets:
            for seed in seeds:
                started = time.monotonic()
                deadline = started + budget
                def cancelled():return cancel_after is not None and time.monotonic() - started >= cancel_after
                road = FrozenEvidenceRoad(case['road_database'], cancelled=cancelled)
                if road.graph_hash != case['graph_hash']:
                    road.close()
                    raise ValueError('Frozen road graph changed between algorithms')
                row = {'algorithm': spec.name, 'entrypoint': spec.entrypoint, 'options': spec.options,
                       'budget_seconds': budget, 'seed': seed, 'input_hash': case['input_hash'], 'graph_hash': case['graph_hash'],
                       'warm_start': bool(warm_start and incumbent is not None), 'cancel_after_seconds': cancel_after}
                try:
                    result = function(deepcopy(body), road, budget, seed, cancelled,
                                      initial_result=deepcopy(incumbent) if warm_start and incumbent else None, **spec.options)
                    audit = independent_audit(data, result, road)
                    row.update(status='VALID' if audit['valid'] else 'INVALID', audit=audit,
                               result_algorithm=result.get('algorithm', {}), road_stats=road.stats())
                    elapsed = time.monotonic() - started
                    row['elapsed_seconds'] = round(elapsed, 6)
                    row['budget_overshoot_seconds'] = round(max(0, time.monotonic() - deadline), 6)
                    name = f'{spec.name}-b{budget}-s{seed}'
                    private_json(output / (name + '-result.json'), result)
                    row['result_file'] = str(output / (name + '-result.json'))
                    if audit['valid']:
                        # Cost is the business objective. Distance/load are
                        # reported separately, never allowed to increase price.
                        rank = (Decimal(audit['total_fee']), audit['road_meters'], audit['used_vehicles'])
                        if winner is None or rank < winner[0]:winner = rank, row
                except Exception as exc:
                    row.update(status='ERROR', exception_type=type(exc).__name__, elapsed_seconds=round(time.monotonic() - started, 6))
                finally:
                    road.close()
                rows.append(row)
                private_json(output / 'progress.json', {'case': case, 'baseline_audit': baseline_audit, 'runs': rows,
                                                       'best': winner[1] if winner else None})
    report = {'schema_version': 1, 'scope': 'SAME_FROZEN_INPUT_AND_REAL_ROAD_GRAPH', 'case': case,
              'source_business_database_written': False, 'external_calls': 0, 'baseline_audit': baseline_audit,
              'runs': rows, 'best': winner[1] if winner else None,
              'all_runs_same_input_and_graph': len({(row['input_hash'], row['graph_hash']) for row in rows}) <= 1,
              'global_optimality_claim': 'Only an independently established exact proof can justify a global optimum.'}
    private_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--database', required=True)
    freeze.add_argument('--group-id', required=True)
    freeze.add_argument('--output', required=True)
    freeze.add_argument('--road-database')
    freeze.add_argument('--road-json')
    run = commands.add_parser('run')
    run.add_argument('--case', required=True)
    run.add_argument('--algorithm', action='append', required=True, help='label=module:function')
    run.add_argument('--seeds', default='0,1,2')
    run.add_argument('--budgets', default='15,60')
    run.add_argument('--output')
    run.add_argument('--cold-start', action='store_true')
    run.add_argument('--cancel-after', type=float)
    args = parser.parse_args()
    if args.command == 'freeze':
        report = freeze_case(args.database, args.output, args.group_id, road_database=args.road_database, road_json=args.road_json)
    else:
        algorithms = [Algorithm(*entry.split('=', 1)) for entry in args.algorithm]
        report = run_benchmark(args.case, algorithms, seeds=[int(s) for s in args.seeds.split(',')],
                               budgets=[float(s) for s in args.budgets.split(',')], output=args.output,
                               warm_start=not args.cold_start, cancel_after=args.cancel_after)
    print(json.dumps({'command': args.command, 'scope': report.get('scope'),
                      'best': report.get('best'), 'runs': len(report.get('runs', []))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
