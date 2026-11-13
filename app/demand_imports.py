"""Daily xlsx upload, explicit column mapping, preview and atomic replacement."""
from collections import defaultdict
from datetime import date, datetime, time
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.datetime import from_excel, CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
from sqlalchemy import delete, select

from app.api import ApiError, event, stamp
from app.master_imports import MAX_ROWS, read_sheet
from app.models import DailyDemand, DemandDay, DemandImport, now
from app.demand_groups import get_group
from app.planning_inputs import configuration, content_hash, demand_rows, encode, freeze, input_context, totals

ALIASES = {'store_name': {'门店名称', '门店', '门店名', '收货门店', '下货门店', 'store_name'},
           'store_code': {'门店编号', '门店编码', '店号', 'store_code'},
           'pieces': {'件数', '配送件数', '数量', '配送货物(件)', '配送货物（件）', '货物件数', 'pieces'},
           'delivery_date': {'配送日期', '送货日期', '日期', 'delivery_date'}}


def header_label(value):
    return re.sub(r'\s+|（下拉选择）|\(下拉选择\)', '', str(value or '')).lower()


def inspect_source(content):
    wb = read_sheet(content)
    try:
        if len(wb.sheetnames) > 30:
            raise ApiError(413, 'TOO_MANY_SHEETS', '工作表超过30张，请先保留所需数据。')
        sheets = []; suggested = None
        for ws in wb:
            ws.reset_dimensions(); samples = []
            for row in ws.iter_rows(max_row=10,max_col=30):
                values = [None if c.value is None else str(c.value)[:150] for c in row]
                samples.append({'row':len(samples)+1,'values':values})
                labels = {header_label(v) for v in values}
                if suggested is None and labels & ALIASES['store_name'] and labels & ALIASES['pieces']:
                    suggested = {'sheet_name':ws.title,'header_row':len(samples)}
            sheets.append({'name':ws.title,'sample_rows':samples})
        return {'sheets':sheets,'suggested':suggested or {'sheet_name':wb.sheetnames[0],'header_row':1}}
    finally:
        wb.close()


def demand_template():
    wb = Workbook(); ws = wb.active; ws.title = '数据'
    ws.append(['门店名称', '件数', '门店编号'])
    ws.freeze_panes = 'A2'; ws.auto_filter.ref = 'A1:C1'
    for col in range(1, 4):
        cell = ws.cell(1, col); cell.font = Font(name='Microsoft YaHei', bold=True, color='743B1D'); cell.fill = PatternFill('solid', fgColor='F3BF94')
        ws.column_dimensions[cell.column_letter].width = 28
    ws.column_dimensions['C'].number_format = '@'; ws.row_dimensions[1].height = 28
    guide = wb.create_sheet('说明'); guide.column_dimensions['A'].width = 105
    for text in ['配送日期和需求分组在物流线路规划页面选择；一次确认只替换当前分组全部仓库的需求，不受页面仓库筛选影响。',
                 '填写门店名称和非负整数件数，门店编号可选，编号必须是文本以保留前导零。',
                 '提供编号时优先按编号匹配；同时填写的名称必须一致，未知编号不回退为按名称猜测。',
                 '未提供编号时名称必须唯一；同名门店请补编号。名称与编号只去除首尾空白，编号统一大写。',
                 '同一门店重复行按件数求和，来源行保留；零件数保留在批次中但不建配送任务。',
                 '空行跳过；有内容但件数为空、负数、小数、未知或重名门店都会阻止整批提交。',
                 '先上传，再确认列匹配和预览，核对配送日期、各仓统计、重复汇总和替换前后差异，最后确认。',
                 '全部有效零行可以将当日需求替换为空；完全没有数据行的文件不能提交。',
                 '单行件数最多1000000000，单批最多10000数据行、5MiB；不接受公式、宏、外链或错误单元格。',
                 '确认后自动保留输入快照；资料或当天资源变化需重新预览。历史输入不会被后续改价、换仓或重传改写。']:
        guide.append([text]); guide.cell(guide.max_row, 1).alignment = Alignment(wrap_text=True, vertical='center'); guide.row_dimensions[guide.max_row].height = 36
    stream = BytesIO(); wb.save(stream); wb.close(); return stream.getvalue()


