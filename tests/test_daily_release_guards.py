"""Publisher preflight and partial-stop recovery against isolated temporary files."""
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from scripts import release_daily_deep as publisher


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def release_environment(tmp_path,monkeypatch):
    root=tmp_path/'application';root.mkdir()
    lab=root/'data/lab';lab.mkdir(parents=True)
    stage=tmp_path/'stage';(stage/'assets').mkdir(parents=True)
    (root/'frontend/dist').mkdir(parents=True)
    (root/'frontend/dist/index.html').write_text('existing application')
    monkeypatch.setattr(publisher,'ROOT',root)
    for path in publisher.VALIDATED_SOURCE_PATHS:
        source=root/path;source.parent.mkdir(parents=True,exist_ok=True);source.write_text(path)
    gate=lab/'source-ready-for-validation.json'
    write_json(gate,{'status':'FEATURE_SOURCE_FROZEN_FOR_FULL_VALIDATION','default_budget_seconds':300,
                     'source_hashes':{path:publisher.digest(root/path) for path in publisher.VALIDATED_SOURCE_PATHS}})
    acceptance=lab/'real-new-group-300s/acceptance.json'
    write_json(acceptance,{'status':'PASS','kind':'NEW_GROUP_REAL_HTTP_DAILY_DEEP_ACCEPTANCE',
                          'budget_seconds':300,'source_business_database_written':False,
                          'prior_source_group_plan_inherited':False,'live_provider_authorized':True,
                          'final_validation':{'mode':'FRESH_DRIVING_VALIDATED','fresh':True}})
    (stage/'assets/index-C2V6c3xi.js').write_text('verified frontend')
    (stage/'index.html').write_text('<script src="/assets/index-C2V6c3xi.js"></script>')
    manifest=stage/'release-manifest.json'
    write_json(manifest,{'main_asset':'index-C2V6c3xi.js','assets':{
        path:{'sha256':publisher.digest(stage/path)}
        for path in ('assets/index-C2V6c3xi.js','index.html')}})
    sources={name:str(root/(name+'.db')) for name in ('main','trial','history','driving')}
    for path in sources.values():
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE planning_jobs (id TEXT PRIMARY KEY,status TEXT)')
            db.execute('INSERT INTO planning_jobs VALUES(?,?)',('complete','COMPLETED'))
            db.commit()
    write_json(lab/'business-sources.json',sources)
    backend_log=tmp_path/'backend-clean.txt';backend_log.write_text('2321 passed in 300s\n')
    bindings=lab/'release-bindings.json'
    artifacts={'source_gate':gate,'frontend_manifest':manifest,'real_acceptance':acceptance,
               'backend_clean_log':backend_log}

    def bind():
        write_json(bindings,{'schema_version':1,'artifacts':{
            name:{'path':str(path),'sha256':publisher.digest(path)} for name,path in artifacts.items()}})
        return publisher.digest(bindings)

    def forbidden(*args,**kwargs):
        pytest.fail('Preflight/recovery test must not execute a process, backup, or HTTP request')

    monkeypatch.setattr(publisher.subprocess,'run',forbidden)
    monkeypatch.setattr(publisher,'backup',forbidden)
    monkeypatch.setattr(httpx,'Client',forbidden)
    return SimpleNamespace(root=root,lab=lab,stage=stage,backup=lab/'private-backup',sources=sources,
                           gate=gate,acceptance=acceptance,manifest=manifest,backend_log=backend_log,
                           bindings=bindings,bind=bind,binding_sha256=bind())


def validate(env,**kwargs):
    return publisher.validate_release_inputs(env.stage,kwargs.get('backup',env.backup),env.lab,
                                             env.bindings,kwargs.get('sha',env.binding_sha256))


def publish(env):
    publisher.release(env.stage,env.backup,env.lab,env.bindings,env.binding_sha256)


def test_complete_trusted_fixture_passes_without_stopping_or_creating_backup(release_environment):
    env=release_environment
    sources,manifest=validate(env)
    assert sources==env.sources and manifest['main_asset']=='index-C2V6c3xi.js'
    assert not env.backup.exists() and not (env.lab/'release-execution.json').exists()


def test_changed_binding_is_rejected_before_stop(release_environment):
    env=release_environment
    env.bindings.write_text(env.bindings.read_text()+'\n')
    with pytest.raises(ValueError,match='Trusted release bindings changed'):publish(env)
    assert not (env.lab/'release-execution.json').exists()


@pytest.mark.parametrize('artifact',['gate','manifest','acceptance','backend_log'])
def test_each_changed_validation_artifact_is_rejected_before_stop(release_environment,artifact):
    env=release_environment
    path=getattr(env,artifact);path.write_text(path.read_text()+'\n')
    with pytest.raises(ValueError,match='Validated artifact changed'):publish(env)
    assert not (env.lab/'release-execution.json').exists()


