from datetime import date, datetime, time, timedelta
import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select

from app.api import ApiError, event, stamp, stale
from app.master_schemas import BEIJING, SCHEMAS, plain, today
from app.location_tasks import location_metadata
from app.regions import infer_region
from app.models import Quote, QuoteVersion, Store, SystemState, Vehicle, VehicleDay, Warehouse, WarehouseDay, StoreDay, now

MODELS = {"warehouses": Warehouse, "stores": Store, "vehicles": Vehicle, "quotes": Quote}
QUOTE_FIELDS = ["name", "mode", "per_km", "base_price", "included_stops", "extra_stop_price", "effective_from", "region_surcharge"]
PROFILE_FIELDS = {"size": "车型大小", "cartype": "车辆类型", "plate_color": "车牌颜色", "length_m": "车长", "width_m": "车宽", "height_m": "车高", "rated_load_t": "核定载重", "full_gross_t": "满载总重", "axles": "轴数"}


def normalize(kind, payload):
    try:
        return plain(SCHEMAS[kind].model_validate(payload).model_dump())
    except ValidationError as exc:
        messages = {"missing": "必填项缺失", "int_type": "必须填写整数，不能填写小数或是/否", "bool_type": "必须明确选择可用或不可用", "string_type": "必须填写文本",
                    "string_too_short": "不能为空或长度不足", "string_too_long": "文字长度超出限制", "literal_error": "请选择有效选项", "extra_forbidden": "不支持此字段",
                    "string_pattern_mismatch": "格式不正确，时间请使用HH:MM", "decimal_parsing": "请输入有效十进制数", "decimal_max_places": "最多支持6位小数（坐标最多8位）",
                    "decimal_max_digits": "数字位数超出允许精度", "decimal_whole_digits": "整数位数超出允许精度"}
        details = []
        for e in exc.errors():
            message = messages.get(e["type"], e["msg"].removeprefix("Value error, "))
            if e["type"] in {"greater_than", "greater_than_equal", "less_than", "less_than_equal"}:
                op = {"greater_than": "大于", "greater_than_equal": "大于或等于", "less_than": "小于", "less_than_equal": "小于或等于"}[e["type"]]
                message = "必须" + op + str(next(iter(e["ctx"].values())))
            details.append({"field": ".".join(map(str, e["loc"])) or "整行", "message": message, "type": e["type"]})
        raise ApiError(422, "MASTER_VALIDATION", "资料校验失败，请检查标出的字段。", details)


def signature(values):
    keys = ["mode", "per_km"] if values["mode"] in {"KM", "KM_REGION"} else ["mode", "base_price", "included_stops", "extra_stop_price"]
    body = {key: values[key] for key in keys}
    if values["mode"].endswith("_REGION"):
        rule=dict(values["region_surcharge"])
        if rule.get('billing_mode','ONCE')=='ONCE':rule.pop('billing_mode',None)
        body["region_surcharge"]=rule
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bump_master(db, *, for_import=True):
    state = db.get(SystemState, 1)
    state.master_revision += 1
    if for_import:
        state.import_revision += 1


def references(db, kind, values):
    if kind in {"stores", "vehicles"} and not db.get(Warehouse, values["warehouse_id"]):
        raise ApiError(422, "WAREHOUSE_NOT_FOUND", "所选出货仓不存在。", [{"field": "warehouse_id", "message": "请选择有效仓库"}])
    if kind == "stores" and values["dedicated_quote_id"] is not None and not db.get(Quote, values["dedicated_quote_id"]):
        raise ApiError(422, "QUOTE_NOT_FOUND", "所选专车报价不存在。", [{"field": "dedicated_quote_id", "message": "请选择有效报价"}])


def existing_values(db, kind, entity):
    if kind == "quotes":
        version = db.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == entity.id).order_by(QuoteVersion.version.desc()))
        return {"code": entity.code, **{k: json.loads(getattr(version,k) or "{}") if k=="region_surcharge" else getattr(version, k) for k in QUOTE_FIELDS}}
    return {key: getattr(entity, key) for key in SCHEMAS[kind].model_fields}


