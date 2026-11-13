"""Inspect a fully proved private application; only --apply appends one DRAFT.

This command never calls HTTP, creates accounts, confirms execution, or changes
old plans/snapshots. Expensive evidence verification precedes the short writer
transaction. The final main-state guard is repeated on that writer's Session.
"""
from __future__ import annotations

import argparse
from contextlib import closing,contextmanager
from copy import deepcopy
from dataclasses import dataclass,field
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from sqlalchemy import create_engine,func,select
from sqlalchemy.orm import Session

from app.api import event
from app.database import start_write
from app.models import Account,DemandGroup,InputSnapshot,PlanningJob,Store,SystemState
from app.plan_service import append_version,expect_head,head,verified_body
from app.planning_inputs import content_hash,input_context
from app.road_service import DrivingRoad
from app.routing_engine import compare_manual,normalize
from scripts.benchmark_algorithms import graph_digest,read_only
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    readonly_session,require,validate_transition,verified_application_result,verify_source_ready,winner_profile)

DEFAULT_OPERATIONS=ROOT/'data/algorithm-lab-20261113/corrected-application-operations.json'
SYSTEM_ACTOR_NAME='系统算法与归仓复核'
APPEND_NOTE='正式研究方法用于独立授权的V3归仓输入；已刷新普通驾车参考，保留旧版本，待人工复核。'
APPLICATION_SCOPE='SEPARATE_CORRECTED_INPUT_APPLICATION_NOT_FORMAL_ALGORITHM_COMPARISON'
MERGE_SCOPE='CROSS_RUN_VALIDATED_ROUTE_POOL_ONLY'
FRESH_SCOPE='FIXED_SELECTED_ROUTES_FRESH_ORDINARY_DRIVING_REFERENCE'


class CommittedAppendError(RuntimeError):
    def __init__(self,summary):
        self.summary=summary
        super().__init__('草稿已提交，但提交后复查失败；请按plan_id核对，勿重复应用：'+summary['plan_id'])


def file_stamp(path):
    stat=Path(path).stat()
    return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)


def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):digest.update(part)
    return digest.hexdigest()


def guard_in_session(db,body,correction):
    """Same token as preparation.main_guard, using the already-reserved writer."""
    snapshot=db.get(InputSnapshot,correction['new_snapshot_id'])
    require(snapshot is not None,'Corrected snapshot no longer exists')
    require(snapshot.content_hash==content_hash(body)==correction['new_snapshot_hash'],'Corrected snapshot hash changed')
    require(verified_body(snapshot)==body and snapshot.version==correction['new_snapshot_version'],'Snapshot body or version differs')
    latest=db.scalar(select(InputSnapshot).where(InputSnapshot.group_id==snapshot.group_id,
        InputSnapshot.delivery_date==snapshot.delivery_date).order_by(InputSnapshot.version.desc()).limit(1))
    require(latest is not None and latest.id==snapshot.id,'A newer input snapshot exists')
    context=input_context(db,snapshot.delivery_date,group_id=snapshot.group_id)
    require(context['fingerprint']==snapshot.input_fingerprint,'Current inputs changed after corrected snapshot')
    state=db.get(SystemState,1)
    require(state is not None and state.master_revision==body['source_stamp']['master_revision'],'Current master revision changed')
    group=db.get(DemandGroup,snapshot.group_id);store=db.get(Store,correction['store_id'])
    require(group is not None and group.revision==body['demand_revision'],'Demand group revision changed')
    require(store is not None and store.revision==correction['revision_after'],'Corrected store revision changed')
    require(all(getattr(store,key)==value for key,value in correction['after'].items()),'Corrected master store fields changed')
    active=db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.status.in_(['QUEUED','RUNNING'])))
    require(active==0,'Main database has a queued or running planning task')
    pending=db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.group_id==snapshot.group_id,
        PlanningJob.status.in_(['QUEUED','RUNNING'])))
    current=head(db,snapshot.delivery_date,snapshot.group_id)
    saved=verified_body(current) if current else None
    guard={'group_id':snapshot.group_id,'delivery_date':snapshot.delivery_date,'snapshot_id':snapshot.id,
        'snapshot_version':snapshot.version,'snapshot_hash':snapshot.content_hash,'input_fingerprint':snapshot.input_fingerprint,
        'master_revision':state.master_revision,'demand_revision':group.revision,'store_id':store.id,
        'store_revision':store.revision,'warehouse_id':store.warehouse_id,'head_id':current.id if current else None,
        'expected_head':current.version if current else 0,'head_content_hash':current.content_hash if current else None,
        'head_snapshot_id':current.snapshot_id if current else None,'head_input_hash':saved['input_hash'] if current else None,
        'head_fee_summary':{'total_fee':saved['result']['total_fee'],'fees_complete':saved['result']['fees_complete'],
            'check':{key:saved['result']['check'][key] for key in ['dedicated_fee','common_fee']}} if current else None,
        'head_total_fee':saved['result']['total_fee'] if current else None,'active_group_jobs':pending}
    guard['guard_token']=content_hash(guard)
    return guard,snapshot,current


