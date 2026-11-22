"""Current daily input and immutable snapshots. No routing or road verification."""
from datetime import date, datetime, time, timedelta
import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from app.api import ApiError, event, stamp
from app.master_schemas import BEIJING
from app.demand_groups import get_group, group_catalog
from app.models import DailyDemand, DemandDay, DemandImport, InputSnapshot
from app.regions import REGION_FIELDS

SCHEMA_VERSION = 1


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def configuration(db, day, group_id=None, *, infer_regions=True):
    data = group_catalog(db, day, group_id, infer_regions=infer_regions)
    source = {'master_revision': data['master_revision']}
    for kind in ['warehouses', 'vehicles', 'stores']:
        source[kind] = sorted([[row['id'], row['day_revision']] for row in data[kind]])
    return {'data': data, 'hash': content_hash(data), 'stamp': source}


def demand_rows(db, day, data, group_id=None):
    stores = {s['id']: s for s in data['stores']}
    warehouses = {w['id']: w for w in data['warehouses']}
    rows = []
    for demand in db.scalars(select(DailyDemand).where(DailyDemand.delivery_date == day, DailyDemand.group_id == group_id)):
        store = stores.get(demand.store_id)
        if store is None:
            raise ApiError(409, 'DEMAND_STORE_MISSING', '需求关联门店不存在，请检查资料后重新导入。')
        sources = json.loads(demand.source_rows)
        rows.append({'id': demand.id, 'store_id': store['id'], 'store_code': store['code'], 'store_name': store['name'],
                     'warehouse_id': store['warehouse_id'], 'warehouse_name': warehouses[store['warehouse_id']]['name'],
                     'import_warehouse_id': demand.warehouse_id, 'pieces': demand.pieces, 'source_rows': sources,
                     'import_id': demand.import_id, 'common_unit_price': store['common_unit_price'],
                     'dedicated_quote_available': store['dedicated_quote_available'], 'common_available': store['common_available'],
                     'configuration_warnings': store['dedicated_config_missing'] + ([] if store['common_available'] else ['未配置共配单价']),
                     'latest_arrival_at': store['latest_arrival_at'], 'latest_arrival_source': store['latest_arrival_source'],
                     'business_close_at': store['business_close_at'], 'day_revision': store['day_revision']})
    return sorted(rows, key=lambda r: r['store_code'])


def totals(rows, data):
    grouped = {w['id']: {'warehouse_id': w['id'], 'warehouse_code': w['code'], 'warehouse_name': w['name'], 'task_count': 0, 'total_pieces': 0} for w in data['warehouses']}
    for row in rows:
        item = grouped[row['warehouse_id']]
        item['task_count'] += 1
        item['total_pieces'] += row['pieces']
    return {'task_count': len(rows), 'total_pieces': sum(r['pieces'] for r in rows),
            'active_warehouse_count': sum(w['task_count'] > 0 for w in grouped.values()), 'warehouses': list(grouped.values())}


def input_context(db, day, config=None, group_id=None):
    config = config or configuration(db, day, group_id)
    state = get_group(db, group_id, day) if group_id else db.get(DemandDay, day)
    if state and state.latest_import_id is None: state = None
    if state and not group_id and db.get(DemandImport, state.latest_import_id).group_id is not None: state = None
    rows = demand_rows(db, day, config['data'], group_id)
    revision = state.revision if state else 0
    fingerprint = content_hash({'schema_version': SCHEMA_VERSION, 'delivery_date': day, 'configuration_hash': config['hash'],
                                'demand_revision': revision, 'demands': rows, **({'group_id': group_id} if group_id else {})})
    batch = db.scalar(select(DemandImport).options(load_only(DemandImport.id,DemandImport.file_name,DemandImport.file_hash,DemandImport.result,DemandImport.mapping)).where(DemandImport.id==state.latest_import_id)) if state else None
    result = json.loads(batch.result) if batch and batch.result else {}
    provenance = {'id': batch.id, 'file_name': batch.file_name, 'file_hash': batch.file_hash,
                  'source':result.get('source') if 'source' in result else json.loads(batch.document).get('source'), 'column_mapping':json.loads(batch.mapping) if batch.mapping else None} if batch else None
    return {'configuration': config, 'state': state, 'rows': rows, 'revision': revision, 'fingerprint': fingerprint, 'batch': provenance, 'group_id': group_id}


