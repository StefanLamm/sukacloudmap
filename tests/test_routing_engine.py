from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json

import httpx
import pytest

from app.config import Settings
from app.planning_inputs import content_hash
from app.road_service import FixtureRoad, TruckRoad, evidence, load_fixture, road_request
from app.routing_engine import RoadError, check_result, common_fee, evaluate_route, line_fee, normalize, optimize, recalculate


@pytest.mark.parametrize('name,total,plates,common',[('MIXED_56','56.00',1,['C','D']),('SHARED_40','40.00',2,[])])
def test_known_joint_answers(name,total,plates,common):
    f=load_fixture(name);r=optimize(f,FixtureRoad(f))
    assert r['total_fee']==total and r['check']['valid'] and not r['confirmable']
    assert r['check']['used_vehicles']==plates and [c['task_id'] for c in r['common']]==common
    for actual,expected in zip(r['routes'],f['expected']['routes']):
        assert actual['vehicle_id']==expected['vehicle_id'] and actual['task_ids']==expected['sequence']
        if 'arrival_at' in expected:assert [s['arrival_at'] for s in actual['stops']]==expected['arrival_at']
    assert r['algorithm']['all_common_baseline']==f['expected']['baseline_fee']


@pytest.mark.parametrize('meters,price,expected',[(12500,'2.5','31.25'),(12345,'2.5','30.86'),(0,'0','0.00'),(10001,'1.005','10.05')])
def test_km_rounding(meters,price,expected): assert line_fee({'mode':'KM','per_km':price},meters,1)==expected


@pytest.mark.parametrize('stops,expected',[(0,'0.00'),(2,'100.00'),(3,'100.00'),(5,'140.00')])
def test_base_stops(stops,expected): assert line_fee({'mode':'BASE_STOPS','base_price':'100','included_stops':3,'extra_stop_price':'20'},999999,stops)==expected


def test_common_zero_null_and_per_ticket_rounding():
    assert common_fee(3,'2.25')=='6.75' and common_fee(3,'0')=='0.00' and common_fee(3,None) is None
    assert common_fee(1,'0.005')=='0.01'


def fixture_route(changes=None):
    f=load_fixture('MIXED_56')
    if changes:f['tasks'][0].update(changes)
    data=normalize(f);return f,data,evaluate_route(data,'V10',['A'],FixtureRoad(f))


def test_arrival_deadline_inclusive_and_waiting_not_arrival():
    f,d,r=fixture_route({'open_at':'2026-11-11T09:00:00+08:00','latest_arrival_at':'2026-11-11T08:15:00+08:00'})
    assert not r['errors'];s=r['stops'][0]
    assert s['arrival_at'].endswith('08:15:00+08:00') and s['unload_start_at'].endswith('09:00:00+08:00') and s['wait_seconds']==2700
    f,d,r=fixture_route({'latest_arrival_at':'2026-11-11T08:14:59+08:00'})
    assert r['errors'][0]['code']=='LATEST_ARRIVAL'


def test_can_finish_unloading_after_close_and_next_leg_uses_exit():
    f=load_fixture('MIXED_56');f['warehouse']['earliest_departure_at']='2026-11-11T21:40:00+08:00'
    for t in f['tasks']:t.update(open_at='2026-11-11T08:00:00+08:00',close_at='2026-11-11T23:00:00+08:00',latest_arrival_at=None)
    f['tasks'][0].update(close_at='2026-11-11T22:00:00+08:00',unload_minutes=20)
    d=normalize(f);r=evaluate_route(d,'V10',['A','B'],FixtureRoad(f))
    assert not r['errors'] and r['stops'][0]['departure_at'].endswith('22:15:00+08:00')
    assert r['stops'][1]['arrival_at'].endswith('22:24:00+08:00')


def test_close_start_boundary_and_overnight():
    f,d,r=fixture_route({'close_at':'2026-11-11T08:15:00+08:00'})
    assert not r['errors']
    f,d,r=fixture_route({'close_at':'2026-11-11T08:14:59+08:00'})
    assert r['errors'][0]['code']=='BUSINESS_CLOSED'
    f=load_fixture('MIXED_56');f['warehouse']['earliest_departure_at']='2026-11-11T23:50:00+08:00'
    f['tasks'][0].update(open_at='2026-11-11T20:00:00+08:00',close_at='2026-11-12T03:00:00+08:00',latest_arrival_at='2026-11-12T00:10:00+08:00')
    r=evaluate_route(normalize(f),'V10',['A'],FixtureRoad(f));assert not r['errors'] and '2026-11-12T00:05' in r['stops'][0]['arrival_at']


