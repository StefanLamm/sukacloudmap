import json
from io import BytesIO
import httpx
from openpyxl import load_workbook
import pytest
from app import regions
from app.api import ApiError
from app.models import Store
from conftest import login
from test_stage3 import warehouse,store_payload,quote,xlsx,preview,commit

TARGET={'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'340104','district_name':'蜀山区'}

def rule(amount='80',targets=None):return {'amount':amount,'targets':targets or [TARGET]}

def test_region_quote_four_modes_validation_signature_history(client):
 login(client)
 q=quote(client,mode='KM_REGION',per_km='5.8',region_surcharge=rule())
 assert q['latest_version']['region_surcharge']==rule()
 same=quote(client,'Q2',mode='KM_REGION',per_km='5.8',region_surcharge=rule('80.0'))
 assert same['latest_version']['signature']==q['latest_version']['signature']
 diff=quote(client,'Q3',mode='KM_REGION',per_km='5.8',region_surcharge=rule('81'))
 assert diff['latest_version']['signature']!=q['latest_version']['signature']
 for changes in [{'region_surcharge':{}},{'region_surcharge':rule('-1')},{'region_surcharge':rule(targets=[TARGET,TARGET])},{'region_surcharge':rule(targets=[{**TARGET,'district_code':'320104'}])}]:
  r=client.post('/api/quotes',json={'code':'BAD','name':'错误规则','mode':'KM_REGION','per_km':'1',**changes});assert r.status_code==422
 r=client.patch('/api/quotes/'+q['id'],json={'expected_revision':q['revision'],'region_surcharge':rule('100')})
 assert r.status_code==200
 versions=client.get('/api/quotes/'+q['id']+'/versions').json()['items']
 assert [v['region_surcharge']['amount'] for v in versions]==['100','80']
 base=quote(client,'BASE',mode='BASE_STOPS_REGION',per_km=None,base_price='100',included_stops=1,extra_stop_price='20',region_surcharge=rule('50'))
 assert base['latest_version']['mode']=='BASE_STOPS_REGION'


def test_store_bulk_update_atomic_conflict_and_clear(client):
 login(client);w=warehouse(client);other=warehouse(client,'W2');q=quote(client)
 stores=[]
 for i in range(2):
  r=client.post('/api/stores',json=store_payload(w,q,code='S'+str(i),province='安徽省',city='合肥市',district='蜀山区',region_code='340104'));assert r.status_code==201;stores.append(r.json())
 items=[{'id':s['id'],'expected_revision':s['revision']} for s in stores]
 r=client.post('/api/master-data/stores/bulk-update',json={'items':items,'changes':{'warehouse_id':other['id'],'business_open':'20:00','business_close':'02:00','close_day_offset':1,'unload_minutes':15,'dedicated_quote_id':None,'common_unit_price':'0'}})
 assert r.status_code==200 and r.json()['updated']==2
 rows=client.get('/api/stores').json()['items']
 assert all(s['warehouse_id']==other['id'] and s['unload_minutes']==15 and s['dedicated_quote_id'] is None and s['common_unit_price']=='0' and s['close_day_offset']==1 for s in rows)
 assert client.post('/api/master-data/stores/bulk-update',json={'items':items,'changes':{'common_unit_price':'9'}}).status_code==409
 assert all(s['common_unit_price']=='0' for s in client.get('/api/stores').json()['items'])
 current=[{'id':s['id'],'expected_revision':s['revision']} for s in rows]
 assert client.post('/api/master-data/stores/bulk-update',json={'items':current,'changes':{'business_open':'23:00','business_close':'01:00','close_day_offset':0}}).status_code==422
 assert all(s['business_open']=='20:00' for s in client.get('/api/stores').json()['items'])
 assert client.post('/api/master-data/stores/bulk-update',json={'items':current,'changes':{'name':'非法批改'}}).status_code==422


def test_selected_store_export_xlsx_roundtrip_and_formula_safety(client):
 login(client);w=warehouse(client);q=quote(client)
 r=client.post('/api/stores',json=store_payload(w,q,code='S01',name='=恶意公式',province='安徽省',city='合肥市',district='蜀山区',region_code='340104'))
 assert r.status_code==201;s=r.json()
 export=client.post('/api/master-data/stores/export.xlsx',json={'items':[{'id':s['id'],'expected_revision':s['revision']}]})
 assert export.status_code==200
 wb=load_workbook(BytesIO(export.content));ws=wb['数据'];columns={c.value:c.column for c in ws[1]}
 assert ws.max_row==2 and ws.cell(2,columns['门店名称']).data_type=='s'
 assert ws.cell(2,columns['区县']).value=='蜀山区'
 assert ws.cell(2,columns['出货仓编号']).value==w['code']
 wb.close()
 p=preview(client,'stores',export.content);assert p['can_commit'];assert commit(client,p).status_code==200
 other=client.post('/api/stores',json=store_payload(w,q,code='S02')).json()
 current=client.get('/api/stores/'+s['id']).json()
 assert client.patch('/api/stores/'+s['id'],json={'expected_revision':current['revision'],'unload_minutes':21}).status_code==200
 stale=client.post('/api/master-data/stores/export.xlsx',json={'items':[{'id':other['id'],'expected_revision':other['revision']},{'id':s['id'],'expected_revision':current['revision']}]})
 assert stale.status_code==409 and stale.headers['content-type'].startswith('application/json')


def test_legacy_import_and_quote_template_still_work(client):
 login(client);w=warehouse(client)
 p=preview(client,'quotes',xlsx(client,'quotes',[{'报价编号':'Q1','方案名称':'普通','计费模式':'按公里','每公里单价':'5.8'}]));assert p['can_commit'];assert commit(client,p).status_code==200
 p=preview(client,'quotes',xlsx(client,'quotes',[{'报价编号':'Q2','方案名称':'地区','计费模式':'按公里加特殊地区','每公里单价':'5.8','特殊地区附加费规则JSON':json.dumps(rule(),ensure_ascii=False)}]));assert p['can_commit'];assert commit(client,p).status_code==200


def test_region_endpoint_auth_and_cached_complete_tree(client,app,settings,tmp_path,monkeypatch):
 monkeypatch.setattr(regions,'CACHE',tmp_path/'regions.json');monkeypatch.setattr(regions,'_memory',None)
 assert client.get('/api/master-data/regions').status_code==401
 # Synthetic transport checks full-tree handling without live requests.
 provinces=[{'name':f'省{i}','adcode':str(510000+i*10000),'level':'province','districts':[{'name':f'市{i}-{j}','adcode':str(510000+i*10000+j*100),'level':'city','districts':[{'name':'区','adcode':str(510001+i*10000+j*100),'level':'district'}]} for j in range(10)]} for i in range(31)]
 calls=[]
 def handler(request):
  calls.append(request);assert request.url.path=='/v3/config/district';assert request.url.params['subdistrict']=='3'
  return httpx.Response(200,json={'status':'1','districts':[{'districts':provinces}]})
 app.state.region_transport=httpx.MockTransport(handler)
 from pydantic import SecretStr
 app.state.settings.amap_web_service_key=SecretStr('SERVER_TEST_ONLY')
 login(client);a=client.get('/api/master-data/regions');b=client.get('/api/master-data/regions')
 assert a.status_code==200 and len(a.json()['items'])==31 and b.status_code==200 and len(calls)==1
 assert 'SERVER_TEST_ONLY' not in a.text
 # A failed refresh keeps prior data and selections available.
 regions._memory['fetched_at']=0;app.state.region_transport=httpx.MockTransport(lambda r:httpx.Response(503))
 c=client.get('/api/master-data/regions');assert c.status_code==200 and c.json()['notice']


def test_master_delete_respects_group_warehouse_references_and_removes_own_arrangements(client,app):
 from sqlalchemy import select
 from app.models import GroupArrangement
 from test_stage3 import vehicle_payload
 from test_demand_groups import create_group
 login(client);w=warehouse(client);other=warehouse(client,'W2')
 v=client.post('/api/vehicles',json=vehicle_payload(w)).json()
 g=create_group(client)
 response=client.put('/api/planning/groups/'+g['id']+'/vehicles/'+v['id'],json={'expected_revision':0,'warehouse_id':other['id'],'available':True})
 assert response.status_code==200
 blocked=client.delete('/api/warehouses/'+other['id'],params={'expected_revision':other['revision']})
 assert blocked.status_code==409 and '分组车辆安排' in blocked.text
 assert client.get('/api/planning/groups/'+g['id']+'/catalog').status_code==200
 assert client.delete('/api/vehicles/'+v['id'],params={'expected_revision':v['revision']}).status_code==200
 with app.state.sessions() as db:
  assert db.scalar(select(GroupArrangement).where(GroupArrangement.kind=='vehicles',GroupArrangement.entity_id==v['id'])) is None
 # Warehouse/store own arrangements are polymorphic; clean them with their master.
 for kind,entity in [('warehouses',other),('stores',client.post('/api/stores',json=store_payload(w,code='CLEAN')).json())]:
  if kind=='warehouses':
   response=client.put('/api/planning/groups/'+g['id']+'/warehouses/'+entity['id'],json={'expected_revision':0,'departure_time':'09:00'})
  else:
   response=client.put('/api/planning/days/'+g['delivery_date']+'/stores/'+entity['id'],params={'group_id':g['id']},json={'expected_revision':0,'latest_arrival_at':None})
  assert response.status_code==200
  assert client.delete('/api/'+kind+'/'+entity['id'],params={'expected_revision':entity['revision']}).status_code==200
  with app.state.sessions() as db:
   assert db.scalar(select(GroupArrangement).where(GroupArrangement.kind==kind,GroupArrangement.entity_id==entity['id'])) is None
