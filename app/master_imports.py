"""xlsx 主资料模板、逐行预览与整批事务提交。"""
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile
import hashlib
import json
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from defusedxml.ElementTree import fromstring
from sqlalchemy import select

from app.api import ApiError, event
from app.master_schemas import BEIJING, today
from app.master_service import MODELS, existing_values, normalize, references, save_entity
from app.location_tasks import enqueue_location
from app.models import MasterImport, Quote, SystemState, Vehicle, Warehouse, now

MAX_FILE = 5 * 1024 * 1024
MAX_ROWS = 10000
LOCATION = [("address", "地址"), ("longitude", "经度"), ("latitude", "纬度"), ("crs", "坐标系"), ("location_source", "位置来源")]
COLUMNS = {
    "warehouses": [("code", "仓库编号"), ("name", "仓库名称"), *LOCATION, ("departure_time", "最早发车时间")],
    "stores": [("code", "门店编号"), ("name", "门店名称"), ("province", "省份"), ("city", "城市"), ("district", "区县"), ("region_code", "行政区编码"), *LOCATION, ("warehouse_code", "出货仓编号"),
               ("business_open", "营业开始"), ("business_close", "营业结束"), ("close_day_offset", "闭店跨日天数"), ("unload_minutes", "卸货分钟数"),
               ("quote_code", "专车报价编号"), ("common_unit_price", "共配每件单价")],
    "vehicles": [("plate", "车牌"), ("warehouse_code", "归属仓编号"), ("max_pieces", "最大件数"), ("available", "默认可用"),
                 ("size", "车型大小"), ("cartype", "车辆类型"), ("plate_color", "车牌颜色"), ("length_m", "车长米"), ("width_m", "车宽米"),
                 ("height_m", "车高米"), ("rated_load_t", "核定载重吨"), ("full_gross_t", "满载总重吨"), ("axles", "轴数")],
    "quotes": [("code", "报价编号"), ("name", "方案名称"), ("mode", "计费模式"), ("per_km", "每公里单价"), ("base_price", "起送价"),
               ("included_stops", "包含门店数"), ("extra_stop_price", "串店费"), ("region_surcharge", "特殊地区附加费规则JSON")],
}
REQUIRED = {
    "warehouses": {"code", "name", "address", "departure_time"},
    "stores": {"code", "name", "address", "warehouse_code", "business_open", "business_close", "close_day_offset", "unload_minutes"},
    "vehicles": {"plate", "warehouse_code", "max_pieces", "available"},
    "quotes": {"code", "name", "mode"},
}
INTEGER_FIELDS = {"max_pieces", "close_day_offset", "unload_minutes", "included_stops", "size", "cartype", "axles"}
COLORS = {"蓝牌": "BLUE", "黄牌": "YELLOW", "绿牌": "GREEN", "黄绿牌": "YELLOW_GREEN", "白牌": "WHITE", "黑牌": "BLACK", "其他": "OTHER"}
VEHICLE_SIZES = {'微型': 1, '轻型': 2, '中型': 3, '重型': 4}
VEHICLE_TYPES = {'普通货车': 0, '纯电动货车': 1, '插电混动货车': 2}


