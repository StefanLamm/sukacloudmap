"""Optional real-loopback API acceptance on disposable business/road copies.

Without --execute-private this command only describes its arguments. It never
modifies main, formal results or the prepared graph. Run only after all timed
experiments, with enough deadline reserve for later fresh-road verification.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import multiprocessing
from pathlib import Path
import secrets
import socket
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from sqlalchemy import delete,func,select
from app.config import Settings
from app.database import make_engine,session_factory
from app.models import Account,InputSnapshot,PlanningJob,RoadCache
from app.plan_service import append_version,head,verified_body
from app.planning_inputs import content_hash
from app.routing_engine import check_result,normalize,recalculate
from app.security import hash_password
from scripts.append_verified_application import (_tracked_file,_tracked_json,allocation,
    verify_origin_report,validate_guard_token)
from scripts.backup_sqlite import backup
from scripts.benchmark_algorithms import independent_audit,private_json,read_only,graph_digest
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    application_run_output,main_guard,readonly_session,require,verify_source_ready,verified_application_result)
from scripts.refresh_application_roads import backup_graph,fixed_draft

PROFILES=['HYBRID','ALNS','V3_REFERENCE','GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH']
SCOPE='PRIVATE_STANDARD_API_WARM_INCUMBENT_ACCEPTANCE_NOT_FORMAL_RANKING'
PRIVATE_KEY_PLACEHOLDER='private-no-network-placeholder'
PROFILE_MODES={'HYBRID':'HYBRID_COST_PORTFOLIO','ALNS':'ADAPTIVE_LARGE_NEIGHBORHOOD',
    'V3_REFERENCE':'MULTI_START_JOINT_FLEET',**{name:'ORTOOLS_MEASURED_HYBRID'
        for name in ['GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH']}}


def private_settings(database,graph,folder,profile,multiplier,port):
    return Settings(_env_file=None,environment='development',database_url='sqlite:///'+str(database),
        public_origin=f'http://127.0.0.1:{port}',allowed_hosts=['127.0.0.1'],secure_cookie=False,
        cookie_namespace='worker_'+uuid.uuid4().hex[:16],planning_road_source='LIVE',planning_route_mode='DRIVING',
        planning_optimizer_profile=profile,planning_common_penalty_multiplier=multiplier,
        truck_live_calls_enabled=False,driving_live_calls_enabled=False,truck_job_max_calls=0,driving_job_max_calls=0,
        amap_web_service_key=PRIVATE_KEY_PLACEHOLDER,amap_js_key=None,amap_security_js_code=None,
        amap_rate_limit_path=folder/'private-rate-limit.db',amap_reference_graph_path=graph,
        map_auto_geocode_enabled=False,map_provider='OPEN')


def serve_private(payload,listener):
    """Separate owned child: native workers cannot survive the final cleanup."""
    import uvicorn
    from app.main import create_app
    settings=Settings(_env_file=None,**payload)
    server=uvicorn.Server(uvicorn.Config(create_app(settings),log_level='warning',access_log=False,
        lifespan='on',timeout_graceful_shutdown=20))
    server.run(sockets=[listener])


def server_payload(settings):
    payload=settings.model_dump(mode='json')
    # SecretStr deliberately masks JSON serialization; this value is only the
    # disposable local database path, never an account/provider credential.
    payload['database_url']=settings.database_url.get_secret_value()
    require(settings.amap_web_service_key is not None
        and settings.amap_web_service_key.get_secret_value()==PRIVATE_KEY_PLACEHOLDER
        and not settings.truck_live_calls_enabled and not settings.driving_live_calls_enabled
        and settings.truck_job_max_calls==settings.driving_job_max_calls==0
        and payload['amap_js_key'] is None and payload['amap_security_js_code'] is None,
            'Private child must use only the public invalid placeholder, disabled live calls and zero-call limits')
    # preflight requires a truthy key even for cached roads; this public invalid
    # value is never a credential and cannot be used with a live-enabled child.
    payload['amap_web_service_key']=PRIVATE_KEY_PLACEHOLDER
    return payload


def verify_search_activity(result,profile,multiplier,provider_edges):
    algorithm=result['algorithm'];stats=algorithm.get('road_stats',{})
    require(algorithm.get('mode')==PROFILE_MODES[profile],'Actual API ran another optimizer profile')
    require(algorithm.get('stop_reason') not in {'NO_AVAILABLE_ROAD_EVIDENCE','MATRIX_UNAVAILABLE','SOLVER_UNAVAILABLE',
        'SOLVER_ERROR','NO_DEDICATED_CANDIDATES'},'Actual API returned a no-road or unavailable-solver fallback')
    require(type(algorithm.get('candidate_count')) is int and algorithm['candidate_count']>0
        and type(algorithm.get('distinct_route_count')) is int and algorithm['distinct_route_count']>0,
            'Actual API did not evaluate measured route candidates')
    require(type(stats.get('reference_graph_edges')) is int and stats['reference_graph_edges']==provider_edges>0,
            'Actual API did not load the complete private measured graph')
    diagnostics=[]
    if profile=='HYBRID':diagnostics=[phase.get('details',{}) for phase in algorithm.get('methods',[])]
    elif profile in {'GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH'}:
        require(algorithm.get('strategy')==profile and Decimal(algorithm['surrogate_common_penalty_multiplier'])==Decimal(str(multiplier)),
                'Actual native strategy or candidate penalty differs from the selected settings')
        diagnostics=[algorithm.get('routing_diagnostics',{})]
    measured=[row for row in diagnostics if row.get('measured_edges',0)>0 and row.get('native_slices',0)>0]
    if profile=='HYBRID' or profile in {'GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH'}:
        require(bool(measured),'Selected native optimizer did not run on a measured matrix')
    return {'profile_mode_verified':True,'candidate_count':algorithm['candidate_count'],
        'distinct_route_count':algorithm['distinct_route_count'],'loaded_provider_edges':stats['reference_graph_edges'],
        'measured_native_phases':len(measured)}


def run_loopback(settings,listener,username,password,snapshot,initial,budget,seed,deadline):
    import httpx
    # No account credential is passed to the child or written to a settings file.
    payload=server_payload(settings)
    process=multiprocessing.get_context('spawn').Process(target=serve_private,args=(payload,listener))
    job_id=None;requests=0;started=False;job_running=False
    try:
        process.start();started=True;listener.close()
        with httpx.Client(base_url=settings.public_origin,timeout=10,trust_env=False,
                          headers={'origin':settings.public_origin}) as client:
            def request(method,path,**kwargs):
                nonlocal requests
                requests+=1;reply=client.request(method,path,**kwargs);reply.raise_for_status();return reply.json()
            try:
                startup=time.monotonic()+30
                while True:
                    try:
                        if request('GET','/healthz').get('status')=='ok':break
                    except httpx.HTTPError:pass
                    require(process.is_alive() and time.monotonic()<startup,'Private API did not become ready')
                    time.sleep(.2)
                login=request('POST','/api/auth/login',json={'username':username,'password':password})
                client.headers['x-csrf-token']=login['csrf_token']
                submitted=request('POST','/api/planning/jobs',json={'snapshot_id':snapshot['id'],'group_id':snapshot['group_id'],
                    'expected_head':initial['version'],'kind':'OPTIMIZE','parent_id':initial['id'],'budget_seconds':budget,
                    'seed':seed,'call_limit':0,'refresh_roads':False,'note':'私有副本标准API验收；非正式算法排名'})
                job_id=submitted['id'];job_running=True;until=min(deadline-30,time.time()+budget+120)
                while True:
                    job=request('GET','/api/planning/jobs/'+job_id)
                    if job['status'] not in {'QUEUED','RUNNING'}:job_running=False;break
                    require(process.is_alive() and time.time()<until,'Private API worker exceeded its bounded acceptance window')
                    time.sleep(.5)
                require(job['status']=='COMPLETED' and job.get('plan_id'),'Private API job did not complete with a saved result')
                saved=request('GET','/api/planning/plans/'+job['plan_id'])
                return saved,job,requests
            finally:
                if job_id and job_running and process.is_alive():
                    try:request('POST','/api/planning/jobs/'+job_id+'/cancel')
                    except Exception:pass
    finally:
        listener.close()
        if started:
            if process.is_alive():process.terminate()
            process.join(timeout=45)
            if process.is_alive():process.kill();process.join(timeout=5)
            require(not process.is_alive(),'Owned private API/worker did not stop')


def verify_worker(case_file,study_file,application_report_file,*,profile='HYBRID',multiplier=1,
                  output='private-api-worker-check',budget=300,seed=0):
    started=time.monotonic();stamps={};hashes={}
    def read(path):return _tracked_json(path,stamps,hashes)
    case=read(case_file);study=read(study_file);application=read(application_report_file)
    body=read(Path(case_file).resolve().parent/'input.json');original=read(Path(case_file).resolve().parent/'original-input.json')
    correction=read(case['paths']['correction']);candidate=read(application['result_file'])
    ready=verify_source_ready(case['source_ready_file'])
    require(case.get('status')=='PREPARED' and ready['source_hashes']==case['source_hashes'],'Prepared source gate changed')
    require(content_hash(read(case['source_ready_file']))==case['source_ready_hash'],'Prepared source-ready artifact changed')
    require(content_hash(body)==case['transition']['corrected_input_hash'] and content_hash(original)==case['transition']['original_input_hash'],
            'Prepared inputs changed')
    require(content_hash(candidate)==application['result_hash'],'Candidate result changed')
    verify_origin_report(application,case,study,original,body,read,stamps,hashes,study_file=study_file)
    helper=ROOT/'scripts/prepare_corrected_application.py'
    _tracked_file(helper,stamps,hashes,case['verification_script_sha256'])
    for relative in ['scripts/verify_application_worker.py','scripts/append_verified_application.py',
                     'scripts/refresh_application_roads.py','scripts/backup_sqlite.py']:
        _tracked_file(ROOT/relative,stamps,hashes)
    for relative,sha in ready['source_hashes'].items():_tracked_file(ROOT/relative,stamps,hashes,sha)
    deadline=study.get('deadline_epoch')
    require(type(budget) is int and 1<=budget<=24000 and type(seed) is int and seed>=0,'Invalid API budget or seed')
    require(type(deadline) is int and time.time()+budget+60<deadline,'Private API acceptance lacks deadline reserve')
    source=Path(case['paths']['database']).resolve();source_graph=Path(case['private_graph']).resolve()
    guard=main_guard(source,body,correction);validate_guard_token(guard)
    require(guard==case['main_guard'],'Main state changed since preparation')
    with readonly_session(source) as db:
        require(db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.status.in_(['QUEUED','RUNNING'])))==0,
                'Source main has active planning jobs')
    _tracked_file(source_graph,stamps,hashes)
    source_graph_sha=hashes[str(source_graph)]
    road=ApplicationFrozenRoad(source_graph)
    try:
        require(road.graph_hash==case['private_graph_hash'],'Prepared source graph changed')
        expected_provider_edges=len(road.metrics)
        verified_application_result(normalize(body),candidate,road)
    finally:road.close()
    target=application_run_output(case_file,study_file,application_report_file,case,output)
    require(not source.is_relative_to(target) and not source_graph.is_relative_to(target),'Private output would contain a source')
    target.mkdir(mode=0o700)
    try:
        private_db=target/'application.db';private_graph=target/'reference-graph.db'
        require(private_db not in {source,source_graph} and private_graph not in {source,source_graph},'Private clone overlaps a source')
        backup_report=backup(source,private_db);backup_graph(source_graph,private_graph)
        with closing(read_only(private_graph)) as db:
            require(graph_digest(db)==case['private_graph_hash'],'Private graph clone differs from source')
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
        settings=private_settings(private_db,private_graph,target,profile,multiplier,listener.getsockname()[1])
        require(Path(settings.amap_reference_graph_path).resolve()==private_graph and not settings.road_calls_enabled
            and settings.amap_web_service_key.get_secret_value()==PRIVATE_KEY_PLACEHOLDER,'Private settings escaped the offline boundary')
        username='qa_worker_'+secrets.token_hex(8);password='A9!'+secrets.token_urlsafe(36)
        engine=make_engine(settings)
        try:
            with session_factory(engine)() as db:
                snapshot=db.get(InputSnapshot,guard['snapshot_id'])
                require(snapshot is not None and verified_body(snapshot)==body,'Private backup snapshot differs from prepared input')
                db.execute(delete(RoadCache))  # Only the disposable copy; old SQL cache must not override the selected graph.
                actor=Account(username=username,display_name='私有API验收管理员',password_hash=hash_password(password),is_admin=True)
                db.add(actor);db.flush()
                previous=head(db,snapshot.delivery_date,snapshot.group_id)
                initial=append_version(db,snapshot,deepcopy(candidate),actor.id,'私有API验收',parent=previous,status='DRAFT',note=SCOPE)
                initial_meta={'id':initial.id,'version':initial.version}
                snapshot_meta={'id':snapshot.id,'group_id':snapshot.group_id};db.commit()
        finally:engine.dispose()
        try:saved,job,request_count=run_loopback(settings,listener,username,password,snapshot_meta,initial_meta,budget,seed,deadline)
        finally:listener.close();password=None
        with closing(read_only(private_graph)) as db:
            require(graph_digest(db)==case['private_graph_hash'],'Offline API unexpectedly changed provider road metrics')
        result=saved['result'];data=normalize(body)
        search_activity=verify_search_activity(result,profile,multiplier,expected_provider_edges)
        require(saved['input_hash']==content_hash(body) and saved['input']==body,'Actual API used another input')
        require(saved['snapshot_id']==guard['snapshot_id'] and saved['parent_id']==initial_meta['id'],'Actual API lost the warm-start lineage')
        require(result['fees_complete'] and not result['unassigned'] and Decimal(result['total_fee'])<=Decimal(candidate['total_fee']),
                'Actual API regressed against its complete warm incumbent')
        require(result.get('algorithm',{}).get('road_stats',{}).get('calls')==0,'Actual API unexpectedly called a road provider')
        actual_check=check_result(data,result)
        require(all(issue['code']=='DRIVING_REFERENCE' for issue in actual_check['issues']),'Actual API contains a blocking issue')
        require(result.get('check')==actual_check,'Actual API stored check differs from independent recomputation')
        if result['routes']:
            require(result.get('road_source')=='DRIVING' and result.get('confirmable') is False,'Actual API promoted ordinary-driving evidence')
        road=ApplicationFrozenRoad(source_graph)
        try:
            actual_audit=independent_audit(data,result,road)
            require(actual_audit['valid'] and actual_audit['unassigned_tasks']==0,'Actual API allocation failed independent accounting')
            replay=recalculate(body,fixed_draft(result),road)
            proof=verified_application_result(data,replay,road)
            require(allocation(result)==allocation(replay) and result['total_fee']==replay['total_fee'],'Actual API fee differs on original prepared graph')
        finally:road.close()
        require(len(data['tasks'])==225 and sum(row['pieces'] for row in data['tasks'].values())==4378,'Actual API coverage is not 225/4378')
        require(main_guard(source,body,correction)==guard,'Source main changed during private API acceptance')
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen sources changed during acceptance')
        for path,stamp in stamps.items():require(module_stamp(path)==stamp,'Source artifact changed during private API acceptance: '+path)
        private_json(target/'actual-api-result.json',result);private_json(target/'offline-replayed-result.json',replay)
        report={'schema_version':1,'status':'VALID_PRIVATE_API_ACCEPTANCE','scope':SCOPE,'formal_ranking_unchanged':True,
            'source_business_database_written':False,'private_database_written':True,'private_dummy_account_created':True,
            'all_owned_server_workers_stopped':True,'external_provider_calls':0,'loopback_http_requests':request_count,
            'profile':profile,'common_penalty_multiplier':multiplier,'actual_algorithm_mode':result['algorithm']['mode'],
            'search_activity':search_activity,'private_environment':settings.environment,
            'budget_seconds':budget,'seed':seed,'case_file':str(Path(case_file).resolve()),'case_hash':content_hash(case),
            'application_report_file':str(Path(application_report_file).resolve()),'application_report_hash':content_hash(application),
            'input_hash':content_hash(body),'graph_hash':case['private_graph_hash'],'source_graph_sha256':source_graph_sha,
            'task_count':225,'pieces':4378,'unassigned_tasks':0,'initial_fee':candidate['total_fee'],'actual_api_fee':result['total_fee'],
            'delta_from_incumbent':str(Decimal(result['total_fee'])-Decimal(candidate['total_fee'])),'actual_api_check':actual_check,
            'actual_api_audit':actual_audit,'offline_replay_proof':proof,'main_guard_before':guard,'main_guard_after':guard,
            'private_backup':backup_report,'job_id':job['id'],'private_plan_id':saved['id'],'cookie_namespace':settings.cookie_namespace,
            'result_file':str(target/'actual-api-result.json'),'result_hash':content_hash(result),'extra_result':str(target/'actual-api-result.json'),
            'result_file_sha256':file_digest(target/'actual-api-result.json'),'effective_settings_hash':content_hash(server_payload(settings)),
            'source_files_sha256':hashes,'source_hashes':ready['source_hashes'],'source_hashes_unchanged':True,
            'script_sha256':hashes[str(Path(__file__).resolve())],'elapsed_seconds':round(time.monotonic()-started,4)}
        private_json(target/'worker-report.json',report)
        return report
    except BaseException as exc:
        private_json(target/'worker-failure.json',{'status':'FAILED_PRIVATE_ACCEPTANCE','error_type':type(exc).__name__,
            'source_business_database_written':False,'scope':SCOPE})
        raise


def module_stamp(path):
    from scripts.append_verified_application import file_stamp
    return file_stamp(path)


def file_digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--study-summary',required=True)
    parser.add_argument('--application-report',required=True);parser.add_argument('--profile',choices=PROFILES,default='HYBRID')
    parser.add_argument('--multiplier',type=float,default=1);parser.add_argument('--budget-seconds',type=int,default=300)
    parser.add_argument('--seed',type=int,default=0);parser.add_argument('--run-output',default='private-api-worker-check')
    parser.add_argument('--execute-private',action='store_true')
    args=parser.parse_args(argv)
    if not args.execute_private:
        print(json.dumps({'status':'NOT_EXECUTED','requires':'--execute-private','profile':args.profile,
            'budget_seconds':args.budget_seconds,'scope':SCOPE},ensure_ascii=False));return
    result=verify_worker(args.case,args.study_summary,args.application_report,profile=args.profile,multiplier=args.multiplier,
        output=args.run_output,budget=args.budget_seconds,seed=args.seed)
    print(json.dumps({key:result[key] for key in ['status','scope','profile','actual_api_fee','extra_result','unassigned_tasks','external_provider_calls']},ensure_ascii=False))


if __name__=='__main__':main()
