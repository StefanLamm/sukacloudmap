"""Production count policy, exercised only against an isolated mock provider."""
import json

import httpx
from pydantic import SecretStr
import pytest

from app.models import PlanningJob
from app.road_service import DrivingRoad, TruckRoad
from app.routing_engine import RoadError
from test_amap_qps import Clock, pacer, truck_response
from test_driving_reference import AT, response
from test_plan_api import frozen, new_job, plan, waited
from test_routing_engine import truck_data
from test_stage4 import basis


@pytest.mark.parametrize('adapter,payload,mode',[(DrivingRoad,response,'DRIVING'),(TruckRoad,truck_response,'TRUCK')])
@pytest.mark.parametrize('fresh_only', [False, True])
def test_production_exceeds_350_and_keeps_shared_qps_cache_and_cancel(settings,tmp_path,adapter,payload,mode,fresh_only):
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    settings.planning_route_mode=mode
    settings.driving_live_calls_enabled=settings.truck_live_calls_enabled=True
    clock=Clock();v,a,b=truck_data();starts=[];cancel=[False]
    def handler(request):
        starts.append(clock());return httpx.Response(200,json=payload())
    road=adapter(settings,350,production_planning=True,fresh_only=fresh_only,transport=httpx.MockTransport(handler),
                 limiter=pacer(tmp_path/'shared.db',clock),timer=clock,cancelled=lambda:cancel[0])
    # Legacy and temporary search quotas cannot restore a production ceiling.
    road.call_limit=10;road.call_limit=0
    for i in range(361):
        road.segment(v,a,{**b,'id':f'S{i}','longitude':str(121.5+i*.0001)},AT)
    assert road.calls==361 and road.call_limit is None
    assert road.stats()['ignored_call_limit_assignments']==3
    assert road.stats()['total_call_policy']=='PRODUCTION_UNLIMITED'
    assert all(sum(t<=at<t+1 for at in starts)<=3 for t in starts)
    cancel[0]=True
    # The frozen response is usable without issuing a new HTTP request.
    road.segment(v,a,{**b,'id':'S360','longitude':str(121.5+360*.0001)},AT)
    assert road.calls==361
    with pytest.raises(RoadError) as exc:road.segment(v,a,{**b,'id':'NEW','longitude':'121.6'},AT)
    assert exc.value.code=='CANCELLED' and road.calls==361


def production_road(settings,tmp_path,clock,handler,**kwargs):
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    return DrivingRoad(settings,production_planning=True,transport=httpx.MockTransport(handler),
                       limiter=pacer(tmp_path/'shared.db',clock),timer=clock,**kwargs)


def test_wall_deadline_interrupts_qps_wait_without_becoming_cancelled(settings,tmp_path):
    clock=Clock();requests=[];v,a,b=truck_data()
    road=production_road(settings,tmp_path,clock,lambda request:(requests.append(request) or httpx.Response(200,json=response())),wall_deadline=100.1)
    road.segment(v,a,b,AT)
    with pytest.raises(RoadError) as exc:road.segment(v,a,{**b,'id':'NEW','longitude':'121.6'},AT)
    assert exc.value.code=='WALL_BUDGET' and road.calls==1 and len(requests)==1
    assert road.stats()['network_stop_reason']=='WALL_BUDGET'
    assert road.stats()['failures']==0


def test_response_past_deadline_is_rejected_and_http_timeout_uses_remaining_wait(settings,tmp_path):
    clock=Clock();requests=[];v,a,b=truck_data()
    def handler(request):
        requests.append(request);clock.value+=.3;return httpx.Response(200,json=response())
    road=production_road(settings,tmp_path,clock,handler,wall_deadline=100.2)
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='WALL_BUDGET'
    assert all(0<value<=.201 for value in requests[0].extensions['timeout'].values())
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='WALL_BUDGET' and road.calls==1 and len(requests)==1
    assert road.stats()['failures']==0


def test_cancel_during_response_does_not_accept_or_cache_it(settings,tmp_path):
    clock=Clock();v,a,b=truck_data();cancel=[False];requests=[]
    def handler(request):
        requests.append(request);cancel[0]=True;return httpx.Response(200,json=response())
    road=production_road(settings,tmp_path,clock,handler,cancelled=lambda:cancel[0])
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='CANCELLED' and road.calls==1 and len(requests)==1
    assert not road.local and road.stats()['failures']==0


