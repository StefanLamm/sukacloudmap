"""Repair fresh-road feasibility in private artifacts, then reprice on the bound case.

This is post-study validation repair, not another formal algorithm comparison.
No provider is called and no business record, bound helper or original graph is written.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from copy import deepcopy
from decimal import Decimal
import hashlib,itertools,json,os,sqlite3,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.planning_inputs import content_hash,encode
from app.route_metrics import evaluate_candidate
from app.routing_engine import assemble,common_fee,compare_manual,evaluate_route,normalize
from scripts.benchmark_algorithms import private_json
from scripts.prepare_corrected_application import ApplicationFrozenRoad,main_guard,require,verified_application_result,verify_source_ready
from scripts.refresh_application_roads import backup_graph,file_sha,fixed_draft,verify_application_origin


def repair(case_file,application_file,failed_file,output='validation-repaired-candidate',budget_seconds=30):
    started=time.monotonic();case_file=Path(case_file).resolve();root=case_file.parent
    read=lambda p:json.loads(Path(p).read_text())
    case=read(case_file);application=read(application_file);failed=read(failed_file)
    body=read(root/'input.json');data=normalize(body);correction=read(case['paths']['correction'])
    study_file=case['latest_input_rebinding']['study_summary_file'];study=read(study_file)
    require(failed['status']=='FAILED' and failed.get('actual_external_calls',0)>0,'A real failed refresh is required')
    require(type(budget_seconds)is int and 1<=budget_seconds<=60,'Invalid validation-repair time budget')
    files={};verify_application_origin(application,case,case_file,study,study_file,files)
    ready=verify_source_ready(case['source_ready_file']);guard=main_guard(case['paths']['database'],body,correction)
    require(guard==case['main_guard'],'Current main changed before repair')
    require(Path(output).name==output and output not in ['.','..'],'Output must be a new private direct child')
    target=root/output;target.mkdir(mode=0o700,exist_ok=False)
    failed_file=Path(failed_file).resolve();source_graph=failed_file.parent/'network-roads.db'
    protected={str(p):file_sha(p) for p in [case_file,Path(application_file).resolve(),failed_file,source_graph,root/'input.json',Path(__file__).resolve()]}
    graph=backup_graph(source_graph,target/'frozen-repair-roads.db')
    with closing(sqlite3.connect(graph)) as db:
        newest=db.execute('SELECT MAX(received_at) FROM metrics').fetchone()[0]
        metadata={'scope':'POST_REFRESH_FEASIBILITY_REPAIR_MIXED_MEASURED_REFERENCE','frozen_at':max(int(time.time()),newest),
            'parent_failed_refresh_report_sha256':file_sha(failed_file),'input_hash':content_hash(body),'network_calls_by_repair':0}
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,encode(v)) for k,v in metadata.items()]);db.commit()
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Private repair graph integrity failed')
    road=ApplicationFrozenRoad(graph);source=read(application['result_file']);draft=fixed_draft(source)
    routes=[evaluate_route(data,r['vehicle_id'],r['task_ids'],road) for r in draft['routes']]
    bad=[r for r in routes if r['errors']];require(len(bad)==1,'Expected exactly one changed infeasible route')
    bad=bad[0];vid=bad['vehicle_id'];ids=list(bad['task_ids']);used={r['vehicle_id'] for r in routes}
    others=[r for r in routes if r['vehicle_id']!=vid];common=set(draft['common_ids'])
    require(all(not r['errors'] for r in others),'Another route also requires repair')
    spare=[v for v,x in data['vehicles'].items() if v not in used and x['available'] and x['warehouse_id']==data['vehicles'][vid]['warehouse_id']]
    memo={};offers={};evaluated=0
    def candidate(vehicle,order):
        nonlocal evaluated
        key=(vehicle,tuple(order))
        if key not in memo:memo[key]=evaluate_candidate(data,vehicle,list(order),road);evaluated+=1
        return memo[key]
    def offer(order,dropped=(),second=None,label='proposal'):
        allocated=[];fee=Decimal(0)
        if order:
            r=candidate(vid,order)
            if r is None or r['errors'] or r['fee'] is None:return
            allocated.append({'vehicle_id':vid,'task_ids':list(order)});fee+=Decimal(r['fee'])
        if second:
            v,order2=second;r=candidate(v,order2)
            if r is None or r['errors'] or r['fee'] is None:return
            allocated.append({'vehicle_id':v,'task_ids':list(order2)});fee+=Decimal(r['fee'])
        if any(data['tasks'][i]['common_unit_price'] is None for i in dropped):return
        fee+=sum((Decimal(common_fee(data['tasks'][i]['pieces'],data['tasks'][i]['common_unit_price'])) for i in dropped),Decimal(0))
        key=(tuple((r['vehicle_id'],tuple(r['task_ids'])) for r in allocated),tuple(sorted(dropped)))
        offers[key]={'routes':allocated,'common':list(dropped),'fee':fee,'label':label}
    late=sorted({e['task_id'] for e in bad['errors'] if e.get('task_id')})
    offer([i for i in ids if i not in late],late,label='all_current_blocking_stores_to_priced_common')
    require(bool(offers),'Guaranteed prefix/common fallback failed')
    for n in [0,1,2]:
        for dropped in itertools.combinations(ids,n):
            keep=[i for i in ids if i not in dropped];offer(keep,dropped,label='same_order_zero_one_two_common')
    for k in range(1,len(ids)):
        for v in spare:offer(ids[:k],second=(v,ids[k:]),label='split_contiguous_to_unused_physical_car')
    for v in spare:
        offer([i for i in ids if i not in late],second=(v,late),label='blocking_stores_to_unused_physical_car')
    depot=data['warehouses'][data['vehicles'][vid]['warehouse_id']]
    seeds=[ids,list(reversed(ids))]
    for metric in ['seconds','meters']:
        remaining=set(ids);origin=depot;order=[]
        while remaining:
            nxt=min(remaining,key=lambda i:(road.known_segment(origin,data['tasks'][i])[metric],i))
            order.append(nxt);remaining.remove(nxt);origin=data['tasks'][nxt]
        seeds.append(order)
    def score(order):
        r=candidate(vid,order)
        if r is None:return (10**9,Decimal('Infinity'),10**9)
        return (len(r['errors']),Decimal(r['seconds']) if r['errors'] else Decimal(r['fee']),r['meters'])
    stop=started+budget_seconds
    for seed in seeds:
        order=seed;current=score(order);offer(order,label='reorder_seed')
        for iteration in range(8):
            best=order;best_score=current
            for i in range(len(order)-1):
                for j in range(i+1,len(order)):
                    if time.monotonic()>=stop:break
                    proposed=order[:i]+list(reversed(order[i:j+1]))+order[j+1:]
                    s=score(proposed);offer(proposed,label='directed_two_opt_validation_repair')
                    if s<best_score:best,best_score=proposed,s
                if time.monotonic()>=stop:break
            if best_score>=current:break
            order,current=best,best_score
            if time.monotonic()>=stop:break
        if time.monotonic()>=stop:break
    require(bool(offers),'No feasible repaired allocation')
    ordered=sorted(offers.values(),key=lambda x:(x['fee'],len(x['routes']),x['label']))
    original=ApplicationFrozenRoad(case['private_graph']);chosen=None;rejected=[]
    try:
        for item in ordered[:50]:
            repaired_draft={'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':r['task_ids']} for r in others]+item['routes'],
                'common_ids':sorted(common|set(item['common'])),'unassigned_ids':[]}
            try:
                actual=assemble(data,others+[evaluate_route(data,r['vehicle_id'],r['task_ids'],road) for r in item['routes']],repaired_draft['common_ids'],[])
                actual_proof=verified_application_result(data,actual,road)
                bound=assemble(data,[evaluate_route(data,r['vehicle_id'],r['task_ids'],original) for r in repaired_draft['routes']],repaired_draft['common_ids'],[])
                bound_proof=verified_application_result(data,bound,original)
            except ValueError as exc:rejected.append({'label':item['label'],'reason':str(exc)});continue
            chosen=(item,repaired_draft,actual,actual_proof,bound,bound_proof);break
        require(chosen is not None,'No candidate also passes the original bound graph')
        item,repaired_draft,actual,actual_proof,bound,bound_proof=chosen
        private_json(target/'repair-recipe.json',repaired_draft);private_json(target/'fresh-graph-repaired-result.json',actual)
        private_json(target/'fresh-graph-repaired-proof.json',actual_proof)
        compare_manual(bound,guard['head_fee_summary']);bound['comparison'].update(source_head_id=guard['head_id'],previous_input_hash=guard['head_input_hash'],current_input_hash=content_hash(body),input_changed=True,scope='LATEST_INPUT_REPRICE_ONLY')
        provenance={'purpose':'POST_FRESH_FEASIBILITY_VALIDATION_REPAIR_NOT_A_NEW_FORMAL_STUDY','producer_script_file':str(Path(__file__).resolve()),
            'producer_script_sha256':file_sha(__file__),'source_latest_report_file':str(Path(application_file).resolve()),'source_latest_report_sha256':file_sha(application_file),
            'failed_refresh_report_file':str(failed_file),'failed_refresh_report_sha256':file_sha(failed_file),'failed_refresh_calls':failed['actual_external_calls'],
            'failed_network_graph_file':str(source_graph),'failed_network_graph_sha256':file_sha(source_graph),
            'private_repair_graph_file':str(graph),'private_repair_graph_hash':road.graph_hash,'private_repair_graph_sha256':file_sha(graph),
            'repaired_vehicle_id':vid,'blocking_task_ids':late,'selected_method':item['label'],'proposal_count':len(offers),'metric_evaluations':evaluated,
            'fresh_graph_fee':actual['total_fee'],'original_bound_graph_fee':bound['total_fee'],'full_fresh_graph_proof_file':str(target/'fresh-graph-repaired-proof.json'),
            'source_rebind_sha_role':'INPUT_AUTHORIZATION_VERIFIER_AND_LINEAGE_NOT_THIS_REPAIR_PRODUCER','old_cp_certificate_invalidated':True,'requires_another_full_selected_road_refresh':True,'external_calls':0,'source_business_database_written':False}
        bound['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','stop_reason':'SEARCH_COMPLETED','selection_scope':'LATEST_INPUT_REPRICE_ONLY',
            'budget_seconds':budget_seconds,'elapsed_seconds':round(time.monotonic()-started,4),'optimality_proven':False,'global_225_task_optimality_proven':False,
            'candidate_pool_optimal':False,'cost_candidate_pool_optimal':False,'finite_pool_cost_optimal':False,'mip_gap':None,'cost_mip_gap':None,'road_stats':original.stats(),'validation_repair':provenance}
        private_json(target/'latest-input-result.json',bound)
        report=deepcopy(application);report.update(result_file=str(target/'latest-input-result.json'),result_hash=content_hash(bound),proof=bound_proof,run_output=str(target),
            producer_script_file=str(Path(__file__).resolve()),producer_script_sha256=file_sha(__file__),validation_repair=provenance,
            main_guard_before=guard,main_guard_after=main_guard(case['paths']['database'],body,correction))
        require(report['main_guard_after']==guard,'Main changed during validation repair')
        for p,expected in protected.items():require(file_sha(p)==expected,'Protected source changed during validation repair: '+p)
        require(verify_source_ready(case['source_ready_file'])['source_hashes']==ready['source_hashes'],'Frozen source changed during repair')
        extra=[target/'repair-recipe.json',target/'fresh-graph-repaired-proof.json',target/'fresh-graph-repaired-result.json',graph]
        report['origin_files_sha256'].update(protected)
        report['origin_files_sha256'].update({str(p.resolve()):file_sha(p) for p in extra})
        verify_application_origin(report,case,case_file,study,study_file,{})
        private_json(target/'latest-input-report.json',report)
        private_json(target/'repair-summary.json',{'status':'VALIDATED_REPAIR_READY_FOR_ANOTHER_LIVE_REFRESH','fresh_graph_fee':actual['total_fee'],'bound_graph_fee':bound['total_fee'],
            'task_count':225,'pieces':4378,'unassigned':0,'method':item['label'],'report_file':str(target/'latest-input-report.json'),'result_file':str(target/'latest-input-result.json'),
            'rejected_after_full_proof':rejected,'proposal_count':len(offers),'metric_evaluations':evaluated,'elapsed_seconds':round(time.monotonic()-started,4),
            'external_calls':0,'source_business_database_written':False,'global_225_task_optimality_proven':False})
        return {'status':report['status'],'fresh_graph_fee':actual['total_fee'],'bound_graph_fee':bound['total_fee'],'method':item['label'],'report_file':str(target/'latest-input-report.json')}
    finally:original.close();road.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--case',required=True);p.add_argument('--application-report',required=True);p.add_argument('--failed-refresh',required=True);p.add_argument('--output',default='validation-repaired-candidate');p.add_argument('--budget-seconds',type=int,default=30)
    a=p.parse_args();print(json.dumps(repair(a.case,a.application_report,a.failed_refresh,a.output,a.budget_seconds),ensure_ascii=False))