def parse_document(content, sheet_name=None, header_row=1):
    wb = read_sheet(content)
    try:
        if sheet_name is None and len(wb.sheetnames) > 1 and ('数据' not in wb.sheetnames or any(n not in {'数据', '说明'} for n in wb.sheetnames)):
            raise ApiError(422, 'SHEET_AMBIGUOUS', '请只保留数据和说明工作表，避免遗漏其他需求。')
        if sheet_name is not None and sheet_name not in wb.sheetnames:
            raise ApiError(422,'SHEET_NOT_FOUND','所选工作表不存在，请重新选择。')
        ws = wb[sheet_name] if sheet_name else wb['数据'] if '数据' in wb.sheetnames else wb.worksheets[0]
        ws.reset_dimensions(); iterator = ws.iter_rows(min_row=header_row); header = next(iterator, ())
        if not header or len(header) > 100:
            raise ApiError(422, 'DEMAND_HEADERS', '请提供最多100列的表头，或使用需求模板。')
        if any(c.data_type in {'f', 'e'} for c in header):
            raise ApiError(422, 'DEMAND_HEADERS', '表头不能包含公式或错误值。')
        if any(c.value is not None and not isinstance(c.value, str) for c in header):
            raise ApiError(422, 'DEMAND_HEADERS', '首行必须是文字列名，请补齐表头，避免把首条需求当作列名。')
        columns = [{'index': i, 'label': str(c.value).strip()[:100] if c.value is not None else f'第{i+1}列'} for i, c in enumerate(header)]
        rows, blank_count = [], 0
        for number, cells in enumerate(iterator, header_row+1):
            if number > 100000 or len(cells) > 100:
                raise ApiError(413, 'SPARSE_WORKSHEET', '工作表空行或列数过多，请重新整理。')
            if not any(c.value is not None and str(c.value).strip() for c in cells):
                blank_count += 1; continue
            if len(rows) >= MAX_ROWS:
                raise ApiError(413, 'TOO_MANY_ROWS', '同日整表最多10000个非空数据行，请先汇总重复门店后上传。')
            values, errors = [], []
            for i, cell in enumerate(cells):
                if cell.data_type in {'f', 'e'} or getattr(cell, 'hyperlink', None):
                    errors.append({'row': number, 'column':i, 'field': columns[i]['label'] if i < len(columns) else '未命名列', 'message': '不接受公式、错误值或外链单元格'})
                value = cell.value
                if isinstance(value, (datetime, date, time)):
                    value = value.isoformat()
                elif isinstance(value, float) and not math.isfinite(value):
                    value = str(value)
                values.append(value)
            if len(values) > len(columns):
                errors.append({'row': number, 'field': '表头', 'message': '存在超出表头范围的数据列'})
            rows.append({'row': number, 'values': values, 'errors': errors})
        suggested = {}
        for field, aliases in ALIASES.items():
            candidates = [c['index'] for c in columns if header_label(c['label']) in aliases]
            if field == 'delivery_date' and not candidates: continue
            suggested[field] = candidates[0] if len(candidates) == 1 else None
        return {'columns': columns, 'rows': rows, 'blank_rows': blank_count, 'suggested_mapping': suggested,
                'source':{'sheet_name':ws.title,'header_row':header_row,'ignored_sheets':[n for n in wb.sheetnames if n != ws.title],
                          'selected_columns_only':sheet_name is not None,'date_epoch': '1904' if wb.epoch == CALENDAR_MAC_1904 else '1900'}}
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, 'INVALID_XLSX', '需求文件读取失败，请使用模板重新保存。')
    finally:
        wb.close()


def upload(db, day, filename, content, actor_id, document=None, group_id=None):
    if group_id: get_group(db,group_id,day)
    document = document if document is not None else parse_document(content)
    batch = DemandImport(delivery_date=day, group_id=group_id, file_name=Path(filename).name[:200], file_hash=hashlib.sha256(content).hexdigest(),
                         document=encode(document), created_by=actor_id)
    db.add(batch); db.flush()
    return import_view(batch)


