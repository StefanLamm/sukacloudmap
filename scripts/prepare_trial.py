"""Create a NEW isolated trial DB from historical demand. Never overwrites business DB.

Actual account password hashes are copied locally so users keep their own login;
no default credentials or sessions are copied. Test vehicle parameters/roads/hours
are explicitly documented. Source workbooks remain untouched.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import ROOT,Settings
from app.database import make_engine,migrate,session_factory,start_write
from app.demand_imports import commit,preview
from app.master_service import normalize,save_entity
from app.models import Account,DemandImport,SystemState
from app.planning_inputs import encode
from app.planning_schemas import CommitDemand
from scripts.logistics_source_data import extract


def seed_catalog(db,actor_id,records,config):
    eligible=[r for r in records if r['warehouse_code'] in ['HF','HA']]
    warehouses={}
    for p,w in config['warehouses'].items():
        entity,_=save_entity(db,'warehouses',{'code':w['code'],'name':w['name'],'address':w['address'],'departure_time':'08:00','crs':'WGS84','location_source':'用户指定市政府；坐标待核验'},actor_id)
        warehouses[w['code']]=entity
    q,_=save_entity(db,'quotes',{'code':'UAT-KM','name':'试用专车 · 用户确认5.8元每公里','mode':'KM','per_km':config['dedicated_per_km'],'effective_from':min(r['delivery_date'] for r in eligible)},actor_id)
    names=defaultdict(list)
    for r in eligible:names[r['store_name']].append(r)
    stores={}
    for n,(name,rs) in enumerate(sorted(names.items()),1):
        whs={r['warehouse_code'] for r in rs};assert len(whs)==1,(name,'warehouse conflict')
        r=rs[-1]
        s,_=save_entity(db,'stores',{'code':f'UAT{n:04}','name':name,'city':r['city'],'address':'待补充实际门店地址：'+name,'crs':'WGS84','location_source':'历史登记；未提供精确坐标',
             'warehouse_id':warehouses[r['warehouse_code']].id,'business_open':'08:00','business_close':'23:59','close_day_offset':0,'unload_minutes':5,'dedicated_quote_id':q.id,'common_unit_price':None},actor_id)
        stores[name]=s
    profile=config['truck_reference_only'];vehicles=[]
    for code,count,prefix in [('HF',35,'皖'),('HA',15,'苏')]:
        for n in range(1,count+1):
            v,_=save_entity(db,'vehicles',{'plate':f'{prefix}UAT{n:03}','max_pieces':400,'warehouse_id':warehouses[code].id,'available':True,
                            **{k:profile[k] for k in ['size','cartype','plate_color','length_m','width_m','height_m','rated_load_t','full_gross_t','axles']}},actor_id)
            vehicles.append(v)
    return eligible,stores,warehouses,vehicles


def normalized_document(rows,stores,source):
    return {'columns':[{'index':0,'label':'门店名称'},{'index':1,'label':'件数'},{'index':2,'label':'门店编号'}],
            'rows':[{'row':r['source_row'],'values':[r['store_name'],r['pieces'],stores[r['store_name']].code],'errors':[]} for r in rows],
            'blank_rows':0,'suggested_mapping':{'store_name':0,'pieces':1,'store_code':2},
            'source':{'sheet_name':rows[0]['source_sheet'],'header_row':2,'ignored_sheets':[],'scope':'安徽、江苏；其他归仓未确认的记录独立列出，未自动导入','original_file_sha256':source['sha256']}}


def import_days(db,actor,records,stores,report):
    sources={Path(s['path']).name:s for s in report['sources']};days=defaultdict(list);receipts=[]
    for r in records:days[r['delivery_date']].append(r)
    for day,rows in sorted(days.items()):
        source=sources[Path(rows[0]['source_file']).name];doc=normalized_document(rows,stores,source)
        batch=DemandImport(delivery_date=day,file_name=Path(rows[0]['source_file']).name,file_hash=source['sha256'],document=encode(doc),created_by=actor.id)
        db.add(batch);db.flush();p=preview(db,batch,{'store_name':0,'pieces':1,'store_code':2,'delivery_date':None})
        assert p['result']['can_commit'],p['result']['errors']
        receipt=commit(db,batch,CommitDemand(delivery_date=day,preview_revision=1),actor.id,actor.display_name)
        assert receipt['totals']['total_pieces']==sum(r['pieces'] for r in rows)
        receipts.append(receipt)
    return receipts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=ROOT.parent/'物流分析表');args=parser.parse_args()
    records,report=extract(args.source);folder=ROOT/'data'/('trial-20261111-'+uuid.uuid4().hex[:6]);path=folder/'trial.db'
    target=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(path),planning_road_source='TEST_GRID',truck_live_calls_enabled=False,amap_web_service_key=None,cookie_namespace='uat')
    engine=make_engine(target);migrate(engine)
    source_engine=make_engine(Settings())
    with session_factory(source_engine)() as original,session_factory(engine)() as db:
        rows=list(original.scalars(select(Account)));assert any(a.active and a.is_admin for a in rows),'需要已有管理员；不会创建默认密码'
        for a in rows:db.add(Account(id=a.id,username=a.username,display_name=a.display_name,password_hash=a.password_hash,is_admin=a.is_admin,active=a.active,auth_revision=a.auth_revision,revision=a.revision,created_at=a.created_at,updated_at=a.updated_at))
        db.commit()
    source_engine.dispose()
    with session_factory(engine)() as db:
        start_write(db,engine);actor=db.scalar(select(Account).where(Account.active.is_(True),Account.is_admin.is_(True)))
        eligible,stores,warehouses,vehicles=seed_catalog(db,actor.id,records,report['user_config'])
        receipts=import_days(db,actor,eligible,stores,report);db.commit()
    engine.dispose()
    raw_days=defaultdict(list)
    for r in records:raw_days[r['delivery_date']].append(r)
    valid_days=[d for d,rs in raw_days.items() if all(r['warehouse_code'] in ['HF','HA'] for r in rs)]
    chosen=max(valid_days,key=lambda d:(len(raw_days[d]),d))
    receipt=next(r for r in receipts if r['delivery_date']==chosen)
    pointer={'database':str(path),'port':8001,'default_delivery_date':chosen,'road_source':'TEST_GRID','account_source':'original account hashes copied locally; no sessions or default credentials',
             'stores':len(stores),'warehouses':2,'vehicles':len(vehicles),'days':len(receipts),'demand_rows':len(eligible),'pieces':sum(r['pieces'] for r in eligible),'excluded_pending_warehouse_rows':report['out_of_scope_rows'],
             'source_workbooks_modified':False,'real_database_modified':False,'default_snapshot_id':receipt['snapshot_id'],
             'assumptions':['35合肥+15淮安测试车位；UAT车牌不对应实车','营业08:00-23:59、卸货5分钟为测试值，需逐店维护','两仓精确坐标、门店地址与坐标待核验','共配缺价；未把收入当成本','车辆尺寸/重量来自公开4.2米级参考车型，非实车核验','道路为非地理固定模拟，不可导航执行']}
    (ROOT/'data/trial-current.json').write_text(json.dumps(pointer,ensure_ascii=False,indent=2))
    (ROOT/'docs/hardening/trial-data.json').write_text(json.dumps(pointer,ensure_ascii=False,indent=2))
    (ROOT/'docs/hardening/source-analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in pointer.items() if k not in ['database','default_snapshot_id','account_source','assumptions']},ensure_ascii=False))


if __name__=='__main__':main()
