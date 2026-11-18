from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from app.planning_inputs import content_hash, encode
from app.routing_engine import normalize, evaluate_route, assemble
from scripts.benchmark_algorithms import write_graph, FrozenEvidenceRoad
from scripts.freeze_plan_diagnostics import model_identity, freeze
from scripts.reference_route_trace import inspect, ReferenceStageTrace
from test_algorithm_benchmark import problem, measured_edges


def shifted(body, days):
    value=deepcopy(body)
    value['delivery_date']=(datetime.fromisoformat(value['delivery_date'])+timedelta(days=days)).date().isoformat()
    for group in ('tasks','warehouses'):
        for row in value[group]:
            for name in ('open_at','close_at','latest_arrival_at','earliest_departure_at'):
                if row.get(name):
                    row[name]=(datetime.fromisoformat(row[name])+timedelta(days=days)).isoformat()
    return value


def test_date_normalization_does_not_hide_cross_day_windows_or_business_changes():
    body=problem()
    body['tasks'][0]['close_at']='2026-11-14T01:00:00+08:00'
    digest=content_hash(model_identity(body))
    assert digest==content_hash(model_identity(shifted(body,2)))
    mutations=[('pieces',6),('common_unit_price','9'),('close_at','2026-11-13T01:00:00+08:00')]
    for key,value in mutations:
        changed=deepcopy(body);changed['tasks'][0][key]=value
        assert digest!=content_hash(model_identity(changed))
    changed=deepcopy(body);changed['vehicles'][0]['available']=False
    assert digest!=content_hash(model_identity(changed))
    changed=deepcopy(body);changed['quotes']['Q']['per_km']='2'
    assert digest!=content_hash(model_identity(changed))


def test_first_omission_is_distinguished_from_protected_complete_routes():
    body=problem()
    reference={'routes':[{'vehicle_id':'V1','task_ids':['T1','T2'],'fee':'1.00'}]}
    events=[{'kind':'candidate','vehicle_id':'V1','task_ids':['T3']},
            {'kind':'candidate','vehicle_id':'V1','task_ids':['T1','T2']},
            {'kind':'checkpoint','stage':'PRE_COLUMN_SELECTION'}]
    row=inspect(body,reference,events,limit=1)['reference_routes'][0]
    assert row['first_visible_boundary']=='OBSERVED_BEFORE_GLOBAL_RETENTION_BUT_ABSENT_AT_POOL_BOUNDARY'
    events.insert(-1,{'kind':'allocation','allocation':{'routes':reference['routes'],'common_ids':['T3']}})
    row=inspect(body,reference,events,limit=1)['reference_routes'][0]
    assert row['first_visible_boundary']=='AVAILABLE_TO_POOL_ORDER_REPRICING'
    no_evaluated=[events[-1]]
    row=inspect(body,reference,no_evaluated,native_events=events[1:2],limit=1)['reference_routes'][0]
    assert row['first_visible_boundary']=='NATIVE_OBSERVED_BUT_NOT_IN_EVALUATED_STREAM'


def test_stage_listener_separates_order_coverage_physical_car_and_rejection():
    trace=ReferenceStageTrace({'routes':[{'vehicle_id':'V1','task_ids':['T1','T2'],'fee':'12.00'}]},clock=lambda:1.)
    trace.mark('NATIVE',status='COMPLETE',scope='ALL_MODELS')
    trace.route('NATIVE',{'vehicle_id':'V2','task_ids':['T2','T1']})
    trace.route('PRICED',{'vehicle_id':'V1','task_ids':['T1','T2']},status='REJECTED',reason='CAPACITY')
    for index in range(1000):
        trace.route('NATIVE',{'vehicle_id':'V1','task_ids':['T1','T2']})
    snapshot=trace.snapshot()
    assert len(snapshot['targets'][0]['events'])==2
    native,priced=snapshot['targets'][0]['events']
    assert native['count']==1001 and native['same_coverage_count']==1001
    assert native['exact_order_count']==1000 and native['same_reference_vehicle_count']==1000
    assert priced['status']=='REJECTED' and priced['reason_codes']==['CAPACITY']
    assert snapshot['fees_evaluated_in_observer']==0
    json.dumps(snapshot,allow_nan=False)


def test_freeze_diagnostic_is_read_only_and_reprices_current_graph(tmp_path):
    body=problem();database=tmp_path/'business.db';graph=tmp_path/'source-graph.db'
    write_graph(graph,body,measured_edges(body))
    road=FrozenEvidenceRoad(graph)
    result=assemble(normalize(body),[evaluate_route(normalize(body),'V1',['T1','T2','T3'],road)],[],[])
    road.close()
    saved={'input':body,'input_hash':content_hash(body),'result':result,'snapshot_id':'S'}
    with sqlite3.connect(database) as db:
        db.executescript('''CREATE TABLE demand_groups(id,name);CREATE TABLE input_snapshots(id,content,content_hash);
          CREATE TABLE plan_versions(id,group_id,snapshot_id,delivery_date,version,content,content_hash);
          CREATE TABLE road_cache(key,content,expires_at);''')
        db.execute('INSERT INTO demand_groups VALUES (?,?)',('G','arbitrary-label'))
        db.execute('INSERT INTO input_snapshots VALUES (?,?,?)',('S',encode(body),content_hash(body)))
        db.execute('INSERT INTO plan_versions VALUES (?,?,?,?,?,?,?)',
            ('P','G','S',body['delivery_date'],1,encode(saved),content_hash(saved)))
        db.commit()
    before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in (database,graph)}
    output=freeze(database,graph,['P'],tmp_path/'frozen')
    assert output['plans'][0]['same_graph_rebound']['fee']=='12.00'
    assert output['plans'][0]['same_graph_rebound']['audit']['valid']
    assert output['external_calls']==0 and not output['business_database_written']
    assert before=={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in (database,graph)}
    with pytest.raises(FileExistsError):
        freeze(database,graph,['P'],tmp_path/'frozen')
