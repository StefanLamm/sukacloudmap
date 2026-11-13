from copy import deepcopy

import httpx
import pytest
from sqlalchemy import func, select

from app.api import ApiError
from app.location_tasks import LocationRunner, exact_candidates
from app.map_service import MapSearchService
from app.models import LocationTask, QuoteVersion, SystemState
from conftest import login
from test_stage3 import commit, post, preview, quote, store_payload, vehicle_payload, warehouse, xlsx
from test_stage4 import basis, commit as commit_demand, previewed


def selection(*rows):
    return {'items': [{'id': r['id'], 'expected_revision': r['revision']} for r in rows]}


def create_pair(c, kind):
    w = warehouse(c)
    if kind == 'warehouses':
        return w, warehouse(c, 'W02')
    if kind == 'quotes':
        return quote(c), quote(c, 'Q02')
    if kind == 'stores':
        return post(c, kind, store_payload(w)), post(c, kind, store_payload(w, code='S02'))
    return post(c, kind, vehicle_payload(w)), post(c, kind, vehicle_payload(w, plate='沪A12346'))


@pytest.mark.parametrize('kind', ['warehouses', 'stores', 'vehicles', 'quotes'])
def test_single_and_batch_deletion_remove_records_and_audit_atomically(client, kind):
    login(client)
    a, b = create_pair(client, kind)
    response = client.delete(f"/api/{kind}/{a['id']}?expected_revision={a['revision']}")
    assert response.status_code == 200, response.text
    assert response.json()['deleted'] == 1
    assert client.get(f"/api/{kind}/{a['id']}").status_code == 404
    response = client.post('/api/master-data/'+kind+'/bulk-delete', json=selection(b))
    assert response.status_code == 200 and response.json()['ids'] == [b['id']]
    assert client.get('/api/master-data/management').json()[kind] == []
    assert sum(e['action'] == 'MASTER_DELETED' for e in client.get('/api/audit-events').json()['items']) == 2


def test_batch_conflict_or_reference_leaves_every_record_intact(client):
    login(client)
    a, b = create_pair(client, 'warehouses')
    post(client, 'stores', store_payload(a))
    response = client.post('/api/master-data/warehouses/bulk-delete', json=selection(b, a))
    assert response.status_code == 409 and response.json()['code'] == 'MASTER_IN_USE'
    assert '关联门店1条' in response.json()['details'][0]['message']
    assert len(client.get('/api/warehouses').json()['items']) == 2
    client.patch('/api/warehouses/'+b['id'], json={'expected_revision': b['revision'], 'name': '别人已修改'})
    response = client.post('/api/master-data/warehouses/bulk-delete', json=selection(b))
    assert response.status_code == 409 and response.json()['code'] == 'VERSION_CONFLICT'
    assert len(client.get('/api/warehouses').json()['items']) == 2


def test_used_quote_and_vehicle_day_warehouse_cannot_be_deleted(client):
    login(client)
    w, other = create_pair(client, 'warehouses'); q = quote(client)
    s = post(client, 'stores', store_payload(other, q))
    v = post(client, 'vehicles', vehicle_payload(other))
    client.put(f"/api/vehicles/{v['id']}/days/2026-11-12", json={'expected_revision': 0, 'warehouse_id': w['id'], 'available': True})
    response = client.delete(f"/api/warehouses/{w['id']}?expected_revision={w['revision']}")
    assert response.status_code == 409 and '车辆当日安排' in response.text
    assert client.delete(f"/api/quotes/{q['id']}?expected_revision=1").status_code == 409
    assert client.delete(f"/api/stores/{s['id']}?expected_revision=1").status_code == 200
    assert client.delete(f"/api/quotes/{q['id']}?expected_revision=1").status_code == 200
    assert client.delete(f"/api/vehicles/{v['id']}?expected_revision=1").status_code == 200
    assert client.delete(f"/api/warehouses/{w['id']}?expected_revision=1").status_code == 200


def test_demand_reference_blocks_store_delete_and_keeps_snapshot_identical(basis):
    c, w, q, s, s2, v = basis
    snap = commit_demand(c, previewed(c, [[s['name'], 3, s['code']]])).json()['snapshot_id']
    before = c.get('/api/planning/snapshots/'+snap).json()
    response = c.post('/api/master-data/stores/bulk-delete', json=selection(s2, s))
    assert response.status_code == 409 and '配送需求' in response.text
    assert c.get('/api/stores/'+s2['id']).status_code == 200
    assert c.get('/api/planning/snapshots/'+snap).json() == before


