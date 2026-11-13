from io import BytesIO
import json

import httpx
from openpyxl import load_workbook
from pydantic import SecretStr
import pytest

from app.amap_service import AMapSearchService
from app.api import ApiError
from app.location_tasks import LocationRunner, exact_candidates
from app.map_service import MapSearchService
from app.road_service import TruckRoad
from conftest import login
from test_master_actions import imported_warehouses
from test_routing_engine import truck_data
from test_stage3 import commit, preview, warehouse, xlsx


def amap_settings(settings):
    return settings.model_copy(update={'map_provider': 'AMAP', 'amap_web_service_key': SecretStr('SERVER_ONLY_KEY'),
                                      'amap_js_key': SecretStr('BROWSER_PUBLIC_KEY'), 'amap_security_js_code': SecretStr('SERVER_SECURITY_CODE')})


def geocode(address='安徽省合肥市蜀山区东流路100号', level='门址', location='117.227185,31.820824'):
    return {'formatted_address': address, 'level': level, 'location': location, 'province': '安徽省', 'city': '合肥市', 'district': '蜀山区', 'street': '东流路', 'number': '100号'}


def test_amap_address_lookup_uses_server_key_gcj_precision_and_cache(settings):
    calls = []; clock = [100.0]
    def handler(request):
        calls.append(request)
        assert request.url.host == 'restapi.amap.com' and request.url.path == '/v3/geocode/geo'
        assert request.url.params['key'] == 'SERVER_ONLY_KEY' and request.url.params['city'] == '合肥市'
        assert request.url.params['address'] == '合肥市东流路100号'
        return httpx.Response(200, json={'status': '1', 'infocode': '10000', 'geocodes': [geocode()]})
    service = MapSearchService(amap_settings(settings), transport=httpx.MockTransport(handler), clock=lambda: clock[0])
    first = service.search('合肥市东流路100号')
    assert service.search('合肥市东流路100号') == first and len(calls) == 1
    assert first['items'][0]['crs'] == 'GCJ02' and len(exact_candidates('合肥市东流路100号', first['items'])) == 1
    assert 'SERVER_ONLY_KEY' not in json.dumps(first)
    # New uncached queries queue behind the shared per-service QPS gate.
    service.search('合肥市东流路100号 ')
    assert len(calls) == 1


def test_precise_entrance_beats_old_centroid_but_equal_precision_stays_ambiguous(settings):
    rows = [geocode(), geocode(level='门牌号', location='117.218291,31.818556')]
    service = AMapSearchService(amap_settings(settings), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'status': 1, 'geocodes': rows})))
    items = service.search('合肥市东流路100号')['items']
    assert exact_candidates('合肥市东流路100号', items)[0]['longitude'] == '117.227185'
    both = [dict(items[0]), dict(items[0], longitude='117.228')]
    assert len(exact_candidates('合肥市东流路100号', both)) == 2


def test_wrong_street_number_and_district_are_not_automatically_associated(settings):
    rows = [geocode(), geocode(address='安徽省合肥市蜀山区长江路100号'),
            geocode(address='安徽省合肥市蜀山区东流路1000号'), geocode(address='安徽省合肥市庐阳区东流路100号')]
    service = AMapSearchService(amap_settings(settings), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'status': '1', 'geocodes': rows})))
    matches = exact_candidates('合肥市蜀山区东流路100号', service.search('合肥市蜀山区东流路100号')['items'])
    assert len(matches) == 1 and matches[0]['address'] == rows[0]['formatted_address']


def test_amap_poi_fallback_and_missing_optional_arrays(settings):
    def handler(request):
        if request.url.path.endswith('/geo'):
            return httpx.Response(200, json={'status': '1', 'geocodes': []})
        assert request.url.path == '/v3/place/text'
        return httpx.Response(200, json={'status': '1', 'pois': [{'name': '合肥市博物馆', 'location': '117.2,31.8', 'pname': '安徽省', 'cityname': '合肥市', 'adname': [], 'address': []}]})
    service = AMapSearchService(amap_settings(settings), transport=httpx.MockTransport(handler))
    result = service.search('合肥市博物馆')
    assert result['items'][0]['match_level'] == 'POI' and '[]' not in result['items'][0]['address']
    assert len(exact_candidates('合肥市博物馆', result['items'])) == 1


@pytest.mark.parametrize('response', [httpx.Response(403, text='SERVER_ONLY_KEY'),
    httpx.Response(200, json={'status': '0', 'infocode': '10001', 'info': 'SERVER_SECURITY_CODE'}),
    httpx.Response(200, json={'status': '0', 'infocode': '10003'}),
    httpx.Response(200, json={'status': '1', 'geocodes': None}), httpx.Response(200, text='not json')])
def test_amap_errors_are_actionable_and_never_expose_keys(settings, response):
    service = AMapSearchService(amap_settings(settings), transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(ApiError) as exc: service.search('合肥市东流路100号')
    assert exc.value.status == 502 and '高德' in exc.value.message
    assert 'SERVER_' not in exc.value.message


def test_missing_amap_key_does_not_send_a_request(settings):
    service = AMapSearchService(settings.model_copy(update={'amap_web_service_key': None}), transport=httpx.MockTransport(lambda r: pytest.fail('No request without key')))
    with pytest.raises(ApiError) as exc: service.search('合肥市东流路100号')
    assert exc.value.code == 'AMAP_KEY_MISSING'


def test_import_background_amap_match_saves_gcj02_and_provider_source(client, app, settings):
    login(client); imported_warehouses(client, '合肥市东流路100号')
    service = AMapSearchService(amap_settings(settings), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'status': '1', 'geocodes': [geocode()]})))
    assert LocationRunner(app.state.engine, app.state.sessions, service).run_once()
    row = client.get('/api/master-data/management').json()['warehouses'][0]
    assert row['crs'] == 'GCJ02' and row['location_status'] == 'LINKED' and '高德地图' in row['location_source']


