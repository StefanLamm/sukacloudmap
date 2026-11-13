from io import BytesIO

import httpx
from openpyxl import load_workbook
import pytest

from app.api import ApiError
from app.config import Settings
from app.map_service import MapSearchService
from conftest import create_member, login, USER_PASSWORD
from test_stage3 import warehouse, quote, store_payload, vehicle_payload, post, xlsx, preview, commit


def test_management_records_are_independent_from_daily_operations(client):
    login(client)
    w=warehouse(client);q=quote(client)
    s=post(client,'stores',store_payload(w,q))
    v=post(client,'vehicles',vehicle_payload(w))
    before=client.get('/api/master-data/management').json()
    assert 'delivery_date' not in before
    assert before['stores'][0]['quote_name']==q['name']
    assert before['quotes'][0]['latest_version']['per_km']=='2'
    assert not any(k.startswith('day_') for rows in [before['warehouses'],before['stores'],before['vehicles']] for row in rows for k in row)
    assert 'effective_version' not in before['quotes'][0]
    assert client.put(f"/api/vehicles/{v['id']}/days/2026-11-11",json={'warehouse_id':w['id'],'available':False,'expected_revision':0}).status_code==200
    assert client.put(f"/api/warehouses/{w['id']}/days/2026-11-11",json={'departure_time':'10:30','expected_revision':0}).status_code==200
    after=client.get('/api/master-data/management?delivery_date=2099-01-01').json()
    assert after['master_revision']==before['master_revision']
    assert after['vehicles']==before['vehicles']
    assert after['warehouses']==before['warehouses']
    planning=client.get('/api/master-data/catalog?delivery_date=2026-11-11').json()
    assert planning['vehicles'][0]['day_available'] is False
    assert planning['warehouses'][0]['day_departure_time']=='10:30'
    assert client.get('/api/master-data/catalog?delivery_date=2026-11-12').json()['vehicles'][0]['day_available'] is True


def test_daily_operations_do_not_invalidate_master_import_preview(client):
    login(client)
    w=warehouse(client)
    batch=preview(client,'warehouses',xlsx(client,'warehouses',[{'仓库编号':'W02','仓库名称':'新增仓库','地址':'地址','最早发车时间':'08:00'}]))
    assert batch['can_commit']
    assert client.put(f"/api/warehouses/{w['id']}/days/2026-11-11",json={'departure_time':'10:30','expected_revision':0}).status_code==200
    assert commit(client,batch).status_code==200
    assert len(client.get('/api/master-data/management').json()['warehouses'])==2


def test_new_master_templates_do_not_require_business_dates(client):
    login(client)
    for kind in ['warehouses','stores','vehicles','quotes']:
        wb=load_workbook(BytesIO(client.get('/api/master-data/templates/'+kind+'.xlsx').content))
        columns=[c.value for c in wb['数据'][1]]
        assert '生效日期' not in columns and '最晚到店日期时间' not in columns
        wb.close()


def test_management_import_keeps_existing_operational_deadline(client):
    login(client)
    w=warehouse(client);q=quote(client)
    store=post(client,'stores',store_payload(w,q))
    row={'门店编号':'S01','门店名称':'资料更新','地址':'更新后的地址','出货仓编号':'W01',
         '营业开始':'08:00','营业结束':'18:00','闭店跨日天数':0,'卸货分钟数':20,'专车报价编号':'Q01','共配每件单价':'2'}
    batch=preview(client,'stores',xlsx(client,'stores',[row]))
    assert batch['can_commit'] and commit(client,batch).status_code==200
    updated=client.get('/api/stores/'+store['id']).json()
    assert updated['name']=='资料更新'
    assert updated['latest_arrival_at']==store['latest_arrival_at']


