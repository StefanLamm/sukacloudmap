from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api import ApiError, Identity, authenticated, database, event
from app.database import start_write
from app.demand_imports import commit, demand_template, import_view, inspect_source, parse_document, preview, upload
from app.master_imports import MAX_FILE
from app.master_service import receiving_deadline
from app.models import DemandGroup, DemandImport, InputSnapshot, Store, StoreDay, GroupArrangement, now
from app.planning_inputs import day_state, encode, freeze, input_context, snapshot_detail, snapshot_meta, configuration, totals
from app.demand_groups import get_group, group_catalog, group_view, delete_group
from app.planning_schemas import ColumnMapping, CommitDemand, FreezeInput, StoreDayInput, CreateDemandGroup, ManualDemands
from app.master_schemas import WarehouseDayInput, VehicleDayInput

router = APIRouter(prefix='/api/planning')


def find_batch(db, batch_id):
    batch = db.get(DemandImport, batch_id)
    if batch is None:
        raise ApiError(404, 'DEMAND_IMPORT_NOT_FOUND', '需求导入批次不存在，请重新上传。')
    return batch


def find_snapshot(db, snapshot_id):
    snapshot = db.get(InputSnapshot, snapshot_id)
    if snapshot is None:
        raise ApiError(404, 'SNAPSHOT_NOT_FOUND', '规划输入快照不存在。')
    return snapshot


