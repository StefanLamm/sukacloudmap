"""Refresh only a verified V3 plan's fixed routes into a new private graph.

No business records, frozen original graph or original result are written. Live
operation requires --execute-live. Tests inject a MockTransport and a private
limiter. A READY report requires actual fresh provider sources plus a separate
offline materialization, exact quotation and independent full-allocation proof.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import gc
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from pydantic import SecretStr
from app.config import Settings
from app.planning_inputs import content_hash,encode
from app.road_graph import ReferenceGraph
from app.road_service import DrivingRoad
from app.routing_engine import StopSearch,compare_manual,normalize,recalculate
from scripts.benchmark_algorithms import graph_digest,private_json,read_only
from scripts.merge_application_routes import SCOPE as MERGE_SCOPE,load_json
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    _check_source_row,application_run_output,main_guard,require,verified_application_result,
    verify_source_ready,winner_profile)

SCOPE='FIXED_SELECTED_ROUTES_FRESH_ORDINARY_DRIVING_REFERENCE'
APPLICATION_SCOPE='SEPARATE_CORRECTED_INPUT_APPLICATION_NOT_FORMAL_ALGORITHM_COMPARISON'


def file_sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def consolidate_private_graph(path):
    with closing(sqlite3.connect(path)) as db:
        require(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0,'Private graph WAL checkpoint is busy')
        require(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()=='delete','Private graph did not leave WAL mode')
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Private graph integrity failed')


def backup_graph(source,target):
    """Include committed WAL content and hand off a closed standalone SQLite DB."""
    target=Path(target);require(not target.exists(),'Fresh graph output already exists')
    target.touch(mode=0o600,exist_ok=False)
    with closing(read_only(source)) as origin,closing(sqlite3.connect(target)) as destination:
        origin.backup(destination);destination.commit()
        require(destination.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0,'Private graph WAL checkpoint is busy')
        require(destination.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()=='delete','Private graph did not leave WAL mode')
        require(destination.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Private graph integrity failed')
    return target


class PrivateCache:
    """Fresh-job evidence cache; never uses the application's RoadCache table."""
    def __init__(self,path):
        self.path=Path(path);require(not self.path.exists(),'Private refresh cache already exists')
        self.path.touch(mode=0o600,exist_ok=False)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE evidence(key TEXT PRIMARY KEY,content TEXT NOT NULL)');db.commit()

    def get(self,key):
        with closing(read_only(self.path)) as db:row=db.execute('SELECT content FROM evidence WHERE key=?',(key,)).fetchone()
        if row is None:return None
        item=json.loads(row[0])
        require(content_hash(item['request'])==key,'Private cached evidence request differs from its key')
        require(DrivingRoad.valid_reference(item),'Private cached evidence is not a current reference')
        return item

    def put(self,key,item):
        require(content_hash(item['request'])==key and DrivingRoad.valid_reference(item),'Actual refreshed cache evidence is invalid')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT OR REPLACE INTO evidence VALUES (?,?)',(key,encode(item)));db.commit()


def fixed_draft(result):
    require(not result.get('unassigned'),'Fresh recalculation requires a complete current plan')
    return {'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':deepcopy(r['task_ids'])} for r in result['routes']],
            'common_ids':[r['task_id'] for r in result['common']],'unassigned_ids':[]}


def provider_keys(result):
    return {content_hash(DrivingRoad.reference_request(item['request']['origin'],item['request']['destination']))
            for route in result['routes'] for item in route['segments']}


def audit_refreshed_sources(path,expected,cache,freshened,started,finished):
    """A best-effort graph write is not proof: require each actual returned row."""
    require(set(freshened)==set(expected),'Actual refreshed provider directions differ from fixed selected routes')
    rows=[]
    with closing(read_only(path)) as db:
        for key in sorted(expected):
            row=db.execute('SELECT * FROM metrics WHERE provider_key=?',(key,)).fetchone()
            require(row is not None,'A selected provider source was not persisted')
            source=_check_source_row(row,key);actual=cache.get(key)
            require(actual is not None and actual['request'].get('source')=='DRIVING_PROVIDER_REFERENCE',
                    'Selected provider source was not returned by this refresh job')
            require(source['evidence_hash']==actual['evidence_hash'] and source==actual,'Fresh graph row differs from actual provider response')
            require(started<=source['received_at']<=finished and 0<=int(time.time())-source['received_at']<3600,
                    'Selected provider source is outside the actual refresh interval')
            rows.append({'provider_key':key,'source':'DRIVING','verified':False,'evidence_hash':source['evidence_hash'],
                         'received_at':source['received_at'],'meters':source['meters'],'seconds':source['seconds']})
    return rows