def template(kind):
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append([label for key, label in COLUMNS[kind]])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(1, len(COLUMNS[kind])).column_letter}1"
    for i, (key, label) in enumerate(COLUMNS[kind], 1):
        cell = ws.cell(1, i)
        cell.font = Font(name="Microsoft YaHei", color="743B1D", bold=True)
        cell.fill = PatternFill("solid", fgColor="F3BF94")
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = 24 if key in {"address", "latest_arrival_at"} else 18
        ws.column_dimensions[cell.column_letter].number_format = "@"
        options = {"crs": '"GCJ02,WGS84,BD09"', "mode": '"按公里,起送加串店,按公里加特殊地区,起送串店加特殊地区"', "available": '"是,否"',
                   'size': '"微型,轻型,中型,重型"', 'cartype': '"普通货车,纯电动货车,插电混动货车"',
                   "close_day_offset": '"0,1"', "plate_color": '"蓝牌,黄牌,绿牌,黄绿牌,白牌,黑牌,其他"'}.get(key)
        if options:
            validation = DataValidation(type="list", formula1=options, allow_blank=key not in REQUIRED[kind], showDropDown=False, errorStyle='stop')
            validation.errorTitle = "请按模板填写"; validation.error = "请选择列表中的值"; validation.showErrorMessage = True
            validation.showInputMessage = True; validation.promptTitle = label; validation.prompt = '请从下拉选项选择；旧版数字编码仍可导入。' if key in {'size', 'cartype'} else '请从下拉选项选择。'
            ws.add_data_validation(validation)
            validation.add(f"{cell.column_letter}2:{cell.column_letter}{MAX_ROWS + 1}")
    ws.row_dimensions[1].height = 28
    guide = wb.create_sheet("说明")
    instructions = [
        "仅填写数据工作表；第一行为固定表头，第二行开始填写。",
        "必填列：" + "、".join(label for key, label in COLUMNS[kind] if key in REQUIRED[kind]),
        "按编号（车辆按车牌）新增或整行更新；同文件重复编号/车牌报错。未出现在文件内的资料不会删除。",
        "更新时可选字段留空会清空原值；日期和时间、价格、编号建议按文本填写，编号必须为文本以保留前导零。",
        "先预览后确认，任一行有错则整批不能提交；预览后资料变化必须重新上传预览。",
        "坐标系只能填GCJ02、WGS84或BD09。经纬度成对填写；未定位或待转换不会冒充已验证道路。",
        "仓库/门店经纬度留空时，导入后会根据地址后台自动关联地图；地址不明确或服务不可用时标记需手动关联。明确提供的坐标保留。",
        "金额支持非负精确小数，最多18位有效数字、6位小数；空白为缺价，明确0为有效零价。",
        "时间格式HH:MM。跨日必须明确闭店跨日天数1；同日填0。最晚到店等业务安排在物流线路规划维护。",
        "出货仓编号、归属仓编号、专车报价编号必须已存在；先导入仓库与报价，再导入门店和车辆。",
        "报价模式可选按公里、起送加串店、按公里加特殊地区、起送串店加特殊地区。按公里填写公里单价；起送串店填写起送价、包含门店数和串店费。特殊地区模式另填写规则JSON，建议在界面通过省市区级联维护。管理界面不要求配送日期，报价修改保存新版本。",
        "车型大小和车辆类型可直接从下拉列表选择中文名称。兼容旧版数字：大小1微型/2轻型/3中型/4重型；类型0普通/1纯电/2插混。道路参数空白可保存但不能标为已验证货车候选。",
        "最大件数只限制件数；长度米、重量吨、轴数个分别维护。基础资料导入不改变已经单独设置的当日车辆安排。",
        "不接受公式或外链单元格。文件不超过5MiB，数据行不超过10000；这是导入批次的技术保护，非业务门店上限。",
    ]
    for text in instructions:
        guide.append([text])
    guide.column_dimensions["A"].width = 110
    for row in guide:
        row[0].font = Font(name="Microsoft YaHei", size=11)
        row[0].alignment = Alignment(wrap_text=True, vertical="center")
        guide.row_dimensions[row[0].row].height = 38
    output = BytesIO(); wb.save(output)
    return output.getvalue()


def read_sheet(content):
    try:
        with ZipFile(BytesIO(content)) as archive:
            files = archive.infolist()
            if len(files) > 2000 or sum(i.file_size for i in files) > 50 * 1024 * 1024:
                raise ApiError(413, "XLSX_TOO_LARGE", "工作簿解压后过大，请拆分文件。")
            if any("vbaproject" in i.filename.lower() or "externallinks/" in i.filename.lower() for i in files):
                raise ApiError(422, "XLSX_UNSUPPORTED", "请移除宏和外部工作簿链接后上传xlsx。")
            for item in files:
                if item.filename.endswith(".rels"):
                    if any(element.attrib.get("TargetMode") == "External" for element in fromstring(archive.read(item))):
                        raise ApiError(422, "XLSX_UNSUPPORTED", "请移除外部链接后上传xlsx。")
        wb = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "INVALID_XLSX", "无法读取xlsx文件，请使用模板重新保存。")
    return wb