def save_entity(db, kind, payload, actor_id, entity=None, expected_revision=None, *, automatic_location=False):
    if entity is not None:
        stale(entity, expected_revision)
        before = existing_values(db, kind, entity)
        values = normalize(kind, {**before, **payload})
        if "code" in values and values["code"] != entity.code:
            raise ApiError(422, "STABLE_CODE", "已创建资料的编号不可修改，请使用原编号。")
    else:
        values = normalize(kind, payload)
    references(db, kind, values)
    key = "plate" if kind == "vehicles" else "code"
    duplicate = db.scalar(select(MODELS[kind]).where(getattr(MODELS[kind], key) == values[key]))
    if duplicate and (entity is None or duplicate.id != entity.id):
        raise ApiError(409, "MASTER_DUPLICATE", "车牌或资料编号已存在。", [{"field": key, "message": "必须唯一"}])
    if entity is not None and values == before:
        return entity, "unchanged"
    action = "created" if entity is None else "updated"
    if kind == "quotes":
        if entity is None:
            entity = Quote(code=values["code"], name=values["name"])
            db.add(entity)
            db.flush()
        else:
            entity.name = values["name"]
            entity.revision += 1
            entity.updated_at = now()
        db.add(QuoteVersion(quote_id=entity.id, version=entity.revision, signature=signature(values), created_by=actor_id,
                            **{k: json.dumps(values[k],ensure_ascii=False,sort_keys=True) if k=="region_surcharge" else values[k] for k in QUOTE_FIELDS}))
    else:
        if entity is None:
            entity = MODELS[kind](**values)
            db.add(entity)
        else:
            for key, value in values.items():
                setattr(entity, key, value)
            entity.revision += 1
            entity.updated_at = now()
    db.flush()
    bump_master(db, for_import=not automatic_location)
    event(db, actor_id, "MASTER_" + action.upper(), entity.id, f"{'新增' if action == 'created' else '更新'}基础资料 {values.get('code', values.get('plate'))}")
    return entity, action


def quote_json(version):
    return {"id": version.id, "quote_id": version.quote_id, "version": version.version, "signature": version.signature,
            **{k: json.loads(getattr(version,k) or "{}") if k=="region_surcharge" else getattr(version, k) for k in QUOTE_FIELDS}, "created_at": stamp(version.created_at)}


def management_catalog(db):
    """Master records independent of any delivery date or operational override."""
    data = {"master_revision": db.get(SystemState, 1).master_revision}
    for kind, model in MODELS.items():
        key = model.plate if kind == "vehicles" else model.code
        data[kind] = [{"id": row.id, "revision": row.revision,
                       **{field: getattr(row, field) for field in SCHEMAS[kind].model_fields if hasattr(row, field)}}
                      for row in db.scalars(select(model).order_by(key))]
    warehouses = {row["id"]: row for row in data["warehouses"]}
    latest = {}
    for version in db.scalars(select(QuoteVersion).order_by(QuoteVersion.version.desc())):
        latest.setdefault(version.quote_id, quote_json(version))
    for row in data["quotes"]:
        row["latest_version"] = latest[row["id"]]
        row["store_count"] = sum(s["dedicated_quote_id"] == row["id"] for s in data["stores"])
    quote_names = {row["id"]: row["name"] for row in data["quotes"]}
    for row in data["stores"]:
        row.update(infer_region(row))
        row["warehouse_name"] = warehouses[row["warehouse_id"]]["name"]
        row["quote_name"] = quote_names.get(row["dedicated_quote_id"])
    for row in data["vehicles"]:
        row["warehouse_name"] = warehouses[row["warehouse_id"]]["name"]
        row["road_profile_missing"] = [label for key, label in PROFILE_FIELDS.items() if row[key] is None]
        row["road_profile_complete"] = not row["road_profile_missing"]
    for row in data["warehouses"]:
        row["store_count"] = sum(s["warehouse_id"] == row["id"] for s in data["stores"])
    location_metadata(db, data)
    return data


def receiving_deadline(store, delivery_date, override=None):
    close_at = datetime.combine(delivery_date + timedelta(days=store.close_day_offset),
                                time.fromisoformat(store.business_close), BEIJING).isoformat()
    day = delivery_date.isoformat()
    legacy = store.latest_arrival_at if store.latest_arrival_at and day <= store.latest_arrival_at[:10] <= (delivery_date + timedelta(days=1)).isoformat() else None
    # An explicit clear restores the live closing-time default and suppresses a
    # legacy dated value. Do not persist a copied closing time as an override.
    custom = override.latest_arrival_at if override else legacy
    return {'business_close_at': close_at, 'latest_arrival_at': custom or close_at,
            'latest_arrival_source': ('DAY' if override else 'LEGACY') if custom else 'BUSINESS_CLOSE'}