@pytest.mark.parametrize('conflict',['duplicate_task','duplicate_car','capacity','wrong_quote','missing_task','wrong_fee','wrong_time','corrupt_evidence'])
def test_independent_checker_detects_mutations(conflict):
    f=load_fixture('MIXED_56');d=normalize(f);r=optimize(f,FixtureRoad(f))
    if conflict=='duplicate_task':r['common'].append(deepcopy(r['common'][0]))
    if conflict=='duplicate_car':r['routes'].append(deepcopy(r['routes'][0]))
    if conflict=='capacity':d['vehicles']['V10']['max_pieces']=9
    if conflict=='wrong_quote':d['tasks']['B']['signature']='DIFFERENT'
    if conflict=='missing_task':r['common'].pop()
    if conflict=='wrong_fee':r['routes'][0]['fee']='0.00'
    if conflict=='wrong_time':r['routes'][0]['stops'][0]['arrival_at']='2026-11-11T01:00:00+08:00'
    if conflict=='corrupt_evidence':r['routes'][0]['segments'][0]['meters']=1
    assert not check_result(d,r)['valid']


def test_manual_conflicts_preserved_and_common_does_not_check_hours():
    f=load_fixture('MIXED_56');f['tasks'][2]['close_at']='2026-11-11T00:00:00+08:00'
    draft={'routes':[{'vehicle_id':'V10','task_ids':['A','B','C']}],'common_ids':['C','D'],'unassigned_ids':[]}
    r=recalculate(f,draft,FixtureRoad(f));assert r['routes'][0]['task_ids']==['A','B','C'] and not r['check']['valid']
    r=recalculate(f,{'routes':[],'common_ids':['A','B','C','D'],'unassigned_ids':[]},FixtureRoad(f))
    assert r['check']['valid'] and not r['confirmable'] and r['total_fee']=='130.00'


def test_missing_road_and_missing_common_remain_unassigned_not_infeasible_proof():
    class Broken:
        def segment(self,*a):raise RoadError('RESTRICTION_UNKNOWN','限行未知')
        def stats(self):return {'calls':0}
    f=load_fixture('MIXED_56');f['tasks'][0]['common_unit_price']=None
    r=optimize(f,Broken());assert not r['check']['valid'] and r['unassigned'][0]['task_id']=='A'
    assert not r['algorithm']['infeasibility_proven']


def test_cancel_and_zero_budget_keep_complete_baseline():
    f=load_fixture('MIXED_56')
    for kwargs,reason in [({'cancelled':lambda:True},'CANCELLED'),({'budget_seconds':0},'TIME_BUDGET')]:
        r=optimize(f,FixtureRoad(f),**kwargs)
        assert r['total_fee']=='130.00' and r['check']['valid'] and r['algorithm']['stop_reason']==reason


def test_more_than_25_stops_are_all_retained_no_return_leg():
    f=load_fixture('MIXED_56');base=f['tasks'][0];f['tasks']=[{**base,'id':f'T{i}','pieces':1,'latest_arrival_at':None,'close_at':'2026-11-13T23:59:00+08:00'} for i in range(30)]
    f['vehicles'][0]['max_pieces']=30
    class Road:
        def segment(self,v,a,b,t):return evidence(road_request(v,a,b,t,'FIXTURE'),10,1,'FIXTURE')
    r=evaluate_route(normalize(f),'V10',[t['id'] for t in f['tasks']],Road())
    assert len(r['stops'])==len(r['segments'])==30 and r['meters']==300 and r['fee']=='0.60'


def truck_data():
    v={'id':'real','plate':'苏A12345','size':2,'cartype':0,'plate_color':'BLUE','length_m':'6','height_m':'2.8','width_m':'2','full_gross_t':'8','rated_load_t':'5','axles':2}
    p={'id':'W','longitude':'121.45','latitude':'31.2','crs':'GCJ02'}
    return v,p,{**p,'id':'S','longitude':'121.5'}


def test_cache_key_binds_every_vehicle_parameter_time_coordinates_strategy_source():
    v,a,b=truck_data();req=road_request(v,a,b,'2026-11-11T08:00:00+08:00','LIVE');key=content_hash(req)
    for field in ['plate','size','cartype','plate_color','length_m','width_m','height_m','full_gross_t','rated_load_t','axles']:
        changed={**v,field:str(v[field])+'1'};assert content_hash(road_request(changed,a,b,req['departure_at'],'LIVE'))!=key
    for args in [(v,a,b,'2026-11-11T08:01:00+08:00','LIVE'),(v,a,b,req['departure_at'],'FIXTURE'),(v,{**a,'longitude':'121.46'},b,req['departure_at'],'LIVE')]:assert content_hash(road_request(*args))!=key


@pytest.mark.parametrize('failure',['missing_key','missing_profile','wrong_crs','zero_budget','timeout','restriction','permission','malformed','http'])
def test_live_road_fails_closed_and_calls_are_bounded(failure):
    v,a,b=truck_data();key=None if failure=='missing_key' else 'TEST_ONLY_NOT_REAL'
    settings=Settings(_env_file=None,amap_web_service_key=key)
    seen=[]
    def transport(request):
        seen.append(request)
        if failure=='timeout':raise httpx.ReadTimeout('redacted')
        if failure=='http':return httpx.Response(503)
        if failure=='malformed':return httpx.Response(200,json={'data':{}})
        if failure=='permission':return httpx.Response(200,json={'errcode':10001})
        return httpx.Response(200,json={'errcode':0,'data':{'route':{'paths':[{'distance':1000,'duration':60,'restriction':0}]}}})
    if failure=='missing_profile':v['axles']=None
    if failure=='wrong_crs':a['crs']='WGS84'
    road=TruckRoad(settings,0 if failure=='zero_budget' else 2,transport=httpx.MockTransport(transport))
    with pytest.raises(RoadError):road.segment(v,a,b,'2026-11-11T08:00:00+08:00')
    assert road.calls<=2
    for req in seen:
        assert 'direction/truck' in req.url.path and 'firsttime' not in req.url.params
        assert req.url.params['load']=='8' and req.url.params['weight']=='5' and req.url.params['consider_load']=='0'


