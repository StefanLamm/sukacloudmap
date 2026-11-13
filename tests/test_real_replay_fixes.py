import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import select

from app.amap_service import AMapSearchService
from app.api import ApiError
from app.location_tasks import LocationRunner, exact_candidates
from app.models import LocationTask
from test_master_actions import imported_warehouses
from conftest import login


def test_engine_response_error_falls_back_to_poi_without_reporting_key_failure(settings):
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/geo'):
            return httpx.Response(200,json={'status':'0','infocode':'30001'})
        return httpx.Response(200,json={'status':'1','pois':[{'name':'合肥验收中心','pname':'安徽省','cityname':'合肥市','adname':'蜀山区','address':'测试路1号','location':'117.2,31.8'}]})
    result=AMapSearchService(settings,transport=httpx.MockTransport(handler)).search('合肥验收中心')
    assert paths==['/v3/geocode/geo','/v3/place/text'] and result['items'][0]['match_level']=='POI'


@pytest.mark.parametrize('code',['20000','20003','30001','32000'])
def test_query_specific_failures_are_not_global_auth_failures(settings,code):
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    service=AMapSearchService(settings,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'0','infocode':code})))
    with pytest.raises(ApiError) as exc:service.fetch('https://restapi.amap.com/v3/geocode/geo',{'address':'合肥市测试地址'})
    assert exc.value.status==422 and exc.value.code=='AMAP_QUERY_RESPONSE'


@pytest.mark.parametrize('secondary_code',['10044','10012'])
def test_secondary_search_limit_preserves_geocodes_and_later_addresses(settings,secondary_code):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');clock=[100.0];paths=[]
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/text'):
            return httpx.Response(200,json={'status':'0','infocode':secondary_code,'info':'TEST_ONLY'})
        if request.url.params['address']=='合肥市东流路100号':
            row={'formatted_address':'安徽省合肥市蜀山区东流路100号','level':'门址','city':'合肥市','district':'蜀山区','location':'117.2,31.8'}
        else:row={'formatted_address':'安徽省合肥市蜀山区','level':'区县','city':'合肥市','district':'蜀山区','location':'117.2,31.8'}
        return httpx.Response(200,json={'status':'1','geocodes':[row]})
    service=AMapSearchService(settings,transport=httpx.MockTransport(handler),clock=lambda:clock[0])
    first=service.search('合肥市未定位商场二楼')
    assert len(first['items'])==1 and first['notice'] and 'TEST_ONLY' not in first['notice']
    assert service.search('合肥市未定位商场二楼')==first
    clock[0]+=2
    second=service.search('合肥市东流路100号');assert len(exact_candidates('合肥市东流路100号',second['items']))==1
    clock[0]+=2
    third=service.search('合肥市另一未定位商场');assert third['notice']
    assert paths==['/v3/geocode/geo','/v3/place/text','/v3/geocode/geo','/v3/geocode/geo']


def test_primary_geocoder_quota_still_stops_and_is_not_reported_as_key_error(settings):
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    service=AMapSearchService(settings,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'0','infocode':'10044'})))
    with pytest.raises(ApiError) as exc:service.search('合肥市测试地址')
    assert exc.value.status==502 and exc.value.code=='AMAP_QUOTA'


@pytest.mark.parametrize('scope_code',['10045','40003'])
def test_overseas_scope_error_does_not_disable_other_domestic_poi_searches(settings,scope_code):
    settings.amap_web_service_key=SecretStr('TEST_ONLY');clock=[100.0];pois=[]
    def handler(request):
        if request.url.path.endswith('/geo'):return httpx.Response(200,json={'status':'1','geocodes':[]})
        pois.append(request.url.params['keywords'])
        if len(pois)==1:return httpx.Response(200,json={'status':'0','infocode':scope_code})
        return httpx.Response(200,json={'status':'1','pois':[{'name':'合肥市博物馆','cityname':'合肥市','adname':'蜀山区','location':'117.2,31.8'}]})
    service=AMapSearchService(settings,transport=httpx.MockTransport(handler),clock=lambda:clock[0])
    assert service.search('合肥市未定位地址')['notice'];clock[0]+=2
    result=service.search('合肥市博物馆')
    assert len(exact_candidates('合肥市博物馆',result['items']))==1 and len(pois)==2


def test_one_bad_address_does_not_cancel_other_queued_records(client,app,settings):
    login(client); imported_warehouses(client,'合肥市测试地址','淮安市测试地址')
    settings.amap_web_service_key = SecretStr('TEST_ONLY')
    service=AMapSearchService(settings,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'status':'0','infocode':'30001'})))
    tick=[100]
    runner=LocationRunner(app.state.engine,app.state.sessions,service,clock=lambda:tick[0])
    # Queue timestamps are Unix times, use an independent current worker clock.
    from app.models import now
    tick[0]=now()+10
    assert runner.run_once()
    with app.state.sessions() as db:
        states=[t.status for t in db.scalars(select(LocationTask))]
        assert sorted(states)==['MANUAL','QUEUED']