def validate_guard_token(guard):
    require(guard.get('guard_token')==content_hash({key:value for key,value in guard.items() if key!='guard_token'}),
            'Captured main guard token is invalid')


def validate_comparison(result,guard,body):
    require(guard.get('head_id') and guard.get('head_fee_summary'),'Application must compare to the captured current HEAD')
    compared=deepcopy(result);compare_manual(compared,guard['head_fee_summary'])
    actual=result.get('comparison',{});expected=compared['comparison']
    require(all(actual.get(key)==value for key,value in expected.items()),'Final comparison does not match current HEAD fees')
    require(actual.get('source_head_id')==guard['head_id'] and actual.get('previous_input_hash')==guard['head_input_hash']
        and actual.get('current_input_hash')==content_hash(body)
        and actual.get('input_changed') is (guard['head_input_hash']!=content_hash(body)),
        'Final comparison input lineage does not match current HEAD')


@dataclass
class VerifiedAppend:
    database:Path
    body:dict
    correction:dict
    result:dict
    expected_guard:dict
    actor_id:str
    source_ready_file:Path
    source_hashes:dict
    fresh_report_hash:str
    result_hash:str
    graph:ApplicationFrozenRoad|None=None
    graph_path:Path|None=None
    graph_hash:str|None=None
    artifact_stamps:dict=field(default_factory=dict)
    artifact_hashes:dict=field(default_factory=dict)

    def stable(self):
        for path,stamp in self.artifact_stamps.items():
            require(Path(path).is_file() and file_stamp(path)==stamp,'Verified artifact changed: '+str(path))
        if self.graph_path is not None:
            require(not any(Path(str(self.graph_path)+suffix).exists() for suffix in ('-wal','-shm')),
                    'Fresh frozen graph acquired WAL/SHM side files')
            require(self.graph is not None and self.graph.graph_hash==self.graph_hash,'Verified graph binding changed')

    def close(self):
        if self.graph is not None:self.graph.close();self.graph=None


def _tracked_json(path,stamps,hashes):
    path=Path(path).resolve();require(path.is_file(),'Required artifact is missing: '+str(path))
    before=file_stamp(path)
    require(str(path) not in stamps or stamps[str(path)]==before,'Previously verified artifact changed: '+str(path))
    value=json.loads(path.read_text());sha=file_sha256(path)
    require(file_stamp(path)==before,'Artifact changed while it was read: '+str(path))
    stamps[str(path)]=before;hashes[str(path)]=sha
    return value


def _tracked_file(path,stamps,hashes,expected=None):
    path=Path(path).resolve();require(path.is_file(),'Required artifact is missing: '+str(path))
    before=file_stamp(path)
    require(str(path) not in stamps or stamps[str(path)]==before,'Previously verified artifact changed: '+str(path))
    sha=file_sha256(path)
    require(file_stamp(path)==before,'Artifact changed while it was hashed: '+str(path))
    require(expected is None or sha==expected,'Artifact file hash changed: '+str(path))
    stamps[str(path)]=before;hashes[str(path)]=sha