def frozen_body(day, context):
    data = context['configuration']['data']
    used_ids = {row['store_id'] for row in context['rows']}
    stores, warehouses = [], []
    delivery_date = date.fromisoformat(day)
    for row in data['stores']:
        if row['id'] not in used_ids:
            continue
        stores.append({**row,
                       'business_open_at': datetime.combine(delivery_date, time.fromisoformat(row['business_open']), BEIJING).isoformat(),
                       'business_close_at': datetime.combine(delivery_date + timedelta(days=row['close_day_offset']), time.fromisoformat(row['business_close']), BEIJING).isoformat()})
    for row in data['warehouses']:
        warehouses.append({**row, 'earliest_departure_at': datetime.combine(delivery_date, time.fromisoformat(row['day_departure_time']), BEIJING).isoformat()})
    quote_ids = {s['dedicated_quote_id'] for s in stores if s['dedicated_quote_id']}
    quotes = [q['effective_version'] for q in data['quotes'] if q['id'] in quote_ids and q['effective_version'] is not None]
    return {'schema_version': SCHEMA_VERSION, 'delivery_date': day, **({'group_id':context['group_id']} if context.get('group_id') else {}), 'timezone': 'Asia/Shanghai',
            'units': {'pieces': '件', 'money': '元', 'length': '米', 'weight': '吨', 'coordinates': 'explicit_crs'},
            'demand_revision': context['revision'], 'configuration_hash': context['configuration']['hash'],
            'source_stamp': context['configuration']['stamp'], 'demand_batch': context['batch'], 'totals': totals(context['rows'], data),
            'tasks': context['rows'], 'stores': stores, 'warehouses': warehouses, 'vehicles': data['vehicles'], 'quotes': quotes,
            'road_status': 'NOT_VERIFIED', 'task_policy': '每个配送日、门店形成一票，不拆分件数；仓库取冻结时的门店归属'}


def _plain_quotes(body):
    used = [store['dedicated_quote_version'] for store in body['stores']]
    return all(quote is None or (quote['mode'] in {'KM', 'BASE_STOPS'} and not quote.get('region_surcharge'))
               for quote in (*used, *body['quotes']))


def _without_cache_regions(body):
    projection = {key: value for key, value in body.items() if key != 'configuration_hash'}
    projection['stores'] = [{key: value for key, value in row.items() if key not in REGION_FIELDS}
                            for row in body['stores']]
    return projection


def snapshot_input_is_current(db, snapshot, context, *, body=None):
    """Match the frozen input, or prove harmless enrichment of a plain-price one."""
    if snapshot.input_fingerprint == context['fingerprint']:
        return True
    if (snapshot.group_id != context['group_id'] or snapshot.demand_revision != context['revision']
            or context['configuration']['data']['delivery_date'] != snapshot.delivery_date):
        return False
    body = json.loads(snapshot.content) if body is None else body
    if (content_hash(body) != snapshot.content_hash
            or body.get('source_stamp') != context['configuration']['stamp'] or not _plain_quotes(body)):
        return False
    current = frozen_body(snapshot.delivery_date, context)
    if not _plain_quotes(current) or _without_cache_regions(body) != _without_cache_regions(current):
        return False
    # Require the exact full persisted catalog. This also rejects changes to
    # unrelated rows and legacy snapshots with a different inferred tree.
    if '_persisted_region_context' not in context:
        raw = configuration(db, snapshot.delivery_date, snapshot.group_id, infer_regions=False)
        context['_persisted_region_context'] = input_context(db, snapshot.delivery_date, config=raw,
                                                              group_id=snapshot.group_id)
    persisted = context['_persisted_region_context']
    return (snapshot.input_fingerprint == persisted['fingerprint']
            and body == frozen_body(snapshot.delivery_date, persisted))


