from datetime import datetime
from io import BytesIO
import json

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
import pytest
from sqlalchemy import select

from app.api import ApiError
from app.database import make_engine, migrate, session_factory
from app.main import create_app
from app.models import Account, Base, MasterImport, QuoteVersion, SharedRecord
from app.security import hash_password
from conftest import ADMIN_PASSWORD, USER_PASSWORD, create_member, login

DAY = '2026-11-11'


def post(client, kind, payload):
    r=client.post('/api/'+kind,json=payload)
    assert r.status_code==201, r.text
    return r.json()


def warehouse(client, code='W01', **changes):
    return post(client,'warehouses',{'code':code,'name':code+'出货仓','address':'测试仓库地址','longitude':'121.45','latitude':'31.20','crs':'GCJ02','location_source':'固定测试资料','departure_time':'08:00',**changes})


def quote(client, code='Q01', **changes):
    return post(client,'quotes',{'code':code,'name':code+'报价','mode':'KM','per_km':'2.00','effective_from':DAY,**changes})


def store_payload(w, q=None, code='S01', **changes):
    return {'code':code,'name':'同名门店','city':'甲市','address':'测试门店地址','longitude':'121.50','latitude':'31.23','crs':'GCJ02','location_source':'固定测试资料',
            'warehouse_id':w['id'],'business_open':'08:00','business_close':'18:00','close_day_offset':0,'unload_minutes':20,
            'latest_arrival_at':DAY+'T09:00:00+08:00','dedicated_quote_id':q['id'] if q else None,'common_unit_price':'2.25',**changes}


def vehicle_payload(w, **changes):
    return {'plate':'苏A12345','warehouse_id':w['id'],'max_pieces':100,'available':True,'size':2,'cartype':0,'plate_color':'BLUE',
            'length_m':'6','width_m':'2.1','height_m':'2.8','rated_load_t':'5','full_gross_t':'8','axles':2,**changes}


@pytest.fixture
def master(client):
    login(client)
    return client,warehouse(client),quote(client)


def xlsx(client, kind, rows, rename_header=None):
    r=client.get('/api/master-data/templates/'+kind+'.xlsx')
    assert r.status_code==200
    wb=load_workbook(BytesIO(r.content));ws=wb['数据']
    labels=[c.value for c in ws[1]]
    # Exercise legacy input columns too, although new management templates no
    # longer ask users for operational dates.
    for legacy in ['最晚到店日期时间', '生效日期']:
        if any(legacy in row for row in rows) and legacy not in labels:
            labels.append(legacy)
            ws.cell(1,len(labels),legacy)
    for row in rows:
        ws.append([row.get(label) for label in labels])
    if rename_header:ws.cell(1,1,rename_header)
    result=BytesIO();wb.save(result);wb.close()
    return result.getvalue()