def test_map_and_planning_access_and_config_do_not_expose_secrets(client,app):
    for path in ['/api/maps/config','/api/maps/search?q=上海','/api/master-data/management']:
        assert client.get(path).status_code==401
    assert client.get('/planning',follow_redirects=False).status_code==303
    login(client);create_member(client)
    login(client,'member',USER_PASSWORD)
    assert client.get('/planning').status_code==200
    app.state.settings.amap_js_key='test-hidden-browser-key'
    app.state.settings.amap_security_js_code='test-hidden-security-code'
    response=client.get('/api/maps/config')
    assert response.status_code==200
    assert response.json()=={'tile_url':'https://tile.openstreetmap.org/{z}/{x}/{y}.png','attribution':'© OpenStreetMap contributors','crs':'WGS84'}
    assert 'test-hidden' not in response.text
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert 'https://tile.openstreetmap.org' in response.headers['content-security-policy']
    assert response.headers['referrer-policy']=='strict-origin-when-cross-origin'


def test_search_results_are_explicit_cached_and_rate_limited(settings):
    calls=[];clock=[100.0]
    def handler(request):
        calls.append(request)
        assert request.url.params['q']=='上海 虹桥站'
        assert request.headers['user-agent'].startswith('SukaCloudmap/')
        return httpx.Response(200,json={'features':[{'geometry':{'coordinates':[121.327,31.195]},'properties':{'name':'虹桥站','city':'上海市','street':'申虹路'}}]})
    service=MapSearchService(settings,transport=httpx.MockTransport(handler),clock=lambda:clock[0])
    first=service.search(' 上海   虹桥站 ')
    assert first['items'][0]['name']=='虹桥站'
    assert first['items'][0]['longitude']=='121.327' and first['items'][0]['latitude']=='31.195'
    assert first['items'][0]['crs']=='WGS84'
    assert service.search('上海 虹桥站')==first and len(calls)==1
    with pytest.raises(ApiError) as exc:service.search('北京站')
    assert exc.value.status==429
    clock[0]+=86401
    assert service.search('上海 虹桥站')==first and len(calls)==2


@pytest.mark.parametrize('response',[
    httpx.Response(503,text='upstream-internal-secret'),
    httpx.Response(200,json={'features':None}),
    httpx.Response(200,text='<html>bad</html>'),
])
def test_search_failure_is_actionable_and_sanitized(settings,response):
    service=MapSearchService(settings,transport=httpx.MockTransport(lambda request:response))
    with pytest.raises(ApiError) as exc:service.search('上海虹桥站')
    assert exc.value.status==502
    assert 'secret' not in exc.value.message and '稍后重试' in exc.value.message


def test_search_validates_coordinates_handles_empty_results_and_endpoint_errors(client,app,settings):
    def handler(request):
        return httpx.Response(200,json={'features':[{'geometry':{'coordinates':[181,31]},'properties':{}},{'geometry':{'coordinates':[120,91]},'properties':{}}]})
    app.state.map_search=MapSearchService(settings,transport=httpx.MockTransport(handler))
    login(client)
    assert client.get('/api/maps/search?q=上海').json()['items']==[]
    assert client.get('/api/maps/search?q=x').status_code==422
    assert client.get('/api/maps/search?q=%20%20').status_code==422
    assert client.get('/api/maps/search?q=苏州').status_code==429


def test_location_search_result_can_be_associated_and_read_back(client):
    login(client)
    w=warehouse(client,crs='WGS84',longitude='120.58',latitude='31.3',location_source='OpenStreetMap地图选点')
    s=post(client,'stores',store_payload(w,crs='WGS84',longitude='121.327',latitude='31.195',location_source='Photon / OpenStreetMap文字搜索'))
    response=client.patch('/api/stores/'+s['id'],json={'expected_revision':s['revision'],'longitude':'121.32701','latitude':'31.19501','crs':'WGS84','location_source':'OpenStreetMap拖拽校正'})
    assert response.status_code==200
    associated=client.get('/api/master-data/management').json()['stores'][0]
    assert associated['longitude']=='121.32701' and associated['latitude']=='31.19501'
    assert associated['crs']=='WGS84' and associated['location_source']=='OpenStreetMap拖拽校正'
    assert associated['id']==s['id']


def test_custom_map_endpoint_requires_https_without_embedded_credentials():
    for value in ['http://example.org/api','https://user:password@example.org/api','file:///tmp/tiles']:
        with pytest.raises(ValueError):Settings(_env_file=None,map_geocoder_url=value)
