"""Preserve the user's latest city correction and reprice old recipes on it.

Only freeze-latest may append one standard input snapshot (after a backup).
Reprice writes private artifacts only. Old cases/reports/graphs remain intact.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from copy import deepcopy
import hashlib,json,sqlite3,time
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from sqlalchemy import select,func
from app.models import Account,InputSnapshot,PlanningJob,Store
from app.database import start_write
from app.plan_service import head,verified_body
from app.planning_inputs import content_hash,freeze,frozen_body,input_context
from app.routing_engine import normalize,recalculate,compare_manual
from scripts.benchmark_algorithms import private_json,read_only,graph_digest
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,require,
    readonly_session,main_guard,validate_transition,verified_application_result,verify_source_ready)
TARGET='fbd51379-c8e4-4264-96b9-3b69fde16775'
SCOPE='LATEST_INPUT_REPRICE_ONLY'
CASE_SCOPE='AUTHORIZED_LATEST_INPUT_APPLICATION_CASE'

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def read(path):return json.loads(Path(path).read_text())

def strict_latest_change(old,new,target=TARGET):
    require(old['group_id']==new['group_id'] and old['delivery_date']==new['delivery_date'],'Latest input changes group/day')
    a=deepcopy(old);b=deepcopy(new)
    before=next((s for s in a['stores'] if s['id']==target),None);after=next((s for s in b['stores'] if s['id']==target),None)
    require(before is not None and after is not None,'Latest changed store is absent')
    require(before['city']=='淮安市' and after['city']=='淮南市' and after['revision']==before['revision']+1,'Unexpected city/revision transition')
    require(before['dedicated_quote_version']['mode']=='KM' and not before['dedicated_quote_version'].get('region_surcharge'),
            'Changed city has a regional quotation; explicit new economic validation is required')
    require(after['updated_at']!=before['updated_at'],'Changed store timestamp is missing')
    for key in ('city','revision','updated_at'):before.pop(key,None);after.pop(key,None)
    require(b['source_stamp']['master_revision']==a['source_stamp']['master_revision']+1,'Unexpected latest master revision')
    a['source_stamp'].pop('master_revision');b['source_stamp'].pop('master_revision')
    a.pop('configuration_hash');b.pop('configuration_hash')
    require(a==b,'Latest input contains changes beyond the explicit city/revision/time/master/config whitelist')
    aa=normalize(old);bb=normalize(new)
    for data in (aa,bb):
        for key in ('city','revision','updated_at'):data['tasks'][target].pop(key,None)
    require(all(aa[key]==bb[key] for key in ('tasks','vehicles','warehouses')),'Normalized economic/road inputs changed')
    return {'store_id':target,'city_before':'淮安市','city_after':'淮南市','master_before':old['source_stamp']['master_revision'],
        'master_after':new['source_stamp']['master_revision'],'quantities_prices_coordinates_times_vehicles_unchanged':True}

def verify_latest_case(case):
    require(case.get('scope')==CASE_SCOPE,'Latest case scope is invalid')
    binding=case['latest_input_rebinding'];old_case=read(binding['old_case_file'])
    require(sha(binding['old_case_file'])==binding['old_case_sha256'],'Original case file changed')
    require(Path(case['paths']['database']).resolve()==Path(old_case['paths']['database']).resolve(),'Latest case changed the MAIN database')
    require(sha(ROOT/'scripts/rebind_application_input.py')==binding['rebind_script_sha256'],'Latest rebind helper changed')
    require(sha(binding['source_manifest_file'])==binding['source_manifest_sha256'] and
        sha(binding['freeze_receipt_file'])==binding['freeze_receipt_sha256'],'Latest manifest or freeze receipt changed')
    manifest=read(binding['source_manifest_file']);receipt=read(binding['freeze_receipt_file'])
    require(Path(receipt['database']).resolve()==Path(old_case['paths']['database']).resolve(),'Latest receipt changed the MAIN database')
    require(sha(manifest['source_report_file'])==manifest['source_report_sha256'] and sha(manifest['result_file'])==manifest['result_sha256'],
            'Original recipe report/result changed')
    require(manifest['case_sha256']==binding['old_case_sha256'] and manifest['source_business_database_written'] is False
        and manifest['external_calls']==0 and manifest['scope']=='OFFLINE_OLD_V3_RECIPE_ONLY','Latest recipe manifest is not bound offline provenance')
    old=read(Path(binding['old_case_file']).parent/'input.json');new=read(Path(case['case_file']).parent/'input.json')
    original=read(Path(binding['old_case_file']).parent/'original-input.json');correction=read(old_case['paths']['correction'])
    validate_transition(original,old,correction,225);change=strict_latest_change(old,new)
    require(manifest.get('input_hash')==content_hash(old) and manifest.get('graph_hash')==old_case['private_graph_hash']
        and manifest.get('source_hashes')==old_case['source_hashes']
        and manifest.get('guard_override_scope')=='PRIVATE_OLD_V3_SHADOW_ONLY','Latest offline manifest economic/source identity changed')
    require(case['source_ready_hash']==old_case['source_ready_hash'] and case['source_hashes']==old_case['source_hashes'],'Latest case changed frozen research sources')
    require(content_hash(verify_source_ready(case['source_ready_file']))==case['source_ready_hash'],'Latest source-ready full hash changed')
    require(case['transition']['corrected_input_hash']==content_hash(new) and case['transition']['original_input_hash']==content_hash(original),'Latest case input hashes changed')
    require(case['private_graph_hash']==old_case['private_graph_hash'],'Latest city edit changed road coordinates')
    require(case['main_guard']['snapshot_hash']==content_hash(new) and case['main_guard']['master_revision']==new['source_stamp']['master_revision'],'Latest guard binds another input')
    require(case['main_guard']['guard_token']==content_hash({k:v for k,v in case['main_guard'].items() if k!='guard_token'}),'Latest guard token is invalid')
    correction_new=read(case['paths']['correction']);guard=case['main_guard']
    require(receipt['snapshot_id']==guard['snapshot_id']==correction_new['new_snapshot_id'] and
        receipt['snapshot_hash']==guard['snapshot_hash']==correction_new['new_snapshot_hash']==content_hash(new) and
        receipt['version']==guard['snapshot_version']==correction_new['new_snapshot_version'] and
        receipt['input_fingerprint']==guard['input_fingerprint'],'Latest snapshot/receipt/correction bindings disagree')
    return {**change,'pieces':sum(t['pieces'] for t in new['tasks']),'task_count':len(new['tasks'])}

def verify_latest_report(report,case,files=None):
    verify_latest_case(case)
    require(report.get('scope')==SCOPE and report.get('status')=='VALIDATED_READY_FOR_GUARDED_APPEND'
        and report.get('eligible_for_guarded_append') is True,'Latest report is not independently ready')
    require(report['case_hash']==content_hash(case) and report['corrected_input_hash']==case['transition']['corrected_input_hash'],'Latest report case/input changed')
    require(report['private_graph_hash']==case['private_graph_hash'] and
        report['study_summary_hash']==content_hash(read(case['latest_input_rebinding']['study_summary_file'])),
            'Latest report graph/study attribution changed')
    require(report['rebind_script_sha256']==sha(ROOT/'scripts/rebind_application_input.py'),'Latest report verifier changed')
    binding=case['latest_input_rebinding'];manifest=read(binding['source_manifest_file']);old=read(binding['old_case_file'])
    oldroot=Path(binding['old_case_file']).resolve().parent
    required={str(Path(p).resolve()) for p in [binding['old_case_file'],oldroot/'input.json',oldroot/'original-input.json',
        binding['study_summary_file'],binding['source_manifest_file'],binding['freeze_receipt_file'],
        manifest['source_report_file'],manifest['result_file'],old['source_ready_file'],old['paths']['correction']]}
    require(isinstance(report.get('origin_files_sha256'),dict) and required.issubset(report['origin_files_sha256']),
            'Latest report omits critical source files')
    for path,expected in report['origin_files_sha256'].items():
        require(sha(path)==expected,'Latest recipe origin file changed: '+path)
        if files is not None:require(files.setdefault(str(Path(path).resolve()),expected)==expected,'Latest source binding differs')

def freeze_latest(case_file,database,backup_file,receipt_file,actor_id='5728ea3a-0918-49fa-8d77-6c719c5962d8'):
    case=read(case_file);old=read(Path(case_file).parent/'input.json')
    require(Path(database).resolve()==Path(case['paths']['database']).resolve(),'Latest freeze changed the MAIN database')
    with readonly_session(database) as db:
        context=input_context(db,old['delivery_date'],group_id=old['group_id']);new=frozen_body(old['delivery_date'],context)
        strict_latest_change(old,new)
        latest=db.scalar(select(InputSnapshot).where(InputSnapshot.group_id==old['group_id']).order_by(InputSnapshot.version.desc()).limit(1))
        if latest and latest.input_fingerprint==context['fingerprint']:
            require(verified_body(latest)==new,'Existing latest snapshot body differs')
            receipt={'snapshot_id':latest.id,'snapshot_hash':latest.content_hash,'version':latest.version,'created':False,
                'input_fingerprint':latest.input_fingerprint,'database':str(Path(database).resolve())}
            private_json(receipt_file,receipt);return receipt
    from scripts.backup_sqlite import backup
    backup_record=backup(database,backup_file)
    from scripts.append_verified_application import write_session
    with write_session(database) as (db,engine):
        try:
            start_write(db,engine)
            require(db.scalar(select(func.count()).select_from(PlanningJob).where(PlanningJob.status.in_(['QUEUED','RUNNING'])))==0,'Main has active jobs')
            actor=db.get(Account,actor_id);require(actor is not None and actor.active and actor.deleted_at is None and actor.is_admin,'Existing actor is not authorized')
            context2=input_context(db,old['delivery_date'],group_id=old['group_id']);new2=frozen_body(old['delivery_date'],context2)
            require(new2==new and context2['fingerprint']==context['fingerprint'],'User input changed before snapshot write')
            strict_latest_change(old,new2)
            current=head(db,old['delivery_date'],old['group_id'])
            require(current is not None and current.id==case['main_guard']['head_id'] and current.content_hash==case['main_guard']['head_content_hash'],'HEAD changed before latest freeze')
            snapshot=freeze(db,old['delivery_date'],actor.id,'系统算法最新输入复核',context2)
            receipt={'snapshot_id':snapshot.id,'snapshot_hash':snapshot.content_hash,'version':snapshot.version,'created':True,
                'input_fingerprint':snapshot.input_fingerprint,'database':str(Path(database).resolve()),'backup':backup_record}
            db.commit()
        except BaseException:db.rollback();raise
    private_json(receipt_file,receipt);return receipt

def rebind(case_file,study_file,source_manifest_file,receipt_file,output):
    old_case=read(case_file);old=read(Path(case_file).parent/'input.json');original=read(Path(case_file).parent/'original-input.json')
    ready=verify_source_ready(old_case['source_ready_file']);require(content_hash(ready)==old_case['source_ready_hash'],'Frozen source-ready changed')
    receipt=read(receipt_file);database=receipt['database'];manifest=read(source_manifest_file)
    require(Path(database).resolve()==Path(old_case['paths']['database']).resolve(),'Latest reprice changed the MAIN database')
    require(manifest['scope']=='OFFLINE_OLD_V3_RECIPE_ONLY' and manifest['source_business_database_written'] is False
        and manifest['external_calls']==0 and manifest['guard_override_scope']=='PRIVATE_OLD_V3_SHADOW_ONLY','Source is not explicitly offline recipe-only')
    require(sha(case_file)==manifest['case_sha256'] and manifest['input_hash']==content_hash(old)
        and manifest['graph_hash']==old_case['private_graph_hash'],'Offline recipe case/input/graph changed')
    source_report=read(manifest['source_report_file']);source_result=read(manifest['result_file'])
    require(sha(manifest['source_report_file'])==manifest['source_report_sha256'] and sha(manifest['result_file'])==manifest['result_sha256'],'Offline recipe artifact changed')
    require(content_hash(source_result)==source_report['result_hash'] and source_report['case_hash']==content_hash(old_case),'Offline result is not bound to original case')
    origins={str(Path(p).resolve()):sha(p) for p in [case_file,study_file,source_manifest_file,receipt_file,manifest['source_report_file'],manifest['result_file'],
        Path(case_file).parent/'input.json',Path(case_file).parent/'original-input.json',old_case['paths']['correction'],old_case['source_ready_file']]}
    for path,expected in source_report.get('origin_files',{}).items():require(sha(path)==expected,'Old recipe source chain changed');origins[str(Path(path).resolve())]=expected
    require(manifest.get('source_hashes')==ready['source_hashes'],'Offline recipe frozen source19 differs')
    with readonly_session(database) as db:
        snapshot=db.get(InputSnapshot,receipt['snapshot_id']);require(snapshot is not None,'Latest snapshot is missing')
        new=verified_body(snapshot);require(snapshot.content_hash==receipt['snapshot_hash'],'Latest receipt hash changed')
    strict_latest_change(old,new)
    require(len(new['tasks'])==225 and sum(t['pieces'] for t in new['tasks'])==4378,'Latest input must retain all225/4378')
    target=Path(output).resolve();require(not target.exists() and not target.is_symlink(),'Latest output already exists')
    require(target.parent==Path(case_file).resolve().parent,'Latest output must be a new private child')
    target.mkdir(mode=0o700)
    correction=deepcopy(read(old_case['paths']['correction']));correction.update(new_snapshot_id=receipt['snapshot_id'],new_snapshot_hash=content_hash(new),new_snapshot_version=receipt['version'])
    guard=main_guard(database,new,correction)
    road=ApplicationFrozenRoad(old_case['private_graph'])
    try:
        require(road.graph_hash==old_case['private_graph_hash'],'Old corrected graph changed')
        verified_application_result(normalize(old),source_result,road)
        draft={'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':r['task_ids']} for r in source_result['routes']],
            'common_ids':[r['task_id'] for r in source_result['common']],'unassigned_ids':[]}
        old_reprice=recalculate(old,draft,road);require(old_reprice['total_fee']==source_result['total_fee'],'Old source economics do not reprice')
        result=recalculate(new,draft,road);proof=verified_application_result(normalize(new),result,road)
        require(result['total_fee']==old_reprice['total_fee'],'City-only nonregional correction changed the full quote')
    finally:road.close()
    from scripts.refresh_application_roads import backup_graph
    graph=backup_graph(old_case['private_graph'],target/'frozen-roads.db')
    with closing(sqlite3.connect(graph)) as db:
        metadata={'input_hash':content_hash(new),'snapshot_id':receipt['snapshot_id'],'scope':SCOPE,
            'parent_graph_hash':old_case['private_graph_hash'],'parent_snapshot_id':old_case['main_guard']['snapshot_id'],
            'source_business_database_written':False}
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in metadata.items()]);db.commit()
    private_json(target/'input.json',new);private_json(target/'original-input.json',original);private_json(target/'correction.json',correction)
    case=deepcopy(old_case);case.update(scope=CASE_SCOPE,case_file=str(target/'case.json'),private_graph=str(graph),main_guard=guard)
    case['paths']={**case['paths'],'database':database,'corrected_input':str(target/'input.json'),'original_input':str(target/'original-input.json'),'correction':str(target/'correction.json')}
    case['transition']={**case['transition'],'corrected_input_hash':content_hash(new),'master_revision_after':new['source_stamp']['master_revision']}
    case['original_authorized_warehouse_transition']=deepcopy(old_case['transition'])
    case['transition'].update(changed_store_count=2,change_scope='TWO_STAGES_ORIGINAL_WAREHOUSE_CORRECTION_THEN_USER_CITY_METADATA',
        latest_city_only_store_id=TARGET,economic_warehouse_change_store_id=correction['store_id'],
        latest_city_edit_has_no_price_capacity_time_coordinate_effect=True)
    case['latest_input_rebinding']={'old_case_file':str(Path(case_file).resolve()),'old_case_sha256':sha(case_file),
        'study_summary_file':str(Path(study_file).resolve()),
        'rebind_script_sha256':sha(__file__),'source_manifest_file':str(Path(source_manifest_file).resolve()),'source_manifest_sha256':sha(source_manifest_file),
        'freeze_receipt_file':str(Path(receipt_file).resolve()),'freeze_receipt_sha256':sha(receipt_file),'change':strict_latest_change(old,new)}
    private_json(target/'case.json',case);verify_latest_case(case)
    road=ApplicationFrozenRoad(graph)
    try:result=recalculate(new,draft,road);proof=verified_application_result(normalize(new),result,road)
    finally:road.close()
    compare_manual(result,guard['head_fee_summary']);result['comparison'].update(source_head_id=guard['head_id'],previous_input_hash=guard['head_input_hash'],current_input_hash=content_hash(new),input_changed=True,scope=SCOPE)
    result['algorithm']={'mode':source_result.get('algorithm',{}).get('mode','HYBRID_COST_PORTFOLIO'),'stop_reason':'LATEST_INPUT_REPRICED',
        'selection_scope':SCOPE,'optimality_proven':False,'source_result_hash':content_hash(source_result),'road_stats':{'calls':0},'latest_input_rebinding':case['latest_input_rebinding']}
    after=main_guard(database,new,correction);require(after==guard,'Latest main changed during repricing')
    require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen sources changed')
    for path,expected in origins.items():require(sha(path)==expected,'Source changed during latest rebind')
    report={'schema_version':1,'status':'VALIDATED_READY_FOR_GUARDED_APPEND','scope':SCOPE,'eligible_for_guarded_append':True,
        'source_business_database_written':False,'external_calls':0,'source_hashes_unchanged':True,'task_count':225,'pieces':4378,
        'proof':proof,'case_file':case['case_file'],'case_hash':content_hash(case),'original_input_hash':content_hash(original),
        'corrected_input_hash':content_hash(new),'private_graph_hash':case['private_graph_hash'],'verification_script_sha256':case['verification_script_sha256'],
        'rebind_script_sha256':sha(__file__),'study_summary_hash':content_hash(read(study_file)),'origin_files_sha256':origins,
        'main_guard_before':guard,'main_guard_after':after,'original_recipe_scope':source_report.get('scope'),
        'original_recipe_report_sha256':manifest['source_report_sha256'],'latest_input_only_repriced_not_a_study_run':True,
        'result_file':str(target/'latest-input-result.json'),'result_hash':content_hash(result),'run_output':str(target)}
    private_json(target/'latest-input-result.json',result);private_json(target/'latest-input-report.json',report);return report

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    f=sub.add_parser('freeze-latest');f.add_argument('--case',required=True);f.add_argument('--database',required=True);f.add_argument('--backup',required=True);f.add_argument('--receipt',required=True)
    r=sub.add_parser('reprice');r.add_argument('--case',required=True);r.add_argument('--study-summary',required=True);r.add_argument('--source-manifest',required=True);r.add_argument('--receipt',required=True);r.add_argument('--output',required=True)
    a=p.parse_args()
    result=freeze_latest(a.case,a.database,a.backup,a.receipt) if a.action=='freeze-latest' else rebind(a.case,a.study_summary,a.source_manifest,a.receipt,a.output)
    print(json.dumps({k:result.get(k) for k in ('status','snapshot_id','version','created','result_file','case_file')},ensure_ascii=False))
if __name__=='__main__':main()