def test_delete_requires_auth_csrf_unique_ids_and_current_revisions(client):
    assert client.delete('/api/warehouses/missing?expected_revision=1').status_code == 401
    login(client); w = warehouse(client)
    saved = client.headers.pop('x-csrf-token')
    assert client.delete(f"/api/warehouses/{w['id']}?expected_revision=1").status_code == 403
    client.headers['x-csrf-token'] = saved
    assert client.post('/api/master-data/warehouses/bulk-delete', json=selection(w, w)).status_code == 422
    assert client.post('/api/master-data/warehouses/bulk-delete', json={'items': []}).status_code == 422
    assert client.post('/api/master-data/warehouses/bulk-delete', json={'items': [{'id': w['id'], 'expected_revision': True}]}).status_code == 422
    assert client.post('/api/master-data/warehouses/bulk-delete', json={'items': [{'id': str(n), 'expected_revision': 1} for n in range(10001)]}).status_code == 422
    assert client.delete('/api/warehouses/'+w['id']).status_code == 422


def imported_warehouses(c, *addresses):
    data = [{'仓库编号': f'IMPORT{n}', '仓库名称': f'导入仓库{n}', '地址': a, '最早发车时间': '08:00'} for n, a in enumerate(addresses)]
    batch = preview(c, 'warehouses', xlsx(c, 'warehouses', data))
    response = commit(c, batch)
    assert response.status_code == 200, response.text
    return batch, response.json()


def candidate(name='合肥市政务中心', lon='117.23', **more):
    return {'name': name, 'address': name, 'longitude': lon, 'latitude': '31.82', 'crs': 'WGS84', **more}


class Search:
    def __init__(self, items=None, error=None, callback=None):
        self.items = items or []; self.error = error; self.calls = []; self.callback = callback

    def search(self, query):
        self.calls.append(query)
        if self.callback: self.callback()
        if self.error: raise self.error
        return {'items': self.items}


def runner(app, search):
    return LocationRunner(app.state.engine, app.state.sessions, search)


def test_import_enqueues_without_network_then_links_unique_match_and_is_idempotent(client, app, settings):
    login(client)
    batch, receipt = imported_warehouses(client, '合肥市政务中心')
    assert receipt['geocoding']['queued'] == 1
    before = client.get('/api/master-data/management').json()['warehouses'][0]
    assert before['location_status'] == 'PENDING' and before['longitude'] is None
    with app.state.sessions() as db:
        token = db.get(LocationTask, ('warehouses', before['id'])).token
    assert commit(client, batch).json() == receipt
    with app.state.sessions() as db:
        assert db.get(LocationTask, ('warehouses', before['id'])).token == token
    service = MapSearchService(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'features': [
        {'geometry': {'coordinates': [117.23, 31.82]}, 'properties': {'name': '合肥市政务中心', 'city': '合肥市'}}]})))
    assert runner(app, service).run_once()
    after = client.get('/api/master-data/management').json()['warehouses'][0]
    assert after['location_status'] == 'LINKED' and after['longitude'] == '117.23' and after['crs'] == 'WGS84'
    assert after['revision'] == before['revision'] + 1 and '地址自动关联' in after['location_source']


def test_stores_use_city_and_address_and_explicit_coordinates_are_preserved(client, app):
    login(client); w = warehouse(client)
    rows = [{'门店编号': 'IM1', '门店名称': '一店', '城市': '合肥市', '地址': '天鹅湖路100号', '出货仓编号': w['code'],
             '营业开始': '08:00', '营业结束': '18:00', '闭店跨日天数': 0, '卸货分钟数': 5},
            {'门店编号': 'IM2', '门店名称': '二店', '地址': '人工地址', '出货仓编号': w['code'], '经度': '0', '纬度': '0', '坐标系': 'WGS84',
             '营业开始': '08:00', '营业结束': '18:00', '闭店跨日天数': 0, '卸货分钟数': 5}]
    batch = preview(client, 'stores', xlsx(client, 'stores', rows)); receipt = commit(client, batch)
    assert receipt.status_code == 200 and receipt.json()['geocoding']['queued'] == 1
    search = Search([candidate('合肥市天鹅湖路100号') | {'address_parts': {'city': '合肥市', 'street': '天鹅湖路', 'housenumber': '100'}}])
    assert runner(app, search).run_once()
    assert search.calls == ['合肥市 天鹅湖路100号']
    stores = client.get('/api/master-data/management').json()['stores']
    assert stores[0]['location_status'] == 'LINKED'
    assert stores[1]['longitude'] == '0' and stores[1]['latitude'] == '0'