def allocation(result):
    return (sorted((row['vehicle_id'],tuple(row['task_ids'])) for row in result['routes']),
            sorted(row['task_id'] for row in result['common']))


def verify_origin_report(report,case,study,original,body,read,stamps,hashes,seen=None,study_file=None):
    """Validate a registered application or a hash-linked merge, never invent ranks."""
    seen=set() if seen is None else seen
    identity=content_hash(report)
    require(identity not in seen and len(seen)<16,'Application provenance has a cycle or excessive nesting')
    seen.add(identity);_report_complete(report)
    require(report['case_hash']==content_hash(case) and report['corrected_input_hash']==content_hash(body)
        and report['private_graph_hash']==case['private_graph_hash']
        and report['verification_script_sha256']==case['verification_script_sha256'],'Application origin bindings disagree')
    result=read(report['result_file']);require(content_hash(result)==report['result_hash'],'Application origin result changed')
    scope=report.get('scope')
    if scope=='LATEST_INPUT_REPRICE_ONLY':
        from scripts.rebind_application_input import verify_latest_report
        verify_latest_report(report,case)
        _tracked_file(ROOT/'scripts/rebind_application_input.py',stamps,hashes,report['rebind_script_sha256'])
        for path,expected in report['origin_files_sha256'].items():_tracked_file(path,stamps,hashes,expected)
        return []  # A fixed-recipe reprice has no invented research profile rank.
    if scope==APPLICATION_SCOPE:
        require(report['original_input_hash']==content_hash(original) and report['study_summary_hash']==content_hash(study),
                'Original application input or formal summary changed')
        _,formal_profile=winner_profile(study,content_hash(original))
        _,selected_profile=winner_profile(study,content_hash(original),report['profile_rank'])
        require(report['winning_profile']==formal_profile and report['selected_profile']==selected_profile,
                'Application profile is not the recorded formal profile')
        return [{'profile_rank':report['profile_rank'],'selected_profile':selected_profile}]
    require(scope==MERGE_SCOPE,'Unknown source application report scope')
    require(report.get('formal_ranking_unchanged') is True,'Merged application changed formal rankings')
    _tracked_file(ROOT/'scripts/merge_application_routes.py',stamps,hashes,report['merge_script_sha256'])
    origins=report.get('origin_files');require(isinstance(origins,dict) and bool(origins),'Merged application omitted source files')
    manifest=read(Path(report['run_output'])/'source-manifest.json')
    require(manifest==origins,'Merged source manifest differs from report')
    required={str(Path(case['case_file']).resolve()),str((Path(case['case_file']).parent/'input.json').resolve()),
        str((Path(case['case_file']).parent/'original-input.json').resolve())}
    if study_file is not None:required.add(str(Path(study_file).resolve()))
    require(required.issubset(origins),'Merged provenance omits prepared input artifacts')
    profiles=[];formal_found=False
    for path,sha in origins.items():
        _tracked_file(path,stamps,hashes,sha);item=read(path)
        if isinstance(item,dict) and content_hash(item)==content_hash(study):formal_found=True
        if isinstance(item,dict) and item.get('scope') in {APPLICATION_SCOPE,MERGE_SCOPE}:
            profiles.extend(verify_origin_report(item,case,study,original,body,read,stamps,hashes,seen.copy(),study_file))
    require(formal_found and profiles,'Merged provenance lacks the formal study or a verified ranked application')
    return profiles


def verify_fee_bridge(fresh,candidate,result,guard):
    bridge=fresh['fee_bridge'];old=Decimal(candidate['total_fee']);new=Decimal(result['total_fee']);head_fee=Decimal(guard['head_total_fee'])
    require(bridge.get('fixed_vehicle_order_common_choices_unchanged') is True,'Fresh fee bridge changed the selected allocation')
    expected={'corrected_frozen_fee':old,'fresh_fixed_order_fee':new,'fresh_reference_delta':new-old,
        'current_head_fee':head_fee,'delta_from_current_head':new-head_fee,'corrected_frozen_delta_from_current_head':old-head_fee}
    require(all(Decimal(bridge[key])==value for key,value in expected.items()),'Fresh fee bridge does not reconcile exactly')


