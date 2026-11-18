"""Freeze unseen-day roads from inputs and coordinates, never solver outcomes.

Preparation reads only frozen case/input JSON and measured-road tables. It
freezes imported receipts before optional HTTP collection. Existing graph rows
are immutable; hint regimes retain their own byte-identical hint rows while
sharing one measured batch. No historical result/baseline file is opened.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import signal
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from app.config import Settings
from app.coordinates import provider_point
from app.planning_inputs import content_hash,encode
from app.road_graph import ReferenceGraph,SCHEMA
from app.road_service import DrivingRoad
from app.routing_engine import RoadError,normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad,graph_digest,private_json,read_only
from scripts.collect_critical_algorithm_roads import import_reference,PrivateCache

POLICY='WAREHOUSE_TO_ALL_AND_SIX_NEAREST_QUOTE_COMPATIBLE_BIDIRECTIONAL'


def load(path):
    raw=Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if raw[:2]==b'\x1f\x8b' else raw)


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def resolve(base,value):return (Path(base)/value).resolve()


def hints(db):
    return [tuple(row) for row in db.execute('SELECT * FROM hints ORDER BY evidence_hash')]


def hint_identity(path):
    """Expose both immutable SQL-row and benchmark hint-dictionary identities."""
    frozen=FrozenEvidenceRoad(path)
    try:
        return {'hint_rows_hash':content_hash(hints(frozen.connection)),
                'hint_hash':content_hash(frozen.route_seed_samples),'hint_count':len(frozen.route_seed_samples)}
    finally:frozen.close()


def point(row):return {key:row[key] for key in ('id','longitude','latitude','crs')}


def coordinate_distance(a,b):
    """Haversine distance ranks neighbours only, never prices a road."""
    a,b=provider_point(a),provider_point(b)
    ax,ay,bx,by=map(math.radians,(a['longitude'],a['latitude'],b['longitude'],b['latitude']))
    value=math.sin((ay-by)/2)**2+math.cos(ay)*math.cos(by)*math.sin((ax-bx)/2)**2
    return 2*math.asin(min(1,math.sqrt(max(0,value))))


def requested_jobs(body):
    data=normalize(body);groups={};rejected=Counter();pairs={}
    for task in data['tasks'].values():
        if task['signature'] is None:rejected['NO_DEDICATED_QUOTE']+=1;continue
        groups.setdefault((task['warehouse_id'],task['signature']),[]).append(task)
    for (wid,_),tasks in sorted(groups.items()):
        warehouse=data['warehouses'][wid]
        fleet=sorted((v for v in data['vehicles'].values() if v['available'] and v['warehouse_id']==wid),key=lambda v:v['id'])
        if not fleet:
            rejected['NO_AVAILABLE_WAREHOUSE_VEHICLE']+=len(tasks);continue
        # Include every quote-compatible task; current capacity does not remove
        # a measured connection that another available fleet could later use.
        for task in sorted(tasks,key=lambda t:t['id']):
            pairs[wid,task['id']]=(warehouse,task,fleet[0],warehouse['earliest_departure_at'])
            neighbours=sorted((other for other in tasks if other['id']!=task['id']),
                              key=lambda other:(coordinate_distance(task,other),other['id']))[:6]
            for other in neighbours:
                pairs[task['id'],other['id']]=(task,other,fleet[0],warehouse['earliest_departure_at'])
                pairs[other['id'],task['id']]=(other,task,fleet[0],warehouse['earliest_departure_at'])
    jobs=[]
    for (a,b),(origin,destination,vehicle,departure) in sorted(pairs.items()):
        request=DrivingRoad.reference_request(origin,destination)
        jobs.append({'key':content_hash(request),'from_id':a,'to_id':b,'origin':point(origin),
                     'destination':point(destination),'vehicle':vehicle,'departure':departure})
    return jobs,dict(rejected)


def measured_sources(data_root,original_graphs):
    data_root=Path(data_root).resolve()
    prioritized=[*original_graphs,data_root/'runtimes/test-working/data/reference-graph.db']
    for path in sorted(data_root.rglob('*.db')):
        if path.is_symlink() or any('stable' in part or part in {'.git','.venv','node_modules'} for part in path.parts):continue
        if any(word in path.name.lower() for word in ('road','graph','frozen')):prioritized.append(path)
    found=[];seen=set()
    for path in prioritized:
        path=Path(path).resolve()
        if path in seen or not path.is_file():continue
        seen.add(path)
        try:
            with closing(read_only(path)) as db:
                names={row['name'] for row in db.execute('PRAGMA table_info(metrics)')}
                if {'provider_key','content','meters','seconds','received_at','evidence_hash'}<=names:found.append(str(path))
        except (OSError,sqlite3.Error):continue
    return found


def prepare(index_file,output,*,data_root):
    index_file=Path(index_file).resolve();output=Path(output).resolve()
    index=load(index_file)
    if not index.get('inputs_frozen_before_any_algorithm_result'):raise ValueError('Unseen cases must be locked before results')
    cases=[];jobs={};graphs=[]
    for entry in index['cases']:
        case_file=resolve(index_file.parent,entry['case_file']);case=load(case_file)
        body_file=resolve(case_file.parent,case['body_file']);body=load(body_file)
        if content_hash(body)!=entry['input_hash'] or entry['input_hash']!=case['input_hash']:raise ValueError('Locked unseen input changed')
        graph=resolve(case_file.parent,case['road_database'])
        with closing(read_only(graph)) as db:
            digest=graph_digest(db);hint_rows=hints(db)
        if digest!=case['graph_hash'] or digest!=entry['graph_hash']:raise ValueError('Original frozen graph changed')
        rows,rejected=requested_jobs(body)
        item={'day':entry['day'],'regime':entry['regime'],'case_file':str(case_file),'case_sha256':sha(case_file),
              'body_file':str(body_file),'input_hash':entry['input_hash'],'source_graph':str(graph),
              'parent_graph_hash':digest,**hint_identity(graph),
              'tasks':len(body['tasks']),'required_directed_aliases':len(rows),'rejected':rejected,
              'provider_keys':sorted({row['key'] for row in rows})}
        cases.append(item);graphs.append(str(graph))
        for row in rows:
            job=jobs.setdefault(row['key'],{**row,'aliases':[]})
            alias=[row['from_id'],row['to_id']]
            if alias not in job['aliases']:job['aliases'].append(alias)
    sources=measured_sources(data_root,list(dict.fromkeys(graphs)))
    references={};provenance={};invalid=Counter()
    for key,job in sorted(jobs.items()):
        for source in sources:
            try:item=import_reference(source,job)
            except (ValueError,OSError,sqlite3.Error):invalid[source]+=1;continue
            if item is not None:
                references[key]=item
                provenance[key]={'source_graph_read_only':source,'evidence_hash':item['evidence_hash'],'received_at':item['received_at']}
                break
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    receipts=output/'imported-receipts.json.gz';receipts.touch(mode=0o600,exist_ok=False)
    receipts.write_bytes(gzip.compress(encode(references).encode(),mtime=0))
    for case in cases:
        case['imported_provider_keys']=sum(key in references for key in case['provider_keys'])
        case['live_missing_provider_keys']=len(case['provider_keys'])-case['imported_provider_keys']
    manifest={'schema_version':1,'scope':'UNSEEN_INPUT_ONLY_MEASURED_ROAD_FREEZE','policy':POLICY,
        'index_file':str(index_file),'index_sha256':sha(index_file),'cases':cases,
        'jobs':[jobs[key] for key in sorted(jobs)],'sources_read_only':sources,
        'imported_receipts_file':str(receipts),'imported_receipts_sha256':sha(receipts),
        'imported_provider_keys':len(references),'live_missing_provider_keys':len(jobs)-len(references),
        'unique_provider_keys':len(jobs),'minimum_live_qps_wait_seconds':round(max(0,len(jobs)-len(references)-1)/3,3),
        'total_call_limit':None,'invalid_source_evidence':dict(invalid),'import_provenance':provenance,
        'solver_results_read':False,'baseline_files_read':False,'external_calls':0,'source_business_database_written':False,
        'shared_measurements_separate_unchanged_hints':True}
    private_json(output/'prepared.json',manifest)
    return manifest


def validate_manifest(manifest):
    if sha(manifest['index_file'])!=manifest['index_sha256'] or sha(manifest['imported_receipts_file'])!=manifest['imported_receipts_sha256']:
        raise ValueError('Prepared lock or imported evidence changed')
    for case in manifest['cases']:
        if sha(case['case_file'])!=case['case_sha256'] or content_hash(load(case['body_file']))!=case['input_hash']:
            raise ValueError('Prepared case/input changed')
        with closing(read_only(case['source_graph'])) as db:
            # v1 preparation manifests named the row digest hint_hash. Keep
            # those frozen receipts usable without rewriting the old manifest.
            if graph_digest(db)!=case['parent_graph_hash'] or content_hash(hints(db))!=case.get('hint_rows_hash',case['hint_hash']):
                raise ValueError('Prepared original graph/hints changed')


def copy_db(source,target):
    target=Path(target);target.touch(mode=0o600,exist_ok=False)
    with closing(read_only(source)) as original,closing(sqlite3.connect(target)) as db:
        original.backup(db);db.commit()


def add_metric(db,key,job,item):
    old=db.execute('SELECT meters,seconds,received_at,evidence_hash FROM metrics WHERE provider_key=?',(key,)).fetchone()
    if old is not None:return False  # Original receipt always wins, even over a newer one.
    db.execute('INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)',(key,job['from_id'],job['to_id'],item['meters'],item['seconds'],
        item['received_at'],item['evidence_hash'],gzip.compress(encode(item).encode(),mtime=0)))
    return True


def finish_db(db):
    db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('PRAGMA journal_mode=DELETE')
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('New isolated graph integrity failed')


def execute(manifest,output,*,wait_seconds,env_file=None,live=False,settings=None,transport=None,limiter=None,cancelled=None):
    if isinstance(wait_seconds,bool) or not math.isfinite(wait_seconds) or wait_seconds<=0:raise ValueError('A finite positive waiting deadline is required')
    validate_manifest(manifest);output=Path(output).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=False)
    private_json(output/'prepared.json',manifest)
    references=load(manifest['imported_receipts_file']);jobs={row['key']:row for row in manifest['jobs']}
    for key,item in references.items():
        if key not in jobs or not DrivingRoad.valid_reference(item,allow_expired=True):raise ValueError('Prepared imported receipt invalid')
        canonical=DrivingRoad.reference_request(item['request']['origin'],item['request']['destination'])
        if content_hash(canonical)!=key:raise ValueError('Prepared imported receipt binding differs')
    started=time.monotonic();wall_deadline=started+wait_seconds;stop=[False];handlers={};road=None;attempts=[]
    def user_cancelled():return stop[0] or bool(cancelled and cancelled())
    missing=[job for key,job in sorted(jobs.items()) if key not in references]
    try:
        if live and missing:
            settings=settings or Settings(_env_file=Path(env_file) if env_file else ROOT/'.env')
            if not settings.driving_live_calls_enabled or not settings.amap_web_service_key:raise ValueError('Live driving service must be enabled with a key')
            collected=output/'collected-reference.db';collected.touch(mode=0o600,exist_ok=False)
            with closing(sqlite3.connect(collected)) as db:db.execute(SCHEMA);db.commit()
            isolated=settings.model_copy(update={'environment':'test','planning_route_mode':'DRIVING',
                'amap_reference_graph_path':collected,'amap_reference_graph_test_enabled':False})
            cache=PrivateCache(output/'collection-cache.db')
            road=DrivingRoad(isolated,None,cache_get=cache.get,cache_put=cache.put,transport=transport,limiter=limiter,
                production_planning=True,wall_deadline=wall_deadline,cancelled=user_cancelled)
            road.graph=ReferenceGraph(collected)
            for sig in (signal.SIGINT,signal.SIGTERM):
                handlers[sig]=signal.getsignal(sig);signal.signal(sig,lambda *_:stop.__setitem__(0,True))
            for job in missing:
                if user_cancelled() or time.monotonic()>=wall_deadline:break
                before=road.calls;row={'provider_key':job['key']}
                try:
                    bound=road.segment(job['vehicle'],job['origin'],job['destination'],job['departure'])
                    item=road.reference_item(DrivingRoad.reference_request(job['origin'],job['destination']),bound)
                    if not DrivingRoad.valid_reference(item):raise ValueError('Collected provider evidence invalid')
                    references[job['key']]=item;row['status']='MEASURED'
                except (RoadError,ValueError,OSError,sqlite3.Error) as exc:
                    row.update(status='FAILED',error_code=getattr(exc,'code',type(exc).__name__))
                row.update(http_attempts=road.calls-before,elapsed_seconds=round(time.monotonic()-started,4));attempts.append(row)
                private_json(output/'attempts.json',attempts)
        # The new shared pool retains every original measured row and alias.
        master=output/'shared-measured-roads.db';copy_db(manifest['cases'][0]['source_graph'],master)
        alias_conflicts=0;added_metrics=0
        with closing(sqlite3.connect(master)) as db:
            for original_path in dict.fromkeys(case['source_graph'] for case in manifest['cases']):
                with closing(read_only(original_path)) as original:
                    for row in original.execute('SELECT * FROM metrics'):
                        old=db.execute('SELECT * FROM metrics WHERE provider_key=?',(row['provider_key'],)).fetchone()
                        if old is None:db.execute('INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)',tuple(row))
                        elif tuple(old)!=tuple(row):raise ValueError('Original graphs disagree; refusing to replace a frozen measurement')
                    for row in original.execute('SELECT * FROM edges'):
                        old=db.execute('SELECT provider_key FROM edges WHERE from_id=? AND to_id=?',(row['from_id'],row['to_id'])).fetchone()
                        if old is None:db.execute('INSERT INTO edges VALUES (?,?,?)',tuple(row))
                        elif old[0]!=row['provider_key']:raise ValueError('Original frozen graph aliases disagree')
            for key,job in jobs.items():
                item=references.get(key)
                if item is None:continue
                added_metrics+=add_metric(db,key,job,item)
                for a,b in job['aliases']:
                    old=db.execute('SELECT provider_key FROM edges WHERE from_id=? AND to_id=?',(a,b)).fetchone()
                    if old is None:db.execute('INSERT INTO edges VALUES (?,?,?)',(a,b,key))
                    elif old[0]!=key:alias_conflicts+=1
            db.execute('DELETE FROM hints')
            batch_hash=graph_digest(db)
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(key,encode(value)) for key,value in {
                'graph_hash':batch_hash,'frozen_at':int(time.time()),'scope':POLICY,'input_only_freeze':True,
                'source_business_database_written':False,'actual_http_attempts':road.calls if road else 0}.items()])
            available={row[0] for row in db.execute('SELECT provider_key FROM metrics')};finish_db(db)
        graph_by_hints={};outputs=[]
        for n,entry in enumerate(manifest['cases']):
            hint_rows_hash=entry.get('hint_rows_hash',entry['hint_hash']);target=graph_by_hints.get(hint_rows_hash)
            if target is None:
                target=output/'graphs'/(entry['regime']+'.db');target.parent.mkdir(mode=0o700,exist_ok=True)
                copy_db(master,target)
                with closing(read_only(entry['source_graph'])) as source,closing(sqlite3.connect(target)) as db:
                    db.executemany('INSERT INTO hints VALUES (?,?,?)',hints(source))
                    if content_hash(hints(db))!=hint_rows_hash:raise ValueError('Original hint regime changed')
                    finish_db(db)
                graph_by_hints[hint_rows_hash]=target
            parent=load(entry['case_file']);case_dir=output/'cases'/entry['day']/entry['regime'];case_dir.mkdir(mode=0o700,parents=True,exist_ok=False)
            private_json(case_dir/'input.json',load(entry['body_file']))
            if parent.get('baseline_file'):parent['baseline_file']=str(resolve(Path(entry['case_file']).parent,parent['baseline_file']))
            parent.update(body_file='input.json',road_database=str(target),graph_hash=batch_hash,parent_case_file=entry['case_file'],
                parent_graph_hash=entry['parent_graph_hash'],road_freeze_policy=POLICY,shared_measurement_batch=batch_hash)
            frozen=FrozenEvidenceRoad(target)
            try:parent['road_stats']=frozen.stats()
            finally:frozen.close()
            private_json(case_dir/'case.json',parent)
            outputs.append({**entry,**hint_identity(target),'case_file':str(case_dir/'case.json'),'graph_hash':batch_hash,
                'missing_provider_keys':sum(key not in available for key in entry['provider_keys'])})
        validate_manifest(manifest)
        uncollected=sum(key not in available for key in jobs)
        report={'status':'COMPLETED' if not uncollected else 'INCOMPLETE','scope':POLICY,'cases':outputs,
            'shared_graph_hash':batch_hash,'shared_measurements_separate_unchanged_hints':True,
            'original_measurements_unchanged':True,'imported_provider_keys':manifest['imported_provider_keys'],
            'measured_provider_keys':len(references)-manifest['imported_provider_keys'],'new_metrics':added_metrics,
            'uncollected_provider_keys':uncollected,'preserved_existing_alias_conflicts':alias_conflicts,
            'actual_http_attempts':road.calls if road else 0,'road_stats':road.stats() if road else None,
            'total_call_limit':None,'wait_seconds':wait_seconds,'elapsed_seconds':round(time.monotonic()-started,4),
            'deadline_overrun_seconds':round(max(0,time.monotonic()-wall_deadline),4),
            'stop_reason':'COMPLETED' if not uncollected else 'CANCELLED' if user_cancelled() else 'WALL_BUDGET' if time.monotonic()>=wall_deadline else 'PROVIDER_FAILURES' if live else 'OFFLINE_ONLY',
            'attempts':attempts,'solver_results_read':False,'baseline_files_read':False,'source_business_database_written':False}
        private_json(output/'frozen-cases.json',report);return report
    finally:
        if road is not None:road.close()
        for sig,handler in handlers.items():signal.signal(sig,handler)


def upgrade_index(index_file,output_file):
    """Correct v1 metadata only; never write graphs, cases, inputs or receipts."""
    index_file=Path(index_file).resolve();output_file=Path(output_file).resolve()
    if output_file.exists() or output_file==index_file:raise ValueError('Require a new metadata-only index path')
    original_sha=sha(index_file);value=deepcopy(load(index_file));identities={};verified=[]
    for entry in value['cases']:
        case_path=Path(entry['case_file']);case=load(case_path);graph=resolve(case_path.parent,case['road_database'])
        if str(graph) not in identities:
            with closing(read_only(graph)) as db:
                digest=graph_digest(db)
            identities[str(graph)]={**hint_identity(graph),'graph_hash':digest,'graph_file_sha256':sha(graph)}
        identity=identities[str(graph)]
        if identity['graph_hash']!=entry['graph_hash'] or identity['graph_hash']!=case['graph_hash']:
            raise ValueError('Metadata correction may not change the measured graph')
        prior_rows_hash=entry.get('hint_rows_hash',entry['hint_hash'])
        if prior_rows_hash!=identity['hint_rows_hash']:raise ValueError('Original hint rows do not match v1 proof')
        entry.update({key:identity[key] for key in ('hint_rows_hash','hint_hash','hint_count')})
        verified.append({'case_file':str(case_path),'case_file_sha256':sha(case_path),'graph':str(graph),
                         **identity,'input_hash':entry['input_hash']})
    value['metadata_only_revision']={'source_index':str(index_file),'source_index_sha256':original_sha,
        'reason':'Separate SQL hint-row hash from FrozenEvidenceRoad route_seed_samples dictionary hash',
        'business_inputs_road_metrics_aliases_hints_receipts_unchanged':True,
        'solver_runs':0,'network_calls':0,'fees_or_algorithm_results_read':False}
    private_json(output_file,value)
    if sha(index_file)!=original_sha:raise ValueError('Original index unexpectedly changed')
    report={'source_index':str(index_file),'source_index_sha256':original_sha,
        'corrected_index':str(output_file),'corrected_index_sha256':sha(output_file),
        'metadata_only':True,'cases':verified,'graphs_cases_inputs_receipts_written':False,
        'solver_runs':0,'network_calls':0,'fees_or_algorithm_results_read':False}
    private_json(output_file.with_suffix('.audit.json'),report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--index',required=True);prep.add_argument('--data-root',required=True);prep.add_argument('--output',required=True)
    run=sub.add_parser('execute');run.add_argument('--prepared',required=True);run.add_argument('--output',required=True)
    run.add_argument('--wait-seconds',type=float,required=True);run.add_argument('--env-file');run.add_argument('--live',action='store_true')
    revise=sub.add_parser('upgrade-index');revise.add_argument('--index',required=True);revise.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    if args.command=='upgrade-index':
        result=upgrade_index(args.index,args.output)
        print(json.dumps({key:value for key,value in result.items() if key!='cases'},ensure_ascii=False,indent=2));return
    result=prepare(args.index,args.output,data_root=args.data_root) if args.command=='prepare' else execute(load(args.prepared),args.output,wait_seconds=args.wait_seconds,env_file=args.env_file,live=args.live)
    summary={key:value for key,value in result.items() if key not in {'cases','jobs','sources_read_only','import_provenance','attempts'}}
    summary['cases']=[{key:case[key] for key in ('day','regime','tasks','required_directed_aliases','live_missing_provider_keys') if key in case} for case in result['cases']]
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
