"""Collect a reproducible real-road graph without changing business data.

Run explicitly with --live. Every HTTP attempt uses the application's shared
per-service QPS limiter. All polylines and original timestamps are preserved in
compressed, private SQLite rows. Geography orders queries; it never prices them.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import defaultdict, Counter
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.road_service import DrivingRoad
from app.routing_engine import normalize, RoadError
from app.planning_inputs import content_hash, encode


def private_json(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.touch(mode=0o600,exist_ok=True);temp.chmod(0o600)
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)


def prepare(source,group_id,folder,snapshot_id=None):
    source=Path(source).resolve();folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        snapshot=db.execute('SELECT * FROM input_snapshots WHERE group_id=? AND id=?',(group_id,snapshot_id)).fetchone() if snapshot_id else db.execute('SELECT * FROM input_snapshots WHERE group_id=? ORDER BY version DESC LIMIT 1',(group_id,)).fetchone()
        if snapshot is None:raise ValueError('No snapshot')
        body=json.loads(snapshot['content']);assert content_hash(body)==snapshot['content_hash']
        data=normalize(body)
        plan=db.execute('SELECT * FROM plan_versions WHERE group_id=? AND snapshot_id=? ORDER BY version DESC LIMIT 1',(group_id,snapshot['id'])).fetchone()
        baseline=json.loads(plan['content']) if plan else None
    existing=folder/'collection-input.json'
    if existing.exists() and content_hash(json.loads(existing.read_text()))!=content_hash(body):raise ValueError('Refusing to overwrite the frozen collection input; resume with its original --snapshot-id')
    private_json(existing,body)
    if baseline:private_json(folder/'collection-baseline.json',baseline)
    points={**data['warehouses'],**data['tasks']};groups=defaultdict(list);vehicles={}
    for vehicle in data['vehicles'].values():
        if vehicle['available']:vehicles.setdefault(vehicle['warehouse_id'],vehicle)
    for task in data['tasks'].values():
        if task['signature'] and task['warehouse_id'] in vehicles:groups[(task['warehouse_id'],task['signature'])].append(task['id'])
    edges={};jobs={}
    def add(a,b,warehouse,priority):
        request=DrivingRoad.reference_request(points[a],points[b]);key=content_hash(request)
        edges[(a,b)]=key
        if key not in jobs:jobs[key]={'key':key,'from_id':a,'to_id':b,'vehicle':vehicles[warehouse],
            'origin':points[a],'destination':points[b],'departure':data['warehouses'][warehouse]['earliest_departure_at'],'priority':priority}
    for (warehouse,signature),ids in groups.items():
        for i in ids:add(warehouse,i,warehouse,(0,0,i))
        # Round-robin nearest neighbours first gives usable broad coverage while
        # still eventually measuring every compatible directed pair.
        for a in ids:
            near=sorted((b for b in ids if b!=a),key=lambda b:((float(points[a]['longitude'])-float(points[b]['longitude']))**2+(float(points[a]['latitude'])-float(points[b]['latitude']))**2,b))
            for rank,b in enumerate(near):add(a,b,warehouse,(1,rank,a))
    return body,dict(snapshot),edges,sorted(jobs.values(),key=lambda row:row['priority'])


def collect(args):
    folder=Path(args.output);body,snapshot,edges,jobs=prepare(args.database,args.group_id,folder,args.snapshot_id)
    path=folder/'full-roads.db';path.touch(mode=0o600,exist_ok=True);path.chmod(0o600)
    db=sqlite3.connect(path,timeout=20);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS metrics(provider_key TEXT PRIMARY KEY,from_id TEXT,to_id TEXT,meters INTEGER,seconds INTEGER,received_at INTEGER,evidence_hash TEXT,content BLOB);
    CREATE TABLE IF NOT EXISTS edges(from_id TEXT,to_id TEXT,provider_key TEXT,PRIMARY KEY(from_id,to_id));
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS hints(evidence_hash TEXT PRIMARY KEY,request TEXT,seconds INTEGER);
    CREATE TABLE IF NOT EXISTS attempts(provider_key TEXT PRIMARY KEY,attempts INTEGER,error_code TEXT,updated_at INTEGER);''')
    def meta(key,value):db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))
    prior=db.execute("SELECT value FROM metadata WHERE key='input_hash'").fetchone()
    if prior and json.loads(prior[0])!=content_hash(body):raise ValueError('Frozen collection input changed')
    meta('input_hash',content_hash(body));meta('snapshot_id',snapshot['id']);meta('source_database_read_only',True)
    meta('collection_policy','ALL_SAME_WAREHOUSE_AND_COMPLETE_QUOTE_SIGNATURE_DIRECTED_PAIRS_PLUS_DEPOT')
    meta('expected_edges',len(edges));meta('expected_provider_keys',len(jobs));meta('strategy',32)
    if not db.execute("SELECT 1 FROM metadata WHERE key='collection_started_at'").fetchone():meta('collection_started_at',int(time.time()))
    db.executemany('INSERT OR REPLACE INTO edges VALUES (?,?,?)',[(a,b,key) for (a,b),key in edges.items()]);db.commit()
    done={row[0] for row in db.execute('SELECT provider_key FROM metrics')};pending=[j for j in jobs if j['key'] not in done]
    settings=Settings();started=time.monotonic();stop=[False];failures=Counter();calls=0;success=0;consecutive=0
    signal.signal(signal.SIGTERM,lambda *_:stop.__setitem__(0,True));signal.signal(signal.SIGINT,lambda *_:stop.__setitem__(0,True))
    def cancelled():return stop[0] or time.monotonic()-started>=args.budget_seconds
    def fetch(job):
        road=DrivingRoad(settings,2,cancelled=cancelled)
        try:
            full=road.segment(job['vehicle'],job['origin'],job['destination'],job['departure'])
            provider=road.reference_item(DrivingRoad.reference_request(job['origin'],job['destination']),full)
            assert DrivingRoad.valid_reference(provider)
            return job,provider,full['request'],None,road.calls
        except RoadError as exc:return job,None,None,exc.code,road.calls
        except Exception as exc:return job,None,None,type(exc).__name__,road.calls
    def progress(status):
        state={'status':status,'completed_provider_keys':len(done),'expected_provider_keys':len(jobs),'logical_directed_edges':len(edges),
            'successful_this_run':success,'http_attempts_this_run':calls,'failures':dict(failures),
            'elapsed_seconds':round(time.monotonic()-started,2),'source_database_read_only':True,'database':str(path.resolve()),'updated_at':int(time.time())}
        private_json(folder/'collection-progress.json',state);print(json.dumps(state,ensure_ascii=False),flush=True)
    progress('RUNNING' if args.live else 'PREPARED_NO_NETWORK')
    if not args.live:db.close();return
    if not settings.driving_live_calls_enabled or not settings.amap_web_service_key:raise ValueError('Ordinary driving API is not enabled')
    iterator=iter(pending);last_report=time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        active={pool.submit(fetch,job) for job in [next(iterator,None) for _ in range(args.workers)] if job is not None}
        while active:
            ready,active=wait(active,timeout=1,return_when=FIRST_COMPLETED)
            for future in ready:
                job,item,request,error,count=future.result();calls+=count
                if error:
                    failures[error]+=1;consecutive+=1
                    db.execute('INSERT INTO attempts VALUES (?,?,?,?) ON CONFLICT(provider_key) DO UPDATE SET attempts=attempts+excluded.attempts,error_code=excluded.error_code,updated_at=excluded.updated_at',(job['key'],count,error,int(time.time())))
                    if consecutive>=10:stop[0]=True
                else:
                    consecutive=0;success+=1;done.add(job['key'])
                    blob=gzip.compress(encode(item).encode(),compresslevel=3)
                    db.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',(job['key'],job['from_id'],job['to_id'],item['meters'],item['seconds'],item['received_at'],item['evidence_hash'],blob))
                    db.execute('INSERT OR REPLACE INTO hints VALUES (?,?,?)',(content_hash(request),encode(request),item['seconds']))
                db.commit()
                if not cancelled():
                    following=next(iterator,None)
                    if following is not None:active.add(pool.submit(fetch,following))
            if time.monotonic()-last_report>=20:progress('RUNNING');last_report=time.monotonic()
    complete=len(done)==len(jobs)
    meta('collection_finished_at',int(time.time()));meta('complete',complete)
    digest=__import__('hashlib').sha256()
    for row in db.execute('SELECT provider_key,meters,seconds,evidence_hash FROM metrics ORDER BY provider_key'):digest.update(encode(list(row)).encode())
    meta('graph_hash',digest.hexdigest());db.commit();db.execute('PRAGMA wal_checkpoint(PASSIVE)');progress('COMPLETED' if complete else 'INCOMPLETE');db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--snapshot-id');p.add_argument('--group-id',default='8d9ac7f2-8308-4697-b314-83d5df8071b6');p.add_argument('--output',default='data/algorithm-lab-20261113');p.add_argument('--live',action='store_true');p.add_argument('--workers',type=int,choices=[1,2,3],default=3);p.add_argument('--budget-seconds',type=int,default=12000)
    collect(p.parse_args())