def _report_complete(report,*,fresh=False):
    status='READY' if fresh else 'VALIDATED_READY_FOR_GUARDED_APPEND'
    require(report.get('status')==status and report.get('eligible_for_guarded_append') is True,'Report is not eligible for append')
    require(report.get('source_business_database_written') is False,'Report already wrote the business database')
    require(report.get('task_count')==225 and report.get('pieces')==4378,'Application must cover exactly 225 tasks and 4378 pieces')
    require(report.get('source_hashes_unchanged') is True,'Report did not preserve frozen sources')
    calls=report.get('offline_proof_external_calls') if fresh else report.get('external_calls')
    require(type(calls) is int and calls==0,'Offline verification must perform zero provider calls')
    proof=report.get('proof',{});audit=proof.get('audit',{});check=proof.get('independent_check',{})
    require(audit.get('valid') is True and audit.get('unassigned_tasks')==0,'Report independent audit is incomplete')
    issues=check.get('issues')
    require(isinstance(issues,list) and check.get('valid') is (not issues)
        and all(isinstance(issue,dict) and issue.get('code')=='DRIVING_REFERENCE' for issue in issues),
        'Report contains blocking independent-check issues or an incorrect reference-only validity flag')


def verify_application(fresh_report_file,operations_file=DEFAULT_OPERATIONS,database=None):
    """Full proof outside a main writer transaction; keep the graph snapshot open."""
    stamps={};hashes={}
    def read(path):return _tracked_json(path,stamps,hashes)
    operations=read(operations_file);fresh=read(fresh_report_file);_report_complete(fresh,fresh=True)
    require(fresh.get('scope')==FRESH_SCOPE,'Unknown fresh-road application scope')
    case=read(fresh['case_file']);application=read(fresh['application_report_file']);_report_complete(application)
    study=read(fresh['study_summary_file']);ready=read(fresh['source_ready_file'])
    result=read(fresh['result_file']);candidate=read(application['result_file'])
    case_folder=Path(fresh['case_file']).resolve().parent
    body=read(case_folder/'input.json');original=read(case_folder/'original-input.json');correction=read(case['paths']['correction'])
    require(case.get('status')=='PREPARED','Application case is not prepared')
    require(Path(case['case_file']).resolve()==Path(fresh['case_file']).resolve(),'Prepared case self-path changed')
    require(fresh['case_hash']==application['case_hash']==content_hash(case),'Prepared case hash changed')
    require(fresh['application_report_hash']==content_hash(application),'Original application report hash changed')
    require(fresh['study_summary_hash']==content_hash(study),'Formal study summary hash changed')
    require(fresh['source_ready_hash']==content_hash(ready)==case['source_ready_hash'],'Source-ready artifact changed')
    require(Path(fresh['source_ready_file']).resolve()==Path(case['source_ready_file']).resolve(),'Source-ready path differs from prepared case')
    require(verify_source_ready(fresh['source_ready_file'])['source_hashes']==ready['source_hashes']==case['source_hashes']==fresh['source_hashes'],
            'Frozen source gates disagree')
    for relative,expected in ready['source_hashes'].items():
        _tracked_file(ROOT/relative,stamps,hashes,expected)
    verifier=ROOT/'scripts/prepare_corrected_application.py';refresher=ROOT/'scripts/refresh_application_roads.py'
    for path,expected in [(verifier,case['verification_script_sha256']),(refresher,fresh['refresh_script_sha256'])]:
        _tracked_file(path,stamps,hashes,expected)
    require(fresh['verification_script_sha256']==application['verification_script_sha256']==case['verification_script_sha256'],
            'Preparation helper bindings disagree')
    require(Path(fresh['refresh_script_file']).resolve()==refresher.resolve(),'Unexpected fresh-road helper path')
    if case.get('scope')=='AUTHORIZED_LATEST_INPUT_APPLICATION_CASE':
        from scripts.rebind_application_input import verify_latest_case
        transition=verify_latest_case(case)
    else:transition=validate_transition(original,body,correction,225)
    require(transition['pieces']==4378 and fresh['corrected_input_hash']==application['corrected_input_hash']==content_hash(body)
        and fresh['original_input_hash']==content_hash(original),'Input hashes or quantities changed')
    require(application['result_hash']==content_hash(candidate) and fresh['result_hash']==content_hash(result),'Result content hash changed')
    require(allocation(result)==allocation(candidate),'Fresh-road application changed selected vehicles, task order or common allocation')
    verify_origin_report(application,case,study,original,body,read,stamps,hashes,study_file=fresh['study_summary_file'])
    winner,_=winner_profile(study,content_hash(original))
    require(winner['graph_hash']==case['base_graph']['graph_hash'],'Formal study graph differs from the prepared original graph')
    require(all(study['manifest']['source_hashes'].get(key)==value for key,value in ready['source_hashes'].items()),'Formal source manifest changed')
    require(fresh['parent_private_graph_hash']==fresh['original_private_graph_hash']==case['private_graph_hash']==application['private_graph_hash'],
            'Fresh-road graph lineage differs from original corrected application')
    require(Path(fresh['original_private_graph_file']).resolve()==Path(case['private_graph']).resolve(),'Parent private graph path changed')
    parent_graph=Path(case['private_graph']).resolve()
    require(not any(Path(str(parent_graph)+suffix).exists() for suffix in ('-wal','-shm')),'Parent frozen graph depends on WAL/SHM')
    _tracked_file(parent_graph,stamps,hashes,fresh['original_private_graph_sha256'])
    with closing(read_only(parent_graph)) as connection:
        require(graph_digest(connection)==case['private_graph_hash'],'Parent private graph changed')
    captured=operations['current_captured_guard'];validate_guard_token(captured)
    for guard in [case['main_guard'],application['main_guard_before'],application['main_guard_after'],fresh['main_guard_before'],fresh['main_guard_after']]:
        validate_guard_token(guard);require(guard==captured,'Main guard differs across preparation, application and refresh')
    require(fresh['guard_token']==captured['guard_token'],'Fresh-report guard token changed')
    actor=operations['actor_candidate_from_corrected_snapshot']
    require(actor.get('active')==1 and actor.get('deleted_at') is None,'Captured actor is not an active existing account')
    database=Path(database or case['paths']['database']).resolve()
    require(database==Path(case['paths']['database']).resolve() and database.is_file(),'Business database path differs from prepared case')
    validate_comparison(result,captured,body)
    verify_fee_bridge(fresh,candidate,result,captured)
    require(isinstance(fresh.get('origin_files_sha256'),dict) and bool(fresh['origin_files_sha256']),
            'Fresh report omitted original source file hashes')
    for path,sha in fresh['origin_files_sha256'].items():_tracked_file(path,stamps,hashes,sha)
    graph_path=Path(fresh['fresh_graph_file']).resolve()
    require(graph_path!=Path(case['private_graph']).resolve() and graph_path!=database,'Fresh graph must be a separate artifact')
    require(graph_path.is_file() and not any(Path(str(graph_path)+suffix).exists() for suffix in ('-wal','-shm')),
            'Fresh graph is missing or depends on WAL/SHM')
    before=file_stamp(graph_path);require(file_sha256(graph_path)==fresh['fresh_graph_sha256'],'Fresh graph file hash changed')
    road=None
    try:
        road=ApplicationFrozenRoad(graph_path);road.connection.execute('BEGIN')
        require(road.connection.execute('PRAGMA journal_mode').fetchone()[0].lower()=='delete','Fresh graph is not a closed immutable SQLite artifact')
        require(graph_digest(road.connection)==road.graph_hash==fresh['fresh_graph_hash'],'Fresh graph semantic hash changed')
        expected_metadata={'scope':FRESH_SCOPE,'input_hash':content_hash(body),'parent_graph_hash':case['private_graph_hash'],
            'snapshot_id':case['main_guard']['snapshot_id'],
            'network_started_at':fresh['network_started_at'],'network_finished_at':fresh['network_finished_at'],
            'actual_external_calls':fresh['actual_external_calls'],'source_business_database_written':False}
        require(all(road.metadata.get(key)==value for key,value in expected_metadata.items()),'Fresh graph metadata lineage changed')
        require(file_stamp(graph_path)==before,'Fresh graph changed during verification')
        data=normalize(body);proof=verified_application_result(data,result,road)
        require(len(data['tasks'])==225 and sum(task['pieces'] for task in data['tasks'].values())==4378,'Final input coverage changed')
        require(result.get('check')==proof['independent_check'] and proof['audit']==fresh['proof']['audit'],
                'Stored result or fresh-report audit differs from recomputed proof')
        require(fresh['proof']['independent_check']==proof['independent_check'],'Fresh-report independent check changed')
        require(fresh['proof']==proof,'Fresh-report complete road proof differs from independent recomputation')
        require(result.get('road_source')=='DRIVING' and result.get('confirmable') is False,'Only nonconfirmable ordinary-driving reference may be appended')
        keys=set()
        for route in result['routes']:
            for segment in route['segments']:
                require(segment.get('frozen_at')==road.frozen_at and segment['received_at']<=road.frozen_at,
                        'Selected road freeze predates its provider evidence')
                request=segment['request'];keys.add(content_hash(DrivingRoad.reference_request(request['origin'],request['destination'])))
        sources=fresh['selected_provider_sources']
        require(len(sources)==len(keys) and {row['provider_key'] for row in sources}==keys
            and len(fresh['selected_provider_keys'])==len(keys) and set(fresh['selected_provider_keys'])==keys,'Selected fresh provider sources are incomplete or duplicated')
        for source in sources:
            metric=road.metrics[source['provider_key']]
            require(source.get('source')=='DRIVING' and all(source[key]==metric[key] for key in ['meters','seconds','received_at','evidence_hash']),
                    'Fresh provider source differs from frozen evidence')
        calls=fresh['actual_external_calls']
        require(type(calls) is int and calls>=0 and fresh['network_stats'].get('calls')==calls,'Actual network-call accounting is inconsistent')
        require(type(fresh['network_started_at']) in (int,float) and type(fresh['network_finished_at']) in (int,float)
            and 0<fresh['network_started_at']<=fresh['network_finished_at']<=time.time()+1,'Fresh network time interval is invalid')
        require(all(fresh['network_started_at']-1<=row['received_at']<=fresh['network_finished_at']+1 for row in sources),
                'Selected road source was not collected during this refresh')
        require(all(0<=int(time.time())-row['received_at']<3600 for row in sources),'Fresh selected road reference is no longer current')
        require(type(road.frozen_at) is int and road.frozen_at>=fresh['network_finished_at'],'Fresh graph freeze predates network completion')
        require(result.get('algorithm',{}).get('optimality_proven') is False
            and result['algorithm'].get('fresh_reference',{}).get('fresh_graph_hash')==road.graph_hash,
                'Refreshed result retained an old-graph optimum certificate or lost fresh provenance')
        stamps[str(graph_path)]=before;hashes[str(graph_path)]=fresh['fresh_graph_sha256']
        bundle=VerifiedAppend(database,body,correction,result,captured,actor['id'],Path(fresh['source_ready_file']).resolve(),
            ready['source_hashes'],content_hash(fresh),content_hash(result),road,graph_path,road.graph_hash,stamps,hashes)
        bundle.stable()
        with readonly_session(database) as db:guard,_,_=guard_in_session(db,body,correction);verify_actor(db,bundle.actor_id)
        require(guard==captured,'Main state changed after fresh verification')
        return bundle
    except BaseException:
        if road is not None:road.close()
        raise