def preview(client, kind, content):
    r=client.post('/api/master-data/imports/'+kind+'/preview?filename=test.xlsx',content=content,headers={'content-type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
    assert r.status_code==200,r.text
    return r.json()


def commit(client, p):
    return client.post('/api/master-data/imports/'+p['preview_id']+'/commit')


@pytest.mark.parametrize('path',['/api/warehouses','/api/stores','/api/vehicles','/api/quotes','/api/master-data/catalog','/api/master-data/templates/stores.xlsx'])
def test_master_data_requires_login(client,path):
    assert client.get(path).status_code==401


def test_business_users_share_and_maintain_master_data(client,app):
    login(client);create_member(client)
    with TestClient(app) as business:
        login(business,'member',USER_PASSWORD)
        w=warehouse(business)
        assert client.get('/api/warehouses').json()['items'][0]['id']==w['id']
        assert business.get('/warehouses').status_code==200
        token=business.headers.pop('x-csrf-token')
        assert business.post('/api/warehouses',json={}).status_code==403


@pytest.mark.parametrize('field,value',[('code',''),('code','编号!'),('name','   '),('address',''),('departure_time','24:00'),('departure_time','8:00'),('longitude','181'),('latitude','-91'),('longitude','bad'),('crs','UNKNOWN')])
def test_warehouse_field_validation(master,field,value):
    c,w,q=master
    payload={'code':'W02','name':'仓库','address':'地址','departure_time':'08:00','longitude':'120','latitude':'30','crs':'GCJ02',field:value}
    assert c.post('/api/warehouses',json=payload).status_code==422


def test_coordinates_source_and_explicit_crs(master):
    c,w,q=master
    assert c.post('/api/warehouses',json={'code':'W02','name':'仓库','address':'地址','departure_time':'08:00','longitude':'120'}).status_code==422
    assert c.post('/api/warehouses',json={'code':'W02','name':'仓库','address':'地址','departure_time':'08:00','longitude':'120','latitude':'30'}).status_code==422
    r=c.patch('/api/warehouses/'+w['id'],json={'expected_revision':w['revision'],'longitude':'120.12345678','latitude':'30.12345678','crs':'WGS84','location_source':'GPS人工校正'})
    assert r.status_code==200
    assert r.json()['longitude']=='120.12345678' and not r.json()['location_ready']
    assert r.json()['location_source']=='GPS人工校正'


@pytest.mark.parametrize('field,value',[('warehouse_id','missing'),('business_open','25:00'),('business_close','07:00'),('unload_minutes',-1),('unload_minutes',1.5),('unload_minutes',True),('latest_arrival_at','09:00'),('latest_arrival_at',DAY+'T09:00:00'),('common_unit_price','-0.01'),('common_unit_price','0.1234567'),('dedicated_quote_id','missing')])
def test_store_validation(master,field,value):
    c,w,q=master
    assert c.post('/api/stores',json=store_payload(w,q,**{field:value})).status_code==422


def test_stable_codes_duplicate_names_cross_midnight_and_zero_unload(master):
    c,w,q=master
    a=post(c,'stores',store_payload(w,q))
    b=post(c,'stores',store_payload(w,q,'S02',business_open='20:00',business_close='02:00',close_day_offset=1,unload_minutes=0))
    assert a['name']==b['name'] and a['id']!=b['id']
    assert b['close_day_offset']==1 and b['unload_minutes']==0
    assert c.patch('/api/stores/'+a['id'],json={'expected_revision':1,'code':'RENAMED'}).status_code==422
    assert c.patch('/api/stores/'+a['id'],json={'expected_revision':1,'code':123}).status_code==422
    updated=c.patch('/api/stores/'+a['id'],json={'expected_revision':1,'name':'新名称'})
    assert updated.status_code==200 and updated.json()['id']==a['id'] and updated.json()['code']=='S01'
    assert c.patch('/api/stores/'+a['id'],json={'expected_revision':1,'name':'旧修订覆盖'}).status_code==409
    assert c.post('/api/stores',json=store_payload(w,q,'s01')).status_code==409


@pytest.mark.parametrize('field,value',[('max_pieces',0),('max_pieces',-1),('max_pieces',1.5),('max_pieces',True),('plate','abc'),('warehouse_id','missing'),('size',0),('cartype',3),('plate_color','invented'),('length_m','-1'),('rated_load_t','0'),('full_gross_t','4'),('axles',0),('axles',2.5),('available','false')])
def test_vehicle_fields(master,field,value):
    c,w,q=master
    assert c.post('/api/vehicles',json=vehicle_payload(w,**{field:value})).status_code==422


def test_plate_normalization_capacity_and_missing_road_profile(master):
    c,w,q=master
    a=post(c,'vehicles',vehicle_payload(w,plate='苏a·12345'))
    assert a['plate']=='苏A12345' and a['road_profile_complete'] and not a['road_verified']
    assert a['cartype']==0
    assert c.post('/api/vehicles',json=vehicle_payload(w)).status_code==409
    b=post(c,'vehicles',{'plate':'苏AD12345','max_pieces':10,'warehouse_id':w['id'],'available':False})
    assert not b['road_profile_complete'] and len(b['road_profile_missing'])==9 and not b['day_available']
    assert c.post('/api/vehicles',json={**vehicle_payload(w,plate='苏A54321'),'range_km':100}).status_code==422
    assert c.post('/api/vehicles',json={**vehicle_payload(w,plate='苏A54321'),'price_multiplier':2}).status_code==422


def test_day_overrides_do_not_modify_other_days_or_master_defaults(master):
    c,w,q=master; w2=warehouse(c,'W02');v=post(c,'vehicles',vehicle_payload(w))
    assert c.put(f"/api/vehicles/{v['id']}/days/{DAY}",json={'warehouse_id':w2['id'],'available':False,'expected_revision':0}).status_code==200
    result=c.get('/api/vehicles?delivery_date='+DAY).json()['items'][0]
    assert result['warehouse_id']==w['id'] and result['day_warehouse_id']==w2['id'] and not result['day_available']
    next_day=c.get('/api/vehicles?delivery_date=2026-11-12').json()['items'][0]
    assert next_day['day_warehouse_id']==w['id'] and next_day['day_available'] and next_day['day_revision']==0
    assert c.put(f"/api/vehicles/{v['id']}/days/{DAY}",json={'warehouse_id':w['id'],'available':True,'expected_revision':0}).status_code==409
    assert c.put(f"/api/warehouses/{w['id']}/days/{DAY}",json={'departure_time':'07:15','expected_revision':0}).status_code==200
    c.patch('/api/warehouses/'+w['id'],json={'departure_time':'09:00','expected_revision':1})
    same=c.get('/api/warehouses?delivery_date='+DAY).json()['items'][0]
    other=c.get('/api/warehouses?delivery_date=2026-11-12').json()['items'][0]
    assert same['departure_time']=='09:00' and same['day_departure_time']=='07:15' and other['day_departure_time']=='09:00'


@pytest.mark.parametrize('values',[{'per_km':None},{'per_km':'-0.01'},{'per_km':'1.0000001'},{'per_km':'1234567890123.123456'},{'base_price':'0'},
    {'mode':'BASE_STOPS','per_km':None,'base_price':'0','included_stops':None,'extra_stop_price':'0'},
    {'mode':'BASE_STOPS','per_km':None,'base_price':'0','included_stops':-1,'extra_stop_price':'0'},
    {'mode':'BASE_STOPS','per_km':None,'base_price':'0','included_stops':1.5,'extra_stop_price':'0'}])
def test_quote_parameter_validation(master,values):
    c,w,q=master
    assert c.post('/api/quotes',json={'code':'Q02','name':'报价','mode':'KM','per_km':'1','effective_from':DAY,**values}).status_code==422


def test_decimal_precision_and_zero_prices(master):
    c,w,q=master
    zero=quote(c,'QZERO',per_km='0')
    a=post(c,'stores',store_payload(w,zero,common_unit_price='0'))
    assert a['common_available'] and a['dedicated_quote_available'] and a['common_unit_price']=='0'
    assert a['dedicated_quote_version']['per_km']=='0'
    b=post(c,'stores',store_payload(w,None,'S02',common_unit_price=None))
    assert not b['common_available'] and not b['dedicated_quote_available']
    d=post(c,'stores',store_payload(w,q,'S03',common_unit_price='123456789012.123456'))
    assert d['common_unit_price']=='123456789012.123456'
    base=quote(c,'QBASE',mode='BASE_STOPS',per_km=None,base_price='0',included_stops=0,extra_stop_price='0')
    assert base['latest_version']['included_stops']==0 and base['latest_version']['base_price']=='0'


def test_price_modes_independently_missing(master):
    c,w,q=master
    a=post(c,'stores',store_payload(w,None,'S01',common_unit_price='0'))
    b=post(c,'stores',store_payload(w,q,'S02',common_unit_price=None))
    assert not a['dedicated_quote_available'] and a['common_available']
    assert b['dedicated_quote_available'] and not b['common_available']


def test_cross_city_same_price_different_name_and_warehouse_rules(master):
    c,w,q=master
    same=quote(c,'Q02',name='另一个城市的同价报价',per_km='2.000000')
    diff=quote(c,'Q03',per_km='2.01')
    other_w=warehouse(c,'W02')
    a=post(c,'stores',store_payload(w,q,'S01',city='甲市'))
    b=post(c,'stores',store_payload(w,same,'S02',city='乙市'))
    d=post(c,'stores',store_payload(w,diff,'S03',city='甲市'))
    e=post(c,'stores',store_payload(other_w,q,'S04',city='甲市'))
    check=lambda ids:c.post('/api/master-data/compatibility',json={'store_ids':ids,'delivery_date':DAY}).json()
    assert check([a['id'],b['id']])['compatible']
    assert not check([a['id'],d['id']])['compatible']
    assert not check([a['id'],e['id']])['compatible']
    assert same['latest_version']['signature']==q['latest_version']['signature']
    missing=post(c,'stores',store_payload(w,None,'S05'))
    assert not check([a['id'],missing['id']])['compatible']
    assert c.post('/api/master-data/compatibility',json={'store_ids':[a['id'],a['id']]}).status_code==422


def test_base_price_compatibility_uses_all_parameters(master):
    c,w,q=master
    params={'mode':'BASE_STOPS','per_km':None,'base_price':'100','included_stops':3,'extra_stop_price':'20'}
    a=quote(c,'BASE1',**params); b=quote(c,'BASE2',**{**params,'base_price':'100.00','extra_stop_price':'20.000'})
    assert a['latest_version']['signature']==b['latest_version']['signature']
    for i,(field,value) in enumerate([('base_price','101'),('included_stops',4),('extra_stop_price','21')]):
        other=quote(c,'DIFF'+str(i),**{**params,field:value})
        assert other['latest_version']['signature']!=a['latest_version']['signature']


def test_quote_versions_immutable_and_effective_by_date(master):
    c,w,q=master;a=post(c,'stores',store_payload(w,q))
    old=c.get('/api/quotes/'+q['id']+'/versions').json()['items'][0]
    same=c.patch('/api/quotes/'+q['id'],json={'per_km':'2.000000','expected_revision':1})
    assert same.status_code==200 and same.json()['revision']==1
    new=c.patch('/api/quotes/'+q['id'],json={'name':'未来价格','per_km':'3.00','effective_from':'2026-11-12','expected_revision':1})
    assert new.status_code==200 and new.json()['latest_version']['version']==2
    assert c.get('/api/quotes/'+q['id']+'/versions').json()['items'][1]==old
    now_row=c.get('/api/stores/'+a['id']+'?delivery_date='+DAY).json()
    future=c.get('/api/stores/'+a['id']+'?delivery_date=2026-11-12').json()
    assert now_row['dedicated_quote_version']['per_km']=='2' and future['dedicated_quote_version']['per_km']=='3'
    assert c.get('/api/stores/'+a['id']+'?delivery_date=2026-11-10').json()['dedicated_quote_version'] is None


def test_xlsx_preview_atomic_commit_and_idempotency(master):
    c,w,q=master
    content=xlsx(c,'warehouses',[{'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:30'},{'仓库编号':'W03','仓库名称':'三仓','地址':'地址','最早发车时间':'09:00'}])
    p=preview(c,'warehouses',content)
    assert p['can_commit'] and p['create_count']==2 and p['rows'][0]['source_row']==2
    assert len(c.get('/api/warehouses').json()['items'])==1
    assert commit(c,p).json()['receipt']=={'created':2,'updated':0,'unchanged':0}
    assert commit(c,p).status_code==200
    assert len(c.get('/api/warehouses').json()['items'])==3
    assert c.get('/api/master-data/imports/'+p['preview_id']).json()['status']=='COMMITTED'


def test_xlsx_errors_do_not_partially_write_and_duplicates_are_reported(master):
    c,w,q=master
    rows=[{'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:00'},
          {'仓库编号':'W02','仓库名称':'重号','地址':'地址','最早发车时间':'08:00'},
          {'仓库编号':'W03','仓库名称':'时间错','地址':'地址','最早发车时间':'27:00'}]
    p=preview(c,'warehouses',xlsx(c,'warehouses',rows))
    assert not p['can_commit'] and {i['row'] for i in p['errors']}=={3,4}
    assert commit(c,p).status_code==422
    assert len(c.get('/api/warehouses').json()['items'])==1


def test_xlsx_stale_preview_rejected_but_account_changes_do_not_invalidate(master):
    c,w,q=master
    content=xlsx(c,'warehouses',[{'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:00'}])
    p=preview(c,'warehouses',content)
    warehouse(c,'W03')
    assert commit(c,p).status_code==409
    p=preview(c,'warehouses',content)
    create_member(c)
    assert commit(c,p).status_code==200


def test_xlsx_store_update_uses_code_not_duplicate_name_and_preserves_id(master):
    c,w,q=master
    a=post(c,'stores',store_payload(w,q));b=post(c,'stores',store_payload(w,q,'S02'))
    row={'门店编号':'S01','门店名称':'同名门店','城市':'乙市','地址':'新地址','出货仓编号':'W01','营业开始':'20:00','营业结束':'02:00','闭店跨日天数':1,'卸货分钟数':0,
         '专车报价编号':'Q01','共配每件单价':'0','最晚到店日期时间':datetime(2026,11,12,1,0)}
    p=preview(c,'stores',xlsx(c,'stores',[row]))
    assert p['can_commit'] and p['update_count']==1
    assert commit(c,p).status_code==200
    changed=c.get('/api/stores/'+a['id']).json()
    assert changed['id']==a['id'] and changed['common_unit_price']=='0' and changed['close_day_offset']==1
    assert changed['latest_arrival_at'].endswith('+08:00')
    assert c.get('/api/stores/'+b['id']).json()['revision']==1


def test_xlsx_quote_versions_and_decimal_normalization(master):
    c,w,q=master
    row={'报价编号':'Q01','方案名称':'新报价','计费模式':'按公里','每公里单价':'2.123456','生效日期':DAY}
    p=preview(c,'quotes',xlsx(c,'quotes',[row]));assert p['can_commit']
    assert commit(c,p).status_code==200 and commit(c,p).status_code==200
    versions=c.get('/api/quotes/'+q['id']+'/versions').json()['items']
    assert len(versions)==2 and versions[0]['per_km']=='2.123456' and versions[1]['per_km']=='2'


def test_xlsx_vehicle_boolean_units_and_more_than_50_cars(master):
    c,w,q=master
    rows=[{'车牌':f'苏A{i:05d}','归属仓编号':'W01','最大件数':100+i,'默认可用':'是' if i%2 else '否'} for i in range(51)]
    p=preview(c,'vehicles',xlsx(c,'vehicles',rows))
    assert p['can_commit'] and p['valid_rows']==51
    assert commit(c,p).status_code==200
    vehicles=c.get('/api/vehicles?delivery_date='+DAY).json()['items']
    assert len(vehicles)==51 and sum(v['day_available'] for v in vehicles)==25
    assert all(not v['road_profile_complete'] for v in vehicles)


def test_xlsx_800_store_batch_complete_and_deterministic_codes(master):
    c,w,q=master
    rows=[{'门店编号':f'{i:05d}','门店名称':'测试门店'+str(i),'地址':'固定测试地址','出货仓编号':'W01','营业开始':'08:00','营业结束':'22:00','闭店跨日天数':0,'卸货分钟数':10,'专车报价编号':'Q01','共配每件单价':'0'} for i in range(800)]
    p=preview(c,'stores',xlsx(c,'stores',rows))
    assert p['can_commit'] and p['valid_rows']==800 and len(p['rows'])==100
    assert commit(c,p).json()['receipt']['created']==800
    stores=c.get('/api/stores').json()['items']
    assert len(stores)==800 and stores[0]['code']=='00000' and stores[-1]['code']=='00799'
    assert len({s['id'] for s in stores})==800


def test_xlsx_formulas_external_links_unknown_headers_and_empty_files(master):
    c,w,q=master
    valid={'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:00'}
    p=preview(c,'warehouses',xlsx(c,'warehouses',[{**valid,'仓库名称':'=1+1'}]))
    assert not p['can_commit'] and '公式' in str(p['errors'])
    assert not preview(c,'warehouses',xlsx(c,'warehouses',[valid],rename_header='未知列'))['can_commit']
    assert not preview(c,'warehouses',xlsx(c,'warehouses',[]))['can_commit']
    assert c.post('/api/master-data/imports/stores/preview?filename=bad.xlsx',content=b'not a workbook').status_code==422
    assert c.post('/api/master-data/imports/stores/preview?filename=bad.csv',content=b'a,b').status_code==422
    workbook=load_workbook(BytesIO(xlsx(c,'warehouses',[valid])));workbook['数据']['B2'].hyperlink='https://example.com';out=BytesIO();workbook.save(out)
    assert c.post('/api/master-data/imports/warehouses/preview?filename=linked.xlsx',content=out.getvalue()).status_code==422


def test_import_failure_mid_transaction_rolls_back_whole_batch(master,monkeypatch):
    c,w,q=master
    rows=[{'仓库编号':code,'仓库名称':code,'地址':'地址','最早发车时间':'08:00'} for code in ['W02','W03']]
    p=preview(c,'warehouses',xlsx(c,'warehouses',rows))
    import app.master_imports as importer
    real=importer.save_entity
    def fail_second(db,kind,payload,*args,**kwargs):
        if payload['code']=='W03':raise ApiError(422,'INJECTED_FAILURE','测试第二行失败')
        return real(db,kind,payload,*args,**kwargs)
    monkeypatch.setattr(importer,'save_entity',fail_second)
    assert commit(c,p).status_code==422
    assert len(c.get('/api/warehouses').json()['items'])==1
    assert c.get('/api/master-data/imports/'+p['preview_id']).json()['status']=='PREVIEW'


def test_account_disabled_during_upload_cannot_save_preview(master,app):
    c,w,q=master;member=create_member(c)
    content=xlsx(c,'warehouses',[{'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:00'}])
    with TestClient(app) as business:
        login(business,'member',USER_PASSWORD)
        def chunks():
            yield content[:100]
            r=c.patch('/api/accounts/'+member['id'],json={'active':False,'expected_revision':1})
            assert r.status_code==200
            yield content[100:]
        r=business.post('/api/master-data/imports/warehouses/preview?filename=test.xlsx',content=chunks())
        assert r.status_code==401
    with app.state.sessions() as db:
        assert list(db.scalars(select(MasterImport)))==[]


def test_upload_size_limit_and_expired_preview(master,app,monkeypatch):
    c,w,q=master
    import app.master_api as routes
    monkeypatch.setattr(routes,'MAX_FILE',100)
    assert c.post('/api/master-data/imports/stores/preview?filename=test.xlsx',content=b'x'*101).status_code==413
    monkeypatch.setattr(routes,'MAX_FILE',5*1024*1024)
    p=preview(c,'warehouses',xlsx(c,'warehouses',[{'仓库编号':'W02','仓库名称':'二仓','地址':'地址','最早发车时间':'08:00'}]))
    with app.state.sessions() as db:
        batch=db.get(MasterImport,p['preview_id']);batch.created_at=1;db.commit()
    assert commit(c,p).status_code==409
    assert len(c.get('/api/warehouses').json()['items'])==1


def test_upgrade_from_stage2_keeps_accounts_records_and_schema(settings):
    engine=make_engine(settings);migrate(engine,'0001_accounts')
    from sqlalchemy import MetaData, Table
    from datetime import datetime, timezone
    # Use genuine old-schema columns. The timestamp is after Beijing midnight
    # but still the preceding UTC date, exercising task-date migration too.
    instant=int(datetime(2026,11,11,18,0,tzinfo=timezone.utc).timestamp())
    with engine.begin() as connection:
        metadata=MetaData()
        accounts=Table('accounts',metadata,autoload_with=connection)
        records=Table('shared_records',metadata,autoload_with=connection)
        connection.execute(accounts.insert().values(id='LEGACY_ADMIN',username='admin',display_name='原管理员',password_hash=hash_password(ADMIN_PASSWORD),
            is_admin=True,active=True,auth_revision=1,revision=1,created_at=instant,updated_at=instant))
        connection.execute(records.insert().values(id='LEGACY_RECORD',title='原共享记录',content='升级后保留',created_by='LEGACY_ADMIN',updated_by='LEGACY_ADMIN',
            revision=1,created_at=instant,updated_at=instant))
    migrate(engine);migrate(engine)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    engine.dispose()
    with TestClient(create_app(settings)) as c:
        login(c)
        row=c.get('/api/workspace/records').json()['items'][0]
        assert row['title']=='原共享记录' and row['task_date']=='2026-11-12'
        assert c.get('/api/master-data/catalog').json()['warehouses']==[]
