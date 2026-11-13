"""Finalize a strict prefix subset of already collected provider roads, without HTTP.

Real network interval, attempts and immutable cache receipts belong to the failed
refresh. This separate producer removes its single late last stop to priced
common delivery, proves the remaining measured prefixes, and never rewrites history.
"""
import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import json,sqlite3,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT)not in sys.path:sys.path.insert(0,str(ROOT))
from app.planning_inputs import content_hash,encode
from app.routing_engine import compare_manual,normalize,recalculate
from scripts.benchmark_algorithms import private_json
from scripts.prepare_corrected_application import ApplicationFrozenRoad,main_guard,require,verified_application_result,verify_source_ready
from scripts.refresh_application_roads import (SCOPE,PrivateCache,audit_refreshed_sources,backup_graph,file_sha,
    fixed_draft,provider_keys,stripped_origin_algorithm,verify_application_origin)

def finalize(case_file,application_file,failed_file,output):
    start=time.monotonic();read=lambda p:json.loads(Path(p).read_text());case_file=Path(case_file).resolve()
    case=read(case_file);root=case_file.parent;application_file=Path(application_file).resolve();application=read(application_file)
    failed_file=Path(failed_file).resolve();failed=read(failed_file);folder=failed_file.parent
    network_file=folder/'network-result.json';network=read(network_file);network_graph=folder/'network-roads.db';cache_file=folder/'private-cache.db'
    body=read(root/'input.json');original=read(root/'original-input.json');correction=read(case['paths']['correction']);data=normalize(body)
    study_file=Path(case['latest_input_rebinding']['study_summary_file']);study=read(study_file);ready=verify_source_ready(case['source_ready_file'])
    files={};verify_application_origin(application,case,case_file,study,study_file,files)
    source=read(application['result_file']);require(content_hash(source)==application['result_hash'],'Source result changed')
    require(failed['status']=='FAILED' and failed['network_stats']['failures']==0,'Expected provider-success/feasibility-only failed refresh')
    require(fixed_draft(network)==fixed_draft(source),'Failed refresh allocation differs from bound source')
    blockers=[e for e in network['check']['issues'] if e['code']!='DRIVING_REFERENCE'];bad={e.get('task_id') for e in blockers}
    require(len(bad)==1 and all(e['code']in ['LATEST_ARRIVAL','BUSINESS_CLOSED','TIME_CONSTRAINT'] for e in blockers),'Expected one late last-store constraint failure')
    task=next(iter(bad));route=next(r for r in network['routes'] if task in r['task_ids'])
    require(route['task_ids'][-1]==task and data['tasks'][task]['common_unit_price'] is not None,'Only a priced last store may be removed')
    draft=fixed_draft(network)
    for row in draft['routes']:
        if row['vehicle_id']==route['vehicle_id']:row['task_ids']=row['task_ids'][:-1]
    draft['routes']=[r for r in draft['routes'] if r['task_ids']];draft['common_ids']=sorted(set(draft['common_ids'])|{task})
    require(Path(output).name==output and output not in ['.','..'],'Output must be a new private child')
    target=root/output;target.mkdir(mode=0o700,exist_ok=False)
    guard=main_guard(case['paths']['database'],body,correction);require(guard==case['main_guard'],'Main changed before finalization')
    protected=[case_file,application_file,Path(application['result_file']),failed_file,network_file,network_graph,cache_file,
        root/'input.json',root/'original-input.json',Path(case['paths']['correction']),study_file,Path(__file__).resolve()]
    files.update({str(p.resolve()):file_sha(p) for p in protected})
    all_actual=provider_keys(network);require(len(all_actual)==failed['network_stats']['calls']==failed['actual_external_calls']==166,'Actual third-refresh count differs')
    cache=object.__new__(PrivateCache);cache.path=cache_file
    started=failed['network_started_at'];finished=failed['network_finished_at']
    all_sources=audit_refreshed_sources(network_graph,all_actual,cache,all_actual,started,finished)
    graph=backup_graph(network_graph,target/'frozen-fresh-roads.db')
    with closing(sqlite3.connect(graph)) as db:
        metadata={'scope':SCOPE,'input_hash':content_hash(body),'parent_graph_hash':case['private_graph_hash'],
            'snapshot_id':guard['snapshot_id'],'frozen_at':max(int(time.time()),finished,*[s['received_at']for s in all_sources]),
            'network_started_at':started,'network_finished_at':finished,'actual_external_calls':166,'source_business_database_written':False,
            'finalization_policy':'STRICT_SELECTED_PREFIX_SUBSET_OF_ACTUAL_COLLECTED_ROADS_NO_NEW_HTTP'}
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,encode(v))for k,v in metadata.items()]);db.commit()
    offline=ApplicationFrozenRoad(graph);boundroad=ApplicationFrozenRoad(case['private_graph'])
    try:
        result=recalculate(body,draft,offline);proof=verified_application_result(data,result,offline)
        selected=provider_keys(result);require(selected<all_actual and len(selected)==165,'Final selected roads must be the strict165-of166 collected subset')
        sources=audit_refreshed_sources(graph,selected,cache,selected,started,finished)
        require(proof['road_evidence']['historical_reference_segments']==0,'Every selected source must be current actual evidence')
        bound=recalculate(body,draft,boundroad);old_proof=verified_application_result(data,bound,boundroad)
        private_json(target/'collected-subset-manifest.json',{'scope':'165_SELECTED_PREFIX_ROADS_FROM166_ACTUAL_PROVIDER_RECEIPTS',
            'actual_refresh_started_at':started,'actual_refresh_finished_at':finished,'actual_provider_calls':166,'new_provider_calls':0,
            'all_actual_provider_keys':sorted(all_actual),'selected_provider_keys':sorted(selected),'dropped_task_id':task,
            'dropped_task_name':data['tasks'][task]['name'],'all_actual_provider_sources':all_sources,'selected_provider_sources':sources,
            'strict_route_prefixes_preserved':True,'source_business_database_written':False})
        subset_file=target/'collected-subset-manifest.json';files[str(subset_file.resolve())]=file_sha(subset_file)
        producer={'file':str(Path(__file__).resolve()),'sha256':file_sha(__file__),'purpose':'POST_COLLECTION_PREFIX_FEASIBILITY_FINALIZATION_NO_NEW_HTTP',
            'source_failed_refresh_report':str(failed_file),'actual_provider_calls':166,'new_provider_calls':0,'all_actual_provider_count':166,
            'selected_provider_count':165,'dropped_task_id':task,'dropped_task_name':data['tasks'][task]['name'],'subset_manifest_file':str(subset_file)}
        def comparison(value):
            compare_manual(value,guard['head_fee_summary']);value['comparison'].update(source_head_id=guard['head_id'],previous_input_hash=guard['head_input_hash'],
                current_input_hash=content_hash(body),input_changed=True,scope=SCOPE)
        comparison(bound);comparison(result)
        bound['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','stop_reason':'SEARCH_COMPLETED','selection_scope':'LATEST_INPUT_REPRICE_ONLY',
            'optimality_proven':False,'candidate_pool_optimal':False,'cost_candidate_pool_optimal':False,'global_225_task_optimality_proven':False,
            'finalization_producer':producer,'road_stats':boundroad.stats()}
        private_json(target/'candidate-result.json',bound)
        candidate=deepcopy(application);candidate.update(producer_script_file=producer['file'],producer_script_sha256=producer['sha256'],
            validation_repair=producer,result_file=str(target/'candidate-result.json'),result_hash=content_hash(bound),proof=old_proof,run_output=str(target),
            main_guard_before=guard,main_guard_after=guard)
        candidate['origin_files_sha256'].update(files)
        verify_application_origin(candidate,case,case_file,study,study_file,{})
        candidate_file=target/'candidate-report.json';private_json(candidate_file,candidate);files[str(candidate_file.resolve())]=file_sha(candidate_file)
        files[str((target/'candidate-result.json').resolve())]=file_sha(target/'candidate-result.json')
        after=main_guard(case['paths']['database'],body,correction);require(after==guard,'Main changed during finalization')
        for path,sha in files.items():require(file_sha(path)==sha,'A finalization source changed: '+path)
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen source changed')
        bridge={'corrected_frozen_fee':bound['total_fee'],'fresh_fixed_order_fee':result['total_fee'],
            'fresh_reference_delta':str(Decimal(result['total_fee'])-Decimal(bound['total_fee'])),'current_head_fee':guard['head_total_fee'],
            'delta_from_current_head':str(Decimal(result['total_fee'])-Decimal(guard['head_total_fee'])),
            'corrected_frozen_delta_from_current_head':str(Decimal(bound['total_fee'])-Decimal(guard['head_total_fee'])),
            'fixed_vehicle_order_common_choices_unchanged':True}
        freshhash=offline.graph_hash;stats=failed['network_stats'];elapsed=round(time.monotonic()-start,4)
        result['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','stop_reason':'SELECTED_ROADS_REFRESHED','elapsed_seconds':elapsed,'budget_seconds':120,
            'distinct_route_count':len(result['routes']),'road_stats':stats,'optimality_proven':False,'global_225_task_optimality_proven':False,
            'candidate_pool_optimal':False,'cost_candidate_pool_optimal':False,'mip_gap':None,'cost_mip_gap':None,'selection_scope':SCOPE,
            'old_graph_certificates_cleared':True,'measured_graph_hash':freshhash,'final_fixed_order_refresh':bridge,'finalization_producer':producer,
            'origin_algorithm_metadata':stripped_origin_algorithm(bound['algorithm']),
            'fresh_reference':{'parent_private_graph_hash':case['private_graph_hash'],'fresh_graph_hash':freshhash,'actual_external_calls':166,
                'network_started_at':started,'network_finished_at':finished,'offline_proof_external_calls':0}}
        report={'schema_version':1,'status':'READY','scope':SCOPE,'eligible_for_guarded_append':True,'source_business_database_written':False,
            'external_calls':0,'offline_proof_external_calls':0,'actual_external_calls':166,'new_external_calls_by_finalizer':0,'network_stats':stats,
            'network_started_at':started,'network_finished_at':finished,'freshened_provider_keys':sorted(all_actual),'freshened_total':166,
            'selected_provider_keys':sorted(selected),'selected_provider_sources':sources,'selected_provider_count':165,
            'case_file':str(case_file),'case_hash':content_hash(case),'application_report_file':str(candidate_file),'application_report_hash':content_hash(candidate),
            'source_application_scope':candidate['scope'],'study_summary_file':str(study_file),'study_summary_hash':content_hash(study),
            'source_ready_file':case['source_ready_file'],'source_ready_hash':content_hash(ready),'source_hashes':ready['source_hashes'],'source_hashes_unchanged':True,
            'verification_script_sha256':case['verification_script_sha256'],'refresh_script_file':str(ROOT/'scripts/refresh_application_roads.py'),
            'refresh_script_sha256':file_sha(ROOT/'scripts/refresh_application_roads.py'),'finalization_producer_file':producer['file'],
            'finalization_producer_sha256':producer['sha256'],'finalization_producer':producer,'original_input_hash':content_hash(original),'corrected_input_hash':content_hash(body),
            'original_private_graph_file':case['private_graph'],'original_private_graph_hash':case['private_graph_hash'],'original_private_graph_sha256':file_sha(case['private_graph']),
            'parent_private_graph_hash':case['private_graph_hash'],'network_graph_file':str(network_graph),'fresh_graph_file':str(graph),'fresh_graph_hash':freshhash,
            'fresh_graph_sha256':file_sha(graph),'main_guard_before':guard,'main_guard_after':after,'guard_token':guard['guard_token'],'proof':proof,
            'original_application_proof':old_proof,'task_count':225,'pieces':4378,'fee_bridge':bridge,'global_225_task_optimality_proven':False,
            'global_corrected_input_optimality_proven':False,'origin_files_sha256':files,'result_file':str(target/'fresh-result.json'),
            'result_hash':content_hash(result),'run_output':str(target),'elapsed_seconds':elapsed}
        private_json(target/'fresh-result.json',result);private_json(target/'refresh-report.json',report)
        return {'status':'READY','fee':result['total_fee'],'candidate_fee':bound['total_fee'],'selected_provider_count':165,'actual_external_calls':166,
            'new_calls':0,'fresh_report_file':str(target/'refresh-report.json'),'result_file':report['result_file']}
    finally:offline.close();boundroad.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',required=True);p.add_argument('--application-report',required=True);p.add_argument('--failed-refresh',required=True);p.add_argument('--output',default='finalized-collected-roads')
    a=p.parse_args();print(json.dumps(finalize(a.case,a.application_report,a.failed_refresh,a.output),ensure_ascii=False))
