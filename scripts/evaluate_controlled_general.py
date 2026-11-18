"""Prelocked controlled fixtures: preparation, independent oracle, serial pairs.

Nothing runs on import. prepare performs no optimization, fee evaluation or
HTTP. run is an explicit, serial subprocess experiment; every returned plan is
audited against pinned input and roads by this evaluator. Synthetic roads are
marked FIXTURE and are never represented as AMap measurements.
"""
from __future__ import annotations

import argparse
from collections import Counter,defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal,ROUND_HALF_UP
import hashlib
import importlib
import inspect
from itertools import permutations
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.evaluate_delivery_stages import (apply_road_execution_policy,
    road_execution_policy,road_execution_state)
FORMULA='Within warehouse cluster: meters=1000+abs(store_ordinal_from-store_ordinal_to)*25, seconds=300+abs(ordinal_from-ordinal_to)*5; warehouse ordinal=-1; directed metrics same formula. Cross-warehouse routes forbidden by warehouse constraint. No return leg.'
THREAD_ENV={key:'1' for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','VECLIB_MAXIMUM_THREADS','NUMEXPR_NUM_THREADS')}


def load(path):return json.loads(Path(path).read_text())
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def money(value):return str(Decimal(value).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
def cents(value):return int(Decimal(money(value))*100)


def save(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary,0o600);json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
    temporary.replace(path)


def source_identity(root):
    root=Path(root).resolve();files={str(p.relative_to(root)):sha(p) for p in sorted((root/'app').rglob('*.py'))}
    if not files:raise ValueError('Source has no app Python inventory')
    return {'root':str(root),'app_hash':digest(files),'files':files}


class FormulaRoad:
    """The exact prelocked synthetic matrix, derived from input row order."""
    call_limit=0;io_seconds=0.;force_refresh=False;allow_historical=False
    def __init__(self,body):
        policy=body.get('road_fixture',{})
        if policy.get('scope')!='SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE' or policy.get('rule')!=FORMULA or policy.get('formula_locked') is not True:
            raise ValueError('Unrecognized synthetic formula; refusing to invent roads')
        self.input_hash=digest(body);self.formula_hash=digest(policy);self.points={};counts=Counter();self.reads=self.hits=0
        for warehouse in body['warehouses']:self.points[warehouse['id']]=(warehouse['id'],-1)
        for task in body['tasks']:
            wid=task['warehouse_id'];self.points[task['id']]=(wid,counts[wid]);counts[wid]+=1
        self.graph_hash=digest({'formula_hash':self.formula_hash,'point_ordinals':self.points})
    def known_segment(self,a,b):
        a=self.points.get(a if isinstance(a,str) else a['id']);b=self.points.get(b if isinstance(b,str) else b['id'])
        if not a or not b or a[0]!=b[0] or b[1]<0:return None
        distance=abs(a[1]-b[1]);return {'meters':1000+distance*25,'seconds':300+distance*5}
    def segment(self,vehicle,a,b,departure):
        from app.road_service import evidence,road_request
        from app.routing_engine import RoadError
        metric=self.known_segment(a,b)
        if metric is None:raise RoadError('FIXTURE_WAREHOUSE','Synthetic matrix has no cross-warehouse or return edge')
        self.reads+=1;request=road_request(vehicle,a,b,departure,'FIXTURE')
        request.update(fixture_id='CONTROLLED_PRELOCKED_MATRIX',input_hash=self.input_hash,formula_hash=self.formula_hash)
        return evidence(request,metric['meters'],metric['seconds'],'FIXTURE',False,
                        restriction='VERIFIED_FIXTURE_ONLY',notice='预锁定合成矩阵，不是真实道路证据')
    def stats(self):return {'source':'FIXTURE','calls':0,'call_limit':0,'fixture_reads':self.reads,'graph_hash':self.graph_hash}
    def close(self):pass


def road_for(body,graph):
    if body.get('road_fixture'):return FormulaRoad(body)
    from scripts.benchmark_algorithms import FrozenEvidenceRoad
    road=FrozenEvidenceRoad(graph);road.route_seed_samples={}
    return road


def controlled_execution_policy(enabled,case):
    synthetic=case.get('road_scope')=='SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE'
    return road_execution_policy(enabled,adapter='FormulaRoad' if synthetic else 'FrozenEvidenceRoad',
        evidence_scope='SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE' if synthetic else 'OFFLINE_FROZEN_DRIVING_REFERENCE')


def prepare(prelock_file,measured_graph,output):
    """Check all locked inputs and every small compatible directed road."""
    from app.routing_engine import normalize
    prelock_file=Path(prelock_file).resolve();prelock=load(prelock_file);cases=[]
    for entry in prelock['cases']:
        body=load(entry['input_file'])
        if sha(entry['input_file'])!=entry['file_sha256'] or digest(body)!=entry['input_hash']:
            raise ValueError('Prelocked controlled input changed')
        data=normalize(body);road=road_for(body,measured_graph);missing=[]
        try:
            if (len(data['tasks'])!=entry['tasks'] or sum(t['pieces'] for t in data['tasks'].values())!=entry['pieces'] or
                sum(bool(v['available']) for v in data['vehicles'].values())!=entry['available_vehicles']):
                raise ValueError('Controlled quantities, task count or actual available fleet differs from prelock')
            if entry.get('warehouses') is not None and len(data['warehouses'])!=entry['warehouses']:
                raise ValueError('Controlled warehouse count differs from prelock')
            if not body.get('road_fixture'):
                for target in data['tasks'].values():
                    if target['signature'] is None:continue
                    origins=[data['warehouses'][target['warehouse_id']],*[t for t in data['tasks'].values()
                        if t['id']!=target['id'] and t['warehouse_id']==target['warehouse_id'] and t['signature']==target['signature']]]
                    for origin in origins:
                        if road.known_segment(origin,target) is None:missing.append([origin['id'],target['id']])
            elif body['road_fixture']!=entry['road_policy']:raise ValueError('Scale formula differs from prelock')
            cases.append({**entry,'road_graph':str(Path(measured_graph).resolve()) if not body.get('road_fixture') else None,
                'graph_hash':road.graph_hash,'missing_required_directions':missing,
                'road_scope':'SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE' if body.get('road_fixture') else 'FIXED_MEASURED_GRAPH',
                'history_hints_disabled':True})
        finally:road.close()
    report={'schema_version':1,'scope':'SUPPLEMENTAL_CONTROLLED_NOT_CORE_REAL_DAY_ACCEPTANCE',
        'prelock_file':str(prelock_file),'prelock_sha256':sha(prelock_file),'cases':cases,
        'status':'READY' if not any(c['missing_required_directions'] for c in cases) else 'MISSING_MEASURED_ROADS',
        'solver_runs':0,'fees_or_results_read':False,'external_calls':0,'source_business_database_written':False}
    output=Path(output).resolve()
    if output.exists():raise ValueError('Require a new preparation file')
    save(output,report);return report


def signature(quote):
    if not quote:return None
    keys=('mode','per_km') if quote['mode'].startswith('KM') else ('mode','base_price','included_stops','extra_stop_price')
    value={key:quote[key] if key=='mode' else str(Decimal(str(quote[key])).normalize()) for key in keys}
    if quote['mode'].endswith('_REGION'):
        rule=quote.get('region_surcharge') or {}
        value['region_surcharge']={'amount':str(Decimal(rule.get('amount','0')).normalize()),
            'billing_mode':rule.get('billing_mode','ONCE'),'targets':sorted(rule.get('targets',[]),key=lambda row:json.dumps(row,sort_keys=True))}
    return digest(value)


def region_charge(quote,tasks):
    """Independent exact matcher for the fully coded controlled fixtures.

    Unsupported incomplete geography refuses a certificate. Production alias
    inference is deliberately not reused as an independent oracle assumption.
    """
    if not quote['mode'].endswith('_REGION'):return Decimal(0)
    rule=quote.get('region_surcharge') or {};targets=rule.get('targets',[])
    if not targets:return Decimal(0)
    matched=[]
    for task in tasks:
        code=task.get('region_code') or ''
        if len(code)!=6 or not code.isdigit() or code.endswith('00'):
            raise ValueError('INDEPENDENT_ORACLE_REQUIRES_FULL_COUNTY_CODES')
        if any(code[:2]==t['province_code'][:2] and
            (t['city_code'].endswith('0000') or code[:4]==t['city_code'][:4]) and
            (not t.get('district_code') or code==t['district_code']) for t in targets):matched.append(task)
    mode=rule.get('billing_mode','ONCE')
    count=(len({t.get('store_id') or t['id'] for t in matched}) if mode=='PER_STORE' else
           len({t['region_code'] for t in matched}) if mode=='PER_REGION' else int(bool(matched)))
    if mode not in {'ONCE','PER_REGION','PER_STORE'}:raise ValueError('Unknown surcharge mode')
    return Decimal(rule.get('amount','0'))*count


def route_model(data,vehicle,ids,road):
    tasks=[data['tasks'][i] for i in ids];errors=[]
    if not vehicle['available'] or not tasks:return None,['VEHICLE_OR_EMPTY']
    if any(t['warehouse_id']!=vehicle['warehouse_id'] for t in tasks):errors.append('WAREHOUSE')
    if len({signature(t.get('quote')) for t in tasks})!=1 or not tasks[0].get('quote'):errors.append('QUOTE')
    pieces=sum(t['pieces'] for t in tasks)
    if pieces>vehicle['max_pieces']:errors.append('CAPACITY')
    if errors:return None,errors
    current=data['warehouses'][vehicle['warehouse_id']];leave=datetime.fromisoformat(current['earliest_departure_at'])
    meters=seconds=0;stops=[];segments=[]
    for task in tasks:
        metric=road.known_segment(current,task)
        if metric is None:return None,['MISSING_EDGE:'+current['id']+'>'+task['id']]
        if any(isinstance(metric[k],bool) or not isinstance(metric[k],int) or metric[k]<0 for k in ('meters','seconds')):
            return None,['INVALID_METRIC']
        arrival=leave+timedelta(seconds=metric['seconds']);start=max(arrival,datetime.fromisoformat(task['open_at']))
        if start>datetime.fromisoformat(task['close_at']):errors.append('WINDOW')
        if task.get('latest_arrival_at') and arrival>datetime.fromisoformat(task['latest_arrival_at']):errors.append('LATEST_ARRIVAL')
        leave=start+timedelta(minutes=task['unload_minutes']);meters+=metric['meters'];seconds+=metric['seconds']
        stops.append({'arrival_at':arrival.isoformat(),'unload_start_at':start.isoformat(),'departure_at':leave.isoformat()})
        segments.append((metric['meters'],metric['seconds']));current=task
    quote=tasks[0]['quote']
    base=Decimal(meters)*Decimal(quote['per_km'])/1000 if quote['mode'].startswith('KM') else (
        Decimal(quote['base_price'])+max(0,len(tasks)-quote['included_stops'])*Decimal(quote['extra_stop_price']))
    fee=cents(base+region_charge(quote,tasks))
    return {'fee_cents':fee,'meters':meters,'seconds':seconds,'pieces':pieces,'stops':stops,'segments':segments},errors


def exact_oracle(body,road,seconds=30):
    """Exhaust routes and actual-vehicle allocations; no search code or MIP."""
    from app.routing_engine import normalize
    data=normalize(body);ids=sorted(data['tasks']);n=len(ids)
    if n>12:raise ValueError('Exact enumeration is restricted to small controlled fixtures')
    bits={item:1<<j for j,item in enumerate(ids)};deadline=time.monotonic()+seconds
    common={i:None if data['tasks'][i]['common_unit_price'] is None else cents(Decimal(data['tasks'][i]['common_unit_price'])*data['tasks'][i]['pieces']) for i in ids}
    required=sum(bits[i] for i in ids if common[i] is None);baseline=sum(v or 0 for v in common.values())
    states={0:(0,0,())};unknown=set();orders=0;complete=True;memo={}
    def control():
        if time.monotonic()>=deadline:raise TimeoutError('EXACT_ORACLE_BUDGET')
    try:
        for car in sorted((v for v in data['vehicles'].values() if v['available']),key=lambda v:v['id']):
            control();car_key=(car['warehouse_id'],car['max_pieces'])
            if car_key not in memo:
                candidates={};eligible=[i for i in ids if data['tasks'][i]['warehouse_id']==car['warehouse_id'] and data['tasks'][i].get('quote')]
                for size in range(1,len(eligible)+1):
                    for order in permutations(eligible,size):
                        control();orders+=1;model,errors=route_model(data,car,order,road)
                        unknown.update(e for e in errors if e.startswith('MISSING_EDGE:'))
                        if errors or model is None:continue
                        mask=sum(bits[i] for i in order);value=(model['fee_cents']-sum(common[i] or 0 for i in order),model['meters'],tuple(order))
                        if mask not in candidates or value<candidates[mask]:candidates[mask]=value
                memo[car_key]=candidates
            following=dict(states)
            for mask,previous in states.items():
                control()
                for used,candidate in memo[car_key].items():
                    if mask&used:continue
                    target=mask|used;value=(previous[0]+candidate[0],previous[1]+candidate[1],previous[2]+((car['id'],candidate[2]),))
                    if target not in following or value<following[target]:following[target]=value
            states=following
    except TimeoutError:complete=False
    mask,best=min(states.items(),key=lambda item:((required&~item[0]).bit_count(),baseline+item[1][0],item[1][1],len(item[1][2])))
    missing=required&~mask;proven=complete and not unknown
    return {'scope':'INDEPENDENT_EXHAUSTIVE_CONTROLLED_FROZEN_MODEL','input_hash':digest(body),'graph_hash':road.graph_hash,
        'route_orders_evaluated':orders,'allocation_masks':len(states),'enumeration_complete':complete,
        'unknown_edges':sorted(unknown),'delivery_infeasible_proven':proven and bool(missing),
        'frozen_model_optimality_proven':proven and not missing,'real_world_global_optimality_proven':False,
        'optimal_fee':money(Decimal(baseline+best[0])/100) if proven and not missing else None,
        'minimum_unassigned':missing.bit_count() if proven else None,
        'routes':[{'vehicle_id':vid,'task_ids':list(order)} for vid,order in best[2]],
        'common_ids':[i for i in ids if not mask&bits[i] and common[i] is not None],
        'independent_pricing_and_time_arithmetic':True,'regional_scope':'FULLY_CODED_CONTROLLED_COUNTIES','external_calls':0}


def audit(body,result,road):
    from app.routing_engine import check_result,normalize
    data=normalize(body);seen=Counter();used=Counter();errors=[];total=0;route_details=[]
    for route in result.get('routes',[]):
        vid=route['vehicle_id'];used[vid]+=1;order=route['task_ids'];seen.update(order)
        if vid not in data['vehicles'] or any(i not in data['tasks'] for i in order):errors.append('UNKNOWN_ID');continue
        model,problems=route_model(data,data['vehicles'][vid],order,road);errors.extend(problems)
        if model is None:continue
        if route.get('fee') is None or cents(route['fee'])!=model['fee_cents']:errors.append('ROUTE_FEE')
        if route.get('meters')!=model['meters'] or route.get('pieces')!=model['pieces']:errors.append('ROUTE_TOTALS')
        if [(s['meters'],s['seconds']) for s in route.get('segments',[])]!=model['segments']:errors.append('ROAD_SEGMENTS')
        for actual,wanted in zip(route.get('stops',[]),model['stops']):
            if any(actual.get(k)!=value for k,value in wanted.items()):errors.append('STOP_TIME')
        if len(route.get('stops',[]))!=len(order):errors.append('STOP_COUNT')
        total+=model['fee_cents'];route_details.append({'vehicle_id':vid,'task_ids':order,'fee':money(Decimal(model['fee_cents'])/100)})
    for row in result.get('common',[]):
        item=row['task_id'];seen[item]+=1;task=data['tasks'].get(item)
        if task is None or task['common_unit_price'] is None:errors.append('MANDATORY_COMMON');continue
        expected=cents(Decimal(task['common_unit_price'])*task['pieces']);total+=expected
        if row.get('pieces')!=task['pieces'] or row.get('fee') is None or cents(row['fee'])!=expected:errors.append('COMMON_FEE')
    for row in result.get('unassigned',[]):seen[row['task_id']]+=1
    coverage=set(seen)==set(data['tasks']) and all(v==1 for v in seen.values())
    if not coverage:errors.append('COVERAGE_OR_SPLIT')
    if any(v>1 for v in used.values()):errors.append('PHYSICAL_VEHICLE_REUSE')
    if result.get('unassigned') or not result.get('fees_complete'):errors.append('INCOMPLETE')
    if cents(result.get('total_fee','0'))!=total:errors.append('TOTAL_FEE')
    proof=check_result(data,result);errors.extend(r['code'] for r in proof['issues'] if r['code']!='DRIVING_REFERENCE')
    return {'valid':not errors,'errors':sorted(set(errors)),'total_fee':money(Decimal(total)/100),
        'all_tasks_retained_once':coverage,'unassigned':len(result.get('unassigned',[])),
        'used_vehicles':len(used),'per_route':route_details,'external_calls':0}


def validate_prepared(prepared):
    if sha(prepared['prelock_file'])!=prepared['prelock_sha256']:raise ValueError('Controlled prelock changed')
    for case in prepared['cases']:
        if sha(case['input_file'])!=case['file_sha256'] or digest(load(case['input_file']))!=case['input_hash']:raise ValueError('Controlled input changed')
        if case['missing_required_directions']:raise ValueError('Complete measured roads required before exact/paired execution')
        road=road_for(load(case['input_file']),case['road_graph'])
        try:
            if road.graph_hash!=case['graph_hash']:raise ValueError('Controlled roads changed')
        finally:road.close()


def worker(spec_file):
    launched=time.monotonic()
    spec=load(spec_file);source=spec['source']['root'];sys.path.insert(0,source)
    if os.environ.get('PYTHONHASHSEED')!=str(spec['hash_seed']):raise ValueError('Hash seed must be fixed before process launch')
    if source_identity(source)!=spec['source']:raise ValueError('Solver source changed before launch')
    case=spec['case'];body=load(case['input_file']);road=road_for(body,case['road_graph']);started=time.monotonic()
    if digest(body)!=case['input_hash'] or road.graph_hash!=case['graph_hash']:raise ValueError('Input or roads changed before worker')
    deadline=launched+spec['wait_seconds'];road.wall_deadline=deadline
    road.settings=SimpleNamespace(planning_optimizer_profile='HYBRID')
    production_policy=spec.get('production_search_policy',False)
    execution_policy=controlled_execution_policy(production_policy,case)
    if spec.get('road_execution_policy',execution_policy)!=execution_policy or (production_policy and 'road_execution_policy' not in spec):
        raise ValueError('Controlled worker road execution policy differs from its specification')
    deadline_state={'triggered':False}
    def cancelled():
        reached=time.monotonic()>=deadline-.25
        deadline_state['triggered']=deadline_state['triggered'] or reached
        return reached
    apply_road_execution_policy(road,production_policy,wall_deadline=deadline,cancelled=cancelled,
        adapter=execution_policy['adapter'],evidence_scope=execution_policy['evidence_scope'])
    row={'side':spec['side'],'compute_seconds':spec['compute_seconds'],'seed':spec['seed'],
        'hash_seed':spec['hash_seed'],'source':spec['source'],'input_hash':case['input_hash'],'graph_hash':road.graph_hash,
        'entrypoint':spec['entrypoint'],'options':spec['options'],
        'external_calls':0,'source_business_database_written':False,
        'production_search_policy':production_policy,'road_execution_policy':execution_policy,
        **road_execution_state(road,launched)}
    try:
        module,name=spec['entrypoint'].split(':');function=getattr(importlib.import_module(module),name)
        options=deepcopy(spec['options']);parameters=inspect.signature(function).parameters
        if 'initial_result' in parameters:options['initial_result']=None
        if 'wall_budget_seconds' in parameters:options['wall_budget_seconds']=max(0.,deadline-time.monotonic())
        result=function(body,road,spec['compute_seconds'],spec['seed'],cancelled,**options)
        result_file=Path(spec_file).parent/'result.json';save(result_file,result)
        stop=result.get('algorithm',{}).get('stop_reason')
        exhausted=deadline_state['triggered'] or time.monotonic()>=deadline or stop in {'WALL_BUDGET','CANCELLED'}
        row.update(status='RETURNED_TIMEOUT' if exhausted else 'RETURNED',deadline_exhausted=exhausted,
            result_file=str(result_file),algorithm=result.get('algorithm',{}))
    except Exception as exc:
        exhausted=deadline_state['triggered'] or time.monotonic()>=deadline
        row.update(status='TIMEOUT' if exhausted else 'FAILED',deadline_exhausted=exhausted,
            error_code=getattr(exc,'code',type(exc).__name__))
    finally:
        row.update(elapsed_seconds=round(time.monotonic()-started,6),source_unchanged=source_identity(source)==spec['source'],road_stats=road.stats())
        road.close();save(Path(spec_file).parent/'worker.json',row)


def run(prepared_file,output,*,baseline_root,candidate_root,profile_file,compute_seconds,wait_seconds,seeds=(0,),hash_seed=0,oracle_seconds=30,lower_seconds=5,production_search_policy=False):
    road_execution_policy(production_search_policy)
    if any(not math.isfinite(float(v)) or float(v)<=0 for v in (compute_seconds,wait_seconds,oracle_seconds,lower_seconds)):
        raise ValueError('Finite positive compute, wait, oracle and bound budgets are required')
    prepared=load(prepared_file);validate_prepared(prepared);output=Path(output).resolve();output.mkdir(mode=0o700,parents=True,exist_ok=False)
    sources={'baseline':source_identity(baseline_root),'candidate':source_identity(candidate_root)}
    profile=load(profile_file)
    if isinstance(profile,list):
        if len(profile)!=1:raise ValueError('Choose one prelocked profile')
        profile=profile[0]
    manifest={'prepared_file':str(Path(prepared_file).resolve()),'prepared_sha256':sha(prepared_file),'sources':sources,
        'profile':profile,'compute_seconds':compute_seconds,'wait_seconds':wait_seconds,'seeds':list(seeds),'hash_seed':hash_seed,
        'execution':'SERIAL_ALTERNATING_FRESH_PROCESSES','warm_start':False,'core_real_day_acceptance':False}
    manifest.update(production_search_policy=production_search_policy,
        road_execution_policy_by_case={case['name']:controlled_execution_policy(production_search_policy,case) for case in prepared['cases']},
        certificate_policy='INDEPENDENT_ORACLE_AND_BOUND_IMPLEMENTATIONS_UNCHANGED')
    save(output/'manifest.json',manifest);oracles={};bounds={};rows=[];ordinal=0;certificate_failures=[]
    for case in prepared['cases']:
        body=load(case['input_file']);road=road_for(body,case['road_graph'])
        try:
            if case['independent_exact_oracle_required']:
                try:oracle=exact_oracle(body,road,oracle_seconds)
                except Exception as exc:oracle={'status':'FAILED','error_code':type(exc).__name__,'delivery_infeasible_proven':False,'frozen_model_optimality_proven':False}
                if not oracle['delivery_infeasible_proven'] and not oracle['frozen_model_optimality_proven']:
                    certificate_failures.append({'case':case['name'],'category':'EXACT_ORACLE_NOT_PROVEN','proof':oracle})
                oracles[case['name']]=oracle;save(output/'oracles'/(case['name']+'.json'),oracle)
            else:
                from app.route_lower_bound import compute_lower_bound
                bound=compute_lower_bound(body,road,lower_seconds,include_certificate=True)
                bound.update(controlled_road_scope=case['road_scope'],real_world_global_optimality_proven=False)
                bounds[case['name']]=bound;save(output/'bounds'/(case['name']+'.json'),bound)
        finally:road.close()
        for seed in seeds:
            for side in (('baseline','candidate') if ordinal%2==0 else ('candidate','baseline')):
                folder=output/'runs'/case['name']/('s'+str(seed))/side;folder.mkdir(mode=0o700,parents=True)
                entry=profile.get(side+'_entrypoint',profile.get('entrypoint','app.route_deep:optimize_deep'))
                options=profile.get(side+'_options',{} if side=='baseline' else profile.get('options',{}))
                spec={**manifest,'side':side,'source':sources[side],'case':case,'seed':seed,'entrypoint':entry,'options':options}
                spec['road_execution_policy']=manifest['road_execution_policy_by_case'][case['name']]
                spec_file=folder/'spec.json';save(spec_file,spec)
                env={**os.environ,**THREAD_ENV,'PYTHONHASHSEED':str(hash_seed),'PYTHONUNBUFFERED':'1'}
                timed_out=False
                with (folder/'worker.log').open('w') as log:
                    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'_worker','--spec',str(spec_file)],cwd=sources[side]['root'],env=env,stdout=log,stderr=log)
                    try:process.wait(timeout=wait_seconds)
                    except subprocess.TimeoutExpired:timed_out=True;process.kill();process.wait()
                row=load(folder/'worker.json') if (folder/'worker.json').exists() else {'side':side,'status':'FAILED','error_code':'NO_WORKER_RESULT'}
                execution_policy=spec['road_execution_policy']
                policy_mismatch=(row.get('road_execution_policy',execution_policy)!=execution_policy or
                    row.get('production_search_policy',production_search_policy)!=production_search_policy)
                timed_out=timed_out or row.get('deadline_exhausted',False) or row.get('status') in {'TIMEOUT','RETURNED_TIMEOUT'}
                row.update(case=case['name'],seed=seed,compute_seconds=compute_seconds,wait_seconds=wait_seconds,supervisor_timeout=timed_out,
                    core_acceptance=False,road_scope=case['road_scope'],production_search_policy=production_search_policy,
                    road_execution_policy=execution_policy)
                oracle=oracles.get(case['name']);negative=bool(oracle and oracle['delivery_infeasible_proven'])
                if row.get('result_file'):
                    result=load(row['result_file']);road=road_for(body,case['road_graph'])
                    try:
                        try:row['audit']=audit(body,result,road)
                        except Exception as exc:row['audit']={'valid':False,'errors':['AUDIT_FAILED_'+type(exc).__name__],'all_tasks_retained_once':False}
                    finally:road.close()
                    row['status']='TIMEOUT' if timed_out else 'VALID' if row['audit']['valid'] else 'INVALID'
                    if negative and row['audit']['valid']:row['status']='ORACLE_CONTRADICTION'
                    elif (negative and not timed_out and row['audit']['all_tasks_retained_once'] and
                          set(row['audit']['errors'])<={'INCOMPLETE','UNASSIGNED'}):
                        row['status']='EXPECTED_INFEASIBLE_PARTIAL'
                    if oracle and oracle['frozen_model_optimality_proven'] and row['audit']['valid']:
                        row['exact_gap']=money(Decimal(row['audit']['total_fee'])-Decimal(oracle['optimal_fee']))
                    if case['name'] in bounds and row['audit']['valid']:
                        lower=Decimal(bounds[case['name']]['cost_lower_bound']);upper=Decimal(row['audit']['total_fee'])
                        if lower>upper:raise ValueError('Lower bound exceeded independently feasible fee')
                        row.update(valid_lower_bound=str(lower),feasible_upper_bound=str(upper),bound_gap=str(upper-lower),global_optimality_proven=False)
                elif negative and not timed_out and row.get('error_code') in {'DEEP_NO_FEASIBLE_PLAN','NO_FEASIBLE_PLAN'}:
                    row['status']='EXPECTED_INFEASIBLE_REJECTION'
                if policy_mismatch:row['status']='POLICY_MISMATCH'
                if row.get('source_unchanged') is not True:row['status']='SOURCE_CHANGED_OR_UNVERIFIED'
                rows.append(row);save(output/'report.json',{'manifest':manifest,'runs':rows,'finished':False})
            ordinal+=1
    validate_prepared(prepared)
    report={'manifest':manifest,'runs':rows,'finished':True,'all_failures':[r for r in rows if r['status'] not in {'VALID','EXPECTED_INFEASIBLE_REJECTION','EXPECTED_INFEASIBLE_PARTIAL'}],
        'certificate_failures':certificate_failures,'oracles':oracles,'bounds':bounds,'external_calls':0,'source_business_database_written':False}
    save(output/'report.json',report);return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    prep=commands.add_parser('prepare');prep.add_argument('--prelock',required=True);prep.add_argument('--measured-graph',required=True);prep.add_argument('--output',required=True)
    execute=commands.add_parser('run');execute.add_argument('--prepared',required=True);execute.add_argument('--output',required=True)
    execute.add_argument('--baseline-root',required=True);execute.add_argument('--candidate-root',required=True);execute.add_argument('--profile',required=True)
    execute.add_argument('--compute-seconds',type=float,required=True);execute.add_argument('--wait-seconds',type=float,required=True)
    execute.add_argument('--seeds',default='0');execute.add_argument('--hash-seed',type=int,default=0)
    execute.add_argument('--oracle-seconds',type=float,default=30);execute.add_argument('--lower-seconds',type=float,default=5)
    execute.add_argument('--production-search-policy',action='store_true',
        help='Use native production search semantics on the same offline evidence; HTTP stays disabled')
    child=commands.add_parser('_worker');child.add_argument('--spec',required=True)
    args=parser.parse_args(argv)
    if args.command=='_worker':worker(args.spec);return
    if args.command=='prepare':
        result=prepare(args.prelock,args.measured_graph,args.output)
        print(json.dumps({'status':result['status'],'cases':len(result['cases']),'missing_edges':sum(len(c['missing_required_directions']) for c in result['cases']),'solver_runs':0,'external_calls':0},ensure_ascii=False));return
    result=run(args.prepared,args.output,baseline_root=args.baseline_root,candidate_root=args.candidate_root,
        profile_file=args.profile,compute_seconds=args.compute_seconds,wait_seconds=args.wait_seconds,
        seeds=tuple(int(v) for v in args.seeds.split(',')),hash_seed=args.hash_seed,oracle_seconds=args.oracle_seconds,lower_seconds=args.lower_seconds,
        production_search_policy=args.production_search_policy)
    print(json.dumps({'finished':result['finished'],'runs':len(result['runs']),'failures':len(result['all_failures']),
        'certificate_failures':len(result['certificate_failures']),'external_calls':0},ensure_ascii=False))


if __name__=='__main__':main()
