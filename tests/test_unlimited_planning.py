import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr, ValidationError
import pytest

from app.config import Settings
from app.main import create_app
from app.plan_api import NewJob
from app.road_service import DrivingRoad
from app.routing_engine import RoadError
from conftest import login
from test_amap_qps import Clock, pacer
from test_driving_reference import AT, response
from test_plan_api import frozen, new_job, plan, waited
from test_routing_engine import truck_data
from test_stage4 import basis


@pytest.mark.parametrize('value',[None,'unlimited','none',''])
def test_unlimited_config_and_job_request_keep_no_count_ceiling(value):
    settings=Settings(_env_file=None,driving_job_max_calls=value)
    assert settings.driving_job_max_calls is None
    assert NewJob(snapshot_id='S',expected_head=0).call_limit is None
    assert NewJob(snapshot_id='S',expected_head=0,call_limit=1000).call_limit==1000


def test_unlimited_adapter_can_pass_ten_calls_but_still_paces_caches_and_cancels(settings,tmp_path):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');clock=Clock();v,a,b=truck_data();starts=[]
    def handler(request):
        starts.append(clock());return httpx.Response(200,json=response())
    road=DrivingRoad(settings,None,transport=httpx.MockTransport(handler),limiter=pacer(tmp_path/'qps.db',clock),cancelled=lambda:len(starts)>=12)
    destinations=[{**b,'id':'S'+str(i),'longitude':str(121.5+i*.001)} for i in range(12)]
    for destination in destinations:road.segment(v,a,destination,AT)
    assert road.calls==12 and road.stats()['call_limit'] is None and starts[-1]-starts[0]>11/3
    road.segment(v,a,destinations[0],AT);assert road.hits==1 and road.calls==12
    with pytest.raises(RoadError) as error:road.segment(v,a,{**b,'id':'NEW'},AT)
    assert error.value.code=='CANCELLED' and road.calls==12


def test_unlimited_count_does_not_create_infinite_provider_retries(settings,tmp_path):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');clock=Clock();v,a,b=truck_data()
    road=DrivingRoad(settings,None,transport=httpx.MockTransport(lambda _:httpx.Response(429)),limiter=pacer(tmp_path/'qps.db',clock))
    with pytest.raises(RoadError) as error:road.segment(v,a,b,AT)
    assert error.value.code=='ROAD_QPS_LIMIT' and road.calls==2


def test_manual_accounting_accepts_unlimited_and_preserves_user_sequence(basis,app,monkeypatch):
    client,w,q,s,s2,v=basis;settings=app.state.settings
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True;settings.driving_job_max_calls=None;settings.amap_web_service_key=SecretStr('TEST_ONLY')
    original=DrivingRoad.__init__
    def init(self,*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(lambda _:httpx.Response(200,json=response()))
        original(self,*args,**kwargs)
    monkeypatch.setattr(DrivingRoad,'__init__',init)
    for entity,kind in [(w,'warehouses'),(s,'stores'),(s2,'stores')]:
        assert client.patch('/api/'+kind+'/'+entity['id'],json={'expected_revision':entity['revision'],'crs':'GCJ02','longitude':'121.45','latitude':'31.2'}).status_code==200
    snapshot=frozen(basis);draft={'routes':[{'vehicle_id':v['id'],'task_ids':[s2['id'],s['id']]}],'common_ids':[],'unassigned_ids':[]}
    cap=client.get('/api/planning/capabilities').json();assert cap['max_calls'] is None and cap['qps']==3
    job=waited(client,new_job(client,snapshot,kind='MANUAL',draft=draft,call_limit=None))
    assert job['status']=='COMPLETED',job
    saved=plan(client,job['plan_id']);assert saved['result']['routes'][0]['task_ids']==[s2['id'],s['id']]
    assert saved['result']['algorithm']['road_stats']['call_limit'] is None
    assert saved['result']['algorithm']['road_stats']['calls']==1
    assert saved['result']['algorithm']['road_stats']['provider_cache_hits']==1
    assert not saved['result']['confirmable']


def test_temporary_https_origin_keeps_secure_login_and_host_origin_checks(app,settings):
    path=Path(__file__).resolve().parents[1]/'scripts/start-external-origin.py'
    spec=importlib.util.spec_from_file_location('external_origin',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    origin='https://test-suka.trycloudflare.com';prod=module.external_settings(origin)
    prod.database_url=settings.database_url;prod.map_auto_geocode_enabled=False
    assert prod.environment=='production' and prod.secure_cookie and prod.allowed_hosts==['test-suka.trycloudflare.com']
    with TestClient(create_app(prod),base_url=origin) as client:
        response=login(client)
        assert '__Host-suka_external_test_session=' in response.headers['set-cookie']
        assert 'Secure' in response.headers['set-cookie'] and 'HttpOnly' in response.headers['set-cookie']
        assert client.get('/api/auth/me').status_code==200
        assert client.post('/api/auth/logout',headers={'origin':'https://untrusted.example'}).status_code==403
        assert client.get('/api/auth/me',headers={'host':'untrusted.example'}).status_code==400
    for url in ['http://test-suka.trycloudflare.com','https://evil.example','https://a.trycloudflare.com@evil.example','https://a.trycloudflare.com/path']:
        with pytest.raises(ValueError):module.external_settings(url)