def import_view(batch):
    document = json.loads(batch.document)
    return {'id': batch.id, 'group_id':batch.group_id, 'delivery_date': batch.delivery_date, 'file_name': batch.file_name, 'file_hash': batch.file_hash,
            'status': batch.status, 'preview_revision': batch.preview_revision, 'columns': document['columns'],
            'source_row_count': len(document['rows']), 'sample_rows': document['rows'][:10], 'suggested_mapping': document['suggested_mapping'],
            'mapping': json.loads(batch.mapping) if batch.mapping else None, 'result': json.loads(batch.result) if batch.result else None,
            'source':document.get('source'),
            'created_at': stamp(batch.created_at)}


def pieces_value(value):
    if isinstance(value, bool):
        raise ValueError('件数必须为非负整数，不能填写是/否')
    if isinstance(value, int) or isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r'[+-]?\d+', value.strip()):
        digits = value.strip()
        sign = '-' if digits.startswith('-') else ''
        normalized = digits.lstrip('+-').lstrip('0') or '0'
        if len(normalized) > 10:
            raise ValueError('单行件数需在0至1000000000之间')
        result = int(sign + normalized)
    else:
        raise ValueError('件数必须填写非负整数，不能留空或填写小数')
    if not 0 <= result <= 1_000_000_000:
        raise ValueError('单行件数需在0至1000000000之间')
    return result


def delivery_date_value(value, epoch='1900'):
    if isinstance(value, bool) or value is None:
        raise ValueError('配送日期为空或格式无效')
    try:
        if isinstance(value,(int,float)):
            value = from_excel(value, CALENDAR_MAC_1904 if epoch == '1904' else CALENDAR_WINDOWS_1900)
        if isinstance(value,datetime):return value.date()
        if isinstance(value,date):return value
        if isinstance(value,str):
            value=value.strip()
            if re.fullmatch(r'\d{4}/\d{1,2}/\d{1,2}',value):return date(*map(int,value.split('/')))
            if len(value)==10:return date.fromisoformat(value)
            parsed=datetime.fromisoformat(value)
            from app.master_schemas import BEIJING
            return parsed.astimezone(BEIJING).date() if parsed.tzinfo else parsed.date()
    except (ValueError,TypeError,OverflowError):
        pass
    raise ValueError('配送日期需为Excel日期或明确的年-月-日，不自动猜测月日顺序')


