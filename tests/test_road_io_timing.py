"""Network wait is charged once, independently of adapter kind or outcome."""
import httpx
from pydantic import SecretStr
import pytest

from app.road_service import DrivingRoad,TruckRoad
from app.routing_engine import RoadError
from test_amap_qps import Clock,pacer,truck_response
from test_driving_reference import AT,response
from test_routing_engine import truck_data


@pytest.fixture(params=[(TruckRoad,truck_response,'TRUCK'),(DrivingRoad,response,'DRIVING')],ids=['truck','driving'])
def context(request,settings,tmp_path):
    adapter,payload,mode=request.param
    settings.planning_route_mode=mode;settings.driving_live_calls_enabled=settings.truck_live_calls_enabled=True
    settings.amap_web_service_key=SecretStr('MOCK_ONLY');clock=Clock();calls=[];cancel=[False];expiry=[None]
    def handler(request):
        calls.append(clock());clock.sleep(.2)
        if expiry[0]=='timeout':raise httpx.ReadTimeout('Mock timeout',request=request)
        return httpx.Response(200,json=payload())
    road=adapter(settings,production_planning=True,transport=httpx.MockTransport(handler),timer=clock,
        limiter=pacer(tmp_path/'shared.db',clock),cancelled=lambda:cancel[0])
    return road,clock,calls,cancel,expiry,truck_data()


def test_qps_and_http_are_charged_once_with_injected_timer(context):
    road,clock,calls,_,_,(v,a,b)=context
    started=clock();road.segment(v,a,b,AT)
    road.segment(v,a,{**b,'id':'B2','longitude':'121.6'},AT)
    assert len(calls)==2 and calls[1]-calls[0]>1/3
    assert road.io_seconds==pytest.approx(clock()-started)
    assert road.stats()['network_wait_seconds']==round(road.io_seconds,4)
    before=road.io_seconds
    clock.sleep(1)  # Work outside the adapter must not inflate network time.
    road.segment(v,a,b,AT)
    assert road.io_seconds==before and road.calls==2


def test_retry_and_failed_http_wait_are_charged_once(context):
    road,clock,calls,_,expiry,(v,a,b)=context;expiry[0]='timeout';started=clock()
    with pytest.raises(RoadError) as exc:road.segment(v,a,b,AT)
    assert exc.value.code=='ROAD_TIMEOUT' and len(calls)==2
    assert road.io_seconds==pytest.approx(clock()-started)
    assert road.io_seconds>=1.4  # Two .2s failures plus shared retry delay.


@pytest.mark.parametrize('reason',['CANCELLED','WALL_BUDGET'])
def test_cancel_or_deadline_qps_wait_is_counted_without_http(context,reason):
    road,clock,calls,cancel,_,(v,a,b)=context;road.segment(v,a,b,AT)
    # The first .2s HTTP has returned while the next QPS slot remains queued.
    instant=clock();before=road.io_seconds
    if reason=='WALL_BUDGET':road.wall_deadline=instant+.05
    else:road.cancelled=lambda:clock()>=instant+.05
    with pytest.raises(RoadError) as exc:road.segment(v,a,{**b,'id':'B2','longitude':'121.6'},AT)
    assert exc.value.code==reason and len(calls)==1
    assert road.io_seconds-before==pytest.approx(clock()-instant)
    assert road.io_seconds>before
