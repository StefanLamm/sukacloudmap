"""Read-only extraction for repeatable local QA. Never edits source workbooks.

Only dated registration rows are business facts; helper sheets and formula tails
are excluded explicitly. Income is preserved separately and is never a cost quote.
"""
from collections import Counter,defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from openpyxl import load_workbook

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_SOURCE=ROOT.parent/'物流分析表'


def registration_files(folder=DEFAULT_SOURCE):
    return sorted([* (folder/'2026年1-9月物流分析/运费登记表').glob('*月份运费登记表.xlsx'),folder/'10月份运费登记表.xlsx'])


def extract(folder=DEFAULT_SOURCE):
    records=[];sources=[];issues=[];province=defaultdict(set);store_cities=defaultdict(set)
    for path in registration_files(folder):
        if not path.is_file():continue
        wb=load_workbook(path,read_only=True,data_only=True)
        if '日期' in wb.sheetnames:
            for r in wb['日期'].iter_rows(min_row=2,max_col=7,values_only=True):
                if r[3] and r[4]:province[str(r[4]).strip()].add(str(r[3]).strip())
                if r[6] and r[4]:store_cities[str(r[6]).strip()].add(str(r[4]).strip())
        ws=wb['登记表'] if '登记表' in wb.sheetnames else wb.worksheets[0]
        header=next(ws.iter_rows(min_row=2,max_row=2,max_col=20,values_only=True))
        labels=[re.sub(r'\s+','',str(v or '')) for v in header]
        def column(prefix,required=True):
            found=[i for i,label in enumerate(labels) if label.startswith(prefix) or prefix=='运输车辆' and label=='车辆信息']
            if len(found)==1:return found[0]
            if required:raise ValueError(f'{path.name} column missing or ambiguous: {prefix}')
        idx={k:column(prefix,k!='city') for k,prefix in [('date','配送日期'),('plate','运输车辆'),('line','线路名称'),('city','城市'),('store','下货门店'),('pieces','配送货物'),('income','物流费单件收入'),('mode','装车情况'),('cost','该线路合计支出')]}
        dated=0;pieces=0;tails=0
        for row_no,r in enumerate(ws.iter_rows(min_row=3,max_col=max(i for i in idx.values() if i is not None)+1,values_only=True),3):
            def cell(k):return r[idx[k]] if idx[k] is not None else None
            if cell('date') is None:
                if any(v is not None for v in r):tails+=1
                continue
            if not isinstance(cell('date'),datetime):issues.append({'file':path.name,'row':row_no,'issue':'invalid_date'});continue
            qty=cell('pieces')
            if not isinstance(qty,(int,float)) or isinstance(qty,bool) or int(qty)!=qty or qty<0 or not cell('store'):
                issues.append({'file':path.name,'row':row_no,'issue':'invalid_pieces_or_store'});continue
            item={'source_file':str(path.relative_to(folder)),'source_sheet':ws.title,'source_row':row_no,'delivery_date':cell('date').date().isoformat(),
                  'plate':str(cell('plate') or '').strip().upper(),'line':str(cell('line') or '').strip(),'city':str(cell('city') or '').strip(),
                  'store_name':str(cell('store')).strip(),'pieces':int(qty),'income_unit':None if cell('income') is None else str(cell('income')),
                  'registered_cost':None if cell('cost') is None else str(cell('cost')),'historical_mode':str(cell('mode') or '').strip()}
            if item['city']:store_cities[item['store_name']].add(item['city'])
            records.append(item);dated+=1;pieces+=int(qty)
        wb.close();sources.append({'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'dated_rows':dated,'pieces':pieces,'excluded_formula_template_rows':tails})
    for r in records:
        if not r['city'] and len(store_cities[r['store_name']])==1:r['city']=next(iter(store_cities[r['store_name']]))
        candidates=province[r['city']];r['province']=next(iter(candidates)) if len(candidates)==1 else None
        r['warehouse_code']={'安徽省':'HF','江苏省':'HA'}.get(r['province'])
    per_day=defaultdict(list)
    for r in records:per_day[r['delivery_date']].append(r)
    report={'sources':sources,'business_rows':len(records),'pieces':sum(r['pieces'] for r in records),'days':len(per_day),'stores':len({r['store_name'] for r in records}),
            'province_rows':dict(Counter(r['province'] or 'UNKNOWN' for r in records)),'issues':issues,
            'out_of_scope_rows':sum(r['warehouse_code'] is None for r in records),
            'daily':[{'date':d,'rows':len(rs),'stores':len({r['store_name'] for r in rs}),'pieces':sum(r['pieces'] for r in rs),'warehouse_rows':dict(Counter(r['warehouse_code'] or 'UNASSIGNED' for r in rs))} for d,rs in sorted(per_day.items())],
            'user_config':{'warehouses':{'安徽省':{'code':'HF','name':'合肥仓','address':'合肥市东流路100号市政务中心'},'江苏省':{'code':'HA','name':'淮安仓','address':'淮安市翔宇南道1号'}},
                           'dedicated_per_km':'5.8','vehicle_max_pieces':400,'common_price':'NOT_YET_CONFIRMED',
                           'truck_reference_only':{'model':'CL5041XLC6GL 4.2米级冷藏车','length_m':'5.995','width_m':'2.35','height_m':'3.35','full_gross_t':'4.495','rated_load_t':'1.09','axles':2,'size':2,'cartype':0,'plate_color':'BLUE','verification':'TEST_REFERENCE_ONLY_NOT_ACTUAL_VEHICLE',
                           'source':'https://www.cnclzg.com/prodview183.html','axles_and_plate_color':'test assumption; verify each actual vehicle'}}}
    return records,report


if __name__=='__main__':
    records,report=extract()
    output=ROOT/'docs/hardening';output.mkdir(parents=True,exist_ok=True)
    (output/'source-analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ['sources','daily','user_config']},ensure_ascii=False))