def preview(db, batch, mapping):
    if batch.status == 'COMMITTED':
        raise ApiError(409, 'IMPORT_ALREADY_COMMITTED', '此批次已提交，请重新上传新文件。')
    if batch.created_at < now() - 86400:
        raise ApiError(409, 'PREVIEW_EXPIRED', '上传已超过24小时，请重新上传。')
    document = json.loads(batch.document)
    indices = [i for i in mapping.values() if i is not None]
    if len(set(indices)) != len(indices) or any(i >= len(document['columns']) for i in indices):
        raise ApiError(422, 'COLUMN_MAPPING', '列不能重复映射，且必须在上传文件范围内。')
    if any(header_label(c['label']) in ALIASES['delivery_date'] for c in document['columns']) and mapping.get('delivery_date') is None:
        raise ApiError(422,'DATE_MAPPING_REQUIRED','检测到配送日期列，请匹配该列，按页面所选日期筛选，防止将整月需求混入一天。')
    config = configuration(db, batch.delivery_date, batch.group_id); data = config['data']
    by_code = {s['code']: s for s in data['stores']}; by_name = defaultdict(list)
    warehouse_map = {w['id']: w for w in data['warehouses']}
    for store in data['stores']:
        by_name[store['name']].append(store)
    aggregated, zero_rows, errors = {}, [], []
    other_date_rows = ignored_nonbusiness_rows = selected_rows = 0
    for row in document['rows']:
        values = row['values']; number = row['row']
        mapped_errors = [e for e in row['errors'] if 'column' not in e or e['column'] in indices]
        row_errors = mapped_errors if document.get('source',{}).get('selected_columns_only') else list(row['errors'])
        def cell(field):
            index = mapping.get(field)
            return values[index] if index is not None and index < len(values) else None
        row_day = None
        if mapping.get('delivery_date') is not None:
            erroneous = {e.get('column') for e in row['errors']}
            # Formatted tail rows can contain only formulas/errors. No business
            # identity/date is present, so they are reported as ignored template rows.
            if all(cell(field) in (None,'') or mapping.get(field) in erroneous for field in ['store_name','store_code','pieces','delivery_date']):
                ignored_nonbusiness_rows += 1; continue
            try:
                row_day = delivery_date_value(cell('delivery_date'),document.get('source',{}).get('date_epoch','1900')).isoformat()
                if row_day != batch.delivery_date:
                    other_date_rows += 1; continue
            except ValueError as exc:
                errors.append({'row':number,'field':'配送日期','message':str(exc)});continue
        selected_rows += 1
        supplied_name = str(cell('store_name')).strip() if cell('store_name') is not None else ''
        code = cell('store_code')
        if isinstance(code, str):
            code = code.strip()
        supplied_code = code.upper() if isinstance(code, str) else ''
        store = None
        if code is not None and code != '':
            if not isinstance(code, str):
                row_errors.append({'row': number, 'field': '门店编号', 'message': '编号必须按文本填写，以保留前导零'})
            else:
                store = by_code.get(supplied_code)
                if store is None:
                    row_errors.append({'row': number, 'field': '门店编号', 'message': '编号不存在，请先维护门店或修正编号'})
                elif supplied_name and supplied_name != store['name']:
                    row_errors.append({'row': number, 'field': '门店名称', 'message': '编号对应的门店名称与本行名称不一致'})
        else:
            matches = by_name.get(supplied_name, [])
            if len(matches) != 1:
                row_errors.append({'row': number, 'field': '门店名称', 'message': '名称对应多家门店，请填写门店编号' if len(matches) > 1 else '名称不存在或为空，请先维护门店或补编号'})
            else:
                store = matches[0]
        try:
            quantity = pieces_value(cell('pieces'))
        except ValueError as exc:
            quantity = None; row_errors.append({'row': number, 'field': '件数', 'message': str(exc)})
        errors.extend(row_errors)
        if row_errors:
            continue
        source = {'row': number, 'pieces': quantity, 'supplied_name': supplied_name, 'supplied_code': supplied_code,
                  'sheet_name':document.get('source',{}).get('sheet_name'), 'delivery_date':row_day or batch.delivery_date}
        if quantity == 0:
            zero_rows.append({**source, 'store_id': store['id'], 'store_code': store['code']}); continue
        if store['id'] not in aggregated:
            aggregated[store['id']] = {'store_id': store['id'], 'store_code': store['code'], 'store_name': store['name'],
                                        'warehouse_id': store['warehouse_id'], 'warehouse_name': warehouse_map[store['warehouse_id']]['name'],
                                        'pieces': 0, 'source_rows': [], 'configuration_warnings': store['dedicated_config_missing'] + ([] if store['common_available'] else ['未配置共配单价'])}
        aggregate = aggregated[store['id']]; aggregate['pieces'] += quantity; aggregate['source_rows'].append(source)
    if not document['rows']:
        errors.append({'row': 2, 'field': '整批', 'message': '文件没有数据行，不能替换当日需求'})
    elif mapping.get('delivery_date') is not None and selected_rows == 0:
        errors.append({'row':document.get('source',{}).get('header_row',1)+1,'field':'配送日期','message':'所选日期没有有效需求行，不能用其他日期或模板空行清空当日需求'})
    tasks = sorted(aggregated.values(), key=lambda r: r['store_code'])
    previous = demand_rows(db, batch.delivery_date, data, batch.group_id)
    previous_by_id = {r['store_id']: r for r in previous}; incoming_by_id = {r['store_id']: r for r in tasks}
    state = get_group(db,batch.group_id,batch.delivery_date) if batch.group_id else db.get(DemandDay, batch.delivery_date)
    result = {'can_commit': not errors, 'source_row_count': len(document['rows']), 'blank_rows': document['blank_rows'],
              'source':document.get('source'),
              'error_count': len(errors), 'errors': errors[:1000], 'zero_row_count': len(zero_rows), 'zero_rows': zero_rows,
              'selected_date_row_count':selected_rows,'other_date_row_count':other_date_rows,'ignored_nonbusiness_row_count':ignored_nonbusiness_rows,
              'merged_row_count': sum(len(r['source_rows']) - 1 for r in tasks), 'before': totals(previous, data), 'after': totals(tasks, data),
              'changes': {'added': sum(sid not in previous_by_id for sid in incoming_by_id),
                          'removed': sum(sid not in incoming_by_id for sid in previous_by_id),
                          'updated': sum(sid in previous_by_id and (r['pieces'], r['warehouse_id']) != (previous_by_id[sid]['pieces'], previous_by_id[sid]['warehouse_id']) for sid, r in incoming_by_id.items())},
              'configuration_warning_count': sum(bool(r['configuration_warnings']) for r in tasks), 'items': tasks}
    batch.mapping, batch.payload, batch.result = encode(mapping), encode(tasks), encode(result)
    batch.configuration_hash = config['hash']; batch.expected_revision = state.revision if state else 0
    batch.preview_revision += 1; batch.previewed_at = now(); batch.status = 'PREVIEW'
    db.flush(); return import_view(batch)


