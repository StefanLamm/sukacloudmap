from io import BytesIO
import json

import httpx
from openpyxl import load_workbook
from pydantic import SecretStr, ValidationError
import pytest

from app.config import Settings
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, evidence, load_fixture, road_request
from app.routing_engine import RoadError, check_result, normalize, optimize, recalculate
from test_plan_api import frozen, new_job, waited, plan
from test_routing_engine import truck_data
from test_stage4 import basis

AT = '2026-11-11T08:00:00+08:00'


def response():
    return {'status': '1', 'infocode': '10000', 'route': {'paths': [
        {'distance': '1000', 'restriction': '0', 'cost': {'duration': '60'}, 'steps': [
            {'polyline': '121.45,31.2;121.47,31.2'}, {'polyline': '121.47,31.2;121.5,31.2'}]}]}}


def test_driving_parameters_geometry_cache_and_separate_evidence(settings):
    settings.amap_web_service_key = SecretStr('TEST_ONLY_KEY'); v, a, b = truck_data(); seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path == '/v5/direction/driving'
        assert set(request.url.params) == {'key','origin','destination','strategy','show_fields','output'}
        assert request.url.params['show_fields'] == 'cost,polyline'
        assert request.url.params['strategy'] == '32'
        return httpx.Response(200, json=response())
    road = DrivingRoad(settings, 1, transport=httpx.MockTransport(handler))
    item = road.segment(v, a, b, AT)
    assert item['meters'] == 1000 and item['seconds'] == 60 and len(item['polyline']) == 3
    assert item['source'] == 'DRIVING' and not item['verified'] and item['restriction'] == 'TRUCK_NOT_VERIFIED'
    assert item['request']['profile']['plate'] == v['plate'] and 'TEST_ONLY_KEY' not in json.dumps(item)
    assert content_hash(item['request']) != content_hash(road_request(v,a,b,AT,'LIVE'))
    assert road.segment(v,a,b,AT) == item and road.calls == road.hits == len(seen) == 1
    with pytest.raises(RoadError): road.segment(v,b,a,AT)
    assert len(seen) == 1


@pytest.mark.parametrize('bad', ['permission','http','timeout','format','missing_cost','missing_line','nan','negative','bad_point','wrong_crs','zero_budget','missing_key'])
def test_driving_errors_budgets_and_no_secret_in_errors(settings, bad):
    settings.amap_web_service_key = SecretStr('TEST_ONLY_KEY'); v,a,b = truck_data(); seen=[]
    body=response(); p=body['route']['paths'][0]
    if bad=='permission': body={'status':'0','infocode':'10012','info':'TEST_ONLY_KEY'}
    if bad=='format': body=[]
    if bad=='missing_cost': p.pop('cost')
    if bad=='missing_line': p['steps']=[]
    if bad=='nan': p['distance']='NaN'
    if bad=='negative': p['cost']['duration']='-1'
    if bad=='bad_point': b['longitude']='Infinity'
    if bad=='wrong_crs': b['crs']='UNKNOWN'
    if bad=='missing_key': settings.amap_web_service_key=None
    def handler(request):
        seen.append(request)
        if bad=='timeout': raise httpx.ReadTimeout('TEST_ONLY_KEY')
        return httpx.Response(503 if bad=='http' else 200,json=body)
    road=DrivingRoad(settings,0 if bad=='zero_budget' else 2,transport=httpx.MockTransport(handler))
    with pytest.raises(RoadError) as exc: road.segment(v,a,b,AT)
    assert 'TEST_ONLY_KEY' not in exc.value.message and road.calls==len(seen)<=2
    if bad in ['bad_point','wrong_crs','missing_key','zero_budget']: assert len(seen)==0


class ReferenceFixture:
    def segment(self,v,a,b,at):
        return evidence(road_request(v,a,b,at,'DRIVING',32),1000,60,'DRIVING',False,[[121.45,31.2],[121.5,31.2]],'TRUCK_NOT_VERIFIED')
    def stats(self): return {'source':'DRIVING','calls':0,'cache_hits':0}


def reference_body():
    f=load_fixture('MIXED_56');f['source']='FROZEN'
    return f