def test_exact_residential_geocode_is_usable_but_different_campus_is_not(settings):
    from app.amap_service import geocode_quality
    q='安徽省安庆市宜秀区大龙山镇燎原小区'
    assert geocode_quality(q,{'formatted_address':q,'level':'住宅区'})
    assert not geocode_quality('蚌埠市龙子湖区安徽财经大学东校区南苑食堂二楼',{'formatted_address':'安徽省蚌埠市龙子湖区财经大学(公交站)东校区','level':'住宅区'})
    assert not geocode_quality('合肥市',{'formatted_address':'合肥市','level':'住宅区'})


@pytest.mark.parametrize('query,city,district,eligible',[
    ('安徽省安庆市桐城市水源小区','安庆市','桐城市',True),
    ('安徽省安庆市怀宁县水源小区','安庆市','桐城市',False),
    ('安徽省安庆市桐城市水源小区9号铺','安庆市','桐城市',False),
    ('安徽省安庆市桐城市测试路水源小区','安庆市','桐城市',False),
    ('安徽省安庆市桐城市水源小区','合肥市','蜀山区',False),
])
def test_poi_exact_name_keeps_city_district_and_address_qualifiers(query,city,district,eligible,settings):
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    def handler(request):
        body={'status':'1','geocodes':[]} if request.url.path.endswith('/geo') else {'status':'1','pois':[{'name':'水源小区','pname':'安徽省','cityname':city,'adname':district,'address':'南三路与龙池路交叉口','location':'117,31'}]}
        return httpx.Response(200,json=body)
    result=AMapSearchService(settings,transport=httpx.MockTransport(handler)).search(query)
    assert bool(exact_candidates(query,result['items'])) is eligible


def test_two_exact_poi_candidates_stay_ambiguous(settings):
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    def handler(request):
        return httpx.Response(200,json={'status':'1','geocodes':[]}) if request.url.path.endswith('/geo') else httpx.Response(200,json={'status':'1','pois':[{'name':'水源小区','cityname':'安庆市','adname':'桐城市','location':p} for p in ['117,31','118,32']]})
    rows=AMapSearchService(settings,transport=httpx.MockTransport(handler)).search('安徽省安庆市桐城市水源小区')['items']
    assert len(exact_candidates('安徽省安庆市桐城市水源小区',rows))==2


@pytest.mark.parametrize('crs,expected',[
    ('WGS84',[117.232667,31.818825]),
    ('BD09',[117.220722,31.814634]),
    ('GCJ02',[117.227185,31.820824]),
])
def test_reference_conversion_matches_existing_ui_and_keeps_frozen_input(crs,expected,settings):
    from app.coordinates import provider_point
    from app.road_service import DrivingRoad
    from test_routing_engine import truck_data
    settings.amap_web_service_key=SecretStr('TEST_ONLY')
    v,a,b=truck_data();a.update(longitude='117.227185',latitude='31.820824',crs=crs)
    original=dict(a)
    assert provider_point(a)=={'longitude':expected[0],'latitude':expected[1],'crs':'GCJ02'}
    def handler(request):
        assert request.url.params['origin']==','.join(f'{n:.6f}' for n in expected)
        return httpx.Response(200,json={'status':'1','infocode':'10000','route':{'paths':[{'distance':'1000','cost':{'duration':'60'},'steps':[{'polyline':'117.23,31.81;117.24,31.82'}]}]}})
    item=DrivingRoad(settings,1,transport=httpx.MockTransport(handler)).segment(v,a,b,'2026-11-12T08:00:00+08:00')
    assert a==original and item['request']['origin']=={k:a[k] for k in ['longitude','latitude','crs']}
    assert not item['verified'] and item['source']=='DRIVING'
    if crs!='GCJ02':
        assert item['request']['coordinate_policy']=='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02'
        assert item['request']['provider_origin']==provider_point(a)


def test_reference_conversion_record_is_independently_checked():
    from app.coordinates import provider_point
    from app.road_service import evidence,road_request,load_fixture
    from app.routing_engine import normalize,recalculate,check_result
    from app.planning_inputs import content_hash
    body=load_fixture('MIXED_56');body['source']='FROZEN'
    for p in body['warehouses']+body['tasks']:p.update(longitude='117.227185',latitude='31.820824',crs='WGS84')
    class Road:
        def segment(self,v,a,b,at):
            request=road_request(v,a,b,at,'DRIVING',32)
            request.update(coordinate_policy='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02',provider_origin=provider_point(a),provider_destination=provider_point(b))
            return evidence(request,1000,60,'DRIVING',False)
    result=recalculate(body,{'routes':[{'vehicle_id':body['vehicles'][0]['id'],'task_ids':[body['tasks'][0]['id']]}],'common_ids':[t['id'] for t in body['tasks'][1:]],'unassigned_ids':[]},Road())
    assert not any(e['code']=='ROAD_CONVERSION' for e in result['check']['issues'])
    segment=result['routes'][0]['segments'][0];segment['request']['provider_origin']['longitude']=0
    segment['evidence_hash']=content_hash({k:v for k,v in segment.items() if k!='evidence_hash'})
    assert any(e['code']=='ROAD_CONVERSION' for e in check_result(normalize(body),result)['issues'])
