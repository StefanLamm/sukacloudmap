from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from app.routing_engine import normalize


def export_xlsx(plan):
    """Export exactly the selected immutable version. All strings are literal text."""
    body=plan; result=body['result']; data=normalize(body['input'])
    book=Workbook(); book.remove(book.active)
    def sheet(name,headers,rows):
        ws=book.create_sheet(name); ws.append(headers)
        for row in rows:
            ws.append([ILLEGAL_CHARACTERS_RE.sub(lambda m:f'_x{ord(m.group()):04X}_',v) if isinstance(v,str) else v for v in row])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value,str): cell.data_type='s'
        for cell in ws[1]: cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='A54C17')
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(55,max(15,max(len(str(c.value or '')) for c in col)+2))
        return ws
    sheet('版本与汇总',['项目','值'],[
        ['配送日期',plan['delivery_date']],['方案版本',plan['version']],['方案ID',plan['id']],['状态',plan['status']],
        ['内容SHA256',plan['content_hash']],['输入SHA256',plan['input_hash']],['输入快照ID',plan['snapshot_id']],
        ['道路来源',result['road_source']],['可确认',str(result['confirmable'])],['输入已变化',str(plan['input_changed'])],
        ['总任务数',result['check']['task_count']],['总件数',result['check']['total_pieces']],['专车费用（元）',result['check']['dedicated_fee']],
        ['共配费用（元）',result['check']['common_fee']],['驾车参考费用（元）' if result['road_source']=='DRIVING' else '总费用（元）' if result.get('fees_complete',True) else '已核算金额（有未定费用）',result['total_fee']],['费用完整',str(result.get('fees_complete',True))],['未分配任务',len(result['unassigned'])],
        ['计算引擎',result['engine_version']],['停止原因',result.get('algorithm',{}).get('stop_reason','')],
        ['说明','DRIVING为普通驾车参考，未核验货车限行及计划发车时刻；FIXTURE为固定测试道路。草稿或校验失败的方案不能确认执行。金额为十进制文本，按所选版本冻结值导出。']])
    sheet('专车线路',['车辆ID','车牌','仓库ID','门店顺序','件数','米数','费用（元）','道路已核验'],[
        [r['vehicle_id'],r.get('plate',''),r.get('warehouse_id',''),' → '.join(data['tasks'][i]['code'] if i in data['tasks'] else i for i in r['task_ids']),r['pieces'],r['meters'],r['fee'],str(r['road_verified'])] for r in result['routes']])
    allocation=[]
    for r in result['routes']:
        for order,i in enumerate(r['task_ids'],1):
            t=data['tasks'].get(i,{})
            stop=next((s for s in r['stops'] if s['task_id']==i),{})
            allocation.append([i,t.get('code',''),t.get('name',''),t.get('pieces',0),'专车',r['vehicle_id'],order,stop.get('arrival_at',''),stop.get('unload_start_at',''),stop.get('departure_at',''),stop.get('wait_seconds','')])
    for mode,key in [('共配','common'),('未分配','unassigned')]:
        for row in result[key]:
            t=data['tasks'][row['task_id']];allocation.append([t['id'],t['code'],t['name'],t['pieces'],mode,'','','','','',''])
    sheet('逐店分配',['任务ID','门店编号','门店名称','件数','方式','车辆ID','顺序','预计到店','预计开始卸货','预计离店','等待秒数'],allocation)
    sheet('共配与费用',['任务ID','门店编号','门店名称','件数','每件价（元）','小计（元）','原因'],[
        [r['task_id'],data['tasks'][r['task_id']]['code'],data['tasks'][r['task_id']]['name'],r['pieces'],r['unit_price'],r['fee'],r['reason']] for r in result['common']])
    sheet('异常',['类型','任务或车辆','原因'],[[e['code'],e.get('task_id',e.get('vehicle_id','')),e['message']] for e in result['check']['issues']]+[['未分配',r['task_id'],r['reason']] for r in result['unassigned']])
    sheet('道路证据',['车辆ID','起点','终点','发车时刻','米数','秒数','来源','限行状态','核验','证据SHA256'],[
        [r['vehicle_id'],s['request']['from_id'],s['request']['to_id'],s['request']['departure_at'],s['meters'],s['seconds'],s['source'],s['restriction'],str(s['verified']),s['evidence_hash']] for r in result['routes'] for s in r['segments']])
    output=BytesIO();book.save(output);return output.getvalue()
