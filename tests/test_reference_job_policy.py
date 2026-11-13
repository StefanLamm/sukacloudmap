from copy import deepcopy
import json
import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from app.road_service import DrivingRoad, TruckRoad
from app.models import PlanningJob
from test_stage4 import basis
from test_plan_api import frozen,new_job,waited,plan
from test_driving_reference import response


def test_explicit_refresh_is_rejected_when_driving_live_service_is_disabled(basis,app):
    app.state.settings.planning_route_mode='DRIVING'
    client,*_=basis;snapshot=frozen(basis)
    reply=client.post('/api/planning/jobs',json={'snapshot_id':snapshot,'expected_head':0,'refresh_roads':True})
    assert reply.status_code==422 and reply.json()['code']=='LIVE_CALLS_DISABLED'


@pytest.mark.parametrize('mode,source,live', [('TRUCK','LIVE',True),('TRUCK','LIVE',False),
                                           ('TRUCK','TEST_GRID',False),('DRIVING','TEST_GRID',False)])
@pytest.mark.parametrize('kind', ['OPTIMIZE','MANUAL'])
def test_refresh_is_explicitly_unsupported_outside_real_driving_mode(basis,app,monkeypatch,mode,source,live,kind):
    client,w,q,s,s2,v=basis
    settings=app.state.settings;settings.planning_route_mode=mode;settings.planning_road_source=source
    settings.truck_live_calls_enabled=live;settings.driving_live_calls_enabled=False
    settings.amap_web_service_key=SecretStr('TEST_ONLY_POLICY')
    def no_network(*args,**kwargs):raise AssertionError('Rejected refresh must never reach a road adapter')
    monkeypatch.setattr(TruckRoad,'segment',no_network);monkeypatch.setattr(DrivingRoad,'segment',no_network)
    snapshot=frozen(basis)
    payload={'snapshot_id':snapshot,'expected_head':0,'refresh_roads':True,'kind':kind}
    if kind=='MANUAL':payload['draft']={'routes':[],'common_ids':[s['id'],s2['id']],'unassigned_ids':[]}
    reply=client.post('/api/planning/jobs',json=payload)
    assert reply.status_code==422 and reply.json()['code']=='ROAD_REFRESH_UNSUPPORTED'
    assert '当前道路模式不支持' in reply.json()['message']
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(PlanningJob))==0


@pytest.mark.parametrize('source', ['LIVE','TEST_GRID'])
def test_usual_manual_accounting_remains_available_for_truck_without_refresh(basis,app,source):
    client,w,q,s,s2,v=basis
    settings=app.state.settings;settings.planning_route_mode='TRUCK';settings.planning_road_source=source
    snapshot=frozen(basis)
    draft={'routes':[],'common_ids':[s['id'],s2['id']],'unassigned_ids':[]}
    job=waited(client,new_job(client,snapshot,kind='MANUAL',draft=draft,call_limit=0))
    assert job['status']=='COMPLETED'
    result=plan(client,job['plan_id'])
    assert result['result']['fees_complete'] and not result['result']['unassigned']
    with app.state.sessions() as db:
        assert json.loads(db.get(PlanningJob,job['id']).request)['refresh_roads'] is False


def test_worker_passes_policy_and_refreshes_existing_saved_roads(basis,app,monkeypatch):
    client,w,q,s,s2,v=basis
    settings=app.state.settings;settings.planning_route_mode='DRIVING';settings.driving_live_calls_enabled=True;settings.amap_web_service_key=SecretStr('TEST_ONLY_POLICY')
    calls=[];options=[];original=DrivingRoad.__init__
    def handler(request):
        calls.append(request);payload=response();payload['route']['paths'][0]['distance']=str(1000*len(calls));return httpx.Response(200,json=payload)
    def init(self,*args,**kwargs):
        options.append({key:kwargs.get(key) for key in ('allow_historical','force_refresh')})
        kwargs['transport']=httpx.MockTransport(handler);original(self,*args,**kwargs)
    monkeypatch.setattr(DrivingRoad,'__init__',init)
    for entity,kind in [(w,'warehouses'),(s,'stores'),(s2,'stores')]:
        assert client.patch('/api/'+kind+'/'+entity['id'],json={'expected_revision':entity['revision'],'crs':'GCJ02','longitude':'121.45','latitude':'31.2'}).status_code==200
    snapshot=frozen(basis)
    first=waited(client,new_job(client,snapshot,budget_seconds=2));assert first['status']=='COMPLETED'
    previous=plan(client,first['plan_id']);before=deepcopy(previous)
    # Automatic deep search can seed from history; final verification is always
    # a new provider batch even when the request omits refresh_roads.
    assert len(calls)==2 and options[0]=={'allow_historical':True,'force_refresh':False}
    assert previous['result']['algorithm']['final_validation']['actual_calls']==1
    second=waited(client,new_job(client,snapshot,expected=previous['version'],refresh_roads=True,budget_seconds=2));assert second['status']=='COMPLETED'
    latest=plan(client,second['plan_id'])
    assert len(calls)==3 and options[1]=={'allow_historical':True,'force_refresh':False}
    assert latest['result']['algorithm']['road_stats']['calls']==1
    assert latest['result']['comparison']['previous_total']==previous['total_fee']
    assert latest['blocking_issue_count']==0
    with app.state.sessions() as db:assert json.loads(db.get(PlanningJob,second['id']).request)['refresh_roads'] is True
    assert plan(client,first['plan_id'])['content_hash']==before['content_hash']
    draft={'routes':[{'vehicle_id':r['vehicle_id'],'task_ids':r['task_ids']} for r in latest['result']['routes']],
           'common_ids':[r['task_id'] for r in latest['result']['common']],'unassigned_ids':[]}
    third=waited(client,new_job(client,snapshot,expected=latest['version'],kind='MANUAL',parent_id=latest['id'],
                               draft=draft,refresh_roads=True))
    assert third['status']=='COMPLETED'
    manual=plan(client,third['plan_id'])
    assert len(calls)==4 and options[2]=={'allow_historical':False,'force_refresh':True}
    assert manual['result']['algorithm']['road_stats']['calls']==1
    assert manual['result']['comparison']['previous_total']==latest['total_fee']
    assert [r['task_ids'] for r in manual['result']['routes']]==[r['task_ids'] for r in latest['result']['routes']]
    with app.state.sessions() as db:
        assert json.loads(db.get(PlanningJob,third['id']).request)['refresh_roads'] is True