def test_live_response_retains_polyline_but_is_not_time_verified_and_cache_hits():
    v,a,b=truck_data();settings=Settings(_env_file=None,amap_web_service_key='TEST_ONLY_NOT_REAL')
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'errcode':0,'data':{'route':{'paths':[{'distance':1000,'duration':60,'restriction':1,'steps':[{'polyline':'121.45,31.2;121.5,31.2'}]}]}}}))
    road=TruckRoad(settings,1,transport=transport);r=road.segment(v,a,b,'2026-11-11T08:00:00+08:00')
    assert not r['verified'] and len(r['polyline'])==2 and 'TEST_ONLY_NOT_REAL' not in json.dumps(r)
    assert road.segment(v,a,b,'2026-11-11T08:00:00+08:00')==r and road.calls==1 and road.hits==1


def test_manual_increase_delta_is_explicit_and_unpriced_routes_not_total():
    from app.routing_engine import compare_manual
    f=load_fixture('MIXED_56');previous=optimize(f,FixtureRoad(f))
    all_common=recalculate(f,{'routes':[],'common_ids':['A','B','C','D'],'unassigned_ids':[]},FixtureRoad(f))
    compare_manual(all_common,previous)
    assert all_common['comparison']['delta']=='74.00' and all_common['fees_complete']
    broken=recalculate(f,{'routes':[{'vehicle_id':'V10','task_ids':['A','B','C']}],'common_ids':['D'],'unassigned_ids':[]},FixtureRoad(f))
    compare_manual(broken,previous)
    assert not broken['fees_complete'] and broken['comparison']['delta'] is None


def test_oversized_ticket_never_split_wrong_warehouse_and_vehicle_profile_difference():
    f=load_fixture('MIXED_56');f['tasks'][0]['pieces']=11
    r=optimize(f,FixtureRoad(f));assert any(c['task_id']=='A' and c['pieces']==11 for c in r['common'])
    assert all('A' not in line['task_ids'] for line in r['routes'])
    f['vehicles'][0]['warehouse_id']='OTHER';f['warehouses'].append({**f['warehouse'],'id':'OTHER'})
    r=evaluate_route(normalize(f),'V10',['A'],FixtureRoad(f));assert any(e['code']=='WAREHOUSE' for e in r['errors'])
    f=load_fixture('MIXED_56');provider=FixtureRoad(f);a=f['warehouse'];b=f['tasks'][0];v=f['vehicles'][0]
    provider.segment(v,a,b,f['warehouse']['earliest_departure_at']);provider.segment({**v,'height_m':'4'},a,b,f['warehouse']['earliest_departure_at'])
    assert provider.misses==2


def test_independent_checker_binds_frozen_coordinates_even_with_rehashed_evidence():
    f=load_fixture('MIXED_56');r=optimize(f,FixtureRoad(f));segment=r['routes'][0]['segments'][0]
    segment['request']['destination']['longitude']='121.00'
    segment['evidence_hash']=content_hash({k:v for k,v in segment.items() if k!='evidence_hash'})
    assert any(e['code']=='ROAD_COORDINATES' for e in check_result(normalize(f),r)['issues'])


def test_unrepresentable_unload_time_is_conflict_not_job_crash():
    f=load_fixture('MIXED_56');f['tasks'][0]['unload_minutes']=10**12
    r=evaluate_route(normalize(f),'V10',['A'],FixtureRoad(f))
    assert r['errors'][0]['code']=='TIME_RANGE' and r['fee'] is None


@pytest.mark.parametrize('name',['=1+1','+1','-1','@SUM(1,2)','\x01=1+1','\x00=1+1'])
def test_excel_all_formula_prefixes_and_control_chars_are_literal(name):
    from io import BytesIO
    from openpyxl import load_workbook
    from app.plan_export import export_xlsx
    f=load_fixture('MIXED_56');f['tasks'][0]['name']=name
    r=optimize(f,FixtureRoad(f));plan={'id':'fixture','delivery_date':f['delivery_date'],'version':1,'status':'DRAFT','content_hash':'test','input_hash':'test','snapshot_id':'test','input_changed':False,'input':f,'result':r}
    wb=load_workbook(BytesIO(export_xlsx(plan)));cell=next(row[2] for row in list(wb['逐店分配'].rows)[1:] if row[0].value=='A')
    assert cell.data_type=='s' and (cell.value==name or cell.value.startswith('_x00'))
