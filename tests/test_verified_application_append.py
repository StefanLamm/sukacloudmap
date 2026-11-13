"""Tiny isolated database guards; production append is never invoked on main."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import func,select
from sqlalchemy.engine import make_url

from app.models import Account,AuditEvent,InputSnapshot,PlanningJob,PlanVersion,Store,SystemState,now
from app.plan_service import append_version,verified_body
from app.planning_inputs import content_hash
from app.routing_engine import assemble,compare_manual,normalize
import scripts.append_verified_application as module
from scripts.prepare_corrected_application import ApplicationGuardError,main_guard
from test_demand_groups import create_group,manual
from test_stage4 import basis


@pytest.fixture
def append_bundle(basis,app,tmp_path,monkeypatch):
    client,warehouse,quote,store,other,vehicle=basis
    group=create_group(client,'独立追加夹具')
    receipt=manual(client,group,[{'store_id':store['id'],'pieces':3},{'store_id':other['id'],'pieces':4}])
    database=Path(make_url(app.state.settings.database_url.get_secret_value()).database).resolve()
    with app.state.sessions() as db:
        snapshot=db.get(InputSnapshot,receipt['snapshot_id']);body=verified_body(snapshot)
        result=assemble(normalize(body),[],[store['id'],other['id']],[])
        previous=append_version(db,snapshot,result,snapshot.created_by,'夹具原版本');db.commit()
        correction={'new_snapshot_id':snapshot.id,'new_snapshot_hash':snapshot.content_hash,
            'new_snapshot_version':snapshot.version,'store_id':store['id'],'revision_after':store['revision'],
            'after':{'warehouse_id':warehouse['id']}}
        actor_id=snapshot.created_by
    captured=main_guard(database,body,correction)
    result=deepcopy(result);compare_manual(result,captured['head_fee_summary'])
    result['comparison'].update(source_head_id=captured['head_id'],previous_input_hash=captured['head_input_hash'],
        current_input_hash=content_hash(body),input_changed=False,scope='CURRENT_HEAD_TO_SEPARATE_CORRECTED_INPUT_APPLICATION')
    gate=tmp_path/'source-ready.json';gate.write_text('{}')
    monkeypatch.setattr(module,'verify_source_ready',lambda path:{'source_hashes':{}})
    bundle=module.VerifiedAppend(database,body,correction,result,captured,actor_id,gate,{},'fresh-report-hash',content_hash(result))
    return bundle


def rows(app):
    with app.state.sessions() as db:
        return [(row.id,row.version,row.content,row.content_hash,row.snapshot_id,row.parent_id,row.status)
            for row in db.scalars(select(PlanVersion).order_by(PlanVersion.version))]


def test_same_session_guard_matches_preparation_token_without_opening_another_session(append_bundle,app):
    bundle=append_bundle
    with app.state.sessions() as db:
        guard,snapshot,current=module.guard_in_session(db,bundle.body,bundle.correction)
        assert guard==bundle.expected_guard
        assert snapshot.id==guard['snapshot_id'] and current.id==guard['head_id']
    module.validate_guard_token(guard)


def test_append_reserves_writer_first_commits_once_and_preserves_old_rows(append_bundle,app,monkeypatch):
    bundle=append_bundle;before=rows(app);commits=[];statements=[]
    original=module.write_session
    @contextmanager
    def recording(database):
        with original(database) as (db,engine):
            execute=db.execute;commit=db.commit
            def tracked(statement,*args,**kwargs):statements.append(str(statement));return execute(statement,*args,**kwargs)
            def committed():commits.append(1);return commit()
            monkeypatch.setattr(db,'execute',tracked);monkeypatch.setattr(db,'commit',committed)
            yield db,engine
    monkeypatch.setattr(module,'write_session',recording)
    result=module.append_verified(bundle)
    assert statements[0]=='BEGIN IMMEDIATE' and commits==[1]
    assert result['status']=='APPENDED_DRAFT' and result['confirmed'] is False
    assert result['parent_id']==before[-1][0] and result['version']==before[-1][1]+1
    after=rows(app);assert after[:-1]==before and after[-1][-1]=='DRAFT'
    with app.state.sessions() as db:
        saved=db.get(PlanVersion,result['plan_id'])
        assert verified_body(saved)['result']==bundle.result
        assert saved.created_by==bundle.actor_id and saved.created_by_name==module.SYSTEM_ACTOR_NAME
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.entity_id==saved.id,AuditEvent.action=='VERIFIED_APPLICATION_APPENDED'))==1


def test_repeat_apply_stops_on_changed_head_instead_of_appending_twice(append_bundle,app):
    module.append_verified(append_bundle);once=rows(app)
    with pytest.raises(ApplicationGuardError,match='HEAD changed'):module.append_verified(append_bundle)
    assert rows(app)==once


@pytest.mark.parametrize('change',['inactive_actor','deleted_actor','nonadmin_actor','master_revision','store_revision','new_snapshot','other_group_running'])
def test_state_changes_after_preflight_abort_without_append(append_bundle,app,change):
    bundle=append_bundle
    with app.state.sessions() as db:
        actor=db.get(Account,bundle.actor_id)
        if change=='inactive_actor':actor.active=False
        elif change=='deleted_actor':actor.deleted_at=now()
        elif change=='nonadmin_actor':actor.is_admin=False
        elif change=='master_revision':db.get(SystemState,1).master_revision+=1
        elif change=='store_revision':db.get(Store,bundle.correction['store_id']).revision+=1
        elif change=='new_snapshot':
            old=db.get(InputSnapshot,bundle.expected_guard['snapshot_id'])
            db.add(InputSnapshot(group_id=old.group_id,delivery_date=old.delivery_date,version=old.version+1,
                demand_revision=old.demand_revision,input_fingerprint=old.input_fingerprint,content_hash=old.content_hash,
                content=old.content,created_by=old.created_by,created_by_name=old.created_by_name))
        else:
            db.add(PlanningJob(group_id=None,delivery_date=bundle.body['delivery_date'],snapshot_id=bundle.expected_guard['snapshot_id'],
                kind='OPTIMIZE',status='RUNNING',request='{}',lease_until=now()+3600,created_by=actor.id,created_by_name=actor.display_name))
        db.commit()
    before=rows(app)
    with pytest.raises(ApplicationGuardError):module.append_verified(bundle)
    assert rows(app)==before


def test_failed_proof_audit_rolls_back_standard_append_and_its_audit(append_bundle,app,monkeypatch):
    before=rows(app)
    def failed(*args,**kwargs):raise RuntimeError('injected audit failure')
    monkeypatch.setattr(module,'event',failed)
    with pytest.raises(RuntimeError,match='audit failure'):module.append_verified(append_bundle)
    assert rows(app)==before


def test_result_or_artifact_changes_are_rejected_before_saving(append_bundle,app,tmp_path):
    before=rows(app);path=tmp_path/'proof.json';path.write_text('original')
    append_bundle.artifact_stamps[str(path)]=module.file_stamp(path)
    path.write_text('changed evidence')
    with pytest.raises(ApplicationGuardError,match='artifact changed'):module.append_verified(append_bundle)
    assert rows(app)==before
    append_bundle.artifact_stamps.clear();append_bundle.result['total_fee']='999.99'
    with pytest.raises(ApplicationGuardError,match='result changed in memory'):module.append_verified(append_bundle)
    assert rows(app)==before


def test_postcommit_read_failure_reports_committed_plan_without_claiming_rollback(append_bundle,app,monkeypatch):
    @contextmanager
    def unreadable(*args):raise RuntimeError('injected postcommit read failure');yield
    monkeypatch.setattr(module,'readonly_session',unreadable)
    with pytest.raises(module.CommittedAppendError,match='草稿已提交') as error:module.append_verified(append_bundle)
    assert rows(app)[-1][0]==error.value.summary['plan_id']


def complete_report(fresh=True):
    return {'status':'READY' if fresh else 'VALIDATED_READY_FOR_GUARDED_APPEND','eligible_for_guarded_append':True,
        'source_business_database_written':False,'task_count':225,'pieces':4378,'source_hashes_unchanged':True,
        'offline_proof_external_calls':0,'external_calls':0,'proof':{'audit':{'valid':True,'unassigned_tasks':0},
            'independent_check':{'valid':False,'issues':[{'code':'DRIVING_REFERENCE'}]}}}


@pytest.mark.parametrize('change',['status','ineligible','already_written','task_count','pieces','changed_source','offline_calls','unassigned','blocking_issue','invalid_check'])
def test_unverified_report_cannot_pass_the_append_gate(change):
    report=complete_report();module._report_complete(report,fresh=True)
    if change=='status':report['status']='MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND'
    elif change=='ineligible':report['eligible_for_guarded_append']=False
    elif change=='already_written':report['source_business_database_written']=True
    elif change=='task_count':report['task_count']=224
    elif change=='pieces':report['pieces']=4377
    elif change=='changed_source':report['source_hashes_unchanged']=False
    elif change=='offline_calls':report['offline_proof_external_calls']=1
    elif change=='unassigned':report['proof']['audit']['unassigned_tasks']=1
    elif change=='blocking_issue':report['proof']['independent_check']['issues'].append({'code':'ROAD_PROFILE'})
    else:report['proof']['independent_check']['valid']=True
    with pytest.raises(ApplicationGuardError):module._report_complete(report,fresh=True)


def test_reference_notices_keep_check_valid_false_but_are_eligible_for_draft():
    report=complete_report()
    assert report['proof']['independent_check']['valid'] is False
    module._report_complete(report,fresh=True)
    report['proof']['independent_check']={'valid':True,'issues':[]}
    module._report_complete(report,fresh=True)
    report['proof']['independent_check']={'valid':False,'issues':[{'code':'ROAD_PROFILE'}]}
    with pytest.raises(ApplicationGuardError,match='blocking'):module._report_complete(report,fresh=True)


def test_default_cli_is_readonly_inspection_and_never_calls_append(append_bundle,monkeypatch,capsys):
    monkeypatch.setattr(module,'verify_application',lambda *args:append_bundle)
    def forbidden(*args):pytest.fail('Default CLI must not apply')
    monkeypatch.setattr(module,'append_verified',forbidden)
    module.main(['--fresh-report','not-read-in-this-unit-test.json'])
    output=json.loads(capsys.readouterr().out)
    assert output['status']=='VERIFIED_DRY_RUN' and output['source_business_database_written'] is False


def test_comparison_detects_correct_amounts_with_incorrect_input_attribution(append_bundle):
    bundle=append_bundle;module.validate_comparison(bundle.result,bundle.expected_guard,bundle.body)
    bundle.result['comparison']['input_changed']=True
    with pytest.raises(ApplicationGuardError,match='lineage'):module.validate_comparison(bundle.result,bundle.expected_guard,bundle.body)


def test_fresh_fee_bridge_reconciles_input_algorithm_and_road_changes_with_decimal_money():
    candidate={'total_fee':'200.05'};result={'total_fee':'210.15'};guard={'head_total_fee':'300.00'}
    report={'fee_bridge':{'fixed_vehicle_order_common_choices_unchanged':True,'corrected_frozen_fee':'200.05',
        'fresh_fixed_order_fee':'210.15','fresh_reference_delta':'10.10','current_head_fee':'300.00',
        'delta_from_current_head':'-89.85','corrected_frozen_delta_from_current_head':'-99.95'}}
    module.verify_fee_bridge(report,candidate,result,guard)
    report['fee_bridge']['fresh_reference_delta']='10.09'
    with pytest.raises(ApplicationGuardError,match='reconcile'):module.verify_fee_bridge(report,candidate,result,guard)


@pytest.fixture
def tiny_origin_chain(tmp_path,monkeypatch):
    original={'marker':'original'};body={'marker':'corrected'};study={'marker':'study'}
    case={'case_file':str(tmp_path/'case.json'),'private_graph_hash':'graph','verification_script_sha256':'verifier'}
    candidate={'routes':[],'common':[],'total_fee':'1.00'}
    profile={'name':'registered-profile'}
    report={**complete_report(False),'scope':module.APPLICATION_SCOPE,'case_hash':content_hash(case),
        'corrected_input_hash':content_hash(body),'private_graph_hash':'graph','verification_script_sha256':'verifier',
        'result_file':str(tmp_path/'result.json'),'result_hash':content_hash(candidate),
        'original_input_hash':content_hash(original),'study_summary_hash':content_hash(study),
        'winning_profile':profile,'selected_profile':profile,'profile_rank':1}
    files={str(tmp_path/'case.json'):case,str(tmp_path/'input.json'):body,str(tmp_path/'original-input.json'):original,
        str(tmp_path/'study.json'):study,str(tmp_path/'result.json'):candidate,str(tmp_path/'application-report.json'):report}
    for path,value in files.items():Path(path).write_text(json.dumps(value))
    monkeypatch.setattr(module,'winner_profile',lambda *args:({'status':'VALID'},profile))
    stamps={};hashes={}
    def read(path):return module._tracked_json(path,stamps,hashes)
    return tmp_path,case,study,original,body,report,files,read,stamps,hashes


def test_registered_application_origin_keeps_formal_rank_metadata(tiny_origin_chain):
    _,case,study,original,body,report,_,read,stamps,hashes=tiny_origin_chain
    profiles=module.verify_origin_report(report,case,study,original,body,read,stamps,hashes)
    assert profiles==[{'profile_rank':1,'selected_profile':report['selected_profile']}]
    report['selected_profile']={'name':'unregistered'}
    with pytest.raises(ApplicationGuardError,match='recorded formal profile'):
        module.verify_origin_report(report,case,study,original,body,read,stamps,hashes)


def test_merge_origin_preserves_hash_chain_without_inventing_its_own_rank(tiny_origin_chain):
    folder,case,study,original,body,report,files,read,stamps,hashes=tiny_origin_chain
    origins={path:module.file_sha256(path) for path in files}
    (folder/'source-manifest.json').write_text(json.dumps(origins))
    merge={**complete_report(False),'scope':module.MERGE_SCOPE,'case_hash':content_hash(case),
        'corrected_input_hash':content_hash(body),'private_graph_hash':'graph','verification_script_sha256':'verifier',
        'result_file':report['result_file'],'result_hash':report['result_hash'],'run_output':str(folder),
        'merge_script_sha256':module.file_sha256(module.ROOT/'scripts/merge_application_routes.py'),
        'formal_ranking_unchanged':True,'origin_files':origins}
    profiles=module.verify_origin_report(merge,case,study,original,body,read,stamps,hashes,study_file=folder/'study.json')
    assert profiles==[{'profile_rank':1,'selected_profile':report['selected_profile']}]
    assert 'profile_rank' not in merge
    (folder/'application-report.json').write_text('{}')
    with pytest.raises(ApplicationGuardError,match='artifact changed'):
        module.verify_origin_report(merge,case,study,original,body,read,stamps,hashes,study_file=folder/'study.json')