def verify_actor(db,actor_id):
    actor=db.get(Account,actor_id)
    require(actor is not None and actor.active and actor.deleted_at is None and actor.is_admin,
            'Existing authorized actor is no longer active, undeleted and administrative')
    return actor


@contextmanager
def write_session(database):
    """Open existing SQLite in rw mode; never create or migrate a database."""
    path=Path(database).resolve();require(path.is_file(),'Business database is missing')
    def connect():
        connection=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,check_same_thread=False,timeout=15)
        connection.execute('PRAGMA foreign_keys=ON');connection.execute('PRAGMA busy_timeout=15000')
        return connection
    engine=create_engine('sqlite://',creator=connect,hide_parameters=True)
    try:
        with Session(engine,autoflush=False,expire_on_commit=False) as db:yield db,engine
    finally:engine.dispose()


def append_verified(bundle):
    bundle.stable()
    with write_session(bundle.database) as (db,engine):
        try:
            start_write(db,engine)  # First operation on this Session: reserve writer.
            bundle.stable()
            require(verify_source_ready(bundle.source_ready_file)['source_hashes']==bundle.source_hashes,'Frozen sources changed before append')
            require(content_hash(bundle.result)==bundle.result_hash,'Validated result changed in memory')
            actor=verify_actor(db,bundle.actor_id)
            current_guard,snapshot,current=guard_in_session(db,bundle.body,bundle.correction)
            require(current_guard==bundle.expected_guard,'Main input or HEAD changed; nothing was appended')
            expect_head(db,snapshot.delivery_date,bundle.expected_guard['expected_head'],snapshot.group_id)
            validate_comparison(bundle.result,current_guard,bundle.body)
            row=append_version(db,snapshot,bundle.result,actor.id,SYSTEM_ACTOR_NAME,parent=current,status='DRAFT',note=APPEND_NOTE)
            event(db,actor.id,'VERIFIED_APPLICATION_APPENDED',row.id,
                  ('离线完整复核后追加草稿；报告 '+bundle.fresh_report_hash+'；结果 '+bundle.result_hash)[:240])
            summary={'status':'APPENDED_DRAFT','plan_id':row.id,'version':row.version,'parent_id':row.parent_id,
                'snapshot_id':row.snapshot_id,'group_id':row.group_id,'content_hash':row.content_hash,
                'result_hash':bundle.result_hash,'total_fee':bundle.result['total_fee'],'confirmed':False}
            bundle.stable()
            db.commit()  # Exactly one commit; standard plan audit and proof audit share it.
        except BaseException:
            db.rollback();raise
    try:
        with readonly_session(bundle.database) as db:
            from app.models import PlanVersion
            saved=db.get(PlanVersion,summary['plan_id']);require(saved is not None,'Appended plan disappeared')
            stored=verified_body(saved)
            require(saved.status=='DRAFT' and saved.parent_id==summary['parent_id'] and saved.content_hash==summary['content_hash']
                and saved.snapshot_id==summary['snapshot_id'] and saved.group_id==summary['group_id']
                and content_hash(stored['result'])==bundle.result_hash,'Post-commit append integrity verification failed')
    except Exception as exc:raise CommittedAppendError(summary) from exc
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fresh-report',required=True)
    parser.add_argument('--operations',default=str(DEFAULT_OPERATIONS))
    parser.add_argument('--database')
    parser.add_argument('--apply',action='store_true',help='追加一个DRAFT；默认仅只读检查')
    args=parser.parse_args(argv)
    bundle=verify_application(args.fresh_report,args.operations,args.database)
    try:
        result=append_verified(bundle) if args.apply else {'status':'VERIFIED_DRY_RUN','source_business_database_written':False,
            'current_head_id':bundle.expected_guard['head_id'],'expected_head':bundle.expected_guard['expected_head'],
            'snapshot_id':bundle.expected_guard['snapshot_id'],'result_hash':bundle.result_hash,
            'total_fee':bundle.result['total_fee'],'next_action':'Explicit --apply is required to append one DRAFT'}
        print(json.dumps(result,ensure_ascii=False))
    finally:bundle.close()


if __name__=='__main__':
    try:main()
    except CommittedAppendError as exc:raise SystemExit(str(exc))
    except (ApplicationGuardError,KeyError,ValueError,OSError) as exc:raise SystemExit('未应用：'+str(exc))
