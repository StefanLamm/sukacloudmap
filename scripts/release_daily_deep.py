"""Publish tested code/static only; business rows must remain byte-equivalent.

Backups and retained old static are created before restarting the owned apps.
There is deliberately no database restore, planning call, or account operation.
"""
import argparse,hashlib,json,os,re,shutil,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.backup_sqlite import backup
from scripts.verify_unchanged_databases import inventory,difference,write_private

VALIDATED_SOURCE_PATHS=frozenset((
    'app/route_deep.py','app/route_verification.py','app/route_columns.py',
    'app/route_search.py','app/plan_service.py','app/plan_api.py',
    'frontend/src/plan-panel.tsx','frontend/src/plan-metrics.ts',
    'frontend/src/main.tsx','frontend/package.json',
))
VALIDATED_ARTIFACTS=frozenset(('source_gate','frontend_manifest','real_acceptance','backend_clean_log'))


def digest(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def validate_release_inputs(stage,backup_folder,lab,bindings_file,bindings_sha256):
    """Check the independently supplied validation binding before stopping anything."""
    stage,folder,lab=Path(stage).resolve(),Path(backup_folder).resolve(),Path(lab).resolve()
    bindings_file=Path(bindings_file).resolve()
    if not re.fullmatch(r'[0-9a-f]{64}',bindings_sha256) or digest(bindings_file)!=bindings_sha256:
        raise ValueError('Trusted release bindings changed')
    bindings=json.loads(bindings_file.read_text())
    artifacts=bindings.get('artifacts',{})
    if bindings.get('schema_version')!=1 or set(artifacts)!=VALIDATED_ARTIFACTS:
        raise ValueError('Four validated artifact bindings are required')
    fixed_paths={'source_gate':lab/'source-ready-for-validation.json',
                 'frontend_manifest':stage/'release-manifest.json',
                 'real_acceptance':lab/'real-new-group-300s/acceptance.json'}
    for name,item in artifacts.items():
        path=Path(item['path'])
        if not path.is_absolute() or not path.is_file():raise ValueError('Validated artifact file is missing: '+name)
        if name in fixed_paths and path.resolve()!=fixed_paths[name]:raise ValueError('Validated artifact path differs: '+name)
        if not re.fullmatch(r'[0-9a-f]{64}',item['sha256']) or digest(path)!=item['sha256']:
            raise ValueError('Validated artifact changed: '+name)
    sources_file=lab/'business-sources.json'
    raw_sources=json.loads(sources_file.read_text())
    if not isinstance(raw_sources,dict) or set(raw_sources)!={'main','trial','history','driving'}:
        raise ValueError('Exactly four separate source databases are required')
    sources={name:str(Path(path).resolve()) for name,path in raw_sources.items()}
    if len(set(sources.values()))!=4 or any(not Path(path).is_file() for path in sources.values()):
        raise ValueError('Exactly four separate source databases are required')
    # All backups stay inside a new private child; an existing path is never reused.
    if not lab.is_dir() or folder.parent!=lab or folder.exists() or Path(backup_folder).is_symlink():
        raise ValueError('A new private backup child of the validation directory is required')
    if not (ROOT/'frontend/dist').is_dir():raise ValueError('An existing static directory is required')
    source_gate=json.loads(fixed_paths['source_gate'].read_text())
    if (source_gate.get('status')!='FEATURE_SOURCE_FROZEN_FOR_FULL_VALIDATION'
            or source_gate.get('default_budget_seconds')!=300
            or set(source_gate.get('source_hashes',{}))!=VALIDATED_SOURCE_PATHS):
        raise ValueError('The complete ten-source validation gate is required')
    for path,expected in source_gate['source_hashes'].items():
        if digest(ROOT/path)!=expected:raise ValueError('Validated application source changed: '+path)
    acceptance=json.loads(fixed_paths['real_acceptance'].read_text())
    final=acceptance.get('final_validation',{})
    if (acceptance.get('status')!='PASS' or acceptance.get('kind')!='NEW_GROUP_REAL_HTTP_DAILY_DEEP_ACCEPTANCE'
            or acceptance.get('budget_seconds')!=300 or acceptance.get('source_business_database_written') is not False
            or acceptance.get('prior_source_group_plan_inherited') is not False
            or acceptance.get('live_provider_authorized') is not True
            or final.get('mode')!='FRESH_DRIVING_VALIDATED' or final.get('fresh') is not True):
        raise ValueError('Fresh real new-group 300-second acceptance is required')
    manifest=json.loads(fixed_paths['frontend_manifest'].read_text())
    if manifest['main_asset']!='index-C2V6c3xi.js':raise ValueError('Unexpected staged frontend')
    for path,expected in manifest['assets'].items():
        asset=(stage/path).resolve()
        if not asset.is_relative_to(stage) or (stage/path).is_symlink():raise ValueError('Unsafe staged asset path')
        if digest(asset)!=expected['sha256']:raise ValueError('Staged asset changed')
    actual_files={str(path.relative_to(stage)) for path in stage.rglob('*') if path.is_file()}
    if actual_files!=set(manifest['assets'])|{'release-manifest.json'}:
        raise ValueError('Staged files differ from the validated manifest')
    return sources,manifest


def release(stage,backup_folder,lab,bindings_file,bindings_sha256):
    import httpx
    stage=Path(stage).resolve();destination=ROOT/'frontend/dist';folder=Path(backup_folder).resolve();lab=Path(lab).resolve()
    sources,manifest=validate_release_inputs(stage,backup_folder,lab,bindings_file,bindings_sha256)
    sources_file=lab/'business-sources.json'
    for path in sources.values():
        with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT COUNT(*) FROM planning_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]:
                raise ValueError('A user planning job is active; do not interrupt it')
    ledger={'status':'RUNNING','started_at':int(time.time()),'backup':str(folder),'stage':str(stage),
            'bindings':str(Path(bindings_file).resolve()),'bindings_sha256':bindings_sha256,'steps':[]}
    ledger_file=lab/'release-execution.json'
    if ledger_file.exists():raise ValueError('Release execution already exists; inspect it rather than repeating publication')
    def save():
        with ledger_file.open('w') as stream:
            os.fchmod(stream.fileno(),0o600);json.dump(ledger,stream,ensure_ascii=False,indent=2)
    def command(args,label):
        result=subprocess.run(args,cwd=ROOT,text=True,capture_output=True)
        ledger['steps'].append({'step':label,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'finished_at':int(time.time())});save()
        if result.returncode:raise RuntimeError('Release operation failed: '+label)
        print(json.dumps({'step':label,'status':'PASS'}),flush=True)
    save();stop_attempted=False;restarted=False
    manager=[sys.executable,str(ROOT/'scripts/manage-services.py')]
    try:
        stop_attempted=True
        command(manager+['stop','all'],'stop-owned-services')
        folder.mkdir(mode=0o700)
        backups={name:backup(path,folder/(name+'.db')) for name,path in sources.items()}
        write_private(folder/'backups.json',backups)
        env=ROOT/'.env';shutil.copy2(env,folder/'private.env');os.chmod(folder/'private.env',0o600)
        shutil.copytree(destination,folder/'old-static')
        captured={name:inventory(folder/(name+'.db')) for name in sources}
        write_private(folder/'prior-rows.json',{'status':'CAPTURED','sources':sources,'sources_file_sha256':digest(sources_file),'databases':captured})
        ledger['env_before_sha256']=digest(env);ledger['old_asset_files']=[str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()];save()
        print(json.dumps({'step':'private-backups-and-row-inventory','status':'PASS'}),flush=True)
        nextpath=Path(tempfile.mkdtemp(dir=destination.parent,prefix='.daily-deep-static-'))
        shutil.copytree(stage,nextpath,dirs_exist_ok=True)
        os.rename(destination,folder/'old-static-renamed')
        try:os.rename(nextpath,destination)
        except BaseException:
            os.rename(folder/'old-static-renamed',destination);raise
        if digest(env)!=ledger['env_before_sha256']:raise ValueError('Local credentials/config unexpectedly changed')
        command(manager+['start','all'],'start-owned-services');restarted=True
        command(manager+['status','all'],'service-readiness')
        current=json.loads((ROOT/'data/external-test/current.json').read_text());url=current['url']
        if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com',url):raise ValueError('Unexpected public test URL')
        host=url.removeprefix('https://')
        with httpx.Client(timeout=20,trust_env=False) as client:
            for port in (8000,8001,8765,8766):
                if client.get(f'http://127.0.0.1:{port}/healthz').status_code!=200:raise ValueError('Local application unhealthy')
            if client.get('http://127.0.0.1:8770/healthz',headers={'host':host}).status_code!=200:raise ValueError('Public origin unhealthy')
            if client.get('http://127.0.0.1:8770/healthz',headers={'host':'invalid.example'}).status_code!=400:raise ValueError('Public host protection failed')
            for origin in ('http://127.0.0.1:8000',url):
                for path in ('/healthz','/login'):
                    if client.get(origin+path).status_code!=200:raise ValueError('Public/login readiness failed')
                if client.get(origin+'/api/auth/me').status_code!=401:raise ValueError('Unauthenticated API protection failed')
                rejected=client.post(origin+'/api/auth/login',headers={'origin':'https://invalid.example'},json={})
                if rejected.status_code!=403 or rejected.json()['code']!='ORIGIN_REJECTED':raise ValueError('Origin protection failed')
                path='assets/'+manifest['main_asset'];response=client.get(origin+'/'+path)
                if response.status_code!=200 or hashlib.sha256(response.content).hexdigest()!=manifest['assets'][path]['sha256']:raise ValueError('Published frontend differs from verified stage')
        changes={name:difference(captured[name],inventory(path)) for name,path in sources.items()}
        unchanged=all(not any(row[k] for k in ('added','changed','deleted')) for tables in changes.values() for row in tables.values())
        write_private(folder/'post-release-preservation.json',{'status':'PASS' if unchanged else 'FAILED','all_business_rows_identical':unchanged,'databases':changes})
        if not unchanged:raise ValueError('Business rows changed; preserve current data and investigate')
        ledger.update(status='PUBLISHED_PENDING_BROWSER_ACCEPTANCE',finished_at=int(time.time()),actual_public_url=url,
            main_asset=manifest['main_asset'],all_business_rows_identical=True,env_preserved=digest(env)==ledger['env_before_sha256'])
        save();print(json.dumps({k:ledger[k] for k in ('status','actual_public_url','main_asset','all_business_rows_identical')},ensure_ascii=False),flush=True)
    except BaseException as exc:
        ledger.update(status='RELEASE_NEEDS_FOLLOWUP',error_type=type(exc).__name__,error=str(exc));save();raise
    finally:
        if stop_attempted and not restarted:
            try:command(manager+['start','all'],'recovery-start-owned-services')
            except Exception:pass


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',required=True);p.add_argument('--backup',required=True);p.add_argument('--lab',default=str(ROOT/'data/daily-deep-20261113'));p.add_argument('--bindings',required=True);p.add_argument('--bindings-sha256',required=True);p.add_argument('--execute',action='store_true')
    args=p.parse_args()
    if not args.execute:p.error('Explicit --execute is required after full validation')
    release(args.stage,args.backup,Path(args.lab).resolve(),args.bindings,args.bindings_sha256)