def convert(key, value):
    if value is None or value == "":
        return None
    if key in {"code", "warehouse_code", "quote_code"}:
        if not isinstance(value, str):
            raise ValueError("编号必须按文本填写，以保留前导零")
        return value.strip().upper()
    if key in {'size', 'cartype'} and isinstance(value, str):
        choices = VEHICLE_SIZES if key == 'size' else VEHICLE_TYPES
        if value.strip() in choices:
            return choices[value.strip()]
        if not re.fullmatch(r'[+-]?\d+', value.strip()):
            raise ValueError('请选择下拉选项，或使用模板说明中的数字编码')
    if key in INTEGER_FIELDS:
        if isinstance(value, bool):
            raise ValueError("必须填写整数，不能填写是/否")
        if isinstance(value, int) or isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value)
        raise ValueError("必须填写整数")
    if key == "available":
        if value in [True, "是", "true", "TRUE", 1]:
            return True
        if value in [False, "否", "false", "FALSE", 0]:
            return False
        raise ValueError("可用性只能填是或否")
    if key in {"business_open", "business_close", "departure_time"} and isinstance(value, time):
        if value.second or value.microsecond:
            raise ValueError("时间精度为分钟")
        return value.strftime("%H:%M")
    if key == "latest_arrival_at" and isinstance(value, datetime):
        # Excel日期时间本身没有时区；此模板明确所有日期时间为北京时间。
        return value.replace(tzinfo=BEIJING).isoformat()
    if key == "effective_from" and isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if key == "mode":
        return {"按公里": "KM", "起送加串店": "BASE_STOPS", "按公里加特殊地区": "KM_REGION", "起送串店加特殊地区": "BASE_STOPS_REGION"}.get(str(value).strip(), str(value).strip())
    if key == "region_surcharge":
        try:return json.loads(str(value))
        except ValueError:raise ValueError("特殊地区规则需为有效JSON；建议在界面级联选择后维护")
    if key == "plate_color":
        return COLORS.get(str(value).strip(), str(value).strip())
    return str(value).strip() if not isinstance(value, bool) else value


