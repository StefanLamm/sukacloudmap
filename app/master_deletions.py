"""Atomic, revision-checked master deletion; historical inputs stay immutable."""
import json
from sqlalchemy import delete, func, select

from app.api import ApiError, event
from app.master_service import MODELS, bump_master
from app.models import DailyDemand, GroupArrangement, LocationTask, QuoteVersion, Store, StoreDay, Vehicle, VehicleDay, WarehouseDay


def selected_entities(db, kind, items):
    by_id = {e.id: e for e in db.scalars(select(MODELS[kind]).where(MODELS[kind].id.in_([i.id for i in items])))}
    missing = [i.id for i in items if i.id not in by_id]
    if missing:
        raise ApiError(404, 'MASTER_NOT_FOUND', '部分资料已不存在，请刷新列表；本次未修改任何资料。')
    stale = [{'id': i.id, 'message': '资料已被更新，请刷新后重新选择'} for i in items if by_id[i.id].revision != i.expected_revision]
    if stale:
        raise ApiError(409, 'VERSION_CONFLICT', '所选资料已有变化，本次未修改任何资料。', stale)
    return [by_id[i.id] for i in items]


def delete_entities(db, kind, items, actor_id):
    entities = selected_entities(db, kind, items)
    ids = [e.id for e in entities]
    checks = {
        'warehouses': [(Store.warehouse_id, '关联门店'), (Vehicle.warehouse_id, '归属车辆'),
                       (VehicleDay.warehouse_id, '车辆当日安排'), (DailyDemand.warehouse_id, '配送需求')],
        'stores': [(DailyDemand.store_id, '配送需求')],
        'vehicles': [], 'quotes': [(Store.dedicated_quote_id, '引用门店')],
    }[kind]
    references = {i: [] for i in ids}
    for column, label in checks:
        for entity_id, count in db.execute(select(column, func.count()).where(column.in_(ids)).group_by(column)):
            references[entity_id].append(f'{label}{count}条')
    if kind == 'warehouses':
        group_counts = {entity_id: 0 for entity_id in ids}
        for arrangement in db.scalars(select(GroupArrangement).where(GroupArrangement.kind == 'vehicles')):
            warehouse_id = json.loads(arrangement.payload).get('warehouse_id')
            if warehouse_id in group_counts:
                group_counts[warehouse_id] += 1
        for entity_id, count in group_counts.items():
            if count:
                references[entity_id].append(f'分组车辆安排{count}条')
    blocked = [{'id': e.id, 'name': getattr(e, 'name', getattr(e, 'plate', '')),
                'message': '、'.join(references[e.id]) + '，请先解除关联后删除'} for e in entities if references[e.id]]
    if blocked:
        raise ApiError(409, 'MASTER_IN_USE', '有关联资料不能删除；本批次全部保留，请先处理列出的关联。', blocked)
    owned = {'warehouses': (WarehouseDay, WarehouseDay.warehouse_id),
             'stores': (StoreDay, StoreDay.store_id), 'vehicles': (VehicleDay, VehicleDay.vehicle_id),
             'quotes': (QuoteVersion, QuoteVersion.quote_id)}
    model, column = owned[kind]
    db.execute(delete(model).where(column.in_(ids)))
    db.execute(delete(LocationTask).where(LocationTask.kind == kind, LocationTask.entity_id.in_(ids)))
    db.execute(delete(GroupArrangement).where(GroupArrangement.kind == kind, GroupArrangement.entity_id.in_(ids)))
    for entity in entities:
        label = getattr(entity, 'code', getattr(entity, 'plate', ''))
        event(db, actor_id, 'MASTER_DELETED', entity.id, f'删除{kind}基础资料 {label}')
        db.delete(entity)
    db.flush()
    bump_master(db)
    return {'deleted': len(ids), 'ids': ids}
