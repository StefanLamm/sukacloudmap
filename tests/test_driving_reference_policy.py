"""Independent policy checks for current, historical and refreshed driving paths.

All sources are local fixtures, all clocks are controlled, and every HTTP
transport rejects the network unless an explicit synthetic response is supplied.
"""
from copy import deepcopy
import gzip
import json
import sqlite3
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.amap_limits import RateLimitWaitError
from app.planning_inputs import content_hash, encode
from app.road_graph import ReferenceGraph, SCHEMA
from app.road_service import DrivingRoad, PROFILE, TruckRoad, evidence, road_request
from app.routing_engine import RoadError
from test_driving_reference import AT, response
from test_routing_engine import truck_data


NOW = 1794528000
NEXT_AT = '2026-11-14T11:30:00+08:00'


class NoWait:
    def acquire(self, service, *, cancelled):
        if cancelled():raise RateLimitWaitError('CANCELLED')
    def defer(self, service, retry):
        pass


@pytest.fixture
def policy(settings, monkeypatch):
    import app.road_service as service
    clock = SimpleNamespace(wall=NOW)
    monkeypatch.setattr(service.time, 'time', lambda: clock.wall)
    settings.amap_web_service_key = SecretStr('TEST_ONLY_REFERENCE_POLICY')
    roads = []
    def create(limit=0, cache=None, transport=None, **kwargs):
        road = DrivingRoad(settings, limit, cache_get=(cache if cache is not None else {}).get,
                    cache_put=lambda key,item:(cache.__setitem__(key,deepcopy(item)) if cache is not None else None),
                    transport=transport or httpx.MockTransport(lambda _:pytest.fail('Unexpected fixture HTTP')),
                    limiter=kwargs.pop('limiter',NoWait()), **kwargs)
        roads.append(road)
        return road
    yield clock, create
    for road in roads:road.close()


def fixture_source(age=0, *, form='provider', meters=1000, notice='原始接口参考说明'):
    vehicle,a,b=truck_data()
    request=(DrivingRoad.reference_request(a,b) if form=='provider'
             else road_request(vehicle,a,b,AT,'DRIVING',32))
    item=evidence(request,meters,60,'DRIVING',False,
                  [[float(a['longitude']),float(a['latitude'])],[float(b['longitude']),float(b['latitude'])]],
                  'TRUCK_NOT_VERIFIED',time_status='CURRENT_TRAFFIC_REFERENCE',received_at=NOW-age,notice=notice)
    return content_hash(DrivingRoad.reference_request(a,b)),item


def raw_graph(path, key, item, *, content=None):
    with sqlite3.connect(path) as connection:
        connection.execute(SCHEMA)
        connection.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',
                    (key,'W','S',item['meters'],item['seconds'],item['received_at'],item['evidence_hash'],
                     content if content is not None else gzip.compress(encode(item).encode(),mtime=0)))
        connection.commit()


def assert_bound_hash(item, vehicle, at):
    assert item['request']['vehicle_id']==vehicle['id']
    assert item['request']['profile']=={field:vehicle.get(field) for field in PROFILE}
    assert item['request']['departure_at']==at
    assert item['evidence_hash']==content_hash({k:v for k,v in item.items() if k!='evidence_hash'})
    assert item['source']=='DRIVING' and item['verified'] is False


@pytest.mark.parametrize('age',[0,3599])
def test_default_accepts_only_current_reference_window(policy,age):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(age)
    road=create(cache={key:item})
    bound=road.segment(vehicle,a,b,AT)
    assert bound['time_status']=='CURRENT_TRAFFIC_REFERENCE'
    assert bound['received_at']==NOW-age and road.calls==0
    assert_bound_hash(bound,vehicle,AT)


@pytest.mark.parametrize('age',[3600,7200,7*86400-1])
def test_default_does_not_use_historical_persistent_or_graph_references(policy,tmp_path,age):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(age)
    path=tmp_path/'graph.db';raw_graph(path,key,item)
    road=create(cache={key:item});road.graph=ReferenceGraph(path)
    with pytest.raises(RoadError) as failure:road.segment(vehicle,a,b,AT)
    assert failure.value.code=='ROAD_CALL_BUDGET' and road.calls==0