def test_self_resigned_stage_manifest_cannot_replace_tested_stage(release_environment):
    env=release_environment
    path=env.stage/'assets/index-C2V6c3xi.js';path.write_text('untested replacement')
    manifest=json.loads(env.manifest.read_text());manifest['assets']['assets/index-C2V6c3xi.js']['sha256']=publisher.digest(path)
    write_json(env.manifest,manifest)
    with pytest.raises(ValueError,match='Validated artifact changed: frontend_manifest'):publish(env)


@pytest.mark.parametrize('change',['missing','duplicate','resolved_alias'])
def test_four_source_database_binding_must_be_complete_and_distinct_before_stop(release_environment,change):
    env=release_environment;sources=dict(env.sources)
    if change=='missing':sources.pop('driving')
    elif change=='duplicate':sources['driving']=sources['history']
    else:sources['driving']=str(env.root/'unused/../history.db')
    write_json(env.lab/'business-sources.json',sources)
    with pytest.raises(ValueError,match='Exactly four separate source databases'):publish(env)
    assert not (env.lab/'release-execution.json').exists()


@pytest.mark.parametrize('change',['outside','nested','existing','symlink'])
def test_backup_must_be_new_private_direct_child(release_environment,change):
    env=release_environment
    if change=='outside':env.backup=env.root/'backup'
    elif change=='nested':env.backup=env.lab/'nested/backup'
    elif change=='existing':env.backup.mkdir()
    else:env.backup.symlink_to(env.lab/'not-created',target_is_directory=True)
    with pytest.raises(ValueError,match='new private backup child'):publish(env)
    assert not (env.lab/'release-execution.json').exists()


@pytest.mark.parametrize('field,value',[
    ('kind','HISTORICAL_PLAN_REPLAY'),('budget_seconds',15),('source_business_database_written',True),
    ('prior_source_group_plan_inherited',True),('live_provider_authorized',False),
    ('final_validation',{'mode':'OFFLINE_REFERENCE_VALIDATED','fresh':False}),
    ('final_validation',{'mode':'FRESH_DRIVING_VALIDATED','fresh':False}),
])
def test_even_trusted_acceptance_must_have_required_real_new_group_semantics(release_environment,field,value):
    env=release_environment;acceptance=json.loads(env.acceptance.read_text());acceptance[field]=value
    write_json(env.acceptance,acceptance);env.binding_sha256=env.bind()
    with pytest.raises(ValueError,match='Fresh real new-group 300-second acceptance'):publish(env)


def test_missing_source_from_rebound_gate_is_rejected(release_environment):
    env=release_environment;gate=json.loads(env.gate.read_text());gate['source_hashes'].pop('app/route_deep.py')
    write_json(env.gate,gate);env.binding_sha256=env.bind()
    with pytest.raises(ValueError,match='complete ten-source'):publish(env)


def test_changed_validated_source_is_rejected_before_stop(release_environment):
    env=release_environment;(env.root/'app/route_deep.py').write_text('modified after validation')
    with pytest.raises(ValueError,match='Validated application source changed'):publish(env)


def test_active_planning_job_is_rejected_before_stop(release_environment):
    env=release_environment
    with sqlite3.connect(env.sources['trial']) as db:
        db.execute('INSERT INTO planning_jobs VALUES(?,?)',('running','RUNNING'));db.commit()
    with pytest.raises(ValueError,match='planning job is active'):publish(env)
    assert not (env.lab/'release-execution.json').exists()


@pytest.mark.parametrize('stop_outcome',['nonzero','raises'])
def test_partial_stop_failure_still_restarts_all_owned_services(release_environment,monkeypatch,stop_outcome):
    env=release_environment;commands=[]
    before={name:publisher.digest(path) for name,path in env.sources.items()}

    def run(args,**kwargs):
        assert kwargs['cwd']==env.root
        commands.append(args[-2:])
        if args[-2:]==['stop','all']:
            if stop_outcome=='raises':raise OSError('partial stop interrupted')
            return SimpleNamespace(returncode=1,stdout='first service stopped',stderr='second service failed')
        assert args[-2:]==['start','all']
        return SimpleNamespace(returncode=0,stdout='all owned services started',stderr='')

    monkeypatch.setattr(publisher.subprocess,'run',run)
    with pytest.raises((RuntimeError,OSError)):publish(env)
    assert commands==[['stop','all'],['start','all']]
    assert not env.backup.exists()
    assert before=={name:publisher.digest(path) for name,path in env.sources.items()}
    ledger=json.loads((env.lab/'release-execution.json').read_text())
    assert ledger['status']=='RELEASE_NEEDS_FOLLOWUP'
    assert ledger['steps'][-1]['step']=='recovery-start-owned-services'
