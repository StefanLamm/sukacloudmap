"""No external API calls: exercise shared QPS pacing and provider throttling."""
import json
import multiprocessing
import sqlite3
import time

import httpx
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import select

from app.amap_limits import AMapRateLimiter, QPS_CODES, RateLimitWaitError
from app.amap_service import AMapSearchService, GEOCODE_URL, security_proxy
from app.api import ApiError
from app.config import Settings
from app.location_tasks import LocationRunner
from app.models import LocationTask, now
from app.road_service import DrivingRoad, TruckRoad
from app.routing_engine import RoadError
from conftest import login
from test_amap_update import amap_settings, geocode
from test_driving_reference import AT, response as driving_response
from test_master_actions import imported_warehouses
from test_routing_engine import truck_data


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


def pacer(path, clock, **kwargs):
    return AMapRateLimiter(path, clock=clock, timer=clock, sleeper=clock.sleep, **kwargs)


@pytest.mark.parametrize('service,cap', [('geocode',3),('poi_search',3),('driving',3),('js_map',10)])
def test_shared_instances_remain_within_every_rolling_second(tmp_path, service, cap):
    clock = Clock(); path = tmp_path / 'shared.db'
    gates = [pacer(path, clock), pacer(path, clock)]; starts=[]
    for n in range(30):
        gates[n % 2].acquire(service); starts.append(clock())
    assert all(sum(t <= at < t + 1 for at in starts) <= cap for t in starts)
    assert clock() - starts[0] > (len(starts)-1) / cap


def process_acquire(path, queue):
    gate = AMapRateLimiter(path)
    for _ in range(3):
        gate.acquire('driving'); queue.put(time.time())


def test_two_real_processes_share_one_service_limit(tmp_path):
    context = multiprocessing.get_context('spawn'); queue = context.Queue()
    jobs=[context.Process(target=process_acquire,args=(str(tmp_path/'process.db'),queue)) for _ in range(2)]
    try:
        for job in jobs: job.start()
        starts=sorted(queue.get(timeout=12) for _ in range(6))
        for job in jobs:
            job.join(timeout=5); assert job.exitcode == 0
        assert all(sum(t <= at < t+1 for at in starts) <= 3 for t in starts)
        assert starts[-1]-starts[0] >= 5/3
    finally:
        for job in jobs:
            if job.is_alive(): job.terminate(); job.join(timeout=5)
        queue.close()


def test_services_are_independent_but_retry_pause_is_shared(tmp_path):
    clock=Clock(); gate=pacer(tmp_path/'qps.db',clock); other=pacer(gate.path,clock)
    gate.acquire('geocode'); other.acquire('poi_search')
    assert clock()==100
    gate.defer('geocode',1); other.acquire('geocode')
    assert clock()>=102
    # The pacing file contains no account/Key/address or usage counters.
    with sqlite3.connect(gate.path) as db:
        assert [r[1] for r in db.execute('PRAGMA table_info(service_pacing)')]==['service','next_at']
    assert gate.path.stat().st_mode & 0o777 == 0o600


def test_default_clients_share_pacing_across_databases_and_account_keys(settings,monkeypatch):
    clock=Clock()
    monkeypatch.setattr('app.amap_limits.wall_time',clock)
    monkeypatch.setattr('app.amap_limits.monotonic',clock)
    monkeypatch.setattr('app.amap_limits.sleep',clock.sleep)
    starts=[]
    def handler(request):
        starts.append(clock());return httpx.Response(200,json={'status':'1','geocodes':[geocode()]})
    first=amap_settings(settings)
    second=first.model_copy(update={'database_url':SecretStr('sqlite:///separate-test.db'),'amap_web_service_key':SecretStr('ANOTHER_TEST_KEY')})
    clients=[AMapSearchService(s,transport=httpx.MockTransport(handler)) for s in [first,second]]
    for client in clients: client.fetch(GEOCODE_URL,{'address':'合肥市东流路100号'})
    assert len(starts)==2 and starts[1]-starts[0]>1/3


def test_wait_can_be_cancelled_and_does_not_issue_a_slot(tmp_path):
    clock=Clock(); gate=pacer(tmp_path/'qps.db',clock)
    gate.acquire('driving')
    with pytest.raises(RateLimitWaitError) as exc:
        gate.acquire('driving',cancelled=lambda:clock()>=100.1)
    assert exc.value.code=='CANCELLED'
    gate.acquire('driving'); assert clock()<100.4


def test_wait_timeout_and_broken_storage_fail_closed(tmp_path):
    clock=Clock(); gate=pacer(tmp_path/'qps.db',clock,max_wait=.2)
    gate.acquire('driving')
    with pytest.raises(RateLimitWaitError) as exc:gate.acquire('driving')
    assert exc.value.code=='WAIT_TIMEOUT'
    broken=tmp_path/'bad.db';broken.write_bytes(b'invalid sqlite')
    with pytest.raises(RateLimitWaitError) as exc:pacer(broken,Clock()).acquire('geocode')
    assert exc.value.code=='STORAGE_UNAVAILABLE'


@pytest.mark.parametrize('field,limit', [('amap_web_qps',4),('amap_js_qps',11),('amap_web_qps',0)])
def test_config_cannot_exceed_screenshot_caps(field,limit):
    with pytest.raises(ValidationError): Settings(_env_file=None,**{field:limit})