def commit(db, batch, request, actor_id, actor_name):
    if request.delivery_date.isoformat() != batch.delivery_date or request.preview_revision != batch.preview_revision:
        raise ApiError(409, 'PREVIEW_MISMATCH', '配送日期或预览版本不一致，请重新核对预览。')
    if batch.status == 'COMMITTED':
        return json.loads(batch.result)['receipt']
    if batch.status != 'PREVIEW' or not batch.result:
        raise ApiError(422, 'PREVIEW_REQUIRED', '请先完成列匹配和数据预览。')
    result = json.loads(batch.result)
    if not result['can_commit']:
        raise ApiError(422, 'DEMAND_HAS_ERRORS', '存在错误行，整批未提交。', result['errors'])
    if batch.previewed_at < now() - 86400 or batch.created_at < now() - 86400:
        raise ApiError(409, 'PREVIEW_EXPIRED', '预览已过期，请重新上传。')
    config = configuration(db, batch.delivery_date, batch.group_id)
    state = get_group(db,batch.group_id,batch.delivery_date) if batch.group_id else db.get(DemandDay, batch.delivery_date)
    if (state.revision if state else 0) != batch.expected_revision or config['hash'] != batch.configuration_hash:
        raise ApiError(409, 'DEMAND_PREVIEW_STALE', '主资料、当天安排或需求已变化，整批未提交，请重新预览。')
    tasks = json.loads(batch.payload)
    if batch.group_id and db.get(DemandDay,batch.delivery_date) is None:
        db.add(DemandDay(delivery_date=batch.delivery_date,latest_import_id=batch.id,updated_by=actor_id));db.flush()
    if state is None:
        state = DemandDay(delivery_date=batch.delivery_date, latest_import_id=batch.id, updated_by=actor_id)
        db.add(state)
    else:
        state.revision += 1; state.latest_import_id = batch.id; state.updated_by = actor_id; state.updated_at = now()
    db.flush()
    db.execute(delete(DailyDemand).where(DailyDemand.delivery_date == batch.delivery_date, DailyDemand.group_id == batch.group_id))
    for task in tasks:
        db.add(DailyDemand(delivery_date=batch.delivery_date, group_id=batch.group_id, warehouse_id=task['warehouse_id'], store_id=task['store_id'], pieces=task['pieces'],
                           source_rows=encode(task['source_rows']), import_id=task.get('import_id',batch.id)))
    db.flush()
    snapshot = freeze(db, batch.delivery_date, actor_id, actor_name, input_context(db, batch.delivery_date, config, group_id=batch.group_id))
    receipt = {'import_id': batch.id, 'delivery_date': batch.delivery_date, 'revision': state.revision,
               'totals': result['after'], 'snapshot_id': snapshot.id, 'snapshot_version': snapshot.version}
    result['receipt'] = receipt; batch.result = encode(result); batch.status = 'COMMITTED'; batch.committed_at = now()
    event(db, actor_id, 'DAILY_DEMAND_REPLACED', batch.id, f'替换{batch.delivery_date}需求，{len(tasks)}票、{result["after"]["total_pieces"]}件')
    return receipt
