"""Verify a *new group's* daily workflow over HTTP on disposable local copies.

The source database and graph are backup/read-only inputs. Provider keys stay
in the existing local .env; credentials created here exist only in the copy.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import json,os,secrets,socket,sqlite3,subprocess,sys,time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def private_json(path,value):
    with Path(path).open('x') as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False,indent=2)


def serve(folder):
    import uvicorn
    from app.config import Settings
    from app.main import create_app
    specification=json.loads((folder/'server.json').read_text())
    settings=Settings(database_url='sqlite:///'+str(folder/'application.db'),environment='development',
        public_origin=specification['origin'],allowed_hosts=['127.0.0.1'],secure_cookie=False,
        cookie_namespace=specification['namespace'],map_auto_geocode_enabled=False,
        planning_route_mode='DRIVING',planning_road_source='LIVE',
        driving_live_calls_enabled=specification['live'],truck_live_calls_enabled=False,
        driving_job_max_calls=None if specification['live'] else 0,truck_job_max_calls=0,
        amap_reference_graph_path=folder/'reference-graph.db',
        **({} if specification['live'] else {'amap_rate_limit_path':folder/'private-pacing.db'}))
    uvicorn.run(create_app(settings),host='127.0.0.1',port=specification['port'],access_log=False,log_level='warning')


def run(args):
    import httpx
    from sqlalchemy import select
    from app.config import Settings
    from app.database import make_engine,session_factory
    from app.models import Account
    from app.plan_service import verify_deep_result
    from app.planning_inputs import content_hash
    from app.routing_engine import check_result,normalize
    from app.security import hash_password
    from scripts.backup_sqlite import backup
    from scripts.refresh_application_roads import backup_graph
    folder=Path(args.output).resolve();folder.mkdir(mode=0o700,parents=False)
    settings=Settings()
    if args.live:assert settings.amap_web_service_key and settings.planning_route_mode=='DRIVING'
    source=Path(args.source).resolve();assert source.is_file() and source!=folder/'application.db'
    backup(source,folder/'application.db')
    backup_graph(Path(args.graph).resolve(),folder/'reference-graph.db')
    username='deepqa_'+secrets.token_hex(6);password=secrets.token_urlsafe(24)
    private_settings=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(folder/'application.db'))
    engine=make_engine(private_settings)
    with session_factory(engine)() as db:
        db.add(Account(username=username,display_name='日常深度隔离验收',password_hash=hash_password(password),is_admin=True));db.commit()
    engine.dispose()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    private_json(folder/'server.json',{'origin':origin,'port':port,'namespace':'deepqa_'+secrets.token_hex(7),'live':args.live})
    private_json(folder/'credentials.json',{'username':username,'password':password})
    output=folder/'server.log';output.touch(mode=0o600)
    process=None;job_id=None;group_id=None;started=time.time();progress=[];terminal=None
    try:
        with output.open('ab') as log:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve',str(folder)],cwd=ROOT,
                stdout=log,stderr=log,start_new_session=True)
        with httpx.Client(base_url=origin,headers={'origin':origin},timeout=30,trust_env=False) as client:
            until=time.monotonic()+30
            while True:
                try:
                    if client.get('/healthz').status_code==200:break
                except httpx.HTTPError:pass
                if process.poll() is not None or time.monotonic()>=until:raise RuntimeError('Private server did not become healthy')
                time.sleep(.2)
            def request(method,path,**kw):
                response=client.request(method,path,**kw);response.raise_for_status();return response.json()
            login=request('POST','/api/auth/login',json={'username':username,'password':password})
            client.headers['x-csrf-token']=login['csrf_token']
            caps=request('GET','/api/planning/capabilities');assert caps['automatic_workflow']=='DEEP_PORTFOLIO'
            groups=request('GET',f'/api/planning/days/{args.day}/groups')['items']
            source_group=next(g for g in groups if g['name']==args.group)
            source_state=request('GET',f'/api/planning/days/{args.day}',params={'group_id':source_group['id']})
            items=[{'store_id':row['store_id'],'pieces':row['pieces']} for row in source_state['items']]
            assert items
            group=request('POST',f'/api/planning/days/{args.day}/groups',json={'name':'日常深度验收-'+secrets.token_hex(4)})
            group_id=group['id']
            receipt=request('PUT',f'/api/planning/groups/{group_id}/demands',json={'expected_revision':0,'items':items})
            snapshot_id=receipt['snapshot_id']
            job=request('POST','/api/planning/jobs',json={'snapshot_id':snapshot_id,'group_id':group_id,
                'expected_head':0,'kind':'OPTIMIZE','budget_seconds':args.budget,'seed':args.seed})
            job_id=job['id'];deadline=time.time()+args.budget+min(1800,max(120,args.budget*2))+120
            last=None
            while True:
                terminal=request('GET','/api/planning/jobs/'+job_id)
                state=terminal.get('progress')
                marker=json.dumps(state,ensure_ascii=False,sort_keys=True)
                if state and marker!=last:
                    progress.append({'observed_at':int(time.time()),**state});last=marker
                    if len(progress)>1500:progress=progress[:100]+progress[-1400:]
                    if state.get('stage')!=(progress[-2].get('stage') if len(progress)>1 else None):
                        print(json.dumps({'event':'STAGE','progress':state},ensure_ascii=False),flush=True)
                if terminal['status'] not in ('QUEUED','RUNNING'):break
                if process.poll() is not None or time.time()>deadline:raise RuntimeError('Private daily workflow exceeded its bounded wall allowance')
                time.sleep(.75)
            private_json(folder/'job.json',terminal);private_json(folder/'progress.json',progress)
            assert terminal['status']=='COMPLETED' and terminal['plan_id'],terminal
            saved=request('GET','/api/planning/plans/'+terminal['plan_id'])
            private_json(folder/'saved-plan.json',saved)
            result=saved['result'];data=normalize(saved['input']);proof=check_result(data,result)
            assert not result['unassigned'] and result['fees_complete'] and all(i['code']=='DRIVING_REFERENCE' for i in proof['issues'])
            assert result['check']==proof and saved['input_hash']==content_hash(saved['input'])
            algorithm=result['algorithm'];assert algorithm['mode']=='DAILY_DEEP_PORTFOLIO'
            assert algorithm['round_count']>=2 and len(set(algorithm['actual_seeds']))==algorithm['round_count']
            assert any(stage['stage']=='COLUMN_SELECTION' for stage in algorithm['stages'])
            validation=algorithm['final_validation']
            if args.live and result['routes']:
                assert validation['fresh'] and validation['mode']=='FRESH_DRIVING_VALIDATED'
                assert validation['selected_subset_of_collected']
            assert algorithm['optimality_proven'] is False
            # The group and test account must exist only in the private copy.
            with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original:
                assert original.execute('SELECT COUNT(*) FROM demand_groups WHERE id=?',(group_id,)).fetchone()[0]==0
                assert original.execute('SELECT COUNT(*) FROM accounts WHERE username=?',(username,)).fetchone()[0]==0
            record={'status':'PASS','kind':'NEW_GROUP_REAL_HTTP_DAILY_DEEP_ACCEPTANCE','started_at':started,'finished_at':time.time(),
                'source_business_database_written':False,'source_group':args.group,'new_private_group_id':group_id,
                'source_day':args.day,'budget_seconds':args.budget,'job_id':job_id,'plan_id':saved['id'],
                'private_origin':origin,'task_count':proof['task_count'],'pieces':proof['total_pieces'],'total_fee':result['total_fee'],
                'round_count':algorithm['round_count'],'actual_seeds':algorithm['actual_seeds'],'distinct_routes':algorithm['distinct_route_count'],
                'column_selection':[s for s in algorithm['stages'] if s['stage']=='COLUMN_SELECTION'],
                'final_validation':validation,'global_optimality_proven':False,'progress_updates':len(progress),
                'prior_source_group_plan_inherited':False,'live_provider_authorized':args.live}
            private_json(folder/'acceptance.json',record)
            print(json.dumps({k:record[k] for k in ('status','task_count','pieces','total_fee','round_count','actual_seeds','progress_updates')},ensure_ascii=False),flush=True)
    finally:
        if process is not None:
            if process.poll() is None:
                if job_id and (terminal is None or terminal.get('status') in ('QUEUED','RUNNING')):
                    try:
                        with httpx.Client(base_url=origin,timeout=5,trust_env=False) as cancel_client:
                            login=cancel_client.post('/api/auth/login',json={'username':username,'password':password}).json()
                            cancel_client.post('/api/planning/jobs/'+job_id+'/cancel',headers={'origin':origin,'x-csrf-token':login['csrf_token']})
                    except Exception:pass
                process.terminate()
                try:process.wait(timeout=45)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            private_json(folder/'process-cleanup.json',{'owned_server_pid':process.pid,'exit_code':process.returncode,'stopped':True})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--serve',type=Path)
    p.add_argument('--source',default=str(ROOT/'data/suka.db'));p.add_argument('--graph',default=str(ROOT/'data/amap/reference-graph.db'))
    p.add_argument('--day',default='2026-11-12');p.add_argument('--group',default='test2');p.add_argument('--budget',type=int,default=300)
    p.add_argument('--seed',type=int,default=0);p.add_argument('--output');p.add_argument('--live',action='store_true');p.add_argument('--execute-private',action='store_true')
    args=p.parse_args()
    if args.serve:return serve(args.serve.resolve())
    if not args.execute_private or not args.output:p.error('Use --execute-private and a new --output directory to run acceptance')
    if not 1<=args.budget<=24000:p.error('Invalid budget')
    run(args)
if __name__=='__main__':main()
