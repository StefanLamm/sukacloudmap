"""Group ownership, independent day overrides and transactional cascade deletion."""
from datetime import date
import json
from types import SimpleNamespace

from sqlalchemy import delete, func, or_, select
from app.api import ApiError, event
from app.master_service import catalog, receiving_deadline
from app.models import (DailyDemand, DemandDay, DemandGroup, DemandImport, GroupArrangement,
                        InputSnapshot, PlanningJob, PlanVersion, Store, now)


def get_group(db, group_id, day=None):
    group=db.get(DemandGroup,group_id)
    if not group or day is not None and group.delivery_date!=day:
        raise ApiError(404,'DEMAND_GROUP_NOT_FOUND','需求分组不存在或已删除，请重新选择。')
    return group


def group_view(db, group):
    demands=list(db.scalars(select(DailyDemand).where(DailyDemand.group_id==group.id)))
    return {'id':group.id,'name':group.name,'delivery_date':group.delivery_date,'revision':group.revision,
            'task_count':len(demands),'total_pieces':sum(r.pieces for r in demands),
            'snapshot_count':db.scalar(select(func.count()).select_from(InputSnapshot).where(InputSnapshot.group_id==group.id)),
            'plan_count':db.scalar(select(func.count()).select_from(PlanVersion).where(PlanVersion.group_id==group.id))}


def group_catalog(db, day, group_id=None, *, infer_regions=True):
    data=catalog(db,date.fromisoformat(day),infer_regions=infer_regions)
    if group_id is None:return data
    get_group(db,group_id,day)
    overrides={(r.kind,r.entity_id):r for r in db.scalars(select(GroupArrangement).where(GroupArrangement.group_id==group_id))}
    warehouses={r['id']:r for r in data['warehouses']}
    stores={r.id:r for r in db.scalars(select(Store))}
    for kind in ['warehouses','vehicles','stores']:
        for row in data[kind]:
            override=overrides.get((kind,row['id']));values=json.loads(override.payload) if override else {}
            row.update(day_revision=override.revision if override else 0,day_override=bool(override))
            if kind=='warehouses':row['day_departure_time']=values.get('departure_time',row['departure_time'])
            elif kind=='vehicles':
                row['day_warehouse_id']=values.get('warehouse_id',row['warehouse_id'])
                row['day_warehouse_name']=warehouses[row['day_warehouse_id']]['name']
                row['day_available']=values.get('available',row['available'])
            else:row.update(receiving_deadline(stores[row['id']],date.fromisoformat(day),SimpleNamespace(**values) if override else None))
    return data


def delete_group(db, group, actor_id):
    # The worker checks the job still exists before saving. Its next cancellation
    # check also sees missing jobs, preventing deleted group results reappearing.
    counts={}
    group.latest_import_id=None;db.flush()
    for model in [PlanVersion,PlanningJob,InputSnapshot,DailyDemand,GroupArrangement]:
        result=db.execute(delete(model).where(model.group_id==group.id));counts[model.__tablename__]=result.rowcount
    anchor=db.get(DemandDay,group.delivery_date)
    if anchor and anchor.latest_import_id:
        batch=db.get(DemandImport,anchor.latest_import_id)
        if batch and batch.group_id==group.id:
            replacement=db.scalar(select(DemandImport).where(DemandImport.delivery_date==group.delivery_date,
                or_(DemandImport.group_id!=group.id,DemandImport.group_id.is_(None))).limit(1))
            if replacement:anchor.latest_import_id=replacement.id;db.flush()
            else:db.delete(anchor);db.flush()
    result=db.execute(delete(DemandImport).where(DemandImport.group_id==group.id));counts['demand_imports']=result.rowcount
    event(db,actor_id,'DEMAND_GROUP_DELETED',group.id,f'删除{group.delivery_date}需求分组 {group.name}及组内需求、快照和方案')
    db.delete(group);db.flush()
    return {'deleted':True,'counts':counts}