def test_driving_solver_keeps_reference_routes_and_independent_check_blocks_confirmation():
    f=reference_body(); result=optimize(f,ReferenceFixture())
    assert result['routes'] and not result['unassigned'] and result['road_source']=='DRIVING'
    assert result['execution_scope']=='REFERENCE_ONLY' and not result['confirmable']
    assert any(e['code']=='DRIVING_REFERENCE' for e in result['check']['issues'])
    for r in result['routes']:
        assert not r['road_verified'] and not r['errors']
        # A forged verified flag cannot turn a driving response into truck proof.
        for s in r['segments']:
            s['verified']=True;s['evidence_hash']=content_hash({k:v for k,v in s.items() if k!='evidence_hash'})
    assert not check_result(normalize(f),result)['valid']


@pytest.mark.parametrize('conflict,expected',[('capacity','CAPACITY'),('warehouse','WAREHOUSE'),('quote','QUOTE_INCOMPATIBLE'),('time','LATEST_ARRIVAL')])
def test_reference_mode_preserves_physical_and_business_constraints(conflict,expected):
    f=reference_body();v=f['vehicles'][0];ids=[f['tasks'][0]['id'],f['tasks'][1]['id']]
    if conflict=='capacity': v['max_pieces']=1
    if conflict=='warehouse': f['tasks'][1]['warehouse_id']='OTHER'
    if conflict=='quote': f['tasks'][1]['quote_id']=None
    if conflict=='time': f['tasks'][0]['latest_arrival_at']='2026-11-11T00:00:00+08:00'
    r=recalculate(f,{'routes':[{'vehicle_id':v['id'],'task_ids':ids}],'common_ids':[t['id'] for t in f['tasks'][2:]],'unassigned_ids':[]},ReferenceFixture())
    assert not r['confirmable'] and not r['check']['valid']
    assert any(e['code']==expected for e in r['routes'][0]['errors'])


def test_reference_job_capabilities_export_and_confirmation_api(basis,app,monkeypatch):
    c,w,q,s,s2,v=basis
    settings=app.state.settings
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True;settings.amap_web_service_key=SecretStr('TEST_ONLY_KEY')
    # Exercise the actual adapter through the durable worker without external calls.
    original=DrivingRoad.__init__
    def init(self,*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(lambda request:httpx.Response(200,json=response()))
        original(self,*args,**kwargs)
    monkeypatch.setattr(DrivingRoad,'__init__',init)
    for obj,path in [(w,'warehouses'),(s,'stores'),(s2,'stores')]:
        r=c.patch('/api/'+path+'/'+obj['id'],json={'expected_revision':obj['revision'],'crs':'GCJ02','longitude':'121.45','latitude':'31.2'})
        assert r.status_code==200,r.text
    snapshot=frozen(basis)
    cap=c.get('/api/planning/capabilities').json()
    assert cap['route_mode']=='DRIVING' and cap['max_calls'] is None and cap['live_calls_enabled']
    draft={'routes':[{'vehicle_id':v['id'],'task_ids':[s['id'],s2['id']]}],'common_ids':[],'unassigned_ids':[]}
    job=waited(c,new_job(c,snapshot,kind='MANUAL',draft=draft,call_limit=2))
    assert job['status']=='COMPLETED',job
    p=plan(c,job['plan_id']);assert p['result']['road_source']=='DRIVING' and not p['result']['confirmable']
    assert p['result']['algorithm']['road_stats']['calls']==1
    assert p['result']['algorithm']['road_stats']['provider_cache_hits']==1
    assert c.post('/api/planning/plans/'+p['id']+'/confirm',json={'expected_head':1}).status_code==422
    book=load_workbook(BytesIO(c.get('/api/planning/plans/'+p['id']+'/export.xlsx').content))
    summary=dict(book['版本与汇总'].values)
    assert '驾车参考费用（元）' in summary and '未核验货车限行' in summary['说明']
    assert '预计到店' in next(book['逐店分配'].values)


def test_isolated_grid_cannot_enable_driving_network_calls():
    with pytest.raises(ValidationError): Settings(_env_file=None,environment='test',planning_road_source='TEST_GRID',driving_live_calls_enabled=True)
