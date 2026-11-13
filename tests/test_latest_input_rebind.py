from copy import deepcopy
import json
from pathlib import Path
import pytest
from sqlalchemy import select,func
from sqlalchemy.engine import make_url
from app.models import Account,AuditEvent,InputSnapshot,Store,SystemState
from app.plan_service import append_version,verified_body
from app.planning_inputs import content_hash
from app.routing_engine import assemble,normalize
import scripts.rebind_application_input as module
from scripts.prepare_corrected_application import ApplicationGuardError
from test_corrected_application import input_pair
from test_stage4 import basis
from test_demand_groups import create_group,manual

def pair():
    _,old,_=input_pair();s=old['stores'][1];s['city']='淮安市'
    new=deepcopy(old);new['stores'][1].update(city='淮南市',revision=s['revision']+1,updated_at='2026-11-13T10:55:35+08:00')
    new['source_stamp']['master_revision']+=1;new['configuration_hash']='latest-config'
    return old,new

def test_strict_city_change_keeps_complete_other_economic_inputs():
    old,new=pair();assert module.strict_latest_change(old,new,'S1')['quantities_prices_coordinates_times_vehicles_unchanged']

@pytest.mark.parametrize('change',['quantity','coordinate','price','vehicle','other_store','window','warehouse','stamp','city','region'])
def test_latest_city_whitelist_rejects_any_other_change(change):
    old,new=pair()
    if change=='quantity':new['tasks'][0]['pieces']+=1
    elif change=='coordinate':new['stores'][1]['longitude']='121'
    elif change=='price':new['stores'][1]['common_unit_price']='101'
    elif change=='vehicle':new['vehicles'][0]['max_pieces']+=1
    elif change=='other_store':new['stores'][0]['name']='Changed'
    elif change=='window':new['stores'][1]['business_close_at']='2026-11-12T19:00:00+08:00'
    elif change=='warehouse':new['tasks'][1]['warehouse_id']='W'
    elif change=='stamp':new['source_stamp']['master_revision']+=1
    elif change=='city':new['stores'][1]['city']='南京市'
    else:
        old['stores'][1]['dedicated_quote_version']['mode']='KM_REGION'
        new['stores'][1]['dedicated_quote_version']['mode']='KM_REGION'
    with pytest.raises(ApplicationGuardError):module.strict_latest_change(old,new,'S1')

def test_freeze_rejects_a_different_database_before_open_or_backup(tmp_path):
    case=tmp_path/'case.json';body=tmp_path/'input.json'
    case.write_text(json.dumps({'paths':{'database':str(tmp_path/'real.db')}}));body.write_text('{}')
    with pytest.raises(ApplicationGuardError,match='MAIN database'):
        module.freeze_latest(case,tmp_path/'shadow.db',tmp_path/'backup.db',tmp_path/'receipt.json')
    assert not (tmp_path/'backup.db').exists()

@pytest.mark.parametrize('changed',['manifest','receipt'])
def test_latest_case_rejects_manifest_or_receipt_byte_hash_change(tmp_path,changed):
    oldpath=tmp_path/'old-case.json';oldpath.write_text(json.dumps({'paths':{'database':str(tmp_path/'main.db')}}))
    manifest=tmp_path/'manifest.json';manifest.write_text('{}');receipt=tmp_path/'receipt.json';receipt.write_text('{}')
    case={'scope':module.CASE_SCOPE,'paths':{'database':str(tmp_path/'main.db')},'latest_input_rebinding':{
        'old_case_file':str(oldpath),'old_case_sha256':module.sha(oldpath),
        'rebind_script_sha256':module.sha(module.ROOT/'scripts/rebind_application_input.py'),
        'source_manifest_file':str(manifest),'source_manifest_sha256':'0'*64 if changed=='manifest' else module.sha(manifest),
        'freeze_receipt_file':str(receipt),'freeze_receipt_sha256':'0'*64 if changed=='receipt' else module.sha(receipt)}}
    with pytest.raises(ApplicationGuardError,match='manifest or freeze receipt changed'):module.verify_latest_case(case)

def test_latest_report_must_capture_all_critical_origin_files(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'verify_latest_case',lambda case:{})
    study=tmp_path/'study.json';study.write_text('{}')
    old=tmp_path/'old.json';old.write_text(json.dumps({'source_ready_file':str(tmp_path/'ready.json'),'paths':{'correction':str(tmp_path/'correction.json')}}))
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'source_report_file':str(tmp_path/'source-report.json'),'result_file':str(tmp_path/'source-result.json')}))
    case={'transition':{'corrected_input_hash':'new'},'private_graph_hash':'graph','latest_input_rebinding':{
        'old_case_file':str(old),'source_manifest_file':str(manifest),'freeze_receipt_file':str(tmp_path/'receipt.json'),'study_summary_file':str(study)}}
    report={'scope':module.SCOPE,'status':'VALIDATED_READY_FOR_GUARDED_APPEND','eligible_for_guarded_append':True,
        'case_hash':content_hash(case),'corrected_input_hash':'new','private_graph_hash':'graph','study_summary_hash':content_hash({}),
        'rebind_script_sha256':module.sha(module.ROOT/'scripts/rebind_application_input.py'),'origin_files_sha256':{}}
    with pytest.raises(ApplicationGuardError,match='critical source files'):module.verify_latest_report(report,case)

def test_standard_freeze_appends_once_preserves_user_fields_and_old_snapshot(basis,app,tmp_path,monkeypatch):
    c,w,q,s,other,v=basis
    with app.state.sessions() as db:
        row=db.get(Store,s['id']);row.city='淮安市';row.province='安徽省';row.district='田家庵区';row.region_code='340403';row.updated_at-=60;db.commit()
    group=create_group(c,'latest-input-test');receipt=manual(c,group,[{'store_id':s['id'],'pieces':3},{'store_id':other['id'],'pieces':4}])
    with app.state.sessions() as db:
        snapshot=db.get(InputSnapshot,receipt['snapshot_id']);old=verified_body(snapshot);actor=snapshot.created_by
        result=assemble(normalize(old),[],[s['id'],other['id']],[])
        previous=append_version(db,snapshot,result,actor,'private-test');db.commit()
        guard={'head_id':previous.id,'head_content_hash':previous.content_hash}
        store=db.get(Store,s['id']);store.city='淮南市';store.revision+=1;store.updated_at+=120;db.get(SystemState,1).master_revision+=1;db.commit()
        before_count=db.scalar(select(func.count()).select_from(InputSnapshot))
        before_audit=db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='INPUT_SNAPSHOT_CREATED'))
    folder=tmp_path/'case';folder.mkdir();case=folder/'case.json';database=Path(make_url(app.state.settings.database_url.get_secret_value()).database).resolve()
    case.write_text(json.dumps({'paths':{'database':str(database)},'main_guard':guard}));(folder/'input.json').write_text(json.dumps(old))
    real=module.strict_latest_change;monkeypatch.setattr(module,'strict_latest_change',lambda a,b:real(a,b,s['id']))
    first=module.freeze_latest(case,database,tmp_path/'backup.db',tmp_path/'receipt.json',actor_id=actor)
    second=module.freeze_latest(case,database,tmp_path/'backup.db',tmp_path/'receipt2.json',actor_id=actor)
    assert first['created'] is True and second['created'] is False and first['snapshot_id']==second['snapshot_id']
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(InputSnapshot))==before_count+1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action=='INPUT_SNAPSHOT_CREATED'))==before_audit+1
        assert verified_body(db.get(InputSnapshot,receipt['snapshot_id']))==old
        assert db.get(Store,s['id']).city=='淮南市'