@pytest.mark.parametrize('form',['provider','bound'])
@pytest.mark.parametrize('age',[3600,2*86400,7*86400-1])
def test_explicit_history_rebinds_original_real_path_with_historical_notice(policy,tmp_path,age,form):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(age,form=form)
    original=deepcopy(item);path=tmp_path/'graph.db';raw_graph(path,key,item)
    cache={};road=create(cache=cache,allow_historical=True);road.graph=ReferenceGraph(path)
    changed={**vehicle,'id':'SECOND_CAR','plate':'苏A99999','length_m':'5.9','full_gross_t':'7.5'}
    bound=road.segment(changed,a,b,NEXT_AT)
    assert bound['meters']==item['meters'] and bound['polyline']==item['polyline']
    assert bound['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'
    assert '历史' in bound['notice'] and '未核验' in bound['notice']
    assert bound['received_at']==bound['provider_received_at']==NOW-age
    assert bound['provider_evidence_hash']==original['evidence_hash']
    assert bound['evidence_hash']!=item['evidence_hash']
    assert_bound_hash(bound,changed,NEXT_AT)
    assert road.calls==0 and road.stats()['historical_segment_hits']==1
    assert not cache and not road.graph.put_reference(key,bound)
    assert item==original
    saved=road.graph.get_recent(key,ttl_seconds=7*86400)
    assert saved==original and saved['notice']=='原始接口参考说明'


@pytest.mark.parametrize('age',[7*86400,7*86400+1,20*86400])
def test_history_has_a_hard_seven_day_boundary(policy,tmp_path,age):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(age)
    path=tmp_path/'graph.db';raw_graph(path,key,item)
    road=create(cache={key:item},allow_historical=True);road.graph=ReferenceGraph(path)
    with pytest.raises(RoadError) as failure:road.segment(vehicle,a,b,AT)
    assert failure.value.code=='ROAD_CALL_BUDGET' and road.calls==0


@pytest.mark.parametrize('history',[False,True])
def test_local_current_cache_cannot_silently_outlive_one_hour(policy,history):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(3599)
    road=create(cache={key:item},allow_historical=history)
    first=road.segment(vehicle,a,b,AT)
    clock.wall+=2
    if history:
        second=road.segment(vehicle,a,b,AT)
        assert second['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'
        assert second['received_at']==first['received_at'] and second['evidence_hash']!=first['evidence_hash']
    else:
        with pytest.raises(RoadError):road.segment(vehicle,a,b,AT)


def test_local_historical_cache_cannot_outlive_seven_days(policy):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(7*86400-1)
    road=create(cache={key:item},allow_historical=True)
    assert road.segment(vehicle,a,b,AT)['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'
    clock.wall+=2
    with pytest.raises(RoadError):road.segment(vehicle,a,b,AT)


def test_full_bound_historical_cache_is_rebound_not_returned_as_current(policy):
    clock,create=policy;vehicle,a,b=truck_data();_,item=fixture_source(7200,form='bound')
    key=content_hash(item['request']);road=create(cache={key:item},allow_historical=True)
    bound=road.segment(vehicle,a,b,AT)
    assert bound['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'
    assert bound['received_at']==item['received_at']
    assert bound['evidence_hash']!=item['evidence_hash']
    assert_bound_hash(bound,vehicle,AT)


def test_force_refresh_ignores_old_full_and_provider_caches_then_reuses_new_measurement(policy,tmp_path):
    clock,create=policy;vehicle,a,b=truck_data();provider_key,provider=fixture_source(7200,meters=111)
    _,full=fixture_source(60,form='bound',meters=222)
    changed={**vehicle,'id':'ANOTHER','plate':'苏A77777','height_m':'2.9'}
    other=deepcopy(full);other['request']=road_request(changed,a,b,NEXT_AT,'DRIVING',32)
    other['evidence_hash']=content_hash({k:v for k,v in other.items() if k!='evidence_hash'})
    cache={provider_key:provider,content_hash(full['request']):full,content_hash(other['request']):other}
    path=tmp_path/'graph.db';raw_graph(path,provider_key,provider)
    calls=[]
    def handler(request):
        calls.append(request);body=response();body['route']['paths'][0]['distance']='3333'
        return httpx.Response(200,json=body)
    road=create(limit=None,cache=cache,force_refresh=True,allow_historical=True,transport=httpx.MockTransport(handler))
    road.graph=ReferenceGraph(path)
    first=road.segment(vehicle,a,b,AT)
    second=road.segment(changed,a,b,NEXT_AT)
    assert first['meters']==second['meters']==3333
    assert len(calls)==road.calls==1
    assert first['received_at']==second['received_at']==clock.wall
    assert first['time_status']==second['time_status']=='CURRENT_TRAFFIC_REFERENCE'
    assert first['evidence_hash']!=second['evidence_hash']
    assert_bound_hash(first,vehicle,AT);assert_bound_hash(second,changed,NEXT_AT)
    assert not first['verified'] and not second['verified']
    assert road.graph.get_recent(provider_key)['received_at']==clock.wall


def test_force_refresh_does_not_accept_an_injected_preexisting_local_binding(policy):
    clock,create=policy;vehicle,a,b=truck_data();_,old=fixture_source(60,form='bound',meters=111)
    calls=[]
    def handler(request):calls.append(request);return httpx.Response(200,json=response())
    road=create(limit=1,force_refresh=True,transport=httpx.MockTransport(handler))
    road.local[content_hash(old['request'])]=old
    bound=road.segment(vehicle,a,b,AT)
    assert bound['meters']==1000 and road.calls==len(calls)==1


def test_force_refresh_is_per_direction_and_provider_coordinates_within_the_job(policy):
    clock,create=policy;vehicle,a,b=truck_data();calls=[]
    def handler(request):calls.append(request);return httpx.Response(200,json=response())
    road=create(limit=2,force_refresh=True,transport=httpx.MockTransport(handler))
    first=road.segment(vehicle,a,b,AT)
    same=road.segment({**vehicle,'id':'NEW_CAR'},a,{**b,'id':'RENAMED_SAME_POINT'},NEXT_AT)
    reverse=road.segment(vehicle,b,a,AT)
    assert road.calls==len(calls)==2
    assert first['polyline']==same['polyline'] and same['request']['to_id']=='RENAMED_SAME_POINT'
    assert reverse['request']['from_id']==b['id'] and reverse['request']['to_id']==a['id']


def test_failed_forced_refresh_does_not_fall_back_or_mark_old_history_as_refreshed(policy,tmp_path):
    clock,create=policy;vehicle,a,b=truck_data();key,old=fixture_source(2*86400)
    cache={key:old};path=tmp_path/'graph.db';raw_graph(path,key,old);calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)<=2:raise httpx.ReadTimeout('synthetic timeout')
        return httpx.Response(200,json=response())
    road=create(limit=None,cache=cache,allow_historical=True,force_refresh=True,transport=httpx.MockTransport(handler))
    road.graph=ReferenceGraph(path)
    with pytest.raises(RoadError) as failure:road.segment(vehicle,a,b,AT)
    assert failure.value.code=='ROAD_TIMEOUT' and road.calls==2
    assert road.graph.get_recent(key,ttl_seconds=7*86400)==old
    fresh=road.segment(vehicle,a,b,AT)
    rebound=road.segment({**vehicle,'id':'NEXT_CAR'},a,b,NEXT_AT)
    assert fresh['received_at']==rebound['received_at']==clock.wall
    assert fresh['time_status']==rebound['time_status']=='CURRENT_TRAFFIC_REFERENCE'
    assert road.calls==len(calls)==3


@pytest.mark.parametrize('bad_kind',['list','string','bool','request','hash','negative','bool_meters','future','nonfinite'])
@pytest.mark.parametrize('cache_kind',['provider','bound','local'])
def test_corrupt_cache_contents_fail_closed_in_all_binding_layers(policy,bad_kind,cache_kind):
    clock,create=policy;vehicle,a,b=truck_data();provider_key,item=fixture_source(60,form='bound' if cache_kind!='provider' else 'provider')
    key=provider_key if cache_kind=='provider' else content_hash(item['request'])
    if bad_kind=='list':bad=[]
    elif bad_kind=='string':bad='corrupt'
    elif bad_kind=='bool':bad=True
    else:
        bad=deepcopy(item)
        if bad_kind=='request':bad['request']='not-a-request'
        if bad_kind=='hash':bad['meters']+=1
        if bad_kind=='negative':bad['meters']=-1
        if bad_kind=='bool_meters':bad['meters']=True
        if bad_kind=='future':bad['received_at']=clock.wall+1
        if bad_kind=='nonfinite':bad['polyline'][0][0]=float('nan')
        if bad_kind not in {'hash','nonfinite'}:
            bad['evidence_hash']=content_hash({k:v for k,v in bad.items() if k!='evidence_hash'})
    road=create(cache={key:bad} if cache_kind!='local' else {},allow_historical=True)
    if cache_kind=='local':road.local[key]=bad
    with pytest.raises(RoadError):road.segment(vehicle,a,b,AT)
    assert road.calls==0


def test_mismatched_provider_coordinates_and_hashed_future_or_truck_sources_are_rejected(policy):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(7200)
    for change in [{'source':'LIVE'}, {'verified':True}, {'time_status':'TIME_UNVERIFIED'},
                   {'request':DrivingRoad.reference_request(a,{**b,'longitude':'122.5'})}]:
        bad={**deepcopy(item),**change};bad['evidence_hash']=content_hash({k:v for k,v in bad.items() if k!='evidence_hash'})
        with pytest.raises(RoadError):create(cache={key:bad},allow_historical=True).segment(vehicle,a,b,AT)


def test_history_read_override_does_not_expand_fresh_graph_writes(policy,tmp_path):
    clock,create=policy;key,item=fixture_source(7200);path=tmp_path/'graph.db';raw_graph(path,key,item)
    graph=ReferenceGraph(path)
    try:
        assert graph.get_recent(key) is None
        assert graph.get_recent(key,ttl_seconds=7*86400)==item
        assert graph.ttl_seconds==3600 and graph.get_recent(key) is None
        assert not graph.put_reference(key,item)
    finally:graph.close()


def test_bad_deflate_stream_is_safe_to_read_and_repair_with_actual_fresh_evidence(policy,tmp_path):
    clock,create=policy;key,item=fixture_source();path=tmp_path/'graph.db'
    compressed=bytearray(gzip.compress(encode(item).encode()));compressed[10:14]=b'\xff'*4
    raw_graph(path,key,item,content=bytes(compressed))
    graph=ReferenceGraph(path)
    try:
        assert graph.get_recent(key,ttl_seconds=7*86400) is None
        assert graph.put_reference(key,item)
        assert graph.get_recent(key)==item
    finally:graph.close()


@pytest.mark.parametrize('field',['origin','destination'])
def test_nested_coordinate_schema_corruption_in_graph_fails_closed_and_can_be_repaired(policy,tmp_path,field):
    clock,create=policy;key,item=fixture_source();bad=deepcopy(item)
    bad['request'][field]=['not','a','point']
    bad['evidence_hash']=content_hash({k:v for k,v in bad.items() if k!='evidence_hash'})
    path=tmp_path/'bad-shape.db';raw_graph(path,key,bad)
    graph=ReferenceGraph(path)
    try:
        assert graph.get_recent(key) is None
        assert graph.put_reference(key,item)
        assert graph.get_recent(key)==item
    finally:graph.close()


def test_persistent_json_decode_corruption_becomes_a_safe_road_failure(policy):
    clock,create=policy;vehicle,a,b=truck_data();road=create()
    def bad_read(key):raise json.JSONDecodeError('synthetic cache corruption','{',0)
    road.cache_get=bad_read
    with pytest.raises(RoadError):road.segment(vehicle,a,b,AT)
    assert road.calls==0


def test_test_environment_never_automatically_loads_the_production_reference_graph(policy,settings,tmp_path):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(7200)
    path=tmp_path/'configured-graph.db';raw_graph(path,key,item)
    settings.amap_reference_graph_path=path
    road=create(allow_historical=True)
    assert road.graph is None and road.known_segment(a,b) is None
    with pytest.raises(RoadError):road.segment(vehicle,a,b,AT)
    road.graph=ReferenceGraph(path)
    assert road.segment(vehicle,a,b,AT)['time_status']=='HISTORICAL_TRAFFIC_REFERENCE'


def test_real_truck_adapter_does_not_consume_driving_history_graph_or_provider_keys(policy,settings,tmp_path):
    clock,create=policy;vehicle,a,b=truck_data();key,item=fixture_source(7200)
    path=tmp_path/'graph.db';raw_graph(path,key,item)
    truck=TruckRoad(settings,0,cache_get={key:item}.get,
                    transport=httpx.MockTransport(lambda _:pytest.fail('No truck HTTP')),limiter=NoWait())
    truck.graph=ReferenceGraph(path)
    try:
        assert content_hash(road_request(vehicle,a,b,AT,'LIVE'))!=key
        with pytest.raises(RoadError) as failure:truck.segment(vehicle,a,b,AT)
        assert failure.value.code=='ROAD_CALL_BUDGET' and truck.calls==0
    finally:truck.graph.close()


@pytest.mark.parametrize('outcome',['success','retry_503','retry_timeout','failure_timeout'])
def test_io_seconds_include_pacer_http_and_failed_retry_waits(policy,monkeypatch,outcome):
    import app.road_service as service
    clock,create=policy;vehicle,a,b=truck_data();timer=SimpleNamespace(value=100.0)
    monkeypatch.setattr(service.time,'monotonic',lambda:timer.value)
    class TimedLimiter:
        def acquire(self,service,*,cancelled):timer.value+=.2
        def defer(self,service,retry):timer.value+=.3
    calls=[]
    def handler(request):
        calls.append(request);timer.value+=.4
        if outcome=='failure_timeout' or outcome=='retry_timeout' and len(calls)==1:
            raise httpx.ReadTimeout('synthetic timeout')
        if outcome=='retry_503' and len(calls)==1:return httpx.Response(503)
        return httpx.Response(200,json=response())
    road=create(limit=2,limiter=TimedLimiter(),transport=httpx.MockTransport(handler))
    if outcome=='failure_timeout':
        with pytest.raises(RoadError) as failure:road.segment(vehicle,a,b,AT)
        assert failure.value.code=='ROAD_TIMEOUT'
    else:road.segment(vehicle,a,b,AT)
    expected=.6 if outcome=='success' else 1.5
    assert road.io_seconds==pytest.approx(expected)
    assert road.stats()['network_wait_seconds']==pytest.approx(expected)
    assert road.calls==len(calls)==(1 if outcome=='success' else 2)


def test_cancelled_pacer_wait_is_included_without_http(policy,monkeypatch):
    import app.road_service as service
    clock,create=policy;vehicle,a,b=truck_data();timer=SimpleNamespace(value=100.0)
    monkeypatch.setattr(service.time,'monotonic',lambda:timer.value)
    class CancelLimiter:
        def acquire(self,service,*,cancelled):
            timer.value+=.2;raise RateLimitWaitError('CANCELLED')
        def defer(self,service,retry):raise AssertionError('No retry')
    road=create(limit=1,limiter=CancelLimiter())
    with pytest.raises(RoadError) as failure:road.segment(vehicle,a,b,AT)
    assert failure.value.code=='CANCELLED'
    assert road.io_seconds==pytest.approx(.2) and road.calls==0