def changed_reasons(db, snapshot, context, body):
    if snapshot_input_is_current(db, snapshot, context, body=body):
        return []
    reasons = []
    if snapshot.demand_revision != context['revision']:
        reasons.append('当日配送需求已替换')
    old, current = body['source_stamp'], context['configuration']['stamp']
    if old['master_revision'] != current['master_revision']:
        reasons.append('基础资料或报价版本已变化')
    for kind, label in [('warehouses', '仓库发车安排'), ('vehicles', '车辆归仓或可用性'), ('stores', '门店当日收货限制')]:
        if old[kind] != current[kind]:
            reasons.append(label + '已变化')
    return reasons or ['当前输入与已冻结内容不一致']


def snapshot_meta(db, snapshot, context):
    body = json.loads(snapshot.content)
    reasons = changed_reasons(db, snapshot, context, body)
    return {'id': snapshot.id, 'group_id':snapshot.group_id, 'delivery_date': snapshot.delivery_date, 'version': snapshot.version,
            'demand_revision': snapshot.demand_revision, 'content_hash': snapshot.content_hash,
            'created_at': stamp(snapshot.created_at), 'created_by': snapshot.created_by_name,
            'totals': body['totals'], 'input_changed': bool(reasons), 'changed_reasons': reasons}


def freeze(db, day, actor_id, actor_name, context=None, group_id=None):
    context = context or input_context(db, day, group_id=group_id)
    group_id = context.get("group_id")
    if context['state'] is None:
        raise ApiError(422, 'DEMAND_NOT_IMPORTED', '请先导入并确认当日需求，再冻结规划输入。')
    latest = db.scalar(select(InputSnapshot).where(InputSnapshot.delivery_date == day, InputSnapshot.group_id == group_id).order_by(InputSnapshot.version.desc()).limit(1))
    if latest and snapshot_input_is_current(db, latest, context):
        return latest
    body = frozen_body(day, context)
    snapshot = InputSnapshot(delivery_date=day, group_id=group_id, version=(db.scalar(select(func.max(InputSnapshot.version)).where(InputSnapshot.delivery_date==day)) or 0)+1, demand_revision=context['revision'],
                             input_fingerprint=context['fingerprint'], content_hash=content_hash(body), content=encode(body),
                             created_by=actor_id, created_by_name=actor_name)
    db.add(snapshot); db.flush()
    event(db, actor_id, 'INPUT_SNAPSHOT_CREATED', snapshot.id, f'冻结{day}规划输入V{snapshot.version}，{len(context["rows"])}票')
    return snapshot


def day_state(db, day, group_id=None):
    context = input_context(db, day, group_id=group_id)
    latest = db.scalar(select(InputSnapshot).where(InputSnapshot.delivery_date == day, InputSnapshot.group_id == group_id).order_by(InputSnapshot.version.desc()).limit(1))
    count = db.scalar(select(func.count()).select_from(InputSnapshot).where(InputSnapshot.delivery_date == day, InputSnapshot.group_id == group_id))
    return {'delivery_date': day, 'group_id':group_id, 'revision': context['revision'], 'input_fingerprint': context['fingerprint'],
            'latest_import_id': context['state'].latest_import_id if context['state'] else None,
            'updated_at': stamp(context['state'].updated_at) if context['state'] else None,
            'items': context['rows'], 'totals': totals(context['rows'], context['configuration']['data']),
            'latest_snapshot': snapshot_meta(db, latest, context) if latest else None, 'snapshot_count': count}


def snapshot_detail(db, snapshot):
    body = json.loads(snapshot.content)
    if content_hash(body) != snapshot.content_hash:
        raise ApiError(500, 'SNAPSHOT_INTEGRITY', '快照完整性检查失败，请联系管理员。')
    return {**snapshot_meta(db, snapshot, input_context(db, snapshot.delivery_date, group_id=snapshot.group_id)), 'input': body}