@pytest.mark.parametrize('items', [[], [candidate(), candidate(lon='117.24')], [candidate('合肥市')]])
def test_unmatched_ambiguous_or_city_only_results_require_manual_link(client, app, items):
    login(client); imported_warehouses(client, '合肥市政务中心')
    assert runner(app, Search(items)).run_once()
    row = client.get('/api/master-data/management').json()['warehouses'][0]
    assert row['location_status'] == 'MANUAL' and row['longitude'] is None
    assert '需手动关联' in row['location_note']


def test_provider_outage_marks_pending_records_without_flooding_or_rolling_back_import(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心', '淮安市人民政府')
    search = Search(error=ApiError(502, 'MAP_SEARCH_UNAVAILABLE', 'unavailable'))
    assert runner(app, search).run_once()
    rows = client.get('/api/master-data/management').json()['warehouses']
    assert len(rows) == 2 and all(r['location_status'] == 'MANUAL' for r in rows)
    assert len(search.calls) == 1
    assert not runner(app, search).run_once()


def test_manual_edit_during_lookup_wins_without_holding_write_transaction(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心')
    row = client.get('/api/master-data/management').json()['warehouses'][0]
    def edit():
        response = client.patch('/api/warehouses/'+row['id'], json={
            'expected_revision': row['revision'], 'longitude': '118', 'latitude': '32', 'crs': 'WGS84', 'location_source': '人工校正'})
        assert response.status_code == 200, response.text
    assert runner(app, Search([candidate()], callback=edit)).run_once()
    after = client.get('/api/master-data/management').json()['warehouses'][0]
    assert after['longitude'] == '118' and after['location_source'] == '人工校正'


def test_deletion_during_lookup_discards_late_result_and_task(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心')
    row = client.get('/api/master-data/management').json()['warehouses'][0]
    def remove():
        assert client.delete(f"/api/warehouses/{row['id']}?expected_revision=1").status_code == 200
    assert runner(app, Search([candidate()], callback=remove)).run_once()
    assert client.get('/api/master-data/management').json()['warehouses'] == []
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(LocationTask)) == 0


def test_worker_claim_is_global_and_expired_lease_can_resume(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心', '淮安市人民政府')
    first, second = runner(app, Search()), runner(app, Search())
    claim = first.claim(); assert claim
    assert second.claim() is None
    with app.state.sessions() as db:
        task = db.get(LocationTask, claim[:2]); task.lease_until = 0; task.last_attempt_at = 0; db.commit()
    assert second.claim() is not None


def test_retry_queues_missing_locations_but_keeps_linked_coordinates(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心')
    runner(app, Search()).run_once()
    a = client.get('/api/master-data/management').json()['warehouses'][0]
    b = warehouse(client)
    response = client.post('/api/master-data/warehouses/locate', json=selection(a, b))
    assert response.status_code == 200 and response.json()['queued'] == 1
    rows = client.get('/api/master-data/management').json()['warehouses']
    assert {r['id']: r['location_status'] for r in rows} == {a['id']: 'PENDING', b['id']: 'LINKED'}


def test_structured_address_matching_rejects_wrong_district_and_wrong_house_number():
    point = candidate('一号楼', address='中国安徽省合肥市蜀山区天鹅湖路100号',
                      address_parts={'city': '合肥市', 'district': '蜀山区', 'street': '天鹅湖路', 'housenumber': '100'})
    assert len(exact_candidates('合肥市天鹅湖路100号', [point])) == 1
    assert exact_candidates('合肥市庐阳区天鹅湖路100号', [point]) == []
    assert exact_candidates('合肥市天鹅湖路1000号', [point]) == []
    assert len(exact_candidates('合肥市天鹅湖路100号', [point, deepcopy(point)])) == 1


def test_background_enrichment_does_not_invalidate_unrelated_import_preview(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心')
    batch = preview(client, 'warehouses', xlsx(client, 'warehouses', [
        {'仓库编号': 'NEXT', '仓库名称': '下一批', '地址': '淮安市人民政府', '最早发车时间': '08:00'}]))
    assert runner(app, Search([candidate()])).run_once()
    assert commit(client, batch).status_code == 200


def test_enrichment_during_same_record_import_preview_keeps_new_position(client, app):
    login(client); imported_warehouses(client, '合肥市政务中心')
    batch = preview(client, 'warehouses', xlsx(client, 'warehouses', [
        {'仓库编号': 'IMPORT0', '仓库名称': '修改名称', '地址': '合肥市政务中心', '最早发车时间': '08:00'}]))
    assert runner(app, Search([candidate()])).run_once()
    response = commit(client, batch)
    assert response.status_code == 200, response.text
    row = client.get('/api/master-data/management').json()['warehouses'][0]
    assert row['name'] == '修改名称' and row['longitude'] == '117.23'
    assert row['location_status'] == 'LINKED' and response.json()['geocoding']['queued'] == 0
