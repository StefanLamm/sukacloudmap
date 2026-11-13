"""Reprice and jointly select discovered route recipes on corrected V3 only.

This is a separate, read-only application improvement after the formal study.
It never changes formal rankings, imports old prices, calls a provider, or writes
the business database. Optimality claims apply only to the evaluated route pool.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from app.planning_inputs import content_hash
from app.route_metrics import evaluate_candidate
from app.route_selection import select_candidate_routes
from app.routing_engine import StopSearch,assemble,common_fee,compare_manual,evaluate_route,normalize
from scripts.benchmark_algorithms import graph_digest,private_json
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    application_run_output,main_guard,require,
    verified_application_result,verify_source_ready,winner_profile)

SCOPE='CROSS_RUN_VALIDATED_ROUTE_POOL_ONLY'


def rank(result):
    return (len(result['unassigned']),Decimal(result['total_fee']),
            sum(r['meters'] for r in result['routes']),len(result['routes']))


def compact(route,*,search=False):
    row={key:deepcopy(route[key]) for key in ['vehicle_id','task_ids','fee','meters','seconds','pieces']}
    if search and route.get('_estimated'):row['_estimated']=True
    return row


def load_json(path,files,expected_sha=None):
    path=Path(path).resolve();raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    require(expected_sha is None or sha==expected_sha,'Source file hash differs: '+str(path))
    previous=files.setdefault(str(path),sha)
    require(previous==sha,'Source changed while collecting recipes: '+str(path))
    return json.loads(raw)


def plan_recipes(result,path,label,category):
    """Retain identities and order only; all old route economics are discarded."""
    require(isinstance(result,dict) and isinstance(result.get('routes'),list),'Source is not a saved delivery result')
    recipes=[]
    for row in result['routes']:
        recipes.append({'vehicle_id':row.get('vehicle_id'),'task_ids':deepcopy(row.get('task_ids'))})
    return {'file':str(Path(path).resolve()),'label':label,'category':category,'recipes':recipes,
            'common_ids':[r.get('task_id') for r in result.get('common',[])],
            'unassigned_ids':[r.get('task_id') for r in result.get('unassigned',[])],
            'old_total_fee_display_only':result.get('total_fee')}


def collect_sources(case,study,study_file,legacy_manifest,application_reports,extra_results,files):
    """Read immutable source snapshots; formal/legacy fees never enter a column."""
    original_hash=case['transition']['original_input_hash'];corrected_hash=case['transition']['corrected_input_hash']
    winner_profile(study,original_hash)
    require(all(study.get('manifest',{}).get('source_hashes',{}).get(k)==v for k,v in case['source_hashes'].items()),
            'Formal study sources differ from the application case')
    sources=[]
    for n,row in enumerate(study.get('runs',[])):
        if row.get('status')!='VALID' or not row.get('audit',{}).get('valid'):continue
        require(row.get('graph_hash')==case['base_graph']['graph_hash'],'Formal recipe belongs to another measured graph')
        path=row.get('result_file');require(path,'Formal valid run has no result file')
        result=load_json(path,files,row.get('result_file_sha256'))
        sources.append(plan_recipes(result,path,'formal:'+str(row.get('case_name'))+':'+str(n),'FORMAL_RECIPE_ONLY'))
    if legacy_manifest is not None:
        legacy=load_json(legacy_manifest,files)
        require(legacy.get('formal_original_input_hash')==original_hash and legacy.get('corrected_input_hash')==corrected_hash,
                'Legacy manifest belongs to another input transition')
        require(legacy.get('formal_original_graph_hash')==case['base_graph']['graph_hash'] and
                legacy.get('corrected_graph_hash')==case['private_graph_hash'],'Legacy manifest belongs to another road graph')
        for n,row in enumerate(legacy.get('candidates',[])):
            require(isinstance(row.get('result_file_sha256'),str),'Legacy recipe lacks its captured file hash')
            result=load_json(row['result_file'],files,row['result_file_sha256'])
            sources.append(plan_recipes(result,row['result_file'],'legacy:'+str(n),'LEGACY_RECIPE_ONLY'))
    current=[]
    for n,path in enumerate(application_reports):
        report=load_json(path,files)
        require(report.get('source_hashes_unchanged') is True and report.get('external_calls')==0 and
                report.get('source_business_database_written') is False,'Application report has an invalid provenance boundary')
        require(report.get('proof',{}).get('audit',{}).get('valid') is True and
                report['proof']['audit'].get('unassigned_tasks')==0,'Application incumbent lacks a complete independent audit')
        require(report.get('corrected_input_hash')==corrected_hash and report.get('private_graph_hash')==case['private_graph_hash'] and
                report.get('verification_script_sha256')==case['verification_script_sha256'],'Application report input/graph/verifier differs')
        result=load_json(report['result_file'],files)
        require(content_hash(result)==report.get('result_hash'),'Application result changed after its report')
        source=plan_recipes(result,report['result_file'],'application:'+str(n),'CURRENT_V3')
        sources.append(source);current.append(source)
    require(bool(current),'At least one verified current V3 application report is required')
    for n,path in enumerate(extra_results):
        sources.append(plan_recipes(load_json(path,files),path,'additional:'+str(n),'ADDITIONAL_RECIPE_ONLY'))
    return sources,current


def order_problem(data,ids):
    if not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids):return 'MALFORMED_TASK_IDS'
    if len(set(ids))!=len(ids):return 'DUPLICATE_TASK_IN_RECIPE'
    if any(i not in data['tasks'] for i in ids):return 'UNKNOWN_CURRENT_TASK'
    return None


def recipe_problem(data,recipe):
    vid=recipe.get('vehicle_id')
    if not isinstance(vid,str) or vid not in data['vehicles']:return 'UNKNOWN_VEHICLE'
    return order_problem(data,recipe.get('task_ids'))


def validate_whole_recipe(data,source):
    require(all(recipe_problem(data,r) is None for r in source['recipes']),'Saved allocation has an invalid recipe')
    ids=[i for r in source['recipes'] for i in (r.get('task_ids') or [])]
    seen=Counter(ids+source['common_ids']+source['unassigned_ids'])
    require(set(seen)==set(data['tasks']) and all(n==1 for n in seen.values()) and not source['unassigned_ids'],
            'Saved allocation does not cover the current task set exactly once')
    require(len({r['vehicle_id'] for r in source['recipes']})==len(source['recipes']),'Saved allocation reuses a physical vehicle')


def repaired_recipe(data,source):
    """Only the previously authorized wrong-warehouse-to-common feasibility repair."""
    validate_whole_recipe(data,source);copy=deepcopy(source);recipes=[];common=set(source['common_ids']);removed=[]
    for recipe in source['recipes']:
        warehouse=data['vehicles'][recipe['vehicle_id']]['warehouse_id']
        keep=[i for i in recipe['task_ids'] if data['tasks'][i]['warehouse_id']==warehouse]
        dropped=set(recipe['task_ids'])-set(keep)
        require(all(data['tasks'][i]['common_unit_price'] is not None for i in dropped),'Warehouse repair has an unpriced common task')
        common.update(dropped);removed.extend(sorted(dropped))
        if keep:recipes.append({'vehicle_id':recipe['vehicle_id'],'task_ids':keep})
    copy.update(recipes=recipes,common_ids=sorted(common),label=source['label']+':warehouse-repair',
                category='REPAIRED_COMPLETE_RECIPE_ONLY')
    return copy,removed


def full_plan(data,source,road,*,repair=False,control=None):
    """Rebuild an allocation with current quantities, constraints and measured prices."""
    validate_whole_recipe(data,source)
    source,removed=repaired_recipe(data,source) if repair else (source,[])
    result=assemble(data,[evaluate_route(data,r['vehicle_id'],r['task_ids'],road,control) for r in source['recipes']],source['common_ids'],[])
    if control:control()
    verified_application_result(data,result,road)
    return result,removed


def reprice_pool(data,sources,road,control):
    memo={};columns={};ledger=[];source_count=Counter();stopped=None;seen=set()
    for source in sources:
        for number,recipe in enumerate(source['recipes']):
            origin={'source':source['label'],'source_file':source['file'],'category':source['category'],
                    'recipe_index':number,'source_vehicle_id':recipe.get('vehicle_id'),'task_ids':recipe.get('task_ids')}
            problem=order_problem(data,recipe.get('task_ids'))
            if problem:
                ledger.append({**origin,'status':'REJECTED','reasons':[problem]});continue
            for vid in data['vehicles']:
                key=(vid,tuple(recipe['task_ids']))
                entry={**origin,'vehicle_id':vid,'candidate_vehicle_id':vid,'vehicle_reassigned':vid!=recipe.get('vehicle_id')}
                try:
                    control()
                    if key not in memo:
                        route=evaluate_candidate(data,vid,list(key[1]),road,control)
                        if route is None:memo[key]={'rejected':['MEASURED_ROUTE_METRICS_MISSING']}
                        elif route['errors'] or route['fee'] is None:
                            memo[key]={'rejected':[e['code'] for e in route['errors']] or ['UNPRICED_ROUTE']}
                        else:memo[key]=compact(route,search=True)
                    row=memo[key]
                except StopSearch as exc:
                    stopped=exc.reason;break
                if 'rejected' in row:
                    ledger.append({**entry,'status':'REJECTED','reasons':row['rejected']});continue
                columns[key]=row;source_count[source['category']]+=1
                ledger.append({**entry,'status':'REPRICED_CURRENT_V3','current_quote':row,
                               'search_metrics_only':True,'cache_reuse':key in seen})
                seen.add(key)
            if stopped:break
        if stopped:break
    return list(columns.values()),ledger,{'unique_ordered_columns':len(columns),'unique_recipes_evaluated':len(memo),
        'valid_source_occurrences_by_category':dict(source_count),'repricing_stop_reason':stopped,
        'vehicle_assignment_scope':'EVERY_CURRENT_PHYSICAL_VEHICLE_REVALIDATED',
        'search_metrics_only_columns':sum(bool(c.get('_estimated')) for c in columns.values()),
        'physical_vehicle_column_counts':dict(Counter(c['vehicle_id'] for c in columns.values())),
        'rejected_reason_counts':dict(Counter(reason for e in ledger if e['status']=='REJECTED' for reason in e['reasons']))}


def materialize_selection(data,chosen,incumbent,road,diagnostics):
    """Search metrics never become published evidence; fail back to full proof."""
    try:
        routes=[]
        for column in chosen:
            route=evaluate_route(data,column['vehicle_id'],column['task_ids'],road)
            require(not route['errors'] and compact(route)==compact(column),
                    'Selected search metrics do not match full route materialization')
            require(not route.get('_estimated') and bool(route.get('segments')),'Selected route lacks full road evidence')
            routes.append(route)
        dedicated={i for r in routes for i in r['task_ids']};common=set(data['tasks'])-dedicated
        require(all(data['tasks'][i]['common_unit_price'] is not None for i in common),'Selected allocation omitted a mandatory dedicated task')
        result=assemble(data,routes,common,[]);proof=verified_application_result(data,result,road)
        require(rank(result)<=rank(incumbent),'Joint selection regressed against its validated incumbent')
        return result,proof,diagnostics
    except Exception as exc:
        result=deepcopy(incumbent);proof=verified_application_result(data,result,road)
        diagnostics={**diagnostics,'fallback':True,'fallback_reason':'SELECTED_ROUTE_MATERIALIZATION_REJECTED',
            'materialization_rejection':str(exc),'materialization_rejection_type':type(exc).__name__,
            'scope':'CANDIDATE_POOL_ONLY','candidate_pool_optimal':False,
            'cost_optimal_for_coverage':False,'cost_candidate_pool_optimal':False,'mip_gap':None,'cost_mip_gap':None,
            'distance_optimal_for_coverage_and_cost':False,'vehicle_count_optimal_for_coverage_cost_distance':False,
            'total_fee':result['total_fee'],'selected_count':len(result['routes']),
            'total_meters':sum(r['meters'] for r in result['routes']),
            'unassigned_count':len(result['unassigned']),'improved_incumbent':False}
        return result,proof,diagnostics


def run_merge(case_file,study_file,application_reports,*,legacy_manifest=None,extra_results=(),
              output='cross-run-merged',budget_seconds=300,selection_seconds=30):
    started=time.monotonic();files={};case=load_json(case_file,files)
    ready=verify_source_ready(case['source_ready_file'])
    require(case.get('status')=='PREPARED' and ready['source_hashes']==case['source_hashes'],'Prepared source gate changed')
    helper=ROOT/'scripts/prepare_corrected_application.py'
    require(hashlib.sha256(helper.read_bytes()).hexdigest()==case['verification_script_sha256'],'Prepared helper changed')
    script_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    root=Path(case_file).resolve().parent
    body=load_json(root/'input.json',files);original=load_json(root/'original-input.json',files)
    require(content_hash(body)==case['transition']['corrected_input_hash'] and content_hash(original)==case['transition']['original_input_hash'],'Prepared input changed')
    correction=load_json(case['paths']['correction'],files);study=load_json(study_file,files)
    require(type(budget_seconds) is int and 1<=budget_seconds<=24000 and type(selection_seconds) is int
            and 0<=selection_seconds<=budget_seconds,'Invalid merge time budget')
    deadline=study.get('deadline_epoch')
    require(type(deadline) is int and time.time()+budget_seconds+30<deadline,'Merge exceeds the authorized deadline/validation reserve')
    sources,current=collect_sources(case,study,study_file,legacy_manifest,application_reports,extra_results,files)
    guard=main_guard(case['paths']['database'],body,correction)
    require(guard['guard_token']==case['main_guard']['guard_token'],'Main input or head changed since application preparation')
    target=application_run_output(case_file,study_file,application_reports[0],case,output)
    road=ApplicationFrozenRoad(case['private_graph']);data=normalize(body)
    try:
        require(road.graph_hash==case['private_graph_hash'],'Private corrected graph changed')
        def overall_control():
            if time.monotonic()-started>=budget_seconds or time.time()>=deadline-30:raise StopSearch('MERGE_TIME_BUDGET')
        incumbent=None;incumbent_origin=None;whole_audits=[]
        for source in current:
            candidate,_=full_plan(data,source,road,control=overall_control)
            require(candidate['total_fee']==source['old_total_fee_display_only'],'Current V3 report fee changed under the same input/graph')
            if incumbent is None or rank(candidate)<rank(incumbent):incumbent,incumbent_origin=candidate,source['label']
        initial_fee=incumbent['total_fee'];repairs=[]
        # Extra results may be a newly verified standard-API plan. Validate its
        # complete fixed allocation before spending the finite proposal budget;
        # otherwise late pool exhaustion could discard a known cheaper plan.
        for source in sources:
            if source['category']!='ADDITIONAL_RECIPE_ONLY':continue
            try:
                repaired,removed=repaired_recipe(data,source)
                candidate,_=full_plan(data,repaired,road,control=overall_control)
                record={'source':source['label'],'source_file':source['file'],'category':source['category'],
                    'status':'REPRICED_EXTRA_COMPLETE_BEFORE_POOL','current_fee':candidate['total_fee'],
                    'warehouse_repair_to_common_ids':removed,'became_incumbent':rank(candidate)<rank(incumbent)}
                if record['became_incumbent']:incumbent,incumbent_origin=candidate,source['label']
                repairs.append((repaired,removed));whole_audits.append(record)
            except (ApplicationGuardError,KeyError,TypeError,ValueError,ArithmeticError) as exc:
                whole_audits.append({'source':source['label'],'source_file':source['file'],'category':source['category'],
                    'status':'EXTRA_NOT_USABLE_AS_WHOLE_INCUMBENT','reason':str(exc)})
        for source in sources:
            if source['category'] in {'CURRENT_V3','ADDITIONAL_RECIPE_ONLY'}:continue
            try:
                repaired,removed=repaired_recipe(data,source)
                repairs.append((repaired,removed))
            except (ApplicationGuardError,KeyError,TypeError,ValueError,ArithmeticError) as exc:
                whole_audits.append({'source':source['label'],'status':'NOT_USABLE_AS_WHOLE_INCUMBENT','reason':str(exc)})
        reserve=min(selection_seconds,max(0,budget_seconds-(time.monotonic()-started)))
        reprice_until=started+max(0,budget_seconds-reserve)
        def control():
            if time.monotonic()>=reprice_until or time.time()>=deadline-30:raise StopSearch('REPRICING_TIME_BUDGET')
        columns,ledger,pool_stats=reprice_pool(data,[*sources,*[r for r,_ in repairs]],road,control)
        by_recipe={(r['vehicle_id'],tuple(r['task_ids'])):r for r in columns}
        for source,removed in repairs:
            try:
                rows=[by_recipe[(r['vehicle_id'],tuple(r['task_ids']))] for r in source['recipes']]
                fee=sum((Decimal(r['fee']) for r in rows),Decimal(0))+sum((Decimal(common_fee(
                    data['tasks'][i]['pieces'],data['tasks'][i]['common_unit_price'])) for i in source['common_ids']),Decimal(0))
                record={'source':source['label'],'status':'REPRICED_COMPLETE','current_fee':str(fee),
                        'warehouse_repair_to_common_ids':removed}
                if (0,fee,sum(r['meters'] for r in rows),len(rows))<rank(incumbent):
                    candidate,_=full_plan(data,source,road,control=overall_control)
                    require(all(compact(r)==compact(by_recipe[(r['vehicle_id'],tuple(r['task_ids']))])
                                for r in candidate['routes']),'Whole-plan search metrics differ from full materialization')
                    if rank(candidate)<rank(incumbent):
                        incumbent,incumbent_origin=candidate,source['label'];record['became_incumbent']=True
                whole_audits.append(record)
            except StopSearch as exc:
                whole_audits.append({'source':source['label'],'status':'WHOLE_PLAN_REPLAY_TIME_BUDGET','reason':exc.reason});break
            except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
                whole_audits.append({'source':source['label'],'status':'NO_COMPLETE_CURRENT_ROUTE_PRICES','reason':str(exc)})
        remaining=max(0,min(selection_seconds,budget_seconds-(time.monotonic()-started)))
        try:
            chosen,diagnostics=select_candidate_routes(data,columns,[compact(r) for r in incumbent['routes']],remaining,
                cancelled=lambda:time.time()>=deadline-30)
        except StopSearch as exc:
            chosen=[compact(r) for r in incumbent['routes']]
            diagnostics={'fallback':True,'fallback_reason':exc.reason,'scope':'CANDIDATE_POOL_ONLY'}
        result,proof,diagnostics=materialize_selection(data,chosen,incumbent,road,diagnostics)
        require(graph_digest(road.connection)==case['private_graph_hash'],'Application graph changed during merge')
        for path,sha in files.items():require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,'Source changed during merge: '+path)
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen source gate changed during merge')
        require(hashlib.sha256(helper.read_bytes()).hexdigest()==case['verification_script_sha256'] and
                hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==script_sha,'Application verification scripts changed during merge')
        try:
            after=main_guard(case['paths']['database'],body,correction);matches=after['guard_token']==guard['guard_token']
        except ApplicationGuardError as exc:after={'guard_error':str(exc)};matches=False
        if guard.get('head_fee_summary'):
            compare_manual(result,guard['head_fee_summary'])
            result['comparison'].update(previous_input_hash=guard['head_input_hash'],current_input_hash=content_hash(body),
                input_changed=guard['head_input_hash']!=content_hash(body),source_head_id=guard['head_id'],scope=SCOPE)
        elapsed=round(time.monotonic()-started,4)
        timed_out=bool(pool_stats.get('repricing_stop_reason')) or diagnostics.get('fallback_reason') in {
            'TIME_LIMIT','TIME_BUDGET','WALL_BUDGET','CANCELLED'}
        result['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','elapsed_seconds':elapsed,'selection_scope':SCOPE,
            'stop_reason':'TIME_BUDGET' if timed_out else 'SEARCH_COMPLETED','budget_seconds':budget_seconds,
            'distinct_route_count':len(columns),'road_stats':road.stats(),
            'optimality_proven':False,'global_225_task_optimality_proven':False,'candidate_pool_selection':diagnostics,
            'origin_manifest_hash':content_hash(files),'corrected_input_hash':content_hash(body),
            'measured_graph_hash':road.graph_hash,'incumbent_origin':incumbent_origin,'cross_run_route_pool':pool_stats}
        report={'schema_version':1,'scope':SCOPE,'status':'VALIDATED_READY_FOR_GUARDED_APPEND' if matches else 'MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND',
            'eligible_for_guarded_append':matches,'source_business_database_written':False,'external_calls':0,
            'formal_ranking_unchanged':True,'global_225_task_optimality_proven':False,
            'corrected_input_hash':content_hash(body),'private_graph_hash':road.graph_hash,'case_hash':content_hash(case),
            'verification_script_sha256':case['verification_script_sha256'],'merge_script_sha256':script_sha,
            'source_hashes_unchanged':True,'origin_files':files,'source_plan_count':len(sources),
            'task_count':len(data['tasks']),'pieces':sum(t['pieces'] for t in data['tasks'].values()),
            'proof':proof,'selection_diagnostics':diagnostics,'pool':pool_stats,'whole_plan_audits':whole_audits,
            'before_current_v3_fee':initial_fee,'best_repriced_whole_fee':incumbent['total_fee'],
            'incumbent_origin':incumbent_origin,
            'merged_fee':result['total_fee'],'delta_from_current_v3':str(Decimal(result['total_fee'])-Decimal(initial_fee)),
            'selected_physical_vehicles':[r['vehicle_id'] for r in result['routes']],
            'physical_vehicle_reuse':len(result['routes'])-len({r['vehicle_id'] for r in result['routes']}),
            'elapsed_seconds':elapsed,'budget_seconds':budget_seconds,'selection_seconds':selection_seconds,
            'main_guard_before':guard,'main_guard_after':after,'result_file':str(target/'merged-result.json'),
            'result_hash':content_hash(result),'run_output':str(target)}
        target.mkdir(mode=0o700)
        private_json(target/'source-manifest.json',files);private_json(target/'candidate-repricing.json',ledger)
        private_json(target/'merged-result.json',result);private_json(target/'merge-report.json',report)
        return report
    finally:road.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True);parser.add_argument('--study-summary',required=True)
    parser.add_argument('--application-report',action='append',required=True)
    parser.add_argument('--legacy-manifest');parser.add_argument('--result',action='append',default=[])
    parser.add_argument('--run-output',default='cross-run-merged');parser.add_argument('--budget-seconds',type=int,default=300)
    parser.add_argument('--selection-seconds',type=int,default=30)
    args=parser.parse_args()
    report=run_merge(args.case,args.study_summary,args.application_report,legacy_manifest=args.legacy_manifest,
        extra_results=args.result,output=args.run_output,budget_seconds=args.budget_seconds,selection_seconds=args.selection_seconds)
    print(json.dumps({k:report[k] for k in ['status','scope','task_count','pieces','merged_fee','external_calls','result_file']},ensure_ascii=False))


if __name__=='__main__':main()
