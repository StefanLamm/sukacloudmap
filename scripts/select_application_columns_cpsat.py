"""Offline CP-SAT selection over independently repriced finite route columns.

This separate experiment does not modify the frozen routing implementation or
formal rankings. Its full result is a recipe source for a subsequent merge,
never a direct input to fresh-road publication or business-database append.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from app.planning_inputs import content_hash
from app.route_metrics import evaluate_candidate
from app.routing_engine import StopSearch,common_fee,normalize
from scripts.benchmark_algorithms import graph_digest,private_json
from scripts.merge_application_routes import (compact,full_plan,load_json,materialize_selection,
    order_problem,plan_recipes,rank)
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    application_run_output,main_guard,require,verify_source_ready)
from scripts.prepare_corrected_application import verified_application_result

SCOPE='CP_SAT_EXACT_EQUIVALENCE_FINITE_COLUMN_POOL_ONLY'
MAX_SOLVER_SECONDS=120
MAX_WALL_SECONDS=180
CUTOFF_EPOCH=1794540175


def file_sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def file_stamp(path):
    value=Path(path).stat()
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def guard_database_path(case,override=None):
    original=Path(case['paths']['database']).resolve()
    if override is None:return original
    shadow=Path(override).resolve()
    require(shadow.is_file() and shadow!=original,'Offline shadow override must be a separate existing database')
    return shadow


def verify_m1_origin(report,case,case_file,study_file,files):
    require(report.get('scope')=='CROSS_RUN_VALIDATED_ROUTE_POOL_ONLY' and report.get('formal_ranking_unchanged') is True,
            'Input must be the independently validated M1 merge')
    require(report.get('case_hash')==content_hash(case) and report.get('merge_script_sha256')==file_sha(ROOT/'scripts/merge_application_routes.py'),
            'M1 case or merge helper changed')
    origins=report.get('origin_files');require(isinstance(origins,dict) and bool(origins),'M1 source provenance is missing')
    root=Path(case_file).resolve().parent
    require({str(Path(case_file).resolve()),str(Path(study_file).resolve()),str(root/'input.json'),
             str(root/'original-input.json')}.issubset(origins),'M1 critical source provenance is incomplete')
    for path,sha in origins.items():
        require(file_sha(path)==sha and files.setdefault(path,sha)==sha,'M1 original source changed')


def save_unique_json(path,value):
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'w') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False);stream.write('\n')
        stream.flush();os.fsync(stream.fileno())


def atomic_private_json(path,value):
    path=Path(path);temporary=path.with_name('.checkpoint-'+str(os.getpid())+'-'+str(time.time_ns())+'.tmp')
    try:
        save_unique_json(temporary,value);os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def worker_environment(environment):
    allowed={'PATH','HOME','LANG','LC_ALL','LC_CTYPE','TMPDIR','TZ','SYSTEMROOT','VIRTUAL_ENV',
             'LD_LIBRARY_PATH','DYLD_LIBRARY_PATH','PYTHONNOUSERSITE','PYTHONDONTWRITEBYTECODE'}
    return {**{key:value for key,value in environment.items() if key in allowed},
        'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1',
        'VECLIB_MAXIMUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1'}


def hard_wall_watchdog(done,owner,seconds):
    if done.wait(seconds):return
    process=owner.get('process')
    if process is not None and process.poll() is None:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    os._exit(124)  # Standalone supervisor; any proved checkpoint is already durable.


def cents(value):
    amount=Decimal(str(value))
    require(amount.is_finite() and amount>=0 and amount==amount.quantize(Decimal('.01')),'Price is not exact nonnegative cents')
    return int(amount*100)


def extract_orders(ledger,sources,data):
    """Read identities/order only; every prior economic field is discarded."""
    orders={};rejected=Counter()
    for number,row in enumerate(ledger):
        if row.get('status')!='REPRICED_CURRENT_V3':continue
        ids=row.get('task_ids');problem=order_problem(data,ids)
        if problem:rejected[problem]+=1;continue
        orders.setdefault(tuple(ids),[]).append({'kind':'M1_LEDGER_RECIPE','index':number,
            'source_vehicle_id':row.get('source_vehicle_id',row.get('vehicle_id'))})
    for source in sources:
        for number,row in enumerate(source['recipes']):
            ids=row.get('task_ids');problem=order_problem(data,ids)
            if problem:rejected[problem]+=1;continue
            orders.setdefault(tuple(ids),[]).append({'kind':source['category'],'source':source['label'],
                'source_file':source['file'],'index':number,'source_vehicle_id':row.get('vehicle_id')})
    return orders,dict(rejected)


def reprice_vehicle_columns(data,orders,road,control):
    columns={vid:{} for vid,v in data['vehicles'].items() if v['available']}
    rejected=Counter();evaluated=0
    for ids in orders:
        for vid in columns:
            control();evaluated+=1
            row=evaluate_candidate(data,vid,list(ids),road,control)
            if row is None:rejected['MEASURED_ROUTE_METRICS_MISSING']+=1;continue
            if row['errors'] or row['fee'] is None:
                rejected.update(e['code'] for e in row['errors'])
                if not row['errors']:rejected['UNPRICED_ROUTE']+=1
                continue
            columns[vid][ids]=compact(row,search=True)
    return columns,{'vehicle_order_evaluations':evaluated,'column_count':sum(map(len,columns.values())),
        'available_physical_vehicles':len(columns),'unavailable_physical_vehicles':len(data['vehicles'])-len(columns),
        'rejected_reason_counts':dict(rejected)}


def pattern_key(row):
    return (tuple(row['task_ids']),cents(row['fee']),row['meters'],row['seconds'],row['pieces'])


def exact_vehicle_classes(data,columns):
    """Merge only cars with the complete same independently validated column set."""
    groups={};vehicle_sets={}
    for vid,rows in columns.items():
        require(data['vehicles'][vid]['available'],'Unavailable car entered the column model')
        signatures=tuple(sorted(pattern_key(row) for row in rows.values()))
        vehicle_sets[vid]=content_hash(signatures)
        key=(data['vehicles'][vid]['warehouse_id'],signatures)
        groups.setdefault(key,[]).append(vid)
    classes=[]
    for number,((warehouse,signatures),members) in enumerate(sorted(groups.items())):
        members=sorted(members);representative=members[0]
        patterns=[deepcopy(columns[representative][signature[0]]) for signature in signatures]
        for member in members:
            require(tuple(sorted(pattern_key(r) for r in columns[member].values()))==signatures,
                    'Vehicle equivalence omitted or changed a column')
        classes.append({'id':number,'warehouse_id':warehouse,'members':members,'patterns':patterns,
                        'complete_column_set_hash':content_hash(signatures)})
    return classes,vehicle_sets


def solve_columns(data,classes,incumbent=None,*,solver_seconds=120,seed=0,solver_factory=None):
    from ortools.sat.python import cp_model
    require(0<=solver_seconds<=MAX_SOLVER_SECONDS,'Invalid CP-SAT time budget')
    model=cp_model.CpModel();variables={};task_vars={i:[] for i in data['tasks']};objective=[]
    common_vars={};incumbent_hints=set();common_hint=set()
    vehicle_class={vid:c for c in classes for vid in c['members']}
    if incumbent is not None:
        for route in incumbent['routes']:
            require(route['vehicle_id'] in vehicle_class,'Incumbent physical car has no validated column set')
            group=vehicle_class[route['vehicle_id']];key=pattern_key(route)
            matches=[n for n,row in enumerate(group['patterns']) if pattern_key(row)==key]
            require(len(matches)==1,'Incumbent route is absent from its exact class')
            incumbent_hints.add((group['id'],matches[0]))
        common_hint={r['task_id'] for r in incumbent['common']}
    for group in classes:
        class_vars=[]
        for index,row in enumerate(group['patterns']):
            variable=model.new_bool_var('r_'+str(group['id'])+'_'+str(index))
            variables[group['id'],index]=variable;class_vars.append(variable)
            for task_id in row['task_ids']:task_vars[task_id].append(variable)
            objective.append(cents(row['fee'])*variable)
            if incumbent is not None:model.add_hint(variable,int((group['id'],index) in incumbent_hints))
        model.add(sum(class_vars)<=len(group['members']))
    for task_id,task in data['tasks'].items():
        if task['common_unit_price'] is not None:
            variable=model.new_bool_var('common_'+task_id);common_vars[task_id]=variable
            task_vars[task_id].append(variable)
            objective.append(cents(common_fee(task['pieces'],task['common_unit_price']))*variable)
            if incumbent is not None:model.add_hint(variable,int(task_id in common_hint))
        model.add(sum(task_vars[task_id])==1)
    total=sum(objective)
    if incumbent is not None:model.add(total<=cents(incumbent['total_fee']))
    model.minimize(total)
    error=model.validate()
    require(not error,'CP-SAT finite column model is invalid: '+error)
    solver=solver_factory() if solver_factory is not None else cp_model.CpSolver()
    solver.parameters.max_time_in_seconds=float(solver_seconds)
    solver.parameters.num_search_workers=1;solver.parameters.random_seed=seed
    status=solver.solve(model);name=solver.status_name(status)
    diagnostics={'solver':'OR_TOOLS_CP_SAT','status':name,'max_time_seconds':solver_seconds,
        'num_search_workers':1,'finite_pool_cost_optimal':False,'global_225_task_optimality_proven':False,
        'scope':SCOPE,'class_count':len(classes),'physical_vehicle_count':len(vehicle_class),
        'pattern_variables':len(variables),'common_variables':len(common_vars),'objective_units':'ACTUAL_INTEGER_CENTS',
        'incumbent_upper_bound_cents':cents(incumbent['total_fee']) if incumbent is not None else None}
    if status not in {cp_model.OPTIMAL,cp_model.FEASIBLE}:return None,diagnostics
    chosen=[]
    for group in classes:
        selected=[r for n,r in enumerate(group['patterns']) if solver.boolean_value(variables[group['id'],n])]
        require(len(selected)<=len(group['members']),'CP-SAT exceeded a physical equivalence-class fleet')
        for vid,row in zip(group['members'],selected):
            chosen.append({**deepcopy(row),'vehicle_id':vid})
    selected_common={i for i,v in common_vars.items() if solver.boolean_value(v)}
    assigned=[i for row in chosen for i in row['task_ids']]
    require(len(assigned)==len(set(assigned)) and set(assigned).isdisjoint(selected_common) and
            set(assigned)|selected_common==set(data['tasks']),'Decoded CP-SAT allocation has invalid coverage')
    fee=sum(cents(r['fee']) for r in chosen)+sum(cents(common_fee(data['tasks'][i]['pieces'],
        data['tasks'][i]['common_unit_price'])) for i in selected_common)
    require(incumbent is None or fee<=cents(incumbent['total_fee']),'Decoded model regressed against its verified upper bound')
    diagnostics.update(integer_objective_cents=fee,selected_common_ids=sorted(selected_common),
        finite_pool_cost_optimal=status==cp_model.OPTIMAL,solver_objective_value=solver.objective_value,
        solver_best_bound=solver.best_objective_bound,solver_wall_seconds=solver.wall_time)
    return chosen,diagnostics


def select_and_materialize(data,columns,incumbent,road,solver_seconds=120,seed=0,solver_factory=None):
    classes,vehicle_sets=exact_vehicle_classes(data,columns)
    try:
        chosen,diagnostics=solve_columns(data,classes,incumbent,solver_seconds=solver_seconds,seed=seed,solver_factory=solver_factory)
    except Exception as exc:
        chosen=None;diagnostics={'solver':'OR_TOOLS_CP_SAT','status':'REJECTED_MODEL_OR_RESULT',
            'failure_type':type(exc).__name__,'finite_pool_cost_optimal':False,'scope':SCOPE}
    if chosen is None:
        chosen=[compact(r) for r in incumbent['routes']]
        diagnostics.update(fallback=True,fallback_reason='NO_VALID_CP_SAT_SOLUTION')
    claim=bool(diagnostics.get('finite_pool_cost_optimal'))
    result,proof,materialization=materialize_selection(data,chosen,incumbent,road,{
        'scope':'CANDIDATE_POOL_ONLY','cost_candidate_pool_optimal':claim,'candidate_pool_optimal':claim,
        'fallback':bool(diagnostics.get('fallback'))})
    if materialization.get('fallback') and materialization.get('fallback_reason')=='SELECTED_ROUTE_MATERIALIZATION_REJECTED':
        diagnostics.update(fallback=True,fallback_reason=materialization['fallback_reason'],
                           finite_pool_cost_optimal=False,materialization_rejection=materialization.get('materialization_rejection'))
    require(rank(result)<=rank(incumbent),'Final CP-SAT result regressed against its verified incumbent')
    diagnostics['finite_pool_cost_optimal']=bool(diagnostics.get('finite_pool_cost_optimal')) and not diagnostics.get('fallback',False)
    return result,proof,diagnostics,classes,vehicle_sets


def worker(spec):
    started=time.monotonic();files={};case=load_json(spec['case'],files);study=load_json(spec['study_summary'],files)
    ready=verify_source_ready(case['source_ready_file']);require(ready['source_hashes']==case['source_hashes'],'Prepared source gate changed')
    require(content_hash(load_json(case['source_ready_file'],files))==case['source_ready_hash'],'Full source-ready artifact changed')
    require(file_sha(ROOT/'scripts/prepare_corrected_application.py')==case['verification_script_sha256'],'Prepared helper changed')
    require(file_sha(__file__)==spec['script_sha256'],'New CP-SAT helper changed before worker import')
    body=load_json(Path(spec['case']).parent/'input.json',files);original=load_json(Path(spec['case']).parent/'original-input.json',files)
    correction=load_json(case['paths']['correction'],files);report=load_json(spec['merge_report'],files)
    require(content_hash(body)==case['transition']['corrected_input_hash'] and content_hash(original)==case['transition']['original_input_hash'],'Prepared inputs changed')
    require(report['corrected_input_hash']==content_hash(body) and report['private_graph_hash']==case['private_graph_hash'] and
            report['eligible_for_guarded_append'] and report['external_calls']==0 and not report['source_business_database_written'],
            'M1 result is not a complete private corrected candidate')
    raw=load_json(report['result_file'],files);require(content_hash(raw)==report['result_hash'],'M1 result changed')
    sources=[plan_recipes(raw,report['result_file'],'M1','CURRENT_V3')]
    guard_database=guard_database_path(case,spec.get('guard_database_override'))
    guard=main_guard(guard_database,body,correction);require(guard==case['main_guard'],'Main state changed before CP-SAT experiment')
    source_hashes={**ready['source_hashes'],**{name:file_sha(ROOT/name) for name in [
        'scripts/select_application_columns_cpsat.py','scripts/merge_application_routes.py',
        'scripts/prepare_corrected_application.py']}}
    target=Path(spec['output']);data=normalize(body);graph_path=Path(case['private_graph']);graph_stamp=file_stamp(graph_path)
    require(target.resolve().parent==Path(spec['case']).resolve().parent and target.resolve()!=Path(report['run_output']).resolve(),
            'CP-SAT worker output must be a separate private case child')
    require(not any((target/name).exists() for name in ['incumbent-checkpoint.json',
            'cpsat-result.json','cpsat-report.json','exact-vehicle-classes.json']),'CP-SAT worker refuses existing artifacts')
    require(not list(target.glob('incumbent-*-result.json')),'CP-SAT worker refuses existing incumbent results')
    def control():
        if time.monotonic()-started>=spec['worker_wall_seconds']-20 or time.time()>=spec['cutoff_epoch']-20:
            raise StopSearch('COLUMN_PREPARATION_WALL_BUDGET')
    road=ApplicationFrozenRoad(graph_path)
    try:
        require(road.graph_hash==case['private_graph_hash'],'CP-SAT used another corrected graph')
        incumbent,_=full_plan(data,sources[0],road,control=control);origin='M1';extra_audits=[]
        checkpoint_index=0
        def checkpoint():
            nonlocal checkpoint_index
            require(len(data['tasks'])==225 and sum(t['pieces'] for t in data['tasks'].values())==4378,'CP-SAT input coverage differs')
            proof=verified_application_result(data,incumbent,road)
            for path,sha in files.items():require(file_sha(path)==sha,'Source artifact changed before incumbent checkpoint')
            for path,sha in source_hashes.items():require(file_sha(ROOT/path)==sha,'Source helper changed before incumbent checkpoint')
            result_path=target/('incumbent-'+str(checkpoint_index).zfill(4)+'-result.json');checkpoint_index+=1
            save_unique_json(result_path,incumbent)
            value={'status':'PROVED_INCUMBENT_CHECKPOINT','scope':SCOPE,'proof':proof,
                'task_count':225,'pieces':4378,'incumbent_fee':incumbent['total_fee'],
                'result_file':str(result_path),'result_hash':content_hash(incumbent),'result_file_sha256':file_sha(result_path),
                'incumbent_origin':origin,'main_guard_before':guard,'graph_hash':road.graph_hash,'graph_stamp':graph_stamp,
                'source_files_sha256':files,'source_hashes':source_hashes,'source_business_database_written':False,'external_calls':0,
                'artifact_stamps':{path:file_stamp(path) for path in [*files,*[str(ROOT/p) for p in source_hashes]]},
                'eligible_for_direct_main_write':False,'global_225_task_optimality_proven':False}
            atomic_private_json(target/'incumbent-checkpoint.json',value)
        checkpoint()  # Durable, full-proof fallback before provenance expansion or large-ledger parsing.
        verify_m1_origin(report,case,spec['case'],spec['study_summary'],files)
        for number,path in enumerate(spec['results']):
            control();source=plan_recipes(load_json(path,files),path,'extra:'+str(number),'EXTRA_RECIPE_ONLY');sources.append(source)
            try:
                candidate,removed=full_plan(data,source,road,repair=True,control=control)
                improved=rank(candidate)<rank(incumbent)
                extra_audits.append({'source':source['label'],'current_fee':candidate['total_fee'],
                                     'status':'FULLY_REPRICED_AND_PROVED','warehouse_repair_to_common_ids':removed,'improved':improved})
            except (ApplicationGuardError,KeyError,TypeError,ValueError,ArithmeticError) as exc:
                extra_audits.append({'source':source['label'],'status':'RECIPE_ONLY_NOT_WHOLE_INCUMBENT','reason':str(exc)})
                continue
            if improved:incumbent,origin=candidate,source['label'];checkpoint()
        control();ledger=load_json(spec['ledger'],files);require(isinstance(ledger,list),'Candidate ledger is not an array')
        orders,rejections=extract_orders(ledger,sources,data)
        # Preserve the verified incumbent's orders even if no source ledger row exists.
        for row in incumbent['routes']:orders.setdefault(tuple(row['task_ids']),[]).append({'kind':'VERIFIED_INCUMBENT'})
        columns,stats=reprice_vehicle_columns(data,orders,road,control)
        seconds=max(0,min(spec['solver_seconds'],spec['worker_wall_seconds']-(time.monotonic()-started)-20))
        result,proof,diagnostics,classes,vehicle_sets=select_and_materialize(data,columns,incumbent,road,seconds,spec['seed'])
        require(len(data['tasks'])==225 and sum(t['pieces'] for t in data['tasks'].values())==4378,'CP-SAT input coverage differs')
        require(graph_digest(road.connection)==case['private_graph_hash'] and file_stamp(graph_path)==graph_stamp,'Frozen corrected graph changed')
        for path,sha in files.items():require(file_sha(path)==sha,'Source artifact changed during CP-SAT selection')
        for path,sha in source_hashes.items():require(file_sha(ROOT/path)==sha,'A source helper changed during CP-SAT selection')
        after=main_guard(guard_database,body,correction);require(after==guard,'Main state changed during CP-SAT selection')
        result['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','stop_reason':'SEARCH_COMPLETED' if diagnostics.get('finite_pool_cost_optimal') else 'TIME_BUDGET',
            'selection_scope':SCOPE,'elapsed_seconds':round(time.monotonic()-started,4),'budget_seconds':spec['solver_seconds'],
            'distinct_route_count':stats['column_count'],'road_stats':road.stats(),'optimality_proven':False,
            'global_225_task_optimality_proven':False,'finite_pool_selection':diagnostics}
        output={'schema_version':1,'status':'VALIDATED_FINITE_COLUMN_RESULT','scope':SCOPE,'case_file':spec['case'],
            'case_hash':content_hash(case),'study_summary_hash':content_hash(study),'merge_report_file':spec['merge_report'],
            'merge_report_hash':content_hash(report),'corrected_input_hash':content_hash(body),'graph_hash':road.graph_hash,
            'source_files_sha256':files,'source_hashes':source_hashes,'script_sha256':spec['script_sha256'],
            'offline_guard_database_override':spec.get('guard_database_override'),
            'input_semantics':'OLD_V3_OFFLINE_SHADOW_ONLY' if spec.get('guard_database_override') else 'BOUND_CURRENT_CASE',
            'main_guard_before':guard,'main_guard_after':after,'proof':proof,'task_count':225,'pieces':4378,
            'external_calls':0,'source_business_database_written':False,'eligible_for_direct_main_write':False,
            'global_225_task_optimality_proven':False,'finite_pool_cost_optimal':diagnostics.get('finite_pool_cost_optimal',False),
            'incumbent_origin':origin,'incumbent_fee':incumbent['total_fee'],'selected_fee':result['total_fee'],
            'delta_from_incumbent':str(Decimal(result['total_fee'])-Decimal(incumbent['total_fee'])),
            'extra_full_plan_audits':extra_audits,'order_count':len(orders),'order_rejections':rejections,
            'repricing':stats,'solver_diagnostics':diagnostics,'equivalence_class_count':len(classes),
            'vehicle_column_set_hashes':vehicle_sets,'result_file':str(target/'cpsat-result.json'),
            'result_hash':content_hash(result),'raw_result_use':'ONLY_AS_M2_RESULT_RECIPE_REPRICE_CURRENT_INPUT_AGAIN',
            'elapsed_seconds':round(time.monotonic()-started,4)}
        private_json(target/'exact-vehicle-classes.json',classes);private_json(target/'cpsat-result.json',result)
        private_json(target/'cpsat-report.json',output);return output
    finally:road.close()


def stop_process(process):
    if process is None:return
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=1)


def install_parent_stop_handlers(stopping):
    previous={sig:signal.getsignal(sig) for sig in (signal.SIGTERM,signal.SIGINT)}
    def request_stop(*_):stopping[0]=True
    for sig in previous:signal.signal(sig,request_stop)
    return previous


def _run_parent(args,started,owner):
    case_file=Path(args.case).resolve();study_file=Path(args.study_summary).resolve()
    case=json.loads(case_file.read_text());study=json.loads(study_file.read_text())
    require(study.get('status')=='COMPLETED','Formal study must finish before a separate column experiment')
    require(type(args.wall_seconds) is int and 30<=args.wall_seconds<=MAX_WALL_SECONDS and
            0<=args.solver_seconds<=MAX_SOLVER_SECONDS and type(args.seed) is int and 0<=args.seed<2**31,'Invalid independent experiment budget')
    cutoff=min(args.cutoff_epoch,study['deadline_epoch']-2700)
    require(time.time()+args.wall_seconds<cutoff,'Not enough pre-deployment cutoff reserve')
    report_file=Path(args.merge_report).resolve();report=json.loads(report_file.read_text())
    target=application_run_output(case_file,study_file,report_file,case,args.run_output);target.mkdir(mode=0o700)
    ledger=Path(args.candidate_ledger).resolve() if args.candidate_ledger else Path(report['run_output'])/'candidate-repricing.json'
    spec={'case':str(case_file),'study_summary':str(study_file),'merge_report':str(report_file),'ledger':str(ledger),
        'results':[str(Path(p).resolve()) for p in args.result],'output':str(target),'solver_seconds':args.solver_seconds,
        'worker_wall_seconds':args.wall_seconds-10,'cutoff_epoch':cutoff,'seed':args.seed,'script_sha256':file_sha(__file__),
        'guard_database_override':str(Path(args.guard_database_override).resolve()) if args.guard_database_override else None}
    private_json(target/'run-spec.json',spec);process=None;reason=None;stopping=[False]
    previous_handlers=install_parent_stop_handlers(stopping)
    try:
        with (target/'worker.log').open('x') as stream:
            os.chmod(target/'worker.log',0o600)
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker-spec',str(target/'run-spec.json')],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,
                env=worker_environment(os.environ))
            owner['process']=process
            while process.poll() is None:
                if stopping[0]:reason='PARENT_CANCELLED';break
                if time.monotonic()-started>=args.wall_seconds-15 or time.time()>=cutoff-15:
                    reason='PARENT_WALL_OR_CUTOFF_LIMIT';break
                time.sleep(.1)
            if reason is None and process.returncode!=0:reason='WORKER_FAILED_OR_PREPARATION_BUDGET'
    finally:
        try:stop_process(process)
        finally:
            for sig,handler in previous_handlers.items():signal.signal(sig,handler)
    complete=target/'cpsat-report.json'
    if reason is None and complete.is_file():
        output=json.loads(complete.read_text());output.update(parent_elapsed_seconds=round(time.monotonic()-started,4),
            parent_wall_limit_seconds=args.wall_seconds,cutoff_epoch=cutoff,owned_worker_stopped=True)
        private_json(complete,output);return output
    checkpoint=target/'incumbent-checkpoint.json'
    require(checkpoint.is_file(),'No complete proved incumbent checkpoint; use the unchanged M1/API source plans')
    output=json.loads(checkpoint.read_text());require(file_sha(__file__)==spec['script_sha256'],'Helper changed after fallback checkpoint')
    require(output['task_count']==225 and output['pieces']==4378 and output['proof']['audit']['valid'] and
            output['proof']['audit']['unassigned_tasks']==0,'Fallback checkpoint is incomplete')
    for path,stamp in output['artifact_stamps'].items():
        require(file_stamp(path)==tuple(stamp),'A captured source changed after checkpoint')
    result_path=Path(output['result_file'])
    require(file_sha(result_path)==output['result_file_sha256'],'Fallback incumbent bytes changed after proof')
    for path,sha in output['source_hashes'].items():require(file_sha(ROOT/path)==sha,'Source helper changed after checkpoint')
    require(file_stamp(case['private_graph'])==tuple(output['graph_stamp']),'Frozen graph changed after checkpoint')
    body=json.loads((case_file.parent/'input.json').read_text());correction=json.loads(Path(case['paths']['correction']).read_text())
    after=main_guard(guard_database_path(case,spec.get('guard_database_override')),body,correction)
    require(after==output['main_guard_before'],'Main state changed after worker fallback')
    output.update(status='VALIDATED_INCUMBENT_FALLBACK',fallback_reason=reason or 'NO_FINAL_CP_SAT_REPORT',
        main_guard_after=after,finite_pool_cost_optimal=False,selected_fee=output['incumbent_fee'],
        raw_result_use='ONLY_AS_M2_RESULT_RECIPE_REPRICE_CURRENT_INPUT_AGAIN',elapsed_seconds=round(time.monotonic()-started,4),
        parent_elapsed_seconds=round(time.monotonic()-started,4),parent_wall_limit_seconds=args.wall_seconds,
        cutoff_epoch=cutoff,owned_worker_stopped=True)
    output.update(offline_guard_database_override=spec.get('guard_database_override'),
                  input_semantics='OLD_V3_OFFLINE_SHADOW_ONLY' if spec.get('guard_database_override') else 'BOUND_CURRENT_CASE')
    private_json(target/'cpsat-report.json',output);return output


def run_parent(args):
    started=time.monotonic();require(30<=args.wall_seconds<=MAX_WALL_SECONDS,'Invalid parent wall budget')
    owner={};done=threading.Event()
    watchdog=threading.Thread(target=hard_wall_watchdog,args=(done,owner,args.wall_seconds),daemon=True)
    watchdog.start()
    try:return _run_parent(args,started,owner)
    finally:done.set()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker-spec')
    parser.add_argument('--case');parser.add_argument('--study-summary');parser.add_argument('--merge-report')
    parser.add_argument('--candidate-ledger');parser.add_argument('--result',action='append',default=[])
    parser.add_argument('--guard-database-override',help='Explicit separate old-input shadow; output is recipe-only, never current-main eligible')
    parser.add_argument('--run-output',default='column-cpsat-selection');parser.add_argument('--solver-seconds',type=int,default=120)
    parser.add_argument('--wall-seconds',type=int,default=180);parser.add_argument('--cutoff-epoch',type=int,default=CUTOFF_EPOCH)
    parser.add_argument('--seed',type=int,default=0);args=parser.parse_args()
    if args.worker_spec:report=worker(json.loads(Path(args.worker_spec).read_text()))
    else:
        require(args.case and args.study_summary and args.merge_report,'case/study-summary/merge-report are required')
        report=run_parent(args)
    print(json.dumps({k:report.get(k) for k in ['status','scope','selected_fee','finite_pool_cost_optimal','result_file','external_calls']},ensure_ascii=False))


if __name__=='__main__':main()