def test_map_config_exposes_only_browser_key_and_security_proxy_is_authenticated(client, app):
    assert client.get('/_AMapService/v4/maps').status_code == 401
    login(client)
    app.state.settings.map_provider = 'AMAP'
    app.state.settings.amap_js_key = SecretStr('BROWSER_PUBLIC_KEY')
    app.state.settings.amap_security_js_code = SecretStr('SERVER_SECURITY_CODE')
    app.state.settings.amap_web_service_key = SecretStr('SERVER_ONLY_KEY')
    r = client.get('/api/maps/config')
    assert r.json()['provider'] == 'AMAP' and r.json()['js_ready'] and r.json()['js_key'] == 'BROWSER_PUBLIC_KEY'
    assert 'SERVER_' not in r.text
    assert 'https://*.amap.com' in r.headers['content-security-policy']
    assert "'unsafe-eval'" in r.headers['content-security-policy']
    def proxy(request):
        assert request.url.host == 'restapi.amap.com'
        assert request.url.params['key'] == 'BROWSER_PUBLIC_KEY' and request.url.params['jscode'] == 'SERVER_SECURITY_CODE'
        assert 'cookie' not in request.headers
        return httpx.Response(200, text='cb({"status":1})', headers={'Content-Type': 'application/javascript'})
    app.state.map_proxy_transport = httpx.MockTransport(proxy)
    result = client.get('/_AMapService/v4/maps?key=attacker&jscode=attacker&callback=cb')
    assert result.status_code == 200 and 'SERVER_' not in result.text
    assert client.get('/_AMapService/v1/logistics/transport/route/direction/truck').status_code == 404
    assert client.get('/_AMapService/v4/maps?callback=cb();alert(1)').status_code == 422
    app.state.map_proxy_transport = httpx.MockTransport(lambda r: httpx.Response(200, text='SERVER_SECURITY_CODE', headers={'Content-Type': 'application/javascript'}))
    assert client.get('/_AMapService/v4/maps').status_code == 502


def test_vehicle_template_exposes_dropdowns_and_stop_validation(client):
    login(client)
    wb = load_workbook(BytesIO(client.get('/api/master-data/templates/vehicles.xlsx').content))
    validations = {str(v.sqref): v for v in wb['数据'].data_validations.dataValidation}
    for column, values in [('E', ['微型', '轻型', '中型', '重型']), ('F', ['普通货车', '纯电动货车', '插电混动货车'])]:
        rule = validations[column+'2:'+column+'10001']
        assert rule.formula1 == '"'+','.join(values)+'"'
        assert rule.showDropDown is False and rule.showErrorMessage and rule.errorStyle == 'stop'
    wb.close()


@pytest.mark.parametrize('size,size_code', [('微型', 1), ('轻型', 2), ('中型', 3), ('重型', 4), ('2', 2)])
@pytest.mark.parametrize('cartype,type_code', [('普通货车', 0), ('纯电动货车', 1), ('插电混动货车', 2), ('0', 0)])
def test_vehicle_chinese_dropdown_and_legacy_codes_import(client, size, size_code, cartype, type_code):
    login(client); w = warehouse(client)
    batch = preview(client, 'vehicles', xlsx(client, 'vehicles', [{'车牌': '沪A12345', '归属仓编号': w['code'], '最大件数': 400, '默认可用': '是', '车型大小': size, '车辆类型': cartype}]))
    assert batch['can_commit'] and commit(client, batch).status_code == 200
    row = client.get('/api/vehicles').json()['items'][0]
    assert row['size'] == size_code and row['cartype'] == type_code


def test_documented_truck_success_code_retains_real_polyline_without_promoting_execution(settings):
    vehicle, origin, destination = truck_data()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'errcode': 10000, 'data': {'route': {'paths': [
        {'distance': '1000', 'duration': '60', 'restriction': '1', 'steps': [{'polyline': '121.45,31.2;121.46,31.21'}, {'polyline': '121.46,31.21;121.5,31.2'}]}]}}}))
    road = TruckRoad(amap_settings(settings), 1, transport=transport)
    segment = road.segment(vehicle, origin, destination, '2026-11-12T08:00:00+08:00')
    assert segment['source'] == 'LIVE' and len(segment['polyline']) == 4 and segment['crs'] == 'GCJ02'
    assert not segment['verified'] and segment['time_status'] == 'TIME_UNVERIFIED'
    assert 'SERVER_ONLY_KEY' not in json.dumps(segment)


def test_unknown_vehicle_dropdown_value_blocks_whole_import(client):
    login(client); w = warehouse(client)
    batch = preview(client, 'vehicles', xlsx(client, 'vehicles', [{'车牌': '沪A12345', '归属仓编号': w['code'], '最大件数': 400, '默认可用': '是', '车型大小': '超大型', '车辆类型': '普通货车'}]))
    assert not batch['can_commit'] and any('下拉选项' in e['message'] for e in batch['errors'])
    assert commit(client, batch).status_code == 422
    assert client.get('/api/vehicles').json()['items'] == []