def stripped_origin_algorithm(value):
    """Old-graph search statistics remain provenance, never current certificates."""
    if isinstance(value,list):return [stripped_origin_algorithm(v) for v in value]
    if not isinstance(value,dict):return deepcopy(value)
    result={}
    for key,item in value.items():
        if 'optimal' in key:result[key]=False if isinstance(item,bool) else None
        elif key.endswith('mip_gap') or key in {'best_bound','cost_lower_bound','lower_bound'}:result[key]=None
        else:result[key]=stripped_origin_algorithm(item)
    return result


def verify_application_origin(application,case,case_file,study,study_file,files):
    require(application.get('status')=='VALIDATED_READY_FOR_GUARDED_APPEND' and
            application.get('case_hash')==content_hash(case),'Source application is not the bound ready case')
    if application.get('scope')=='LATEST_INPUT_REPRICE_ONLY':
        from scripts.rebind_application_input import verify_latest_report
        verify_latest_report(application,case,files)
        require(application['study_summary_hash']==content_hash(study),'Latest report changed formal-study attribution')
    elif application.get('scope')==APPLICATION_SCOPE:
        require(application.get('original_input_hash')==case['transition']['original_input_hash'] and
                application.get('study_summary_hash')==content_hash(study),'Application formal-input lineage differs')
        _,selected=winner_profile(study,case['transition']['original_input_hash'],application.get('profile_rank'))
        require(type(application.get('profile_rank')) is int and application['selected_profile']==selected,
                'Application selected profile differs from the registered formal rank')
    elif application.get('scope')==MERGE_SCOPE:
        merge_script=ROOT/'scripts/merge_application_routes.py';merge_sha=file_sha(merge_script)
        require(application.get('merge_script_sha256')==merge_sha,'Merged application script changed')
        origins=application.get('origin_files')
        require(isinstance(origins,dict) and bool(origins),'Merged application lacks source-file provenance')
        origins={str(Path(p).resolve()):sha for p,sha in origins.items()}
        root=Path(case_file).resolve().parent
        required={str(Path(case_file).resolve()),str(Path(study_file).resolve()),str(root/'input.json'),str(root/'original-input.json')}
        require(required.issubset(origins),'Merged application omits critical input/study artifacts')
        for path,sha in {**origins,str(merge_script):merge_sha}.items():
            require(file_sha(path)==sha,'Merged application origin changed: '+path)
            require(files.setdefault(path,sha)==sha,'Merged source differs from the captured input')
    else:raise ApplicationGuardError('Unsupported source application scope')