def test_network_timeout_at_wall_cutoff_keeps_deadline_reason(settings,tmp_path):
    clock=Clock();v,a,b=truck_data();requests=[]
    def handler(request):
        requests.append(request);clock.value+=.3
        raise httpx.ReadTimeout('mock timeout',request=request)
    road=production_road(settings,tmp_path,clock,handler,wall_deadline=100.2)
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='WALL_BUDGET' and road.calls==1 and len(requests)==1
    assert road.stats()['failures']==0


def test_external_control_propagates_and_disabled_is_separate_from_none(settings,tmp_path):
    from app.routing_engine import StopSearch
    clock=Clock();v,a,b=truck_data();requests=[]
    road=production_road(settings,tmp_path,clock,lambda request:(requests.append(request) or httpx.Response(200,json=response())))
    def expired_phase():raise StopSearch('PHASE_BUDGET')
    road.network_control=expired_phase
    with pytest.raises(StopSearch) as exc:road.segment(v,a,b,AT)
    assert exc.value.reason=='PHASE_BUDGET' and not requests
    road.network_control=None;settings.driving_live_calls_enabled=False
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='LIVE_CALLS_DISABLED' and road.call_limit is None and not requests


@pytest.mark.parametrize('kind,optimize,refresh,legacy',[('OPTIMIZE',False,False,350),('OPTIMIZE',False,True,0),
    ('MANUAL',False,False,10),('MANUAL',True,False,350),('MANUAL',False,True,0)])
def test_production_job_entrypoints_ignore_legacy_limits_and_share_deadline(basis,app,monkeypatch,kind,optimize,refresh,legacy):
    client,w,q,s,s2,v=basis;settings=app.state.settings
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True
    settings.driving_job_max_calls=350;settings.amap_web_service_key=SecretStr('TEST_ONLY')
    options=[];original=DrivingRoad.__init__
    def init(self,*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(lambda _:httpx.Response(200,json=response()))
        original(self,*args,**kwargs);options.append(self)
    monkeypatch.setattr(DrivingRoad,'__init__',init)
    for entity,entity_kind in [(w,'warehouses'),(s,'stores'),(s2,'stores')]:
        assert client.patch('/api/'+entity_kind+'/'+entity['id'],json={'expected_revision':entity['revision'],'crs':'GCJ02','longitude':'121.45','latitude':'31.2'}).status_code==200
    snapshot=frozen(basis)
    draft={'routes':[{'vehicle_id':v['id'],'task_ids':[s['id'],s2['id']]}],'common_ids':[],'unassigned_ids':[]}
    args={'kind':kind,'call_limit':legacy,'budget_seconds':2,'wall_budget_seconds':20,'refresh_roads':refresh}
    if kind=='MANUAL':args.update(draft=draft,optimize_routes=optimize)
    job=waited(client,new_job(client,snapshot,**args))
    assert job['status']=='COMPLETED',job
    saved=plan(client,job['plan_id'])['result'];road=options[0]
    assert road.production_planning and road.call_limit is None and callable(road.network_control)
    assert road.wall_deadline==saved['algorithm']['job_wall_deadline']
    assert saved['algorithm']['job_wall_budget_seconds']==20
    assert saved['algorithm']['requested_compute_budget_seconds']==2
    assert saved['algorithm']['total_call_policy']=='PRODUCTION_UNLIMITED'
    with app.state.sessions() as db:
        request=json.loads(db.get(PlanningJob,job['id']).request)
        assert request['call_limit'] is None


def test_enabled_missing_key_cannot_be_disguised_by_old_zero_request(basis,app):
    client,*_=basis;settings=app.state.settings
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True
    snapshot=frozen(basis)
    reply=client.post('/api/planning/jobs',json={'snapshot_id':snapshot,'expected_head':0,'call_limit':0})
    assert reply.status_code==422 and reply.json()['code']=='LIVE_KEY_MISSING'


@pytest.mark.parametrize('reason,status',[('CANCELLED','CANCELLED'),('WALL_BUDGET','FAILED')])
def test_worker_preserves_cancel_and_wall_stop_reasons(basis,app,monkeypatch,reason,status):
    from app.routing_engine import StopSearch
    client,*_=basis;settings=app.state.settings
    settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    def stop(body,road,*args,**kwargs):
        if reason=='WALL_BUDGET':
            road.wall_deadline=0;road.check_network_control()
        raise StopSearch(reason)
    monkeypatch.setattr('app.plan_service.optimize_deep',stop)
    snapshot=frozen(basis);job=waited(client,new_job(client,snapshot,budget_seconds=1))
    assert job['status']==status and job['error_code']==reason
    assert job['plan_id'] is None