@pytest.mark.parametrize('code',sorted(QPS_CODES)+['HTTP_429'])
def test_geocode_qps_retries_are_paced_and_success_is_cached(settings,tmp_path,code,caplog):
    clock=Clock(); gate=pacer(tmp_path/'qps.db',clock); starts=[]
    def handler(request):
        starts.append(clock())
        if len(starts)==1:
            return httpx.Response(429 if code=='HTTP_429' else 200,json={'status':'0','infocode':code,'info':'SERVER_ONLY_KEY'})
        return httpx.Response(200,json={'status':'1','geocodes':[geocode()]})
    service=AMapSearchService(amap_settings(settings),transport=httpx.MockTransport(handler),limiter=gate)
    result=service.search('合肥市东流路100号')
    assert result['items'][0]['automatic_eligible'] and len(starts)==2 and starts[1]-starts[0]>=1
    assert service.search('合肥市东流路100号')==result and len(starts)==2
    assert 'SERVER_ONLY_KEY' not in caplog.text and '东流路' not in caplog.text


def test_sustained_throttle_stops_after_three_attempts_without_auth_error(settings,tmp_path):
    clock=Clock(); starts=[]
    def handler(request):
        starts.append(clock());return httpx.Response(200,json={'status':'0','infocode':'10020'})
    service=AMapSearchService(amap_settings(settings),transport=httpx.MockTransport(handler),limiter=pacer(tmp_path/'qps.db',clock))
    with pytest.raises(ApiError) as exc:service.search('合肥市东流路100号')
    assert exc.value.status==429 and exc.value.code=='AMAP_QPS_LIMIT'
    assert len(starts)==3 and starts[1]-starts[0]>=1 and starts[2]-starts[1]>=2


@pytest.mark.parametrize('secondary',[False,True])
def test_one_throttled_address_does_not_stop_the_rest_of_queue(client,app,settings,tmp_path,secondary):
    login(client);imported_warehouses(client,'合肥市测试地址','淮安市测试地址')
    def handler(request):
        if secondary and request.url.path.endswith('/geo'):
            return httpx.Response(200,json={'status':'1','geocodes':[]})
        return httpx.Response(200,json={'status':'0','infocode':'10021'})
    clock=Clock()
    service=AMapSearchService(amap_settings(settings),transport=httpx.MockTransport(handler),limiter=pacer(tmp_path/'qps.db',clock))
    runner=LocationRunner(app.state.engine,app.state.sessions,service,clock=lambda:now()+10)
    assert runner.run_once()
    with app.state.sessions() as db:
        tasks=list(db.scalars(select(LocationTask)))
        assert sorted(t.status for t in tasks)==['MANUAL','QUEUED']
        assert '请求暂时较多' in next(t.reason for t in tasks if t.status=='MANUAL')


def truck_response():
    return {'errcode':10000,'data':{'route':{'paths':[{'distance':'1000','duration':'60','restriction':'1',
        'steps':[{'polyline':'121.45,31.2;121.5,31.2'}]}]}}}


@pytest.mark.parametrize('adapter,success',[(DrivingRoad,driving_response),(TruckRoad,truck_response)])
@pytest.mark.parametrize('code',['10020','HTTP_429'])
def test_road_throttle_retry_counts_toward_budget_and_cache_avoids_calls(settings,tmp_path,adapter,success,code):
    settings=amap_settings(settings);clock=Clock();starts=[];v,a,b=truck_data()
    def handler(request):
        starts.append(clock())
        if len(starts)==1:
            return httpx.Response(429 if code=='HTTP_429' else 200,json={'status':'0','infocode':code})
        return httpx.Response(200,json=success())
    road=adapter(settings,2,transport=httpx.MockTransport(handler),limiter=pacer(tmp_path/'qps.db',clock))
    result=road.segment(v,a,b,AT)
    assert result['meters']==1000 and not result['verified'] and road.calls==2 and starts[1]-starts[0]>=1
    assert road.segment(v,a,b,AT)==result and len(starts)==2 and road.hits==1


@pytest.mark.parametrize('budget,expected',[(1,'ROAD_CALL_BUDGET'),(2,'ROAD_QPS_LIMIT')])
def test_sustained_road_throttle_never_exceeds_call_budget(settings,tmp_path,budget,expected):
    clock=Clock();starts=[];v,a,b=truck_data()
    def handler(request):
        starts.append(clock());return httpx.Response(200,json={'status':'0','infocode':'10020'})
    road=DrivingRoad(amap_settings(settings),budget,transport=httpx.MockTransport(handler),limiter=pacer(tmp_path/'qps.db',clock))
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code==expected and road.calls==len(starts)==budget


def test_cancel_while_waiting_never_charges_a_road_call(settings,tmp_path):
    clock=Clock();gate=pacer(tmp_path/'qps.db',clock);gate.acquire('driving');v,a,b=truck_data()
    road=DrivingRoad(amap_settings(settings),1,transport=httpx.MockTransport(lambda r:pytest.fail('Cancelled')),
                     limiter=gate,cancelled=lambda:clock()>=100.1)
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='CANCELLED' and road.calls==0


def test_js_security_proxy_uses_separate_ten_qps_bucket_and_retries_429(settings,tmp_path):
    settings=amap_settings(settings);clock=Clock();gate=pacer(tmp_path/'qps.db',clock);starts=[]
    def handler(request):
        starts.append(clock());return httpx.Response(429 if len(starts)==1 else 200,text='map_ready')
    body,_=security_proxy(settings,'v4/maps',[],transport=httpx.MockTransport(handler),limiter=gate)
    assert body==b'map_ready' and len(starts)==2 and starts[1]-starts[0]>=1
    assert 'SERVER_' not in json.dumps({'body':body.decode()} )
