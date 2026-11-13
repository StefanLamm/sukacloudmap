"""Read-only test2 replay; default mode never calls AMap.

The optimization result proves only the finite cached candidate pool, not all
possible roads or the user's full real-world optimum. ``--refresh-selected``
explicitly opts into finite real ordinary-driving calls. No business DB writes.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, PROFILE
from app.routing_engine import normalize,evaluate_route,assemble,common_fee
import httpx

GID='8d9ac7f2-8308-4697-b314-83d5df8071b6'


def private_json(path,value):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)


def compare_results(data, previous, result):
    """Reconcile dedicated/common deltas from each physically changed route."""
    old_common={i['task_id'] for i in previous['common']}
    new_common={i['task_id'] for i in result['common']}
    to_dedicated=old_common-new_common;to_common=new_common-old_common
    def task_rows(ids):
        return [{'task_id':i,'name':data['tasks'][i]['name'],'pieces':data['tasks'][i]['pieces'],
                 'common_fee':common_fee(data['tasks'][i]['pieces'],data['tasks'][i]['common_unit_price'])}
                for i in sorted(ids)]
    old_cars={r['vehicle_id']:r for r in previous['routes']}
    new_cars={r['vehicle_id']:r for r in result['routes']}
    routes=[]
    for vid in sorted(old_cars.keys()|new_cars.keys()):
        before=old_cars.get(vid);after=new_cars.get(vid)
        if before and after and before['task_ids']==after['task_ids'] and before['fee']==after['fee']:continue
        before_ids=set(before['task_ids']) if before else set();after_ids=set(after['task_ids']) if after else set()
        routes.append({'vehicle_id':vid,'plate':(after or before)['plate'],
            'before_fee':before['fee'] if before else '0.00','after_fee':after['fee'] if after else '0.00',
            'before_task_count':len(before_ids),'after_task_count':len(after_ids),
            'removed_tasks':task_rows(before_ids-after_ids),'added_tasks':task_rows(after_ids-before_ids)})
    dedicated_delta=Decimal(result['check']['dedicated_fee'])-Decimal(previous['check']['dedicated_fee'])
    common_delta=Decimal(result['check']['common_fee'])-Decimal(previous['check']['common_fee'])
    total_delta=Decimal(result['total_fee'])-Decimal(previous['total_fee'])
    assert dedicated_delta+common_delta==total_delta
    return {'baseline_total':previous['total_fee'],'result_total':result['total_fee'],
        'baseline_dedicated_fee':previous['check']['dedicated_fee'],'result_dedicated_fee':result['check']['dedicated_fee'],
        'baseline_common_fee':previous['check']['common_fee'],'result_common_fee':result['check']['common_fee'],
        'dedicated_fee_delta':str(dedicated_delta),'common_fee_delta':str(common_delta),'total_fee_delta':str(total_delta),
        'common_to_dedicated':task_rows(to_dedicated),'dedicated_to_common':task_rows(to_common),'changed_routes':routes,
        'reconciliation_ok':True}


def load_input(database,group_id):
    connection=sqlite3.connect(f'{Path(database).resolve().as_uri()}?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    snap=connection.execute('SELECT * FROM input_snapshots WHERE group_id=? ORDER BY version DESC LIMIT 1',(group_id,)).fetchone()
    if not snap:raise ValueError('分组没有快照')
    body=json.loads(snap['content']);assert content_hash(body)==snap['content_hash']
    plan=connection.execute('SELECT * FROM plan_versions WHERE group_id=? ORDER BY version DESC LIMIT 1',(group_id,)).fetchone()
    baseline=json.loads(plan['content']) if plan else None
    if plan:assert content_hash(baseline)==plan['content_hash']
    cache={key:json.loads(content) for key,content in connection.execute('SELECT key,content FROM road_cache')}
    connection.close()
    return body,baseline,cache,dict(snap)


def replay(database,output,group_id=GID):
    output=Path(output);output.mkdir(mode=0o700,parents=True,exist_ok=True)
    body,previous,cache,snap=load_input(database,group_id);data=normalize(body)
    valid={};adj=defaultdict(list);points={**data['tasks'],**data['warehouses']}
    for key,item in cache.items():
        try:
            request=item['request'];a=points.get(request['from_id']);b=points.get(request['to_id']);vehicle=data['vehicles'].get(request['vehicle_id'])
            if item['source']!='DRIVING' or not a or not b or not vehicle:continue
            if request['origin']!={k:a.get(k) for k in ['longitude','latitude','crs']} or request['destination']!={k:b.get(k) for k in ['longitude','latitude','crs']}:continue
            if request['profile']!={k:vehicle.get(k) for k in PROFILE}:continue
            if content_hash(request)!=key or content_hash({k:v for k,v in item.items() if k!='evidence_hash'})!=item['evidence_hash']:continue
            valid[key]=item
            adj[(request['vehicle_id'],request['from_id'],request['departure_at'])].append(request['to_id'])
        except (KeyError,ValueError):continue
    misses=set()
    def cache_get(key):
        if key not in valid:misses.add(key)
        return valid.get(key)
    def no_network(_):raise AssertionError('Offline replay must never send a network request')
    road=DrivingRoad(Settings(),0,cache_get=cache_get,transport=httpx.MockTransport(no_network))
    candidates={};nodes=0
    def store(route):
        if route['fee'] is None or route['errors']:return
        if not route['task_ids']:return
        # Calculate opportunity saving from source common prices, not income.
        source_common=sum(Decimal(common_fee(data['tasks'][sid]['pieces'],data['tasks'][sid]['common_unit_price']) or '0') for sid in route['task_ids'])
        savings=source_common-Decimal(route['fee'])
        if savings<=0:return
        k=(route['vehicle_id'],frozenset(route['task_ids']))
        if k not in candidates or Decimal(route['fee'])<Decimal(candidates[k]['route']['fee']):
            candidates[k]={'route':route,'saving':savings}
    if previous:
        for route in previous['result']['routes']:
            fresh=evaluate_route(data,route['vehicle_id'],route['task_ids'],road)
            store(fresh)
    def walk(vehicle_id,current,leave,path,quantity):
        nonlocal nodes
        if nodes>=20000 or len(path)>=40:return
        for next_id in sorted(set(adj.get((vehicle_id,current,leave),[]))):
            if next_id not in data['tasks'] or next_id in path:continue
            task=data['tasks'][next_id];vehicle=data['vehicles'][vehicle_id]
            if quantity+task['pieces']>vehicle['max_pieces'] or task['warehouse_id']!=vehicle['warehouse_id']:continue
            if path and data['tasks'][path[0]]['signature']!=task['signature']:continue
            nodes+=1;route=evaluate_route(data,vehicle_id,path+[next_id],road)
            if route['fee'] is None or route['errors']:continue
            store(route)
            walk(vehicle_id,next_id,route['arrival_end_at'],path+[next_id],quantity+task['pieces'])
    for vehicle in data['vehicles'].values():
        if vehicle['available']:
            wh=data['warehouses'][vehicle['warehouse_id']]
            walk(vehicle['id'],wh['id'],wh['earliest_departure_at'],[],0)
    rows=list(candidates.values());selected=[];pool_optimal=False;solver_message='No profitable cached candidates'
    if rows:
        import numpy as np
        from scipy.optimize import milp,Bounds,LinearConstraint
        from scipy.sparse import csc_matrix
        all_tasks=sorted(data['tasks']);all_cars=sorted(data['vehicles']);constraint_keys=[('task',i) for i in all_tasks]+[('car',i) for i in all_cars];indices={k:i for i,k in enumerate(constraint_keys)}
        ii=[];jj=[]
        for column,item in enumerate(rows):
            route=item['route']
            for key in [('car',route['vehicle_id'])]+[('task',i) for i in route['task_ids']]:ii.append(indices[key]);jj.append(column)
        matrix=csc_matrix((np.ones(len(ii)),(ii,jj)),shape=(len(indices),len(rows)))
        values=np.array([-float(r['saving']) for r in rows])
        result=milp(c=values,integrality=np.ones(len(rows)),bounds=Bounds(0,1),constraints=LinearConstraint(matrix,0,1),options={'time_limit':30})
        if result.x is not None:selected=[r['route'] for r,decision in zip(rows,result.x) if decision>.5]
        pool_optimal=result.status==0;solver_message=str(result.message)
    assigned={i for r in selected for i in r['task_ids']};common=set(data['tasks'])-assigned
    result=assemble(data,selected,common,[])
    result['algorithm']={'mode':'OFFLINE_EXACT_CACHED_CANDIDATE_POOL','candidate_count':len(rows),'pool_optimal':pool_optimal,'optimality_proven':False,'message':solver_message,'external_calls':0,'request_binding':'ORIGINAL_CACHED_VEHICLE_DEPARTURE_AND_COORDINATES','expired_cache_replay':True}
    # Original reference evidence remains validly bound. The driving-reference
    # limitation is expected; no business plan or execution confirmation is saved.
    unexpected=[e for e in result['check']['issues'] if e['code'] not in {'DRIVING_REFERENCE'}]
    stores=data['tasks']
    summary={'scope':'OFFLINE_CACHED_DRIVING_REFERENCE_ONLY','group_id':group_id,'snapshot_id':snap['id'],'snapshot_version':snap['version'],'source_database':str(Path(database).resolve()),
        'task_count':len(data['tasks']),'pieces':sum(t['pieces'] for t in data['tasks'].values()),'vehicles':len(data['vehicles']),
        'baseline':{k:previous['result'][k] for k in ['total_fee']} if previous else None,
        'baseline_routes':len(previous['result']['routes']) if previous else None,'baseline_common':len(previous['result']['common']) if previous else None,
        'candidate_pool':len(rows),'cached_rows':len(cache),'matching_evidence_rows':len(valid),'enumerated_nodes':nodes,
        'result':{'total_fee':result['total_fee'],'dedicated_fee':result['check']['dedicated_fee'],'common_fee':result['check']['common_fee'],'routes':len(selected),'common':len(common),'unassigned':len(result['unassigned']),'delta_vs_baseline':str(Decimal(result['total_fee'])-Decimal(previous['result']['total_fee'])) if previous else None,'unexpected_issues':unexpected},
        'route_details':[{'vehicle_id':r['vehicle_id'],'plate':r['plate'],'fee':r['fee'],'pieces':r['pieces'],'meters':r['meters'],'task_ids':r['task_ids'],'names':[stores[i]['name'] for i in r['task_ids']]} for r in selected],
        'limitations':['Only the original cached candidate pool was optimized; missing roads were not synthesized.','Expired cached current-traffic reference measurements are reused offline, not a fresh high-confidence truck route.','No source DB writes, network requests or executable plan confirmation.'],
        'external_calls':road.stats()['calls'],'pool_optimal':pool_optimal,'global_optimality_proven':False}
    assert road.stats()['calls']==0
    compact_candidates=[{'vehicle_id':r['route']['vehicle_id'],'task_ids':r['route']['task_ids'],
        'fee':r['route']['fee'],'meters':r['route']['meters'],'pieces':r['route']['pieces'],
        'common_replacement_saving':str(r['saving'])} for r in rows]
    private_json(output/'cached-candidate-pool.json',compact_candidates)
    private_json(output/'cached-pool-result.json',result);private_json(output/'cached-pool-summary.json',summary)
    if previous:private_json(output/'cached-pool-comparison.json',compare_results(data,previous['result'],result))
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary,result


def refresh_selected(database,output,group_id=GID,joint_seconds=60):
    """Explicit opt-in: refresh selected directed roads, then replay offline.

    Only the finite cached-pool winner and the unchanged comparison baseline
    are sent to the ordinary DRIVING API (their common edges are shared).
    The later joint search has a zero HTTP budget and a rejecting transport.
    The source DB remains read-only; fresh measurements go to private JSON.
    """
    output=Path(output);body,previous,_,snapshot=load_input(database,group_id)
    data=normalize(body)
    selected=json.loads((output/'cached-pool-result.json').read_text())
    cache_path=output/'refreshed-road-cache.json'
    fresh={}
    if cache_path.exists():
        fresh={key:item for key,item in json.loads(cache_path.read_text()).items() if DrivingRoad.valid_reference(item)}
    def save_cache(key,item):
        fresh[key]=item
        private_json(cache_path,fresh)
    road=DrivingRoad(Settings(),None,cache_get=fresh.get,cache_put=save_cache)
    road.seed_reference_cache(fresh.values())
    def aborted(current,phase):
        failure={'scope':'SELECTED_DRIVING_REFERENCE_REFRESH','status':'FAILED','phase':phase,
            'source_database_read_only':True,'current_fee':None,'comparison_available':False,
            'errors':current['errors'],'road_stats':road.stats(),
            'external_request_attempts':road.stats()['calls'],'successful_provider_edges':sum(i['request'].get('source')=='DRIVING_PROVIDER_REFERENCE' for i in fresh.values()),
            'reason':'A complete fresh route could not be verified; no partial total is reported as a plan fee.'}
        private_json(output/'refreshed-summary.json',failure)
        print(json.dumps(failure,ensure_ascii=False,indent=2),flush=True)
        return failure,None
    routes=[]
    for original in selected['routes']:
        current=evaluate_route(data,original['vehicle_id'],original['task_ids'],road)
        routes.append(current)
        print(json.dumps({'phase':'REFRESH_SELECTED','plate':original['plate'],'fee':current['fee'],
              'stops':len(current['task_ids']),'errors':current['errors'],'road_stats':road.stats()},ensure_ascii=False),flush=True)
        if current['errors']:return aborted(current,'REFRESH_SELECTED')
    baseline_routes=[]
    for original in previous['result']['routes']:
        current=evaluate_route(data,original['vehicle_id'],original['task_ids'],road)
        baseline_routes.append(current)
        print(json.dumps({'phase':'REFRESH_COMPARISON_BASELINE','plate':original['plate'],'fee':current['fee'],
              'stops':len(current['task_ids']),'errors':current['errors'],'road_stats':road.stats()},ensure_ascii=False),flush=True)
        if current['errors']:return aborted(current,'REFRESH_COMPARISON_BASELINE')
    assigned={i for r in routes for i in r['task_ids']}
    result=assemble(data,routes,set(data['tasks'])-assigned,[])
    result['algorithm']={'mode':'REFRESH_SELECTED_REAL_DRIVING_REFERENCE','optimality_proven':False,
        'source_database_read_only':True,'snapshot_id':snapshot['id'],'road_stats':road.stats()}
    private_json(output/'refreshed-selected-result.json',result)
    refresh_summary={'scope':'FRESH_SELECTED_DRIVING_REFERENCE_ONLY','status':'SUCCESS','snapshot_id':snapshot['id'],
        'selected_stop_count':sum(len(r['task_ids']) for r in routes),'selected_route_count':len(routes),
        'source_database_read_only':True,'network_scope':'Only selected and baseline directed route segments; no distance matrix',
        'external_calls':road.stats()['calls'],'road_stats':road.stats(),
        'current_fee':result['total_fee'],'current_check':result['check'],
        'historical_selected_fee':selected['total_fee'],'historical_baseline_fee':previous['result']['total_fee']}
    private_json(output/'refreshed-selected-vs-historical-baseline.json',compare_results(data,previous['result'],result))
    # All further optimization is explicitly barred from making network calls.
    def no_network(_):raise AssertionError('Offline joint replay must never send a network request')
    offline=DrivingRoad(Settings(),0,cache_get=fresh.get,transport=httpx.MockTransport(no_network))
    offline.seed_reference_cache(fresh.values())
    offline.route_seed_samples=fresh
    from app.route_search import optimize_joint
    covered={i for r in baseline_routes for i in r['task_ids']}
    current_baseline=assemble(data,baseline_routes,set(data['tasks'])-covered,[])
    private_json(output/'refreshed-baseline-result.json',current_baseline)
    private_json(output/'refreshed-selected-vs-baseline.json',compare_results(data,current_baseline,result))
    joint=optimize_joint(body,offline,joint_seconds,seed=0,initial_result=current_baseline)
    private_json(output/'refreshed-joint-result.json',joint)
    private_json(output/'refreshed-joint-vs-baseline.json',compare_results(data,current_baseline,joint))
    refresh_summary['current_baseline_routes_replayable']=len(baseline_routes)
    refresh_summary['current_baseline_fee']=current_baseline['total_fee']
    refresh_summary['joint']={'total_fee':joint['total_fee'],'routes':len(joint['routes']),
        'common':len(joint['common']),'check':joint['check'],'algorithm':joint['algorithm']}
    refresh_summary['offline_extra_calls']=offline.stats()['calls']
    assert offline.stats()['calls']==0
    private_json(output/'refreshed-summary.json',refresh_summary)
    print(json.dumps(refresh_summary,ensure_ascii=False,indent=2),flush=True)
    return refresh_summary,joint


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--output',required=True);p.add_argument('--group-id',default=GID)
    p.add_argument('--refresh-selected',action='store_true',help='Opt in to finite real ordinary-driving requests for selected routes and the comparison baseline')
    p.add_argument('--joint-seconds',type=float,default=60)
    args=p.parse_args()
    if args.refresh_selected:
        summary,_=refresh_selected(args.database,args.output,args.group_id,args.joint_seconds)
        if summary['status']!='SUCCESS':raise SystemExit(1)
    else:replay(args.database,args.output,args.group_id)
