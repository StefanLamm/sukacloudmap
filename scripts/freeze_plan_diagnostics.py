"""Read-only saved-plan diagnosis on one newly frozen measured graph.

Plan IDs are diagnostic inputs, never hints injected into a production search.
No HTTP client or business-database writer is created.
"""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.planning_inputs import content_hash, encode
from app.road_service import DrivingRoad, PROFILE
from app.routing_engine import normalize
from app.route_verification import recipe, replay, complete
from scripts.benchmark_algorithms import (read_only, private_json, graph_digest,
    compatible_pairs, FrozenEvidenceRoad, independent_audit)


def relative_time(value, day):
    if value is None:
        return None
    instant = datetime.fromisoformat(value)
    return {'day_offset': (instant.date() - date.fromisoformat(day)).days,
            'clock_and_offset': instant.timetz().isoformat()}


def model_identity(body):
    """Only fields actually used for cost, feasibility and road binding."""
    data = normalize(body)
    tasks = {}
    for key, row in data['tasks'].items():
        fields = ('id','store_id','warehouse_id','pieces','longitude','latitude','crs',
                  'unload_minutes','province','city','district','region_code','signature')
        task = {name: row.get(name) for name in fields}
        task['common_unit_price'] = (None if row['common_unit_price'] is None else
                                    str(Decimal(row['common_unit_price']).normalize()))
        for name in ('open_at','close_at','latest_arrival_at'):
            task[name] = relative_time(row.get(name), body['delivery_date'])
        tasks[key] = task
    vehicles = {key: {name: row.get(name) for name in
                ('id','warehouse_id','max_pieces','available',*PROFILE)}
                for key, row in data['vehicles'].items()}
    warehouses = {key: {**{name: row.get(name) for name in
                    ('id','longitude','latitude','crs')},
                    'earliest_departure': relative_time(row['earliest_departure_at'], body['delivery_date'])}
                    for key, row in data['warehouses'].items()}
    return {'tasks':tasks,'vehicles':vehicles,'warehouses':warehouses}


