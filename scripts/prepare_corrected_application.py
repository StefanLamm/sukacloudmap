"""Read-only preparation and validation of the authorized warehouse correction.

Only private experiment artifacts are written. There is deliberately no apply
command and no HTTP transport. The formal study keeps collection-input V2;
this separate application case uses corrected V3 plus its measured extension.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import closing,contextmanager
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from app.planning_inputs import content_hash, encode, input_context
from app.road_service import DrivingRoad,DRIVING_ENDPOINT
from app.routing_engine import assemble, check_result, compare_manual, evaluate_route, normalize
from scripts.benchmark_algorithms import (Algorithm, FrozenEvidenceRoad, compatible_pairs,
    decode_evidence, graph_digest, independent_audit, private_json, read_only, replay_baseline)


class ApplicationGuardError(ValueError):
    pass


def require(condition,message):
    if not condition:raise ApplicationGuardError(message)


def read_json(path):
    return json.loads(Path(path).read_text())


def verify_source_ready(path):
    ready=read_json(path)
    require(ready.get('status')=='READY' and isinstance(ready.get('source_hashes'),dict),'Source-ready gate is not READY')
    from scripts.run_algorithm_study import MATH_SOURCES,RESEARCH_SOURCES
    required=set(MATH_SOURCES)|set(RESEARCH_SOURCES)|{'app/route_cache.py','app/road_graph.py','app/route_baseline.py','app/config.py'}
    require(required.issubset(ready['source_hashes']),'Source-ready gate omits a frozen source')
    for relative,expected in ready['source_hashes'].items():
        target=(ROOT/relative).resolve()
        require(not Path(relative).is_absolute() and target.is_relative_to(ROOT),'Source path escapes the project')
        require(target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==expected,'Frozen source changed: '+relative)
    return ready


def _rows(body,key,identity):
    rows={row[identity]:row for row in body[key]}
    require(len(rows)==len(body[key]),'Duplicate identities in '+key)
    return rows


def validate_transition(original,corrected,correction,expected_tasks=225):
    target=correction['store_id'];old_hash=content_hash(original);new_hash=content_hash(corrected)
    require(new_hash==correction['new_snapshot_hash'],'Corrected export differs from authorized snapshot')
    require(original.get('group_id')==corrected.get('group_id') and original['delivery_date']==corrected['delivery_date'],
            'Warehouse correction must retain the same group and delivery day')
    before=_rows(original,'tasks','store_id');after=_rows(corrected,'tasks','store_id')
    require(set(before)==set(after) and len(before)==expected_tasks,'Correction lost or added demand tasks')
    require(target in before,'Corrected store is not present in demands')
    for task_id,row in before.items():
        changed={key for key in row.keys()|after[task_id].keys() if row.get(key)!=after[task_id].get(key)}
        require(changed<={'warehouse_id','warehouse_name'} if task_id==target else not changed,
                'Correction changed quantities, prices or another demand')
    old_stores=_rows(original,'stores','id');new_stores=_rows(corrected,'stores','id')
    require(set(old_stores)==set(new_stores),'Correction changed the set of frozen stores')
    for store_id,row in old_stores.items():
        changed={key for key in row.keys()|new_stores[store_id].keys() if row.get(key)!=new_stores[store_id].get(key)}
        require(changed<={'warehouse_id','warehouse_name','revision','updated_at','province'} if store_id==target else not changed,
                'Correction changed coordinates, quote or another frozen store')
    require(before[target]['warehouse_id']==correction['before']['warehouse_id'] and
            after[target]['warehouse_id']==correction['after']['warehouse_id'],'Warehouse transition differs from authorization')
    require(new_stores[target]['revision']==correction['revision_after'],'Corrected store revision differs from authorization')
    require(original['vehicles']==corrected['vehicles'] and original['quotes']==corrected['quotes'],
            'Correction must not change vehicles or quotations')
    old_wh=_rows(original,'warehouses','id');new_wh=_rows(corrected,'warehouses','id')
    require(set(old_wh)==set(new_wh),'Correction changed warehouses')
    for wid,row in old_wh.items():
        require({k:v for k,v in row.items() if k!='store_count'}=={k:v for k,v in new_wh[wid].items() if k!='store_count'},
                'Correction changed warehouse positions or departure times')
        delta=int(wid==correction['after']['warehouse_id'])-int(wid==correction['before']['warehouse_id'])
        if 'store_count' in row:require(new_wh[wid]['store_count']==row['store_count']+delta,'Warehouse store counts do not match one-store reassignment')
    old_stamp=deepcopy(original['source_stamp']);new_stamp=deepcopy(corrected['source_stamp'])
    old_master=old_stamp.pop('master_revision');new_master=new_stamp.pop('master_revision')
    require(old_stamp==new_stamp and new_master==old_master+1,'Unexpected master/day-override revision changes')
    allowed={'configuration_hash','source_stamp','stores','tasks','totals','warehouses'}
    require(all(original.get(key)==corrected.get(key) for key in original.keys()|corrected.keys() if key not in allowed),
            'Unrelated frozen input changed')
    quantities=[]
    for body in [original,corrected]:
        counts=Counter();pieces=Counter()
        for task in body['tasks']:
            counts[task['warehouse_id']]+=1;pieces[task['warehouse_id']]+=task['pieces']
        require(body['totals']['task_count']==len(body['tasks']) and body['totals']['total_pieces']==sum(pieces.values()),'Frozen totals do not match demands')
        quantities.append({'tasks_by_warehouse':dict(counts),'pieces_by_warehouse':dict(pieces),'total_pieces':sum(pieces.values())})
    require(quantities[0]['total_pieces']==quantities[1]['total_pieces'],'Correction changed total quantity')
    return {'original_input_hash':old_hash,'corrected_input_hash':new_hash,'target_store_id':target,
            'task_count':len(before),'pieces':quantities[1]['total_pieces'],'before':quantities[0],'after':quantities[1],
            'changed_store_count':1,'quantities_quotes_coordinates_vehicles_unchanged':True,
            'master_revision_before':old_master,'master_revision_after':new_master}


@contextmanager
def readonly_session(database):
    from sqlalchemy import create_engine,text
    from sqlalchemy.orm import Session
    path=Path(database).resolve();require(path.is_file(),'Main database is missing')
    def connect():
        connection=sqlite3.connect(f'{path.as_uri()}?mode=ro',uri=True,check_same_thread=False)
        connection.execute('PRAGMA query_only=ON')
        return connection
    engine=create_engine('sqlite://',creator=connect,hide_parameters=True)
    try:
        with Session(engine,autoflush=False) as session:
            session.execute(text('BEGIN'))  # One consistent read snapshot, never BEGIN IMMEDIATE.
            yield session
    finally:engine.dispose()


def main_guard(database,corrected,correction):
    from sqlalchemy import select,func
    from app.models import DemandGroup,InputSnapshot,PlanningJob,Store,SystemState
    from app.plan_service import head,verified_body
    with readonly_session(database) as db:
        snapshot=db.get(InputSnapshot,correction['new_snapshot_id'])
        require(snapshot is not None,'Corrected snapshot no longer exists')
        require(snapshot.content_hash==content_hash(corrected)==correction['new_snapshot_hash'], 'Corrected snapshot hash changed')
        require(verified_body(snapshot)==corrected and snapshot.version==correction['new_snapshot_version'],'Snapshot body or version differs')
        latest=db.scalar(select(InputSnapshot).where(InputSnapshot.group_id==snapshot.group_id,InputSnapshot.delivery_date==snapshot.delivery_date).order_by(InputSnapshot.version.desc()).limit(1))
        require(latest.id==snapshot.id,'A newer input snapshot exists')
        context=input_context(db,snapshot.delivery_date,group_id=snapshot.group_id)
        require(context['fingerprint']==snapshot.input_fingerprint,'Current inputs changed after corrected snapshot')
        master=db.get(SystemState,1).master_revision
        require(master==corrected['source_stamp']['master_revision'],'Current master revision changed')
        group=db.get(DemandGroup,snapshot.group_id);store=db.get(Store,correction['store_id'])
        require(group is not None and group.revision==corrected['demand_revision'],'Demand group revision changed')
        require(store is not None and store.revision==correction['revision_after'],'Corrected store revision changed')
        require(all(getattr(store,key)==value for key,value in correction['after'].items()),'Corrected master store fields changed')
        pending=db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.group_id==snapshot.group_id,PlanningJob.status.in_(['QUEUED','RUNNING'])))
        require(pending==0,'Demand group has a queued or running planning task')
        current=head(db,snapshot.delivery_date,snapshot.group_id)
        current_body=verified_body(current) if current else None
        guard={'group_id':snapshot.group_id,'delivery_date':snapshot.delivery_date,'snapshot_id':snapshot.id,
               'snapshot_version':snapshot.version,'snapshot_hash':snapshot.content_hash,
               'input_fingerprint':snapshot.input_fingerprint,'master_revision':master,'demand_revision':group.revision,
               'store_id':store.id,'store_revision':store.revision,'warehouse_id':store.warehouse_id,
               'head_id':current.id if current else None,'expected_head':current.version if current else 0,
               'head_content_hash':current.content_hash if current else None,
               'head_snapshot_id':current.snapshot_id if current else None,
               'head_input_hash':current_body['input_hash'] if current else None,
               'head_fee_summary':{'total_fee':current_body['result']['total_fee'],
                    'fees_complete':current_body['result']['fees_complete'],
                    'check':{k:current_body['result']['check'][k] for k in ['dedicated_fee','common_fee']}} if current else None,
               'head_total_fee':current_body['result']['total_fee'] if current else None,'active_group_jobs':pending}
        guard['guard_token']=content_hash(guard)
        return guard


def graph_state(path):
    with read_only(path) as connection:
        meta={key:json.loads(value) for key,value in connection.execute('SELECT key,value FROM metadata')}
        known=connection.execute('SELECT COUNT(*) FROM edges e JOIN metrics m ON e.provider_key=m.provider_key').fetchone()[0]
        planned=connection.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
        providers=connection.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]
        expected=meta.get('expected_provider_keys',meta.get('expected_edges'))
        complete=meta.get('complete') is True or meta.get('status')=='COMPLETED'
        return {'metadata':meta,'planned_edges':planned,'known_edges':known,'provider_edges':providers,'graph_hash':graph_digest(connection),
                'complete':bool(complete and known==planned and providers==expected)}


def inspect_application(database,original_path,corrected_path,correction_path,base_graph,extension_graph,extension_summary,source_ready,expected_tasks=225):
    ready=verify_source_ready(source_ready);original=read_json(original_path);corrected=read_json(corrected_path);correction=read_json(correction_path)
    transition=validate_transition(original,corrected,correction,expected_tasks)
    guard=main_guard(database,corrected,correction);base=graph_state(base_graph);extension=graph_state(extension_graph)
    summary=read_json(extension_summary)
    require(base['metadata'].get('input_hash')==transition['original_input_hash'],'Base graph is not the collection-start graph')
    require(extension['metadata'].get('input_hash')==summary.get('input_hash')==transition['corrected_input_hash'],'Extension input hash differs')
    require(summary.get('status')=='COMPLETED' and not summary.get('errors'),'Correction road extension is incomplete')
    old_pairs=compatible_pairs(normalize(original));new_pairs=compatible_pairs(normalize(corrected));added=new_pairs-old_pairs
    with read_only(extension_graph) as connection:
        actual={(r['from_id'],r['to_id']) for r in connection.execute('SELECT * FROM edges')}
    require(actual==added and extension['known_edges']==len(added),'Extension does not exactly cover the authorized new compatible directions')
    status='READY_TO_PREPARE' if base['complete'] else 'WAITING_COMPLETE_BASE_GRAPH'
    return {'schema_version':1,'status':status,'scope':'SEPARATE_AUTHORIZED_CORRECTION_APPLICATION_CASE',
            'formal_study_input_unchanged':True,'source_business_database_written':False,'external_calls':0,
            'transition':transition,'main_guard':guard,'base_graph':base,'extension_graph':extension,
            'source_hashes':ready['source_hashes'],'source_ready_file':str(Path(source_ready).resolve()),
            'verification_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_ready_hash':content_hash(ready),'required_corrected_directions':len(new_pairs),
            'new_correction_directions':len(added),'removed_old_compatibility_directions':len(old_pairs-new_pairs),
            'paths':{'database':str(Path(database).resolve()),'original_input':str(Path(original_path).resolve()),
                     'corrected_input':str(Path(corrected_path).resolve()),'correction':str(Path(correction_path).resolve()),
                     'base_graph':str(Path(base_graph).resolve()),'extension_graph':str(Path(extension_graph).resolve())}}


def _check_source_row(row,key):
    item=decode_evidence(row['content'])
    require(DrivingRoad.valid_reference(item,allow_expired=True),'Invalid original provider evidence')
    raw=item['request'];canonical=DrivingRoad.reference_request(raw['origin'],raw['destination'])
    require(content_hash(canonical)==key,'Provider coordinates do not match graph key')
    require(raw.get('source') in {'DRIVING_PROVIDER_REFERENCE','DRIVING'},'Non-driving provider evidence')
    require(raw==canonical if raw['source']=='DRIVING_PROVIDER_REFERENCE' else
            raw.get('endpoint')==DRIVING_ENDPOINT and raw.get('strategy')==32,'Provider endpoint/strategy differs from actual ordinary driving')
    require(all(item[k]==row[k] for k in ['meters','seconds','received_at','evidence_hash']),'Provider metrics do not match source evidence')
    return item


def prepare_application(inspection,output):
    require(inspection['status']=='READY_TO_PREPARE','Complete original graph is required before preparation')
    require(inspection['verification_script_sha256']==hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'Application verifier changed after inspection')
    verify_source_ready(inspection['source_ready_file'])
    output=Path(output).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=output/'frozen-roads.db';require(not target.exists(),'Private frozen graph already exists')
    require(target not in {Path(p).resolve() for p in inspection['paths'].values()},'Output must not replace a source')
    partial=output/('.merging-'+str(time.time_ns())+'.db');partial.touch(mode=0o600)
    corrected=read_json(inspection['paths']['corrected_input']);data=normalize(corrected);points={**data['warehouses'],**data['tasks']}
    try:
        # sqlite3's connection context commits/rolls back but does not close.
        # Backup can inherit WAL mode; consolidate it before merging or renaming
        # so the finished private graph never depends on a .merging-*-wal file.
        with closing(read_only(inspection['paths']['base_graph'])) as source,closing(sqlite3.connect(partial)) as destination:
            source.backup(destination)
            destination.commit()
            checkpoint=destination.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            require(checkpoint[0]==0,'Private backup WAL checkpoint is busy')
            require(destination.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()=='delete',
                    'Private backup could not leave WAL mode')
        with closing(read_only(inspection['paths']['extension_graph'])) as extension,closing(sqlite3.connect(partial)) as destination:
            destination.row_factory=sqlite3.Row
            require(graph_digest(destination)==inspection['base_graph']['graph_hash'],'Original graph changed since inspection')
            require(graph_digest(extension)==inspection['extension_graph']['graph_hash'],'Extension graph changed since inspection')
            for row in extension.execute('SELECT * FROM metrics'):
                item=_check_source_row(row,row['provider_key'])
                prior=destination.execute('SELECT * FROM metrics WHERE provider_key=?',(row['provider_key'],)).fetchone()
                require(prior is None or prior['evidence_hash']==row['evidence_hash'],'Extension would reprice an original-study edge')
                if prior is None:destination.execute('INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)',tuple(row[k] for k in ['provider_key','from_id','to_id','meters','seconds','received_at','evidence_hash','content']))
            for row in extension.execute('SELECT * FROM edges'):
                a,b,key=row['from_id'],row['to_id'],row['provider_key']
                require(a in points and b in points and content_hash(DrivingRoad.reference_request(points[a],points[b]))==key,'Extension alias does not match corrected coordinates')
                prior=destination.execute('SELECT provider_key FROM edges WHERE from_id=? AND to_id=?',(a,b)).fetchone()
                require(prior is None or prior[0]==key,'Extension conflicts with original coordinate alias')
                destination.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?)',(a,b,key))
            metadata={'scope':'CORRECTED_INPUT_APPLICATION_REAL_DRIVING_REFERENCE','input_hash':content_hash(corrected),
                      'original_input_hash':inspection['transition']['original_input_hash'],
                      'parent_graph_hash':inspection['base_graph']['graph_hash'],'extension_graph_hash':inspection['extension_graph']['graph_hash'],
                      'frozen_at':int(time.time()),'snapshot_id':inspection['main_guard']['snapshot_id'],
                      'source_business_database_written':False,'external_calls':0}
            destination.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,encode(v)) for k,v in metadata.items()]);destination.commit()
            require(destination.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Private merged graph failed integrity check')
            require(destination.execute('PRAGMA journal_mode').fetchone()[0].lower()=='delete',
                    'Private merged graph still depends on WAL side files')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
        for suffix in ['-wal','-shm']:Path(str(partial)+suffix).unlink(missing_ok=True)
    road=ApplicationFrozenRoad(target)
    try:
        missing=[(a,b) for a,b in compatible_pairs(data) if road.known_segment(a,b) is None]
        require(not missing,'Merged graph does not cover every corrected compatible direction')
        report={**inspection,'status':'PREPARED','private_graph':str(target),'private_graph_hash':road.graph_hash,
                'case_file':str(output/'case.json'),'prepared_at':int(time.time())}
    finally:road.close()
    private_json(output/'input.json',corrected);private_json(output/'original-input.json',read_json(inspection['paths']['original_input']))
    private_json(output/'case.json',report);verify_source_ready(inspection['source_ready_file'])
    return report


class ApplicationFrozenRoad(FrozenEvidenceRoad):
    def segment(self,vehicle,origin,destination,departure):
        item=super().segment(vehicle,origin,destination,departure)
        age=int(time.time())-item['received_at']
        require(0<=age<7*86400,'Application reference is future-dated or older than seven days')
        if age>=3600:item['time_status']='HISTORICAL_TRAFFIC_REFERENCE'
        item.update(provider_received_at=item['received_at'],
                    notice='归仓修正离线实路参考；保留原采集时间，未核验当前路况、货车限行及计划发车时刻',
                    application_scope='AUTHORIZED_WAREHOUSE_CORRECTION_ONLY')
        item['evidence_hash']=content_hash({k:v for k,v in item.items() if k!='evidence_hash'})
        return item


def verified_application_result(data,result,road,extension_keys=()):
    proof=check_result(data,result);audit=independent_audit(data,result,road)
    require(audit['valid'] and not result['unassigned'] and result['fees_complete'],'Application result is incomplete or invalid')
    require(all(e['code']=='DRIVING_REFERENCE' for e in proof['issues']),'Application has blocking independent-check issues')
    if result['routes']:
        require(not result['confirmable'] and result['road_source']=='DRIVING','A driving reference cannot become executable truck proof')
    else:
        require(result['road_source']=='COMMON_ONLY','A fully common plan must not invent a dedicated road source')
    counts=Counter();received=[];extension_segments=0
    for route in result['routes']:
        for item in route['segments']:
            request=item['request'];key=content_hash(DrivingRoad.reference_request(request['origin'],request['destination']))
            row=road.connection.execute('SELECT * FROM metrics WHERE provider_key=?',(key,)).fetchone()
            require(row is not None,'Selected edge has no original measured source')
            source=_check_source_row(row,key)
            require(item['provider_evidence_hash']==source['evidence_hash'] and item['received_at']==source['received_at'],'Selected source age/hash was changed')
            require(item['polyline']==source['polyline'] and item['source']=='DRIVING' and item['verified'] is False,'Selected geometry/source was invented or promoted')
            require(item.get('frozen_graph_hash')==road.graph_hash,'Selected edge belongs to another frozen graph')
            age=int(time.time())-item['received_at'];status='HISTORICAL_TRAFFIC_REFERENCE' if age>=3600 else 'CURRENT_TRAFFIC_REFERENCE'
            require(item['time_status']==status and 0<=age<7*86400,'Selected historical marking is incorrect')
            counts[status]+=1;received.append(item['received_at']);extension_segments+=key in extension_keys
    require(road.stats()['calls']==0,'Offline application unexpectedly called a provider')
    return {'audit':audit,'independent_check':proof,'road_evidence':{'original_source_hashes_verified':True,
            'historical_reference_segments':counts['HISTORICAL_TRAFFIC_REFERENCE'],
            'current_reference_segments':counts['CURRENT_TRAFFIC_REFERENCE'],'extension_segments':extension_segments,
            'oldest_received_at':min(received,default=None),'newest_received_at':max(received,default=None),
            'scope':'ORDINARY_DRIVING_REFERENCE_NOT_TRUCK_OR_FUTURE_TRAFFIC_PROOF' if received else 'COMMON_ONLY_NO_DEDICATED_ROAD_PROOF','external_calls':0}}


def repair_for_corrected_input(original_result,data,road):
    routes=[];common={r['task_id'] for r in original_result['common']};removed=[]
    for route in original_result['routes']:
        vehicle=data['vehicles'][route['vehicle_id']]
        keep=[i for i in route['task_ids'] if data['tasks'][i]['warehouse_id']==vehicle['warehouse_id']]
        dropped=set(route['task_ids'])-set(keep)
        require(all(data['tasks'][i]['common_unit_price'] is not None for i in dropped),'Minimal correction repair has no priced common alternative')
        common.update(dropped);removed.extend(sorted(dropped))
        if keep:routes.append(evaluate_route(data,route['vehicle_id'],keep,road))
    result=assemble(data,routes,common,[])
    verified_application_result(data,result,road)
    return result,removed


def winner_profile(study_summary,original_hash,profile_rank=None):
    from scripts.run_algorithm_study import profiles,profile_ranking
    require(study_summary.get('status') in {'COMPLETED','COMPLETED_WITH_DEADLINE_SKIPS'},'Formal study is not complete')
    require(study_summary.get('source_files_unchanged') is True,'Formal study did not preserve frozen sources')
    require(profile_rank is None or type(profile_rank) is int and 1<=profile_rank<=10,'Application profile rank must be between 1 and 10')
    best=study_summary.get('best_by_case',{}).get('main225')
    ranks=study_summary.get('ranking_by_case',{}).get('main225')
    if ranks is not None or profile_rank is not None:
        require(isinstance(ranks,list) and bool(ranks),'Formal study has no main225 profile ranking')
        require(ranks[0].get('best')==best,'Formal ranking disagrees with the main225 winner')
        require(len({row.get('profile_name') for row in ranks})==len(ranks),'Formal ranking repeats a profile')
        if 'runs' in study_summary:
            require(profile_ranking(study_summary['runs'],'main225')==ranks,'Formal ranking disagrees with its recorded runs')
        index=(profile_rank or 1)-1
        require(index<len(ranks),'Formal study has no requested ranked profile')
        selected=ranks[index];winner=selected.get('best')
        require(isinstance(winner,dict) and selected.get('profile_name')==winner.get('profile_name')
                and selected.get('best_fee')==winner.get('audit',{}).get('total_fee'),'Ranked profile metadata differs from its best run')
    else:winner=best  # Backward-compatible rank-one selection for older reports.
    require(isinstance(winner,dict) and winner.get('status')=='VALID' and winner.get('audit',{}).get('valid'),'No valid main225 winner')
    require(winner['input_hash']==original_hash and not winner['audit']['unassigned_tasks'],'Winner is not the complete original-input comparison')
    require(winner.get('case_name','main225')=='main225','Ranked profile belongs to another case')
    found=next((p for p in profiles() if p['name']==winner['profile_name']),None)
    require(found is not None and found['entrypoint']==winner['entrypoint'] and found['options']==winner['options'],'Winner differs from a registered frozen profile')
    return winner,found


def application_run_output(case_file,study_summary_file,baseline_file,case,run_output=None):
    """Reserve only a new direct child of the already-private application case."""
    root=Path(case_file).resolve().parent
    require(root.is_dir() and root.stat().st_mode&0o077==0,'Prepared application directory is not private')
    output=Path(run_output) if run_output is not None else root/'application-run'
    if not output.is_absolute() and len(output.parts)==1:output=root/output
    require(not output.exists() and not output.is_symlink(),'Application run output already exists')
    output=output.resolve()
    require(output.parent==root and output.name not in {'.','..'},'Run output must be a new direct child of the private application case')
    protected={Path(p).resolve() for p in case['paths'].values()}
    protected.update(Path(p).resolve() for p in [case_file,study_summary_file,baseline_file,
                     case['source_ready_file'],case['private_graph'],root/'input.json',root/'original-input.json'])
    require(output not in protected,'Run output would replace a protected source')
    study_file=Path(study_summary_file).resolve();formal=study_file.parent
    if study_file.name=='study_summary.json' or (formal/'study_manifest.json').exists():
        require(not output.is_relative_to(formal) and not formal.is_relative_to(output),'Run output overlaps the formal study')
    return output


def run_application(case_file,study_summary_file,baseline_file,*,budget_seconds=None,seed=None,profile_rank=None,run_output=None):
    case=read_json(case_file);ready=verify_source_ready(case['source_ready_file'])
    require(case['status']=='PREPARED' and ready['source_hashes']==case['source_hashes'],'Application case/source gate changed')
    require(case['verification_script_sha256']==hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'Prepared application verifier changed')
    body=read_json(Path(case_file).parent/'input.json');original=read_json(Path(case_file).parent/'original-input.json')
    require(content_hash(body)==case['transition']['corrected_input_hash'] and content_hash(original)==case['transition']['original_input_hash'],'Prepared inputs changed')
    correction=read_json(case['paths']['correction'])
    guard=main_guard(case['paths']['database'],body,correction)
    require(guard['guard_token']==case['main_guard']['guard_token'],'Main input or head changed since preparation')
    study=read_json(study_summary_file);winner,formal_profile=winner_profile(study,content_hash(original))
    selected,profile=winner_profile(study,content_hash(original),profile_rank)
    require(winner['graph_hash']==selected['graph_hash']==case['base_graph']['graph_hash'],'Formal winner used another original graph')
    require(all(study['manifest']['source_hashes'].get(k)==v for k,v in ready['source_hashes'].items()),'Formal study used different frozen sources')
    saved=read_json(baseline_file)
    require(saved['input_hash']==content_hash(original) and saved['input']==original,'Collection baseline belongs to another original input')
    original_winner=read_json(winner['result_file'])
    output=application_run_output(case_file,study_summary_file,baseline_file,case,run_output)
    budget=selected['budget_seconds'] if budget_seconds is None else budget_seconds
    chosen_seed=selected['seed'] if seed is None else seed
    require(isinstance(budget,int) and 1<=budget<=24000 and isinstance(chosen_seed,int) and chosen_seed>=0,'Invalid application budget or seed')
    deadline_epoch=study.get('deadline_epoch',ready.get('deadline_epoch'))
    require(isinstance(deadline_epoch,int) and time.time()+budget+30<deadline_epoch,'Application budget exceeds the authorized deadline/validation reserve')
    output.mkdir(mode=0o700)
    road=ApplicationFrozenRoad(case['private_graph']);data=normalize(body);old_data=normalize(original)
    try:
        require(road.graph_hash==case['private_graph_hash'],'Private merged graph changed')
        saved_repriced=replay_baseline(old_data,saved,road);require(saved_repriced is not None,'Collection baseline cannot be replayed completely')
        original_repriced=replay_baseline(old_data,{'result':original_winner},road);require(original_repriced is not None,'Original winner cannot be replayed completely')
        verified_application_result(old_data,saved_repriced,road);verified_application_result(old_data,original_repriced,road)
        require(original_repriced['total_fee']==winner['audit']['total_fee']==original_winner['total_fee'],'Original winning fee changed on the merged graph')
        repaired,removed=repair_for_corrected_input(original_repriced,data,road)
        private_json(output/'original-baseline-repriced.json',saved_repriced);private_json(output/'original-winner-repriced.json',original_repriced)
        private_json(output/'corrected-minimal-repair.json',repaired)
        started=time.monotonic()
        def cancelled():return time.monotonic()-started>budget+3 or time.time()>=deadline_epoch
        result=Algorithm(profile['name'],profile['entrypoint'],profile['options']).function()(deepcopy(body),road,budget,chosen_seed,cancelled,initial_result=repaired,**profile['options'])
        elapsed=time.monotonic()-started
        with read_only(case['paths']['extension_graph']) as extension:extension_keys={row[0] for row in extension.execute('SELECT provider_key FROM metrics')}
        proof=verified_application_result(data,result,road,extension_keys)
        require(graph_digest(road.connection)==case['private_graph_hash'],'Private graph changed during application run')
        algorithm=result.get('algorithm',{})
        require(isinstance(algorithm.get('mode'),str) and bool(algorithm['mode']),'Algorithm identity is missing')
        require(isinstance(algorithm.get('elapsed_seconds'),(float,int)) and not isinstance(algorithm['elapsed_seconds'],bool)
                and isfinite(algorithm['elapsed_seconds']) and algorithm['elapsed_seconds']>=0,'Algorithm elapsed time is missing')
        require(Decimal(result['total_fee'])<=Decimal(repaired['total_fee']),'Winning method regressed against its corrected feasible baseline')
        try:
            after=main_guard(case['paths']['database'],body,correction)
            guard_matches=after['guard_token']==guard['guard_token']
        except ApplicationGuardError as exc:
            after={'guard_error':str(exc)};guard_matches=False
        prices=[Decimal(r['total_fee']) for r in [saved['result'],saved_repriced,original_repriced,repaired,result]]
        attribution={'collection_baseline_fee':str(prices[0]),'collection_baseline_repriced_fee':str(prices[1]),
            'original_input_formal_winner_fee':str(prices[2]),'corrected_minimal_repair_fee':str(prices[3]),'corrected_optimized_fee':str(prices[4]),
            'road_repricing_delta':str(prices[1]-prices[0]),'original_input_algorithm_delta':str(prices[2]-prices[1]),
            'warehouse_minimal_repair_delta':str(prices[3]-prices[2]),'additional_corrected_optimization_delta':str(prices[4]-prices[3]),
            'total_delta_from_collection_baseline':str(prices[4]-prices[0]),
            'warehouse_component_scope':'MINIMAL_FEASIBILITY_REPAIR_NOT_AN_ISOLATED_GLOBAL_OPTIMUM_EFFECT'}
        require(sum((prices[i+1]-prices[i] for i in range(4)),Decimal(0))==prices[4]-prices[0],'Attribution does not reconcile')
        if guard.get('head_fee_summary'):
            compare_manual(result,guard['head_fee_summary'])
            result['comparison'].update(previous_input_hash=guard['head_input_hash'],current_input_hash=content_hash(body),
                input_changed=guard['head_input_hash']!=content_hash(body),source_head_id=guard['head_id'],
                scope='CURRENT_HEAD_TO_SEPARATE_CORRECTED_INPUT_APPLICATION')
        result['algorithm']['correction_application']={'original_input_hash':content_hash(original),'corrected_input_hash':content_hash(body),
            'formal_winning_profile':formal_profile,'application_selected_profile':profile,'application_profile_rank':profile_rank or 1,
            'formal_input_scope':'ORIGINAL_V2_ONLY','application_input_scope':'CORRECTED_V3_ONLY',
            'measured_graph_hash':road.graph_hash,'captured_main_guard':guard,'wall_elapsed_seconds':round(elapsed,4),
            'frozen_source_hashes_hash':content_hash(ready['source_hashes']),'global_corrected_input_optimality_proven':False}
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],
                'Frozen sources changed during the application run')
        report={'schema_version':1,'status':'VALIDATED_READY_FOR_GUARDED_APPEND' if guard_matches else 'MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND',
                'scope':'SEPARATE_CORRECTED_INPUT_APPLICATION_NOT_FORMAL_ALGORITHM_COMPARISON',
                'source_business_database_written':False,'external_calls':0,'task_count':len(data['tasks']),
                'eligible_for_guarded_append':guard_matches,'global_corrected_input_optimality_proven':False,
                'pieces':sum(t['pieces'] for t in data['tasks'].values()),'proof':proof,'attribution':attribution,
                'minimal_repair_to_common_task_ids':removed,'winning_profile':formal_profile,'selected_profile':profile,
                'profile_rank':profile_rank or 1,'seed':chosen_seed,'budget_seconds':budget,
                'run_output':str(output),'case_file':str(Path(case_file).resolve()),
                'case_hash':content_hash(case),'corrected_input_hash':content_hash(body),'original_input_hash':content_hash(original),
                'private_graph_hash':case['private_graph_hash'],'study_summary_hash':content_hash(study),
                'elapsed_seconds':round(elapsed,4),'main_guard_before':guard,'main_guard_after':after,
                'application_deadline_epoch':deadline_epoch,
                'verification_script_sha256':case['verification_script_sha256'],
                'source_hashes_unchanged':True,
                'result_file':str(output/'corrected-result.json'),'result_hash':content_hash(result),
                'append_recipe':{'service':'app.plan_service.append_version','status':'DRAFT','parent_id':guard['head_id'],
                    'snapshot_id':guard['snapshot_id'],'expected_head':guard['expected_head'],'must_recheck_guard_token':guard['guard_token'],
                    'required_short_transaction_guards':['start_write','expect_head','verified_body','input_context fingerprint',
                        'master/store/demand revisions','no active group jobs','check_result','independent_audit','source hashes unchanged']}}
        private_json(output/'corrected-result.json',result);private_json(output/'application-report.json',report)
        return report
    finally:road.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for name in ['inspect','prepare']:
        command=sub.add_parser(name);command.add_argument('--database',required=True);command.add_argument('--original-input',required=True)
        command.add_argument('--corrected-input',required=True);command.add_argument('--correction',required=True)
        command.add_argument('--base-graph',required=True);command.add_argument('--extension-graph',required=True)
        command.add_argument('--extension-summary',required=True);command.add_argument('--source-ready',required=True)
        command.add_argument('--expected-tasks',type=int,default=225);command.add_argument('--output',required=name=='prepare')
    run=sub.add_parser('run');run.add_argument('--case',required=True);run.add_argument('--study-summary',required=True)
    run.add_argument('--collection-baseline',required=True);run.add_argument('--budget-seconds',type=int);run.add_argument('--seed',type=int)
    run.add_argument('--profile-rank',type=int,choices=range(1,11));run.add_argument('--run-output')
    args=parser.parse_args()
    if args.action=='run':report=run_application(args.case,args.study_summary,args.collection_baseline,budget_seconds=args.budget_seconds,
                                                seed=args.seed,profile_rank=args.profile_rank,run_output=args.run_output)
    else:
        report=inspect_application(args.database,args.original_input,args.corrected_input,args.correction,args.base_graph,
                                  args.extension_graph,args.extension_summary,args.source_ready,args.expected_tasks)
        if args.action=='prepare':report=prepare_application(report,args.output)
        elif args.output:private_json(args.output,report)
    print(json.dumps({k:report.get(k) for k in ['status','scope','external_calls','source_business_database_written','case_file','task_count','pieces']},ensure_ascii=False))


if __name__=='__main__':main()