def preview_import(db, kind, content, filename, actor_id):
    wb = read_sheet(content)
    errors, rows, seen = [], [], {}
    labels = dict(COLUMNS[kind])
    if kind == "quotes":
        labels["effective_from"] = "生效日期"
    elif kind == "stores":
        labels["latest_arrival_at"] = "最晚到店日期时间"
    header_keys = {label: key for key, label in COLUMNS[kind]}
    header_keys.update({key: key for key in labels})
    header_keys.update({label: key for key, label in labels.items()})
    existing = {getattr(e, "plate" if kind == "vehicles" else "code"): e for e in db.scalars(select(MODELS[kind]))}
    warehouses = {w.code: w.id for w in db.scalars(select(Warehouse))}
    quotes = {q.code: q.id for q in db.scalars(select(Quote))}
    try:
        if len(wb.sheetnames) > 1 and ("数据" not in wb.sheetnames or any(n not in {"数据", "说明"} for n in wb.sheetnames)):
            raise ApiError(422, "SHEET_AMBIGUOUS", "请只保留数据和说明工作表，避免遗漏其他工作表。")
        ws = wb["数据"] if "数据" in wb.sheetnames else wb.worksheets[0]
        # Do not trust a forged/stale worksheet dimension which could hide rows.
        ws.reset_dimensions()
        iterator = ws.iter_rows()
        header = next(iterator, ())
        if len(header) > 100:
            raise ApiError(422, "INVALID_HEADERS", "列数超出限制，请使用模板。")
        mapped = []
        for cell in header:
            text = str(cell.value).strip() if cell.value is not None else ""
            key = header_keys.get(text)
            if not text:
                mapped.append(None)
                continue
            if key is None or key in mapped or cell.data_type == "f":
                errors.append({"row": 1, "field": text[:80], "message": "表头未知、重复或包含公式"})
            mapped.append(key)
        missing = REQUIRED[kind] - set(mapped)
        for key in sorted(missing):
            errors.append({"row": 1, "field": labels[key], "message": "缺少必填列"})
        if errors:
            return store_preview(db, kind, filename, content, actor_id, [], errors, 0)
        count = 0
        for row_number, cells in enumerate(iterator, 2):
            if row_number > 100000 or len(cells) > 100:
                raise ApiError(413, "SPARSE_WORKSHEET", "工作表空行或列数过多，请使用模板重新整理。")
            if not any(cell.value is not None and str(cell.value).strip() for cell in cells):
                continue
            count += 1
            if count > MAX_ROWS:
                raise ApiError(413, "TOO_MANY_ROWS", "每批最多10000行，请拆分导入。")
            values, row_errors = {}, []
            for index, cell in enumerate(cells):
                key = mapped[index] if index < len(mapped) else None
                if cell.data_type in {"f", "e"} or getattr(cell, "hyperlink", None):
                    row_errors.append({"row": row_number, "field": labels.get(key, "单元格"), "message": "不接受公式、错误值或外链单元格"})
                elif key:
                    try:
                        values[key] = convert(key, cell.value)
                    except ValueError as exc:
                        row_errors.append({"row": row_number, "field": labels[key], "message": str(exc)})
                elif cell.value is not None and str(cell.value).strip():
                    row_errors.append({"row": row_number, "field": "未命名列", "message": "有数据的列必须使用模板表头"})
            for key in REQUIRED[kind]:
                if values.get(key) is None:
                    row_errors.append({"row": row_number, "field": labels[key], "message": "必填项为空"})
            # Optional empty columns use the schema default unless NULL is itself
            # meaningful (prices, coordinates, quote references and road fields).
            for key in ["province", "city", "district", "region_code", "crs", "location_source"]:
                if values.get(key) is None:
                    values.pop(key, None)
            if kind=="quotes" and values.get("region_surcharge") is None:values.pop("region_surcharge",None)
            if kind in {"warehouses", "stores"}:
                values.setdefault("location_source", "Excel导入")
            if kind in {"stores", "vehicles"}:
                code = values.pop("warehouse_code", None)
                values["warehouse_id"] = warehouses.get(code, "")
                if code and code not in warehouses:
                    row_errors.append({"row": row_number, "field": labels["warehouse_code"], "message": "仓库编号不存在，请先维护仓库"})
            if kind == "stores":
                prior = existing.get(values.get("code"))
                if prior:
                    for region_field in ["province","district","region_code"]:
                        if region_field not in mapped:values[region_field]=getattr(prior,region_field)
                if prior and "latest_arrival_at" not in mapped:
                    values["latest_arrival_at"] = prior.latest_arrival_at
                code = values.pop("quote_code", None)
                values["dedicated_quote_id"] = quotes.get(code) if code else None
                if code and code not in quotes:
                    row_errors.append({"row": row_number, "field": labels["quote_code"], "message": "报价编号不存在，请先维护报价"})
            if not row_errors:
                try:
                    values = normalize(kind, values)
                    references(db, kind, values)
                except ApiError as exc:
                    for detail in exc.details or [{"field": "整行", "message": exc.message}]:
                        row_errors.append({"row": row_number, "field": labels.get(detail["field"], detail["field"]), "message": detail["message"]})
            key_value = values.get("plate" if kind == "vehicles" else "code")
            if key_value:
                if key_value in seen:
                    row_errors.append({"row": row_number, "field": "车牌" if kind == "vehicles" else "编号", "message": f"与第{seen[key_value]}行重复，请先合并资料"})
                seen[key_value] = row_number
            if not row_errors:
                entity = existing.get(key_value)
                rows.append({"source_row": row_number, "values": values, "entity_id": entity.id if entity else None,
                             'previous_values': existing_values(db, kind, entity) if entity else None,
                             "expected_revision": entity.revision if entity else None, "action": "update" if entity else "create"})
            errors.extend(row_errors)
            if len(errors) > 1000:
                errors.append({"row": row_number, "field": "整批", "message": "错误过多，仅保留前1000项；请分批修正"})
                break
        if count == 0:
            errors.append({"row": 2, "field": "整批", "message": "文件没有可导入的资料"})
        return store_preview(db, kind, filename, content, actor_id, rows, errors, count)
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "INVALID_XLSX", "工作表读取失败，请使用模板重新保存。")
    finally:
        wb.close()