@router.get('/demand-template.xlsx')
def get_template(identity: Identity = Depends(authenticated)):
    return Response(demand_template(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="daily-demand-template.xlsx"'})


async def file_body(request,filename):
    if not filename.lower().endswith('.xlsx'):
        raise ApiError(422, 'XLSX_REQUIRED', '请上传xlsx格式文件。')
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_FILE:
            raise ApiError(413, 'FILE_TOO_LARGE', '需求文件不能超过5MiB。')
    return bytes(content)


@router.post('/demand-imports/inspect')
async def inspect_demand(request:Request,filename:str=Query(min_length=1,max_length=200),
                         identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    content=await file_body(request,filename)
    result=await run_in_threadpool(inspect_source,content)
    db.rollback();db.expunge_all();authenticated(request,db)
    return result


@router.post('/demand-imports/upload')
async def upload_demand(request: Request, delivery_date: date, filename: str = Query(min_length=1, max_length=200),
                        group_id:str|None=None, sheet_name:str|None=Query(default=None,min_length=1,max_length=31),header_row:int=Query(default=1,ge=1,le=100),
                        identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    content=await file_body(request,filename)
    document=await run_in_threadpool(parse_document,content,sheet_name,header_row)
    start_write(db, request.app.state.engine); db.expunge_all()
    current = authenticated(request, db)
    result = upload(db, delivery_date.isoformat(), filename, content, current.account.id, document, group_id=group_id)
    db.commit(); return result


@router.get('/demand-imports/{batch_id}')
def get_import(batch_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return import_view(find_batch(db, batch_id))


@router.post('/demand-imports/{batch_id}/preview')
def preview_demand(batch_id: str, mapping: ColumnMapping, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    result = preview(db, find_batch(db, batch_id), mapping.model_dump())
    db.commit(); return result


@router.post('/demand-imports/{batch_id}/commit')
def commit_demand(batch_id: str, payload: CommitDemand, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    result = commit(db, find_batch(db, batch_id), payload, identity.account.id, identity.account.display_name)
    db.commit(); return result


@router.get('/days/{delivery_date}')
def get_day(delivery_date: date, group_id:str|None=None, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return day_state(db, delivery_date.isoformat(), group_id)


@router.post('/days/{delivery_date}/snapshots')
def freeze_input(delivery_date: date, payload: FreezeInput, group_id:str|None=None, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    day = delivery_date.isoformat(); context = input_context(db, day, group_id=group_id)
    if context['revision'] != payload.expected_revision or context['fingerprint'] != payload.expected_fingerprint:
        raise ApiError(409, 'INPUT_CHANGED', '需求或当天输入已变化，请刷新后重新冻结。')
    snapshot = freeze(db, day, identity.account.id, identity.account.display_name, context)
    db.commit(); return snapshot_meta(snapshot, context)


@router.get('/days/{delivery_date}/snapshots')
def list_snapshots(delivery_date: date, group_id:str|None=None, offset: int = Query(0, ge=0, le=2147483647), limit: int = Query(10, ge=1, le=100),
                   identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    day = delivery_date.isoformat(); context = input_context(db, day, group_id=group_id)
    items = db.scalars(select(InputSnapshot).where(InputSnapshot.delivery_date == day, InputSnapshot.group_id == group_id).order_by(InputSnapshot.version.desc()).offset(offset).limit(limit))
    count = db.scalar(select(func.count()).select_from(InputSnapshot).where(InputSnapshot.delivery_date == day, InputSnapshot.group_id == group_id))
    return {'items': [snapshot_meta(s, context) for s in items], 'total': count}


@router.get('/snapshots/{snapshot_id}/export.json')
def export_snapshot(snapshot_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    result = snapshot_detail(db, find_snapshot(db, snapshot_id))
    return Response(encode(result), media_type='application/json', headers={'Content-Disposition': f'attachment; filename="planning-input-{snapshot_id}.json"'})


@router.get('/snapshots/{snapshot_id}')
def get_snapshot(snapshot_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return snapshot_detail(db, find_snapshot(db, snapshot_id))


@router.put('/days/{delivery_date}/stores/{store_id}')
def set_receiving_deadline(delivery_date: date, store_id: str, payload: StoreDayInput, group_id:str|None=None,
                           identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    store = db.get(Store, store_id)
    if store is None:
        raise ApiError(404, 'STORE_NOT_FOUND', '门店不存在，请刷新资料。')
    if payload.latest_arrival_at and payload.latest_arrival_at.date() not in {delivery_date, delivery_date + timedelta(days=1)}:
        raise ApiError(422, 'DEADLINE_DATE', '最晚到店日期只能选择配送当日或次日。')
    day = delivery_date.isoformat()
    if group_id:
        get_group(db,group_id,day)
        row=save_arrangement(db,group_id,'stores',store_id,payload.expected_revision,{'latest_arrival_at':payload.latest_arrival_at.isoformat() if payload.latest_arrival_at else None})
        event(db,identity.account.id,'GROUP_STORE_DAY_UPDATED',store_id,f'更新{day}分组收货限制')
        db.commit()
        from types import SimpleNamespace
        import json
        return {'store_id':store_id,'delivery_date':day,'day_revision':row.revision,**receiving_deadline(store,delivery_date,SimpleNamespace(**json.loads(row.payload)))}
    row = db.get(StoreDay, (store_id, day))
    if (row.revision if row else 0) != payload.expected_revision:
        raise ApiError(409, 'VERSION_CONFLICT', '当日收货限制已被更新，请刷新后重试。')
    value = payload.latest_arrival_at.isoformat() if payload.latest_arrival_at else None
    if row is None:
        row = StoreDay(store_id=store_id, delivery_date=day, latest_arrival_at=value); db.add(row)
    else:
        row.latest_arrival_at = value; row.revision += 1
    event(db, identity.account.id, 'STORE_DAY_UPDATED', store_id, f'更新{day}门店最晚到店限制')
    db.commit(); return {'store_id': store_id, 'delivery_date': day, 'day_revision': row.revision,
                         **receiving_deadline(store, delivery_date, row)}


@router.get('/days/{delivery_date}/groups')
def list_groups(delivery_date:date,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    rows=db.scalars(select(DemandGroup).where(DemandGroup.delivery_date==delivery_date.isoformat()).order_by(DemandGroup.created_at,DemandGroup.id))
    return {'items':[group_view(db,r) for r in rows]}


@router.post('/days/{delivery_date}/groups',status_code=201)
def create_group(delivery_date:date,payload:CreateDemandGroup,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    day=delivery_date.isoformat()
    if db.scalar(select(DemandGroup.id).where(DemandGroup.delivery_date==day,DemandGroup.name==payload.name)):
        raise ApiError(409,'GROUP_NAME_EXISTS','当日已有同名分组，请换一个名称。')
    group=DemandGroup(delivery_date=day,name=payload.name,updated_by=identity.account.id)
    db.add(group);db.flush();event(db,identity.account.id,'DEMAND_GROUP_CREATED',group.id,f'创建{day}需求分组 {group.name}')
    db.commit();return group_view(db,group)


@router.delete('/groups/{group_id}')
def remove_group(group_id:str,expected_revision:int=Query(ge=0),identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    group=get_group(db,group_id)
    if group.revision!=expected_revision:raise ApiError(409,'VERSION_CONFLICT','分组需求已有更新，请刷新后再删除。')
    result=delete_group(db,group,identity.account.id);db.commit();return result


@router.get('/groups/{group_id}/catalog')
def get_group_catalog(group_id:str,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    group=get_group(db,group_id);return group_catalog(db,group.delivery_date,group_id)


def save_arrangement(db,group_id,kind,entity_id,expected_revision,values):
    row=db.get(GroupArrangement,(group_id,kind,entity_id))
    if (row.revision if row else 0)!=expected_revision:raise ApiError(409,'VERSION_CONFLICT','分组安排已更新，请刷新后重试。')
    if row:row.payload=encode(values);row.revision+=1
    else:row=GroupArrangement(group_id=group_id,kind=kind,entity_id=entity_id,payload=encode(values));db.add(row)
    db.flush();return row


@router.put('/groups/{group_id}/warehouses/{entity_id}')
def group_warehouse(group_id:str,entity_id:str,payload:WarehouseDayInput,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    from app.models import Warehouse
    get_group(db,group_id)
    if not db.get(Warehouse,entity_id):raise ApiError(404,'WAREHOUSE_NOT_FOUND','仓库不存在。')
    row=save_arrangement(db,group_id,'warehouses',entity_id,payload.expected_revision,{'departure_time':payload.departure_time})
    event(db,identity.account.id,'GROUP_RESOURCE_UPDATED',group_id,'更新分组仓库发车时间');db.commit();return {'day_revision':row.revision}


@router.put('/groups/{group_id}/vehicles/{entity_id}')
def group_vehicle(group_id:str,entity_id:str,payload:VehicleDayInput,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    from app.models import Vehicle,Warehouse
    get_group(db,group_id)
    if not db.get(Vehicle,entity_id) or not db.get(Warehouse,payload.warehouse_id):raise ApiError(404,'RESOURCE_NOT_FOUND','车辆或仓库不存在。')
    row=save_arrangement(db,group_id,'vehicles',entity_id,payload.expected_revision,{'warehouse_id':payload.warehouse_id,'available':payload.available})
    event(db,identity.account.id,'GROUP_RESOURCE_UPDATED',group_id,'更新分组车辆安排');db.commit();return {'day_revision':row.revision}


@router.put('/groups/{group_id}/demands')
def manual_demands(group_id:str,payload:ManualDemands,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    group=get_group(db,group_id)
    if group.revision!=payload.expected_revision:raise ApiError(409,'VERSION_CONFLICT','需求已有更新，请刷新后重新编辑。')
    ids=[r.store_id for r in payload.items]
    if len(ids)!=len(set(ids)):raise ApiError(422,'DUPLICATE_STORE','同一分组内每家门店只能创建一票需求。')
    config=configuration(db,group.delivery_date,group_id);stores={s['id']:s for s in config['data']['stores']}
    if any(i not in stores for i in ids):raise ApiError(422,'STORE_NOT_FOUND','门店不存在，请刷新资料。')
    from app.models import DailyDemand
    import json
    previous={r.store_id:r for r in db.scalars(select(DailyDemand).where(DailyDemand.group_id==group_id))}
    rows=[];tasks=[]
    for number,item in enumerate(payload.items,2):
        store=stores[item.store_id];source={'row':number,'pieces':item.pieces,'supplied_name':store['name'],'supplied_code':store['code'],'sheet_name':'人工创建','delivery_date':group.delivery_date}
        rows.append({'row':number,'values':[store['name'],item.pieces,store['code']],'errors':[]})
        prior=previous.get(store['id'])
        unchanged=prior is not None and prior.pieces==item.pieces
        tasks.append({'store_id':store['id'],'store_code':store['code'],'store_name':store['name'],
                      'warehouse_id':prior.warehouse_id if unchanged else store['warehouse_id'],
                      'pieces':item.pieces,'source_rows':json.loads(prior.source_rows) if unchanged else [source],
                      **({'import_id':prior.import_id} if unchanged else {})})
    document={'columns':[{'index':0,'label':'门店名称'},{'index':1,'label':'件数'},{'index':2,'label':'门店编号'}],'rows':rows,'blank_rows':0,'suggested_mapping':{'store_name':0,'pieces':1,'store_code':2},'source':{'sheet_name':'人工创建','header_row':1}}
    result={'can_commit':True,'after':totals(tasks,config['data'])}
    batch=DemandImport(delivery_date=group.delivery_date,group_id=group_id,file_name='人工需求',file_hash=__import__('hashlib').sha256(encode(document).encode()).hexdigest(),document=encode(document),payload=encode(tasks),mapping=encode(document['suggested_mapping']),result=encode(result),configuration_hash=config['hash'],expected_revision=group.revision,preview_revision=1,status='PREVIEW',previewed_at=now(),created_by=identity.account.id)
    db.add(batch);db.flush()
    receipt=commit(db,batch,CommitDemand(delivery_date=date.fromisoformat(group.delivery_date),preview_revision=1),identity.account.id,identity.account.display_name)
    db.commit();return receipt
