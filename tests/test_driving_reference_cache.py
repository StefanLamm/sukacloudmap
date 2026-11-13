from copy import deepcopy
import time
import httpx
from pydantic import SecretStr
import pytest
from app.road_service import DrivingRoad
from app.planning_inputs import content_hash
from app.routing_engine import RoadError
from test_routing_engine import truck_data
from test_driving_reference import response, AT


def test_provider_path_is_reused_but_vehicle_and_time_evidence_are_rebound(settings):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');v,a,b=truck_data();calls=[];cache={}
    def handler(request):calls.append(request);return httpx.Response(200,json=response())
    road=DrivingRoad(settings,None,cache.get,lambda k,v:cache.__setitem__(k,deepcopy(v)),transport=httpx.MockTransport(handler))
    first=road.segment(v,a,b,AT)
    second=road.segment({**v,'id':'OTHER','plate':'苏A12346'},a,b,'2026-11-11T10:00:00+08:00')
    assert len(calls)==1 and road.stats()['provider_cache_hits']==1
    assert second['polyline']==first['polyline'] and second['request']['vehicle_id']=='OTHER'
    assert second['request']['departure_at']=='2026-11-11T10:00:00+08:00'
    assert first['evidence_hash']!=second['evidence_hash'] and not second['verified']
    assert second['provider_received_at']==first['received_at']
    assert content_hash({k:v for k,v in second.items() if k!='evidence_hash'})==second['evidence_hash']
    next_job=DrivingRoad(settings,0,cache.get,transport=httpx.MockTransport(lambda _:pytest.fail('No HTTP for cached provider path')))
    assert next_job.segment({**v,'id':'THIRD'},a,b,AT)['provider_cache_key']==second['provider_cache_key']
    assert next_job.calls==0
    with pytest.raises(RoadError):next_job.segment(v,b,a,AT)


def test_legacy_reference_seed_reuses_only_valid_recent_driving_evidence(settings):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');v,a,b=truck_data()
    source=DrivingRoad(settings,1,transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response())))
    first=source.segment(v,a,b,AT)
    target=DrivingRoad(settings,0,transport=httpx.MockTransport(lambda _:pytest.fail('No HTTP')))
    bad=deepcopy(first);bad['meters']+=1
    old=deepcopy(first);old['received_at']=int(time.time())-3601;old['evidence_hash']=content_hash({k:v for k,v in old.items() if k!='evidence_hash'})
    assert target.seed_reference_cache([bad,old])==0
    assert len(target.route_seed_samples)==1
    with pytest.raises(RoadError) as expired:
        target.segment({**v,'id':'OLD_ONLY'},a,b,AT)
    assert expired.value.code=='ROAD_CALL_BUDGET'
    assert target.seed_reference_cache([first])==1
    item=target.segment({**v,'id':'NEW'},a,b,'2026-11-12T12:00:00+08:00')
    assert item['time_status']=='CURRENT_TRAFFIC_REFERENCE' and not item['verified']
    assert item['received_at']==first['received_at'] and target.calls==0
