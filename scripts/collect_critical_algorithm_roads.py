"""Prepare or explicitly collect a small, isolated measured-road extension.

Prepare is the default and performs no HTTP. --offline-import reads existing
measured evidence without reading credentials or creating an HTTP client.
Live execution requires --live
and a new output directory. It uses one DrivingRoad instance, so max_calls
counts all GET attempts including retries, and reuses the shared QPS limiter.
Only genuinely missing, same-warehouse, quote-compatible directed edges are
eligible. Existing frozen measurements and historical hints are preserved.
Both algorithms must be rerun on the new graph at the same budget/seed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import gzip
import hashlib
import json
import math
from pathlib import Path
import signal
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.planning_inputs import content_hash, encode
from app.road_graph import ReferenceGraph
from app.road_service import DRIVING_ENDPOINT, DrivingRoad
from app.routing_engine import RoadError, common_fee, normalize
from scripts.benchmark_algorithms import (
    FrozenEvidenceRoad, decode_evidence, graph_digest, private_json, read_only,
)


def load_json(path):
    value = Path(path).read_bytes()
    return json.loads(gzip.decompress(value) if value[:2] == b'\x1f\x8b' else value)


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def resolved(base, value):
    return (Path(base) / value).resolve()


def validate_limits(directions, max_calls, wait_seconds):
    if directions not in {12, 24}:
        raise ValueError('Directions must use the supported bounded edge budgets: 12 or 24')
    if (isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 0 <= max_calls <= 96
            or not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 600):
        raise ValueError('max_calls must be 0..96; wait_seconds must be finite and within (0,600]')


def prepare(case_file, result_file, *, directions=12, max_calls=24, wait_seconds=120):
    validate_limits(directions, max_calls, wait_seconds)
    case_file, result_file = Path(case_file).resolve(), Path(result_file).resolve()
    case, result = load_json(case_file), load_json(result_file)
    result = result.get('result', result)
    body_path = resolved(case_file.parent, case['body_file'])
    graph_path = resolved(case_file.parent, case['road_database'])
    body = load_json(body_path)
    if content_hash(body) != case['input_hash']:
        raise ValueError('Frozen input identity differs from case')
    data = normalize(body)
    points = {**data['warehouses'], **data['tasks']}
    missing = result.get('algorithm', {}).get('diagnostics', {}).get('missing_edges', [])
    attempts = Counter()
    for row in missing:
        count = row['attempts']
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError('Missing-edge observation count must be a positive integer')
        attempts[row['from_id'], row['to_id']] += count
    rejected = Counter()
    candidates = []
    road = FrozenEvidenceRoad(graph_path)
    try:
        if road.graph_hash != case['graph_hash']:
            raise ValueError('Frozen graph identity differs from case')
        result_graph = result.get('algorithm', {}).get('road_stats', {}).get('graph_hash')
        if result_graph != road.graph_hash:
            raise ValueError('Saved diagnostics must identify the exact frozen source graph')
        for (origin_id, destination_id), count in attempts.items():
            origin, destination = points.get(origin_id), data['tasks'].get(destination_id)
            if (origin is None or destination is None or origin_id == destination_id
                    or not destination.get('signature')):
                rejected['UNKNOWN_OR_INELIGIBLE_POINT'] += 1
                continue
            warehouse = destination['warehouse_id']
            source_task = data['tasks'].get(origin_id)
            if (source_task is None and origin_id != warehouse) or (source_task is not None and (
                    source_task['warehouse_id'] != warehouse or source_task['signature'] != destination['signature'])):
                rejected['WAREHOUSE_OR_QUOTE_INCOMPATIBLE'] += 1
                continue
            tasks = [destination] + ([source_task] if source_task is not None else [])
            pieces = sum(task['pieces'] for task in tasks)
            fleet = sorted((vehicle for vehicle in data['vehicles'].values() if vehicle['available']
                            and vehicle['warehouse_id'] == warehouse and vehicle['max_pieces'] >= pieces),
                           key=lambda vehicle: vehicle['id'])
            if not fleet:
                rejected['NO_ELIGIBLE_PHYSICAL_CAR'] += 1
                continue
            if road.known_segment(origin, destination) is not None:
                rejected['ALREADY_MEASURED'] += 1
                continue
            try:
                request = DrivingRoad.reference_request(origin, destination)
            except (KeyError, TypeError, ValueError):
                rejected['COORDINATES_NOT_READY'] += 1
                continue
            opportunity = sum((Decimal(common_fee(task['pieces'], task['common_unit_price']) or '0')
                               for task in tasks), Decimal(0))
            mandatory = sum(task['common_unit_price'] is None for task in tasks)
            candidates.append({'key': content_hash(request), 'from_id': origin_id, 'to_id': destination_id,
                'origin': origin, 'destination': destination, 'vehicle': fleet[0],
                'departure': data['warehouses'][warehouse]['earliest_departure_at'],
                'observed_missing_attempts': count, 'common_opportunity_fee': str(opportunity),
                'mandatory_tasks': mandatory, 'priority_score': str(opportunity * count)})
        candidates.sort(key=lambda row: (-row['mandatory_tasks'], -Decimal(row['priority_score']),
                                         -row['observed_missing_attempts'],
                                         -Decimal(row['common_opportunity_fee']), row['from_id'], row['to_id']))
        # The budget is directed aliases. Coordinate-identical aliases may share
        # one actual provider GET; no reverse direction is inferred.
        jobs = candidates[:directions]
        return {'schema_version': 1, 'scope': 'CRITICAL_MISSING_DIRECTED_ROADS_ONLY',
            'case_file': str(case_file), 'case_sha256': file_sha(case_file),
            'result_file': str(result_file), 'result_sha256': file_sha(result_file),
            'body_file': str(body_path), 'input_hash': content_hash(body),
            'source_graph': str(graph_path), 'source_graph_hash': road.graph_hash,
            'ranking': 'MANDATORY_FIRST_THEN_OBSERVED_COUNT_TIMES_COMMON_OPPORTUNITY_FEE',
            'ranking_is_a_proposal_not_a_saving_proof': True,
            'requested_directed_edges': directions, 'eligible_missing_directions': len(candidates),
            'selected_directions': len(jobs), 'selected_provider_keys': len({job['key'] for job in jobs}),
            'max_calls': max_calls, 'wait_seconds': wait_seconds, 'rejected': dict(rejected), 'jobs': jobs,
            'source_business_database_written': False, 'both_algorithms_must_rerun_on_new_graph': True,
            'existing_measurements_must_not_be_replaced': True}
    finally:
        road.close()


class PrivateCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE evidence(key TEXT PRIMARY KEY,content BLOB NOT NULL)')
            db.commit()

    def get(self, key):
        with closing(read_only(self.path)) as db:
            row = db.execute('SELECT content FROM evidence WHERE key=?', (key,)).fetchone()
        return decode_evidence(row['content']) if row is not None else None

    def put(self, key, item):
        if content_hash(item['request']) != key or not DrivingRoad.valid_reference(item):
            raise ValueError('Provider cache evidence failed integrity validation')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT OR REPLACE INTO evidence VALUES (?,?)',
                       (key, gzip.compress(encode(item).encode(), mtime=0)))
            db.commit()


def validate_source(manifest):
    if (file_sha(manifest['case_file']) != manifest['case_sha256']
            or file_sha(manifest['result_file']) != manifest['result_sha256']
            or content_hash(load_json(manifest['body_file'])) != manifest['input_hash']):
        raise ValueError('A prepared source changed')
    with closing(read_only(manifest['source_graph'])) as db:
        if graph_digest(db) != manifest['source_graph_hash']:
            raise ValueError('The original frozen graph changed')


def import_reference(path, job):
    """Read-only import from a measured metrics graph, never from scalar hints."""
    with closing(read_only(path)) as db:
        row = db.execute('SELECT * FROM metrics WHERE provider_key=?', (job['key'],)).fetchone()
    if row is None:
        return None
    item = decode_evidence(row['content'])
    if not DrivingRoad.valid_reference(item, allow_expired=True):
        raise ValueError('Imported measured evidence failed integrity validation')
    raw = item['request']
    request = DrivingRoad.reference_request(job['origin'], job['destination'])
    canonical = (raw if raw.get('source') == 'DRIVING_PROVIDER_REFERENCE' else
                 DrivingRoad.reference_request(raw['origin'], raw['destination'])
                 if raw.get('source') == 'DRIVING' and raw.get('endpoint') == DRIVING_ENDPOINT
                 and raw.get('strategy') == 32 else None)
    if canonical != request or content_hash(canonical) != job['key'] or any(
            row[key] != item[key] for key in ('meters', 'seconds', 'received_at', 'evidence_hash')):
        raise ValueError('Imported evidence does not bind this exact directed request')
    # Preserve the actual original evidence, receive time and geometry. The
    # frozen adapter will bind it to the selected physical car independently.
    return item


def execute(manifest, output, *, env_file=None, evidence_databases=(), offline_import=False):
    validate_source(manifest)
    validate_limits(manifest['requested_directed_edges'], manifest['max_calls'], manifest['wait_seconds'])
    expected = prepare(manifest['case_file'], manifest['result_file'],
        directions=manifest['requested_directed_edges'], max_calls=manifest['max_calls'],
        wait_seconds=manifest['wait_seconds'])
    if content_hash(expected) != content_hash(manifest):
        raise ValueError('Prepared jobs differ from the validated same-input missing-edge ranking')
    if offline_import and not evidence_databases:
        raise ValueError('Offline import requires at least one read-only measured evidence database')
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_json(output / 'jobs.json', manifest)
    started, stop = time.monotonic(), [False]
    old_handlers = {}
    def cancelled():
        return stop[0] or time.monotonic() - started >= manifest['wait_seconds']
    road = None
    if not offline_import:
        settings = Settings(_env_file=Path(env_file) if env_file else ROOT / '.env')
        if not settings.driving_live_calls_enabled or not settings.amap_web_service_key:
            raise ValueError('Live driving calls must be explicitly enabled in the selected configuration')
        # Credentials and shared pacing are only accessed in explicit live mode.
        # Cache/graph paths always belong to this new collection directory.
        reference_path, cache_path = output / 'collected-reference.db', output / 'collection-cache.db'
        reference_path.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(reference_path)) as db:
            from app.road_graph import SCHEMA
            db.execute(SCHEMA)
            db.commit()
        isolated = settings.model_copy(update={'environment': 'test', 'amap_reference_graph_path': reference_path,
            'amap_reference_graph_test_enabled': False, 'planning_route_mode': 'DRIVING'})
        cache = PrivateCache(cache_path)
        road = DrivingRoad(isolated, manifest['max_calls'], cache_get=cache.get, cache_put=cache.put,
                           cancelled=cancelled, force_refresh=True)
        road.graph = ReferenceGraph(reference_path)
    def calls():
        return road.calls if road is not None else 0
    imported, measured, attempts, import_sources = {}, {}, [], {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: stop.__setitem__(0, True))
    try:
        for job in manifest['jobs']:
            if cancelled():
                break
            request = DrivingRoad.reference_request(job['origin'], job['destination'])
            if content_hash(request) != job['key']:
                raise ValueError('Prepared job request changed')
            if job['key'] in imported or job['key'] in measured:
                continue
            before = calls()
            row = {'provider_key': job['key'], 'from_id': job['from_id'], 'to_id': job['to_id']}
            try:
                item = None
                for path in evidence_databases:
                    item = import_reference(path, job)
                    if item is not None:
                        imported[job['key']] = item
                        import_sources[job['key']] = {'read_only_database': str(Path(path).resolve()),
                            'evidence_hash': item['evidence_hash'], 'received_at': item['received_at']}
                        break
                if item is None:
                    if offline_import:
                        row.update(status='MISSING', error_code='MEASURED_EVIDENCE_NOT_FOUND')
                    else:
                        full = road.segment(job['vehicle'], job['origin'], job['destination'], job['departure'])
                        item = road.reference_item(request, full)
                        if not DrivingRoad.valid_reference(item):
                            raise ValueError('Fresh provider evidence is invalid')
                        measured[job['key']] = item
                if item is not None:
                    row['status'] = 'IMPORTED' if job['key'] in imported else 'MEASURED'
            except (RoadError, ValueError, sqlite3.Error, OSError) as exc:
                row.update(status='FAILED', error_code=getattr(exc, 'code', type(exc).__name__))
            row.update(http_attempts=calls() - before, elapsed_seconds=round(time.monotonic() - started, 4))
            attempts.append(row)
            private_json(output / 'attempts.json', attempts)
        metrics = {**imported, **measured}
        # sqlite backup preserves every original metric and hint verbatim.
        target = output / 'expanded-frozen-roads.db'
        target.touch(mode=0o600, exist_ok=False)
        added = 0
        with closing(read_only(manifest['source_graph'])) as source, closing(sqlite3.connect(target)) as db:
            source.backup(db)
            has_hints = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hints'").fetchone()
            old_hints = ([tuple(row) for row in source.execute('SELECT * FROM hints ORDER BY evidence_hash')]
                         if has_hints else [])
            for job in manifest['jobs']:
                item = metrics.get(job['key'])
                if item is None:
                    continue
                if db.execute('SELECT 1 FROM edges WHERE from_id=? AND to_id=?',
                              (job['from_id'], job['to_id'])).fetchone() is not None:
                    raise ValueError('Refusing to replace an original directed edge')
                existing = db.execute('SELECT * FROM metrics WHERE provider_key=?', (job['key'],)).fetchone()
                if existing is not None:
                    # Coordinate aliases can share a provider key. Its old
                    # metric is immutable and must agree before adding an alias.
                    original = source.execute('SELECT * FROM metrics WHERE provider_key=?', (job['key'],)).fetchone()
                    if original is not None and any(original[key] != item[key] for key in
                            ('meters', 'seconds', 'received_at', 'evidence_hash')):
                        raise ValueError('Refusing to replace a measured provider direction')
                else:
                    db.execute('INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)',
                        (job['key'], job['from_id'], job['to_id'], item['meters'], item['seconds'],
                         item['received_at'], item['evidence_hash'], gzip.compress(encode(item).encode(), mtime=0)))
                db.execute('INSERT INTO edges VALUES (?,?,?)', (job['from_id'], job['to_id'], job['key']))
                added += 1
            if has_hints and [tuple(row) for row in db.execute('SELECT * FROM hints ORDER BY evidence_hash')] != old_hints:
                raise ValueError('Original historical hints changed')
            graph_hash = graph_digest(db)
            metadata = {'graph_hash': graph_hash, 'parent_graph_hash': manifest['source_graph_hash'],
                'input_hash': manifest['input_hash'], 'frozen_at': int(time.time()),
                'scope': 'CRITICAL_MISSING_EDGES_ADDED_WITHOUT_REPLACING_OLD_MEASUREMENTS',
                'new_directed_edges': added, 'actual_http_attempts': calls(),
                'collection_mode': 'OFFLINE_VALIDATED_EVIDENCE_IMPORT' if offline_import else 'EXPLICIT_LIVE_COLLECTION',
                'existing_hints_unchanged': True, 'both_algorithms_must_rerun_on_new_graph': True}
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', [(k, encode(v)) for k, v in metadata.items()])
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Expanded graph integrity failed')
        case = deepcopy(load_json(manifest['case_file']))
        private_json(output / 'input.json', load_json(manifest['body_file']))
        if case.get('baseline_file'):
            case['baseline_file'] = str(resolved(Path(manifest['case_file']).parent, case['baseline_file']))
        case.update(body_file='input.json', road_database=str(target), graph_hash=graph_hash,
                    parent_case_file=manifest['case_file'], parent_graph_hash=manifest['source_graph_hash'],
                    road_extension=metadata)
        private_json(output / 'case.json', case)
        validate_source(manifest)
        report = {**metadata, 'status': 'COMPLETED' if added == manifest['selected_directions'] else 'INCOMPLETE',
            'stop_reason': ('COMPLETED' if added == manifest['selected_directions'] else
                            'INTERRUPTED' if stop[0] else
                            'WAIT_DEADLINE' if time.monotonic() - started >= manifest['wait_seconds'] else
                            'OFFLINE_EVIDENCE_INCOMPLETE' if offline_import else
                            'CALL_BUDGET' if calls() >= manifest['max_calls'] else 'PROVIDER_FAILURES'),
            'uncollected_directions': manifest['selected_directions'] - added,
            'max_calls': manifest['max_calls'], 'wait_seconds': manifest['wait_seconds'],
            'elapsed_seconds': round(time.monotonic() - started, 4),
            'deadline_policy': 'NO_NEW_GET_AFTER_GLOBAL_DEADLINE; IN_FLIGHT_HTTPX_PHASE_TIMEOUT_6_SECONDS',
            'deadline_overrun_seconds': round(max(0., time.monotonic() - started - manifest['wait_seconds']), 4),
            'measured_provider_keys': len(measured), 'imported_provider_keys': len(imported),
            'imported_sources': import_sources,
            'attempts': attempts, 'case_file': str(output / 'case.json'),
            'source_business_database_written': False,
            'road_stats': (road.stats() if road is not None else
                {'source': 'OFFLINE_VALIDATED_EVIDENCE_IMPORT', 'calls': 0, 'call_limit': 0,
                 'http_client_created': False, 'credentials_read': False})}
        private_json(output / 'collection-report.json', report)
        return report
    finally:
        if road is not None:
            road.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case')
    parser.add_argument('--result')
    parser.add_argument('--jobs')
    parser.add_argument('--directions', type=int, choices=[12, 24], default=12)
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--wait-seconds', type=float, default=120)
    parser.add_argument('--output', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--offline-import', action='store_true')
    parser.add_argument('--env-file')
    parser.add_argument('--evidence-database', action='append', default=[])
    args = parser.parse_args()
    if args.jobs:
        if args.case or args.result:
            parser.error('--jobs supplies frozen identities; do not combine with --case/--result')
        manifest = load_json(args.jobs)
        validate_source(manifest)
    else:
        if not args.case or not args.result:
            parser.error('Provide --case and --result, or a prepared --jobs file')
        manifest = prepare(args.case, args.result, directions=args.directions,
                           max_calls=args.max_calls, wait_seconds=args.wait_seconds)
    if args.live or args.offline_import:
        report = execute(manifest, args.output, env_file=args.env_file, evidence_databases=args.evidence_database,
                         offline_import=args.offline_import)
    else:
        output = Path(args.output).resolve()
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        private_json(output / 'jobs.json', manifest)
        report = {'status': 'PREPARED_NO_NETWORK', 'selected_directions': manifest['selected_directions'],
                  'selected_provider_keys': manifest['selected_provider_keys'], 'actual_http_attempts': 0,
                  'max_calls': manifest['max_calls'], 'wait_seconds': manifest['wait_seconds'],
                  'jobs_file': str(output / 'jobs.json'), 'source_business_database_written': False}
        private_json(output / 'collection-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
