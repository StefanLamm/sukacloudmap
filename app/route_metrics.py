"""Fast proposals from measured road metrics, never final road evidence.

These compact estimates omit all geometry. A caller must materialize accepted
routes through evaluate_route and independently check the complete allocation.
Unknown edges return None so the normal road adapter retains responsibility.
"""
from datetime import datetime, timedelta
from app.routing_engine import RoadError, line_fee, regional_extra, problem


def evaluate_candidate(data, vehicle_id, ids, road, control=None):
    lookup=getattr(road,'known_segment',None)
    if not callable(lookup):return None
    errors=[]
    result={'vehicle_id':vehicle_id,'task_ids':list(ids),'meters':0,'seconds':0,'pieces':0,
            'fee':None,'errors':errors,'_estimated':True}
    vehicle=data['vehicles'].get(vehicle_id)
    if vehicle is None:
        errors.append(problem('UNKNOWN_VEHICLE','车辆不存在',vehicle_id=vehicle_id));return result
    if not ids:
        errors.append(problem('EMPTY_ROUTE','请移除空线路',vehicle_id=vehicle_id));return result
    tasks=data['tasks']
    if any(i not in tasks for i in ids):
        errors.append(problem('UNKNOWN_TASK','线路包含不存在的任务'));return result
    selected=[tasks[i] for i in ids];result['pieces']=sum(t['pieces'] for t in selected)
    if not vehicle['available']:errors.append(problem('VEHICLE_UNAVAILABLE','车辆当日不可用',vehicle_id=vehicle_id))
    if result['pieces']>vehicle['max_pieces']:errors.append(problem('CAPACITY','装载件数超过车辆容量',vehicle_id=vehicle_id))
    if any(t['warehouse_id']!=vehicle['warehouse_id'] for t in selected):errors.append(problem('WAREHOUSE','线路门店与车辆必须属于同一个出货仓'))
    if selected[0]['signature'] is None or len({t['signature'] for t in selected})!=1:errors.append(problem('QUOTE_INCOMPATIBLE','线路必须具有相同的完整计费参数'))
    if len(set(ids))!=len(ids):errors.append(problem('TASK_COVERAGE','线路重复配送同一门店'))
    if not errors:
        try:regional_extra(selected[0]['quote'],selected)
        except RoadError as exc:errors.append(problem(exc.code,exc.message))
    if errors:return result
    origin=data['warehouses'][vehicle['warehouse_id']]
    leave=datetime.fromisoformat(origin['earliest_departure_at'])
    result['departure_at']=leave.isoformat()
    for task in selected:
        if control:control()
        try:segment=lookup(origin,task)
        except (KeyError,ValueError,TypeError,RoadError):return None
        if not isinstance(segment,dict):return None
        meters,seconds=segment.get('meters'),segment.get('seconds')
        if any(isinstance(n,bool) or not isinstance(n,int) or n<0 for n in (meters,seconds)):return None
        try:
            arrival=leave+timedelta(seconds=seconds)
            start=max(arrival,datetime.fromisoformat(task['open_at']))
            leave=start+timedelta(minutes=task['unload_minutes'])
        except OverflowError:
            errors.append(problem('TIME_RANGE','行驶或卸货时长超出可表示的日期范围',task_id=task['id']));return result
        result['meters']+=meters;result['seconds']+=seconds
        if task.get('latest_arrival_at') and arrival>datetime.fromisoformat(task['latest_arrival_at']):errors.append(problem('LATEST_ARRIVAL','实际到店晚于最晚到店时间',task_id=task['id']))
        if start>datetime.fromisoformat(task['close_at']):errors.append(problem('BUSINESS_CLOSED','开始卸货时已经超过经营时间',task_id=task['id']))
        origin=task
    result['arrival_end_at']=leave.isoformat()
    if errors and getattr(road,'force_refresh',False):return None
    result['fee']=line_fee(selected[0]['quote'],result['meters'],len(ids),selected)
    return result