def refresh_application(case_file,study_file,application_report_file,*,output='selected-roads-refreshed',
                        budget_seconds=600,execute_live=False,settings=None,transport=None,limiter=None):
    started_monotonic=time.monotonic();files={};target=None;owns_target=False;road=None;network_stats={};freshened=set()
    network_started_at=None;network_finished_at=None
    try:
        case=load_json(case_file,files);ready=verify_source_ready(case['source_ready_file'])
        require(case.get('status')=='PREPARED' and ready['source_hashes']==case['source_hashes'],'Prepared source gate changed')
        helper=ROOT/'scripts/prepare_corrected_application.py'
        require(file_sha(helper)==case['verification_script_sha256'],'Prepared application verifier changed')
        script_file=Path(__file__).resolve();script_sha=file_sha(script_file)
        source_ready=load_json(case['source_ready_file'],files)
        require(content_hash(source_ready)==case['source_ready_hash'],'Source-ready artifact changed')
        body=load_json(Path(case_file).resolve().parent/'input.json',files)
        original=load_json(Path(case_file).resolve().parent/'original-input.json',files)
        require(content_hash(body)==case['transition']['corrected_input_hash'] and
                content_hash(original)==case['transition']['original_input_hash'],'Prepared input body changed')
        correction=load_json(case['paths']['correction'],files);study=load_json(study_file,files)
        winner_profile(study,content_hash(original))
        require(all(study['manifest']['source_hashes'].get(k)==v for k,v in ready['source_hashes'].items()),'Formal source hashes differ')
        deadline=study.get('deadline_epoch')
        require(type(budget_seconds) is int and 1<=budget_seconds<=24000,'Invalid refresh wall budget')
        require(type(deadline) is int and time.time()+budget_seconds+30<deadline,'Refresh exceeds the authorized deadline/validation reserve')
        application=load_json(application_report_file,files)
        verify_application_origin(application,case,case_file,study,study_file,files)
        require(application.get('eligible_for_guarded_append') is True and application.get('source_hashes_unchanged') is True and
                application.get('source_business_database_written') is False and application.get('external_calls')==0,
                'Source application is not a verified private offline candidate')
        require(application.get('corrected_input_hash')==content_hash(body) and application.get('private_graph_hash')==case['private_graph_hash'] and
                application.get('verification_script_sha256')==case['verification_script_sha256'],'Source application input/graph/verifier differs')
        require(application.get('proof',{}).get('audit',{}).get('valid') is True and
                application['proof']['audit'].get('unassigned_tasks')==0,'Source application lacks complete independent proof')
        old_result=load_json(application['result_file'],files)
        require(content_hash(old_result)==application.get('result_hash'),'Source application result changed')
        guard=main_guard(case['paths']['database'],body,correction)
        require(guard['guard_token']==case['main_guard']['guard_token'],'Current input or head changed since preparation')
        draft=fixed_draft(old_result);data=normalize(body)
        original_graph=Path(case['private_graph']).resolve();original_graph_sha=file_sha(original_graph)
        original_road=ApplicationFrozenRoad(original_graph)
        try:
            require(original_road.graph_hash==case['private_graph_hash'],'Original corrected graph changed')
            old_repriced=recalculate(body,draft,original_road)
            old_proof=verified_application_result(data,old_repriced,original_road)
            require(old_repriced['total_fee']==old_result['total_fee'],'Source fee differs under its bound corrected graph')
            expected=provider_keys(old_repriced)
        finally:original_road.close()
        require(transport is not None or execute_live,'Live refresh requires explicit --execute-live')
        settings=settings if settings is not None else Settings()
        require(settings.planning_route_mode=='DRIVING' and settings.planning_road_source=='LIVE','Refresh must use the ordinary-driving live adapter')
        require(not expected or settings.driving_live_calls_enabled,'Actual ordinary-driving calls are disabled')
        require(not expected or settings.amap_web_service_key and settings.amap_web_service_key.get_secret_value(),'AMap Web service key is missing')
        require(settings.amap_rate_limit_path.resolve() not in {Path(case['paths']['database']).resolve(),original_graph,
                    Path(case['paths']['base_graph']).resolve(),Path(case['paths']['extension_graph']).resolve()},
                'Shared rate limiter must not write a business or frozen graph database')
        target=application_run_output(case_file,study_file,application_report_file,case,output);target.mkdir(mode=0o700);owns_target=True
        network_graph=backup_graph(original_graph,target/'network-roads.db')
        cache=PrivateCache(target/'private-cache.db')
        private_settings=settings.model_copy(update={'amap_reference_graph_path':network_graph,
            'database_url':SecretStr('sqlite:///'+str(cache.path))})
        wall_deadline=min(started_monotonic+budget_seconds,time.monotonic()+deadline-30-time.time())
        def control():
            if time.monotonic()>=wall_deadline:raise StopSearch('WALL_BUDGET')
        network_started_at=int(time.time())
        road=DrivingRoad(private_settings,None,cache.get,cache.put,transport=transport,
                         limiter=limiter,force_refresh=True,allow_historical=False,
                         production_planning=True,wall_deadline=wall_deadline,network_control=control)
        if road.graph is None:road.graph=ReferenceGraph(network_graph)  # Explicit private graph in isolated tests too.
        require(Path(road.graph.path).resolve()==network_graph.resolve(),'Refresh adapter escaped its private graph')
        try:
            network_result=recalculate(body,draft,road,control)
            private_json(target/'network-result.json',network_result)
            network_stats=road.stats();freshened=set(road._freshened_keys)
        finally:
            network_stats=road.stats();freshened=set(road._freshened_keys);road.close();road=None
            network_finished_at=int(time.time())
        require(network_result['fees_complete'] and not network_result['unassigned'] and
                all(i['code']=='DRIVING_REFERENCE' for i in network_result['check']['issues']),
                'Fresh provider roads made the fixed allocation infeasible or unpriced')
        # The frozen ReferenceGraph writer commits in a connection context but
        # does not explicitly close that local connection. Its returned frames
        # retain no writer references; collect those cycles before SQLite needs
        # the exclusive lock to leave WAL mode in this private graph only.
        gc.collect()
        consolidate_private_graph(network_graph)
        sources=audit_refreshed_sources(network_graph,expected,cache,freshened,network_started_at,network_finished_at)
        frozen_graph=backup_graph(network_graph,target/'frozen-fresh-roads.db')
        with closing(sqlite3.connect(frozen_graph)) as db:
            meta={'scope':SCOPE,'input_hash':content_hash(body),'parent_graph_hash':case['private_graph_hash'],'snapshot_id':case['main_guard']['snapshot_id'],
                  'frozen_at':max(int(time.time()),network_finished_at,*[s['received_at'] for s in sources]),
                  'network_started_at':network_started_at,'network_finished_at':network_finished_at,
                  'actual_external_calls':network_stats['calls'],'source_business_database_written':False}
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,encode(v)) for k,v in meta.items()]);db.commit()
        offline=ApplicationFrozenRoad(frozen_graph)
        try:
            result=recalculate(body,draft,offline);proof=verified_application_result(data,result,offline)
            require(provider_keys(result)==expected,'Offline selected provider coverage changed')
            require(proof['road_evidence']['historical_reference_segments']==0,'Final selected sources are no longer fresh')
            require(result['total_fee']==network_result['total_fee'],'Offline final quote differs from actual refreshed roads')
            fresh_graph_hash=offline.graph_hash
            require(graph_digest(offline.connection)==fresh_graph_hash,'Fresh graph changed during offline proof')
        finally:offline.close()
        fresh_graph_sha=file_sha(frozen_graph)
        require(file_sha(original_graph)==original_graph_sha,'Original application graph was modified')
        for path,sha in files.items():require(file_sha(path)==sha,'An input artifact changed during refresh: '+path)
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen mathematical source gate changed')
        require(file_sha(helper)==case['verification_script_sha256'] and file_sha(script_file)==script_sha,'Refresh verification scripts changed')
        try:
            after=main_guard(case['paths']['database'],body,correction);eligible=after['guard_token']==guard['guard_token']
        except ApplicationGuardError as exc:after={'guard_error':str(exc)};eligible=False
        previous=guard.get('head_fee_summary')
        if previous:
            compare_manual(result,previous)
            result['comparison'].update(previous_input_hash=guard['head_input_hash'],current_input_hash=content_hash(body),
                input_changed=guard['head_input_hash']!=content_hash(body),source_head_id=guard['head_id'],scope=SCOPE)
        delta=Decimal(result['total_fee'])-Decimal(old_repriced['total_fee'])
        fee_bridge={'corrected_frozen_fee':old_repriced['total_fee'],'fresh_fixed_order_fee':result['total_fee'],
                    'fresh_reference_delta':str(delta),'current_head_fee':guard.get('head_total_fee'),
                    'delta_from_current_head':str(Decimal(result['total_fee'])-Decimal(guard['head_total_fee'])) if guard.get('head_total_fee') is not None else None,
                    'fixed_vehicle_order_common_choices_unchanged':True,'prior_application_attribution':application.get('attribution'),
                    'prior_cross_run_merge':{key:application[key] for key in ['before_current_v3_fee','best_repriced_whole_fee',
                        'merged_fee','delta_from_current_v3'] if key in application}}
        if guard.get('head_total_fee') is not None:
            prior_delta=Decimal(old_repriced['total_fee'])-Decimal(guard['head_total_fee'])
            fee_bridge['corrected_frozen_delta_from_current_head']=str(prior_delta)
            require(prior_delta+delta==Decimal(fee_bridge['delta_from_current_head']),'Fresh fee bridge does not reconcile')
        origin=stripped_origin_algorithm(old_result.get('algorithm',{}));elapsed=round(time.monotonic()-started_monotonic,4)
        result['algorithm']={'mode':old_result.get('algorithm',{}).get('mode','MANUAL'),'stop_reason':'SELECTED_ROADS_REFRESHED',
            'elapsed_seconds':elapsed,'budget_seconds':budget_seconds,'distinct_route_count':len(result['routes']),
            'road_stats':network_stats,'optimality_proven':False,'global_225_task_optimality_proven':False,
            'candidate_pool_optimal':False,'cost_candidate_pool_optimal':False,'mip_gap':None,'cost_mip_gap':None,
            'selection_scope':SCOPE,'origin_algorithm_metadata':origin,'origin_algorithm_graph_hash':case['private_graph_hash'],
            'old_graph_certificates_cleared':True,'measured_graph_hash':fresh_graph_hash,'final_fixed_order_refresh':fee_bridge,
            'fresh_reference':{'parent_private_graph_hash':case['private_graph_hash'],'fresh_graph_hash':fresh_graph_hash,
                'actual_external_calls':network_stats['calls'],'network_started_at':network_started_at,
                'network_finished_at':network_finished_at,'offline_proof_external_calls':0}}
        if origin.get('correction_application'):result['algorithm']['correction_application']=origin['correction_application']
        report={'schema_version':1,'status':'READY' if eligible else 'MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND',
            'scope':SCOPE,'eligible_for_guarded_append':eligible,'source_business_database_written':False,
            'external_calls':0,'offline_proof_external_calls':0,'actual_external_calls':network_stats['calls'],
            'network_stats':network_stats,'network_started_at':network_started_at,'network_finished_at':network_finished_at,
            'freshened_provider_keys':sorted(freshened),'selected_provider_keys':sorted(expected),'selected_provider_sources':sources,
            'case_file':str(Path(case_file).resolve()),'case_hash':content_hash(case),
            'application_report_file':str(Path(application_report_file).resolve()),'application_report_hash':content_hash(application),
            'source_application_scope':application['scope'],
            'study_summary_file':str(Path(study_file).resolve()),'study_summary_hash':content_hash(study),
            'source_ready_file':case['source_ready_file'],'source_ready_hash':content_hash(ready),'source_hashes':ready['source_hashes'],
            'source_hashes_unchanged':True,'verification_script_sha256':case['verification_script_sha256'],
            'refresh_script_file':str(script_file),'refresh_script_sha256':script_sha,
            'original_input_hash':content_hash(original),'corrected_input_hash':content_hash(body),
            'original_private_graph_file':str(original_graph),'original_private_graph_hash':case['private_graph_hash'],
            'original_private_graph_sha256':original_graph_sha,'parent_private_graph_hash':case['private_graph_hash'],
            'network_graph_file':str(network_graph),'fresh_graph_file':str(frozen_graph),'fresh_graph_hash':fresh_graph_hash,
            'fresh_graph_sha256':fresh_graph_sha,'main_guard_before':guard,'main_guard_after':after,'guard_token':guard['guard_token'],
            'proof':proof,'original_application_proof':old_proof,'task_count':len(data['tasks']),
            'pieces':sum(t['pieces'] for t in data['tasks'].values()),'fee_bridge':fee_bridge,
            'global_225_task_optimality_proven':False,'global_corrected_input_optimality_proven':False,
            'origin_files_sha256':files,'result_file':str(target/'fresh-result.json'),'result_hash':content_hash(result),
            'run_output':str(target),'elapsed_seconds':elapsed}
        require(time.time()<deadline,'Fresh proof exceeded the authorized deadline')
        private_json(target/'fresh-result.json',result);private_json(target/'refresh-report.json',report)
        return report
    except Exception as exc:
        if road is not None:
            network_stats=road.stats();road.close()
        if owns_target and target is not None and target.is_dir():
            private_json(target/'refresh-report.json',{'schema_version':1,'status':'FAILED','scope':SCOPE,
                'eligible_for_guarded_append':False,'source_business_database_written':False,
                'failure_code':getattr(exc,'code',type(exc).__name__),'failure_type':type(exc).__name__,
                'actual_external_calls':network_stats.get('calls',0),'network_stats':network_stats,
                'network_started_at':network_started_at,'network_finished_at':network_finished_at})
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--study-summary',required=True)
    parser.add_argument('--application-report',required=True);parser.add_argument('--run-output',default='selected-roads-refreshed')
    parser.add_argument('--budget-seconds',type=int,default=600);parser.add_argument('--execute-live',action='store_true')
    args=parser.parse_args()
    report=refresh_application(args.case,args.study_summary,args.application_report,output=args.run_output,
                               budget_seconds=args.budget_seconds,execute_live=args.execute_live)
    print(json.dumps({k:report[k] for k in ['status','scope','task_count','pieces','actual_external_calls','result_file','fresh_graph_file']},ensure_ascii=False))


if __name__=='__main__':main()