def freeze(database, graph_database, plan_ids, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    records = []
    with read_only(database) as source:
        for plan_id in plan_ids:
            row = source.execute('''SELECT p.*,g.name AS group_name,s.content AS input_body,
                s.content_hash AS input_hash FROM plan_versions p
                JOIN input_snapshots s ON s.id=p.snapshot_id
                LEFT JOIN demand_groups g ON g.id=p.group_id WHERE p.id=?''', (plan_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown saved plan')
            saved, body = json.loads(row['content']), json.loads(row['input_body'])
            if content_hash(saved) != row['content_hash'] or content_hash(body) != row['input_hash']:
                raise ValueError('Stored plan/input integrity mismatch')
            if saved['input_hash'] != content_hash(body):
                raise ValueError('Saved plan belongs to another input')
            model = model_identity(body)
            records.append({'name': f"{row['delivery_date']}-{row['group_name']}-V{row['version']}",
                'plan_id': plan_id, 'snapshot_id': row['snapshot_id'], 'plan_hash': row['content_hash'],
                'input_hash': row['input_hash'], 'model_hash_without_calendar_date': content_hash(model),
                'tasks':len(model['tasks']), 'pieces':sum(t['pieces'] for t in model['tasks'].values()),
                'saved_fee':saved['result']['total_fee'], 'saved':saved, 'body':body,
                'model':model})
    common_body = records[-1]['body']
    private_json(output / 'input.json', common_body)
    for index, row in enumerate(records):
        private_json(output / f'original-{index}.json', row['saved'])
        private_json(output / f'input-{index}.json', row['body'])
        private_json(output / f'model-{index}.json', row['model'])
    graph = output / 'frozen-roads.db'
    graph.touch(mode=0o600)
    with read_only(graph_database) as source, sqlite3.connect(graph) as destination:
        source.backup(destination)
        destination.executescript('''CREATE TABLE IF NOT EXISTS edges(from_id TEXT,to_id TEXT,
          provider_key TEXT,PRIMARY KEY(from_id,to_id));
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS hints(evidence_hash TEXT PRIMARY KEY,request TEXT,seconds INTEGER);''')
        keys = {r[0] for r in destination.execute('SELECT provider_key FROM metrics')}
        data = normalize(common_body)
        points = {**data['warehouses'], **data['tasks']}
        pairs = compatible_pairs(data)
        edges = []
        for a,b in sorted(pairs):
            key = content_hash(DrivingRoad.reference_request(points[a], points[b]))
            if key in keys:
                edges.append((a,b,key))
        destination.executemany('INSERT OR REPLACE INTO edges VALUES (?,?,?)', edges)
        hint_bytes = hint_rows = valid_rows = 0
        with read_only(database) as business:
            for cached in business.execute('SELECT key,content FROM road_cache ORDER BY expires_at DESC LIMIT 10000'):
                hint_bytes += len(cached['content'])
                if hint_bytes > 128*1024*1024:
                    break
                hint_rows += 1
                try:
                    item = json.loads(cached['content']); raw = item['request']
                    if (content_hash(raw) != cached['key'] or not DrivingRoad.valid_reference(item, allow_expired=True)
                        or raw.get('source') != 'DRIVING' or raw.get('strategy') != 32
                        or not all(raw.get(k) for k in ('vehicle_id','from_id','to_id','departure_at'))):
                        continue
                    destination.execute('INSERT OR IGNORE INTO hints VALUES (?,?,?)',
                        (item['evidence_hash'], encode(raw), item['seconds']))
                    valid_rows += 1
                except (ValueError,TypeError,KeyError):
                    continue
        metadata = {'scope':'READ_ONLY_SAVED_PLAN_DIAGNOSTIC', 'frozen_at':int(time.time()),
            'input_hash':content_hash(common_body), 'required_directed_edges':len(pairs),
            'known_directed_edges':len(edges), 'source_metric_count':len(keys),
            'hint_cache_rows_examined':hint_rows, 'hint_bytes_examined':hint_bytes,
            'hint_cache_valid_rows':valid_rows,
            'hints_hash':content_hash([list(r) for r in destination.execute('SELECT * FROM hints ORDER BY evidence_hash')])}
        metadata['graph_hash'] = graph_digest(destination)
        destination.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
            [(k,encode(v)) for k,v in metadata.items()])
        destination.commit()
    road = FrozenEvidenceRoad(graph)
    try:
        data = normalize(common_body)
        for index, row in enumerate(records):
            rebound = replay(data, recipe(row['saved']['result']), road)
            audit = independent_audit(data, rebound, road)
            private_json(output / f'rebound-{index}.json', rebound)
            row['same_graph_rebound'] = {'fee':rebound['total_fee'],'audit':audit,
                'complete':complete(data,rebound), 'issues':rebound['check']['issues'],
                'routes':len(rebound['routes']), 'common_tasks':len(rebound['common'])}
            row['algorithm'] = {key: row['saved']['result'].get('algorithm',{}).get(key) for key in
                ('mode','budget_seconds','compute_seconds','elapsed_seconds','seed','initial_plan_supplied',
                 'initial_plan_accepted','round_count','candidate_observations','dropped_candidate_observations',
                 'candidate_retention','road_call_budget','experiments')}
        manifest = {'schema_version':1, 'scope':'DIAGNOSTIC_NOT_A_COLD_START_OR_BUSINESS_PLAN',
            'database_read_only':str(Path(database).resolve()), 'source_graph_read_only':str(Path(graph_database).resolve()),
            'all_date_normalized_models_equal':len({r['model_hash_without_calendar_date'] for r in records})==1,
            'body_file':'input.json','input_hash':content_hash(common_body),
            'road_database':str(graph),'graph_hash':road.graph_hash,'metadata':metadata,
            'plans':[{k:v for k,v in r.items() if k not in ('body','saved','model')} for r in records],
            'external_calls':0,'business_database_written':False}
        private_json(output / 'case.json', manifest)
        return manifest
    finally:
        road.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',required=True);p.add_argument('--graph',required=True)
    p.add_argument('--plan-id',action='append',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=freeze(a.database,a.graph,a.plan_id,a.output)
    print(json.dumps({'same_models':r['all_date_normalized_models_equal'],'graph_hash':r['graph_hash'],
        'metadata':r['metadata'], 'plans':[{k:v for k,v in row.items() if k not in ('algorithm','same_graph_rebound')}
        |{'same_graph_fee':row['same_graph_rebound']['fee'],'valid':row['same_graph_rebound']['audit']['valid']}
        for row in r['plans']]},ensure_ascii=False))