def catalog(db, delivery_date: date | None = None):
    day = (delivery_date or today()).isoformat()
    warehouses = list(db.scalars(select(Warehouse).order_by(Warehouse.code)))
    stores = list(db.scalars(select(Store).order_by(Store.code)))
    vehicles = list(db.scalars(select(Vehicle).order_by(Vehicle.plate)))
    quotes = list(db.scalars(select(Quote).order_by(Quote.code)))
    versions = list(db.scalars(select(QuoteVersion).order_by(QuoteVersion.effective_from.desc(), QuoteVersion.version.desc())))
    effective, latest = {}, {}
    for version in versions:
        if version.quote_id not in latest or latest[version.quote_id].version < version.version:
            latest[version.quote_id] = version
        if version.effective_from <= day and version.quote_id not in effective:
            effective[version.quote_id] = version
    warehouse_map = {w.id: w for w in warehouses}
    warehouse_days = {w.warehouse_id: w for w in db.scalars(select(WarehouseDay).where(WarehouseDay.delivery_date == day))}
    vehicle_days = {v.vehicle_id: v for v in db.scalars(select(VehicleDay).where(VehicleDay.delivery_date == day))}

    def common(entity, kind):
        return {"id": entity.id, "revision": entity.revision, "created_at": stamp(entity.created_at), "updated_at": stamp(entity.updated_at),
                **{k: getattr(entity, k) for k in SCHEMAS[kind].model_fields if hasattr(entity, k)}}

    warehouse_rows = []
    for w in warehouses:
        row = common(w, "warehouses")
        override = warehouse_days.get(w.id)
        row.update(day_revision=override.revision if override else 0, day_departure_time=override.departure_time if override else w.departure_time,
                   store_count=sum(s.warehouse_id == w.id for s in stores), day_override=bool(override), location_ready=w.longitude is not None and w.crs == "GCJ02")
        warehouse_rows.append(row)
    store_days = {s.store_id:s for s in db.scalars(select(StoreDay).where(StoreDay.delivery_date == day))}
    store_rows = []
    for s in stores:
        row = common(s, "stores")
        row.update(infer_region(row))
        override = store_days.get(s.id)
        row.update(day_revision=override.revision if override else 0, day_override=bool(override),
                   **receiving_deadline(s, date.fromisoformat(day), override))
        q = effective.get(s.dedicated_quote_id)
        warehouse = warehouse_map[s.warehouse_id]
        missing = []
        if q is None:
            missing.append("未配置有效专车报价")
        if s.longitude is None:
            missing.append("门店未定位")
        elif s.crs != "GCJ02":
            missing.append("门店坐标待转换")
        if warehouse.longitude is None or warehouse.crs != "GCJ02":
            missing.append("仓库坐标未就绪")
        row.update(warehouse_name=warehouse.name, dedicated_quote_version=quote_json(q) if q else None,
                   dedicated_quote_available=q is not None, common_available=s.common_unit_price is not None,
                   dedicated_config_missing=missing, dedicated_config_ready=not missing, road_verified=False)
        store_rows.append(row)
    vehicle_rows = []
    for v in vehicles:
        row = common(v, "vehicles")
        override = vehicle_days.get(v.id)
        wid = override.warehouse_id if override else v.warehouse_id
        missing = [label for key, label in PROFILE_FIELDS.items() if getattr(v, key) is None]
        row.update(warehouse_name=warehouse_map[v.warehouse_id].name, day_warehouse_id=wid, day_warehouse_name=warehouse_map[wid].name,
                   day_available=override.available if override else v.available, day_revision=override.revision if override else 0,
                   day_override=bool(override), road_profile_missing=missing, road_profile_complete=not missing, road_verified=False)
        vehicle_rows.append(row)
    quote_rows = []
    for q in quotes:
        row = common(q, "quotes")
        row.update(latest_version=quote_json(latest[q.id]), effective_version=quote_json(effective[q.id]) if q.id in effective else None,
                   store_count=sum(s.dedicated_quote_id == q.id for s in stores))
        quote_rows.append(row)
    return {"delivery_date": day, "master_revision": db.get(SystemState, 1).master_revision,
            "warehouses": warehouse_rows, "stores": store_rows, "vehicles": vehicle_rows, "quotes": quote_rows}


def compatibility(db, store_ids, delivery_date):
    data = catalog(db, delivery_date)
    by_id = {s["id"]: s for s in data["stores"]}
    if any(sid not in by_id for sid in store_ids):
        raise ApiError(404, "STORE_NOT_FOUND", "所选门店不存在，请刷新资料。")
    selected = [by_id[sid] for sid in store_ids]
    reasons, groups = [], {}
    for s in selected:
        q = s["dedicated_quote_version"]
        if q is None:
            reasons.append(f"{s['code']} {s['name']} 缺少该日期有效的专车报价")
        else:
            key = s["warehouse_id"] + ":" + q["signature"]
            groups.setdefault(key, {"warehouse_id": s["warehouse_id"], "warehouse_name": s["warehouse_name"], "signature": q["signature"],
                                    "parameters": {k: q[k] for k in ["mode", "per_km", "base_price", "included_stops", "extra_stop_price"]}, "store_ids": []})["store_ids"].append(s["id"])
    if len({s["warehouse_id"] for s in selected}) > 1:
        reasons.append("出货仓不同，不能合为一条专车线路")
    signatures = {s["dedicated_quote_version"]["signature"] for s in selected if s["dedicated_quote_version"] is not None}
    if len(signatures) > 1:
        reasons.append("计费模式或完整价格参数不同，不能合线")
    return {"compatible": not reasons, "reasons": reasons, "groups": list(groups.values()), "delivery_date": data["delivery_date"],
            "notice": "此结果仅判断同仓与报价兼容；容量、时间和真实货车道路仍需规划时校验。"}
