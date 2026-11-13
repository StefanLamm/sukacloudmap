from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api import ApiError, Identity, authenticated, database, event
from app.database import start_write
from app.master_imports import MAX_FILE, commit_import, preview_import, template
from app.master_schemas import CompatibilityInput, MasterBatchSelection, MasterSelection, StoreBulkUpdate, VehicleDayInput, WarehouseDayInput, today
from app.master_deletions import delete_entities, selected_entities
from app.location_tasks import enqueue_location
from app.master_service import MODELS, catalog, compatibility, management_catalog, quote_json, save_entity
from app.models import MasterImport, Quote, QuoteVersion, Vehicle, VehicleDay, Warehouse, WarehouseDay
from app.regions import region_tree

router = APIRouter(prefix="/api")
Kind = Literal["warehouses", "stores", "vehicles", "quotes"]


def find(db, kind, entity_id):
    entity = db.get(MODELS[kind], entity_id)
    if entity is None:
        raise ApiError(404, "MASTER_NOT_FOUND", "资料不存在，请刷新列表。")
    return entity


@router.get("/master-data/catalog")
def get_catalog(delivery_date: date | None = None, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return catalog(db, delivery_date)


@router.get("/master-data/management")
def get_management(identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return management_catalog(db)


@router.get('/master-data/regions')
def administrative_regions(request:Request, identity:Identity=Depends(authenticated)):
    return region_tree(request.app.state.settings,transport=getattr(request.app.state,'region_transport',None))


@router.post('/master-data/stores/bulk-update')
def bulk_update_stores(payload:StoreBulkUpdate,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    entities=selected_entities(db,'stores',payload.items)
    # Validate every merged record before changing any record. The dependency
    # also rolls the whole transaction back if a later save fails.
    from app.master_service import existing_values,normalize,references
    for entity in entities:
        values=normalize('stores',{**existing_values(db,'stores',entity),**payload.changes})
        references(db,'stores',values)
    changed=0
    for entity in entities:
        _,action=save_entity(db,'stores',payload.changes,identity.account.id,entity,entity.revision)
        changed+=action=='updated'
    event(db,identity.account.id,'STORES_BULK_UPDATED',None,f'批量更新{changed}家门店')
    db.commit()
    return {'updated':changed,'selected':len(entities)}


@router.post('/master-data/stores/export.xlsx')
def export_stores(payload:MasterBatchSelection,identity:Identity=Depends(authenticated),db:Session=Depends(database)):
    from io import BytesIO
    from openpyxl import load_workbook
    from app.master_imports import COLUMNS
    rows=selected_entities(db,'stores',payload.items)
    data=management_catalog(db);values={row['id']:row for row in data['stores']}
    warehouse_codes={row['id']:row['code'] for row in data['warehouses']};quote_codes={row['id']:row['code'] for row in data['quotes']}
    wb=load_workbook(BytesIO(template('stores')));ws=wb['数据']
    for entity in rows:
        row={**values[entity.id],'warehouse_code':warehouse_codes[entity.warehouse_id],'quote_code':quote_codes.get(entity.dedicated_quote_id)}
        for index,(field,_) in enumerate(COLUMNS['stores'],1):
            cell=ws.cell(ws.max_row if index>1 else ws.max_row+1,index,row.get(field))
            # Text cells must remain text even when user data begins with '='.
            if isinstance(row.get(field),str):cell.data_type='s'
    output=BytesIO();wb.save(output);wb.close()
    return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="stores-export.xlsx"'})


@router.post('/master-data/{kind}/bulk-delete')
def bulk_delete(kind: Kind, payload: MasterBatchSelection, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    result = delete_entities(db, kind, payload.items, identity.account.id)
    db.commit()
    return result


@router.post('/master-data/{kind}/locate')
def locate_missing(kind: Literal['warehouses', 'stores'], payload: MasterBatchSelection,
                   identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    entities = selected_entities(db, kind, payload.items)
    queued = sum(enqueue_location(db, kind, entity, identity.account.id) for entity in entities)
    db.commit()
    return {'queued': queued}


@router.delete('/{kind}/{entity_id}')
def delete_one(kind: Kind, entity_id: str, expected_revision: int = Query(ge=1),
               identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    result = delete_entities(db, kind, [MasterSelection(id=entity_id, expected_revision=expected_revision)], identity.account.id)
    db.commit()
    return result


@router.post("/master-data/compatibility")
def check_compatibility(payload: CompatibilityInput, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    return compatibility(db, payload.store_ids, payload.delivery_date)


@router.get("/master-data/templates/{kind}.xlsx")
def get_template(kind: Kind, identity: Identity = Depends(authenticated)):
    return Response(content=template(kind), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}-template.xlsx"'})


@router.post("/master-data/imports/{kind}/preview")
async def preview(kind: Kind, request: Request, filename: str = Query(min_length=1, max_length=200), identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    if not filename.lower().endswith(".xlsx"):
        raise ApiError(422, "XLSX_REQUIRED", "请上传xlsx格式文件。")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_FILE:
            raise ApiError(413, "FILE_TOO_LARGE", "文件不能超过5MiB，请拆分导入。")
    # Do not hold the shared writer while the client uploads. Re-authenticate
    # after acquiring the transaction in case the account was disabled meanwhile.
    start_write(db, request.app.state.engine)
    db.expunge_all()
    current_identity = authenticated(request, db)
    result = await run_in_threadpool(preview_import, db, kind, bytes(content), filename, current_identity.account.id)
    db.commit()
    return result


@router.get("/master-data/imports/{preview_id}")
def get_preview(preview_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    import json
    batch = db.get(MasterImport, preview_id)
    if batch is None:
        raise ApiError(404, "IMPORT_NOT_FOUND", "导入预览不存在。")
    return {"preview_id": batch.id, "kind": batch.kind, "status": batch.status, "master_revision": batch.master_revision, **json.loads(batch.result)}


@router.post("/master-data/imports/{preview_id}/commit")
def commit(preview_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    batch = db.get(MasterImport, preview_id)
    if batch is None:
        raise ApiError(404, "IMPORT_NOT_FOUND", "导入预览不存在，请重新上传。")
    result = commit_import(db, batch, identity.account.id)
    db.commit()
    return result


@router.get("/quotes/{quote_id}/versions")
def quote_versions(quote_id: str, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    find(db, "quotes", quote_id)
    return {"items": [quote_json(v) for v in db.scalars(select(QuoteVersion).where(QuoteVersion.quote_id == quote_id).order_by(QuoteVersion.version.desc()))]}


@router.put("/{kind}/{entity_id}/days/{delivery_date}")
def save_day(kind: Literal["warehouses", "vehicles"], entity_id: str, delivery_date: date, payload: dict,
             identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    find(db, kind, entity_id)
    try:
        values = (WarehouseDayInput if kind == "warehouses" else VehicleDayInput).model_validate(payload)
    except ValidationError as exc:
        raise ApiError(422, "MASTER_VALIDATION", "当日设置格式不正确。", [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()])
    model = WarehouseDay if kind == "warehouses" else VehicleDay
    entity = db.get(model, (entity_id, delivery_date.isoformat()))
    if (entity.revision if entity else 0) != values.expected_revision:
        raise ApiError(409, "VERSION_CONFLICT", "当日设置已更新，请刷新后重试。")
    if kind == "vehicles" and not db.get(Warehouse, values.warehouse_id):
        raise ApiError(422, "WAREHOUSE_NOT_FOUND", "所选出货仓不存在。")
    data = values.model_dump(exclude={"expected_revision"})
    if entity is None:
        entity = model(**{("warehouse_id" if kind == "warehouses" else "vehicle_id"): entity_id, "delivery_date": delivery_date.isoformat(), **data})
        db.add(entity)
    else:
        for key, value in data.items():
            setattr(entity, key, value)
        entity.revision += 1
    # Operational overrides have their own row revisions. They do not change
    # master records or invalidate a pending master-data import.
    event(db, identity.account.id, "DAY_SETTINGS_UPDATED", entity_id, f"更新{delivery_date.isoformat()}的{'仓库发车' if kind == 'warehouses' else '车辆归仓及可用'}设置")
    db.commit()
    return next(row for row in catalog(db, delivery_date)[kind] if row["id"] == entity_id)


@router.get("/{kind}")
def list_entities(kind: Kind, delivery_date: date | None = None, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    data = catalog(db, delivery_date)
    return {"items": data[kind], "master_revision": data["master_revision"], "delivery_date": data["delivery_date"]}


@router.get("/{kind}/{entity_id}")
def get_entity(kind: Kind, entity_id: str, delivery_date: date | None = None, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    find(db, kind, entity_id)
    return next(row for row in catalog(db, delivery_date)[kind] if row["id"] == entity_id)


@router.post("/{kind}", status_code=201)
def create_entity(kind: Kind, payload: dict, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    entity, action = save_entity(db, kind, payload, identity.account.id)
    db.commit()
    return next(row for row in catalog(db)[kind] if row["id"] == entity.id)


@router.patch("/{kind}/{entity_id}")
def update_entity(kind: Kind, entity_id: str, payload: dict, identity: Identity = Depends(authenticated), db: Session = Depends(database)):
    payload = dict(payload)
    revision = payload.pop("expected_revision", None)
    if type(revision) is not int or revision < 1:
        raise ApiError(422, "REVISION_REQUIRED", "请携带有效资料修订号后重试。")
    entity = find(db, kind, entity_id)
    entity, action = save_entity(db, kind, payload, identity.account.id, entity, revision)
    db.commit()
    return next(row for row in catalog(db)[kind] if row["id"] == entity.id)