def store_preview(db, kind, filename, content, actor_id, rows, errors, count):
    summary = {"row_count": count, "valid_rows": len(rows), "error_count": len(errors),
               "create_count": sum(r["action"] == "create" for r in rows), "update_count": sum(r["action"] == "update" for r in rows),
               "errors": errors, "rows": [{"source_row": r["source_row"], "action": r["action"], "code": r["values"].get("code", r["values"].get("plate")),
                "name": r["values"].get("name", r["values"].get("plate"))} for r in rows[:100]], "can_commit": not errors and bool(rows)}
    batch = MasterImport(kind=kind, file_name=Path(filename).name[:200], file_hash=hashlib.sha256(content).hexdigest(),
                         master_revision=db.get(SystemState, 1).import_revision, payload=json.dumps(rows, ensure_ascii=False),
                         result=json.dumps(summary, ensure_ascii=False), created_by=actor_id)
    db.add(batch); db.flush()
    return {"preview_id": batch.id, "kind": kind, "master_revision": batch.master_revision, **summary}


def commit_import(db, batch, actor_id):
    summary = json.loads(batch.result)
    if batch.status == "COMMITTED":
        return {"preview_id": batch.id, "status": batch.status, "receipt": summary["receipt"], 'geocoding': summary.get('geocoding')}
    if not summary["can_commit"]:
        raise ApiError(422, "IMPORT_HAS_ERRORS", "存在错误行或文件为空，整批未提交。", summary["errors"])
    if batch.created_at < now() - 86400:
        raise ApiError(409, "PREVIEW_EXPIRED", "预览已超过24小时，请重新上传。")
    if db.get(SystemState, 1).import_revision != batch.master_revision:
        raise ApiError(409, "PREVIEW_STALE", "预览后基础资料已变化，请重新上传核对，整批未提交。")
    receipt = {"created": 0, "updated": 0, "unchanged": 0}
    queued = 0
    for row in json.loads(batch.payload):
        entity = db.get(MODELS[batch.kind], row["entity_id"]) if row["entity_id"] else None
        values, expected_revision = row['values'], row['expected_revision']
        if entity is not None and entity.revision != expected_revision and batch.kind in {'warehouses', 'stores'}:
            # User edits invalidate the import stamp. Address enrichment alone
            # may safely advance row revisions while the user reviews a file.
            location_fields = {'longitude', 'latitude', 'crs', 'location_source'}
            prior, current = row.get('previous_values'), existing_values(db, batch.kind, entity)
            if not prior or {k: v for k, v in current.items() if k not in location_fields} != {k: v for k, v in prior.items() if k not in location_fields}:
                raise ApiError(409, 'PREVIEW_STALE', '资料已变化，请重新上传核对，整批未提交。')
            if values['longitude'] is None and values['address'] == entity.address and values.get('city') == getattr(entity, 'city', None):
                values.update({k: current[k] for k in location_fields})
            expected_revision = entity.revision
        saved, action = save_entity(db, batch.kind, values, actor_id, entity, expected_revision)
        receipt[action] += 1
        queued += enqueue_location(db, batch.kind, saved, actor_id)
    batch.status, batch.committed_at = "COMMITTED", now()
    summary["receipt"] = receipt
    summary['geocoding'] = {'queued': queued}
    batch.result = json.dumps(summary, ensure_ascii=False)
    event(db, actor_id, "MASTER_IMPORTED", batch.id, f"批量导入{batch.kind}，新增{receipt['created']}、更新{receipt['updated']}、未变{receipt['unchanged']}")
    return {"preview_id": batch.id, "status": batch.status, "receipt": receipt, 'geocoding': summary['geocoding']}
