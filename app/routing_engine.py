"""Pure monetary/time evaluation and bounded joint allocation. No mutable database reads."""
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import permutations
import time

from app.planning_inputs import content_hash

CENT = Decimal('0.01')
ENGINE_VERSION = 'suka-joint-1'


def money(value):
    return format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def common_fee(pieces, price):
    return None if price is None else money(Decimal(pieces) * Decimal(price))


def regional_extra(quote, tasks):
    rule = quote.get('region_surcharge') if quote['mode'].endswith('_REGION') else None
    if not rule:
        return Decimal(0)
    billing_mode = rule.get('billing_mode', 'ONCE')
    per_region = billing_mode == 'PER_REGION'
    per_store = billing_mode == 'PER_STORE'
    charged = set()
    # Canonical aliases let legacy name-only stores share a district with a
    # fully coded stop, without merging equal district names in other cities.
    aliases = {}
    province_names = {target['province_code'][:2]:target['province_name'] for target in rule['targets']}
    def alias_key(row, code=''):
        province = row.get('province') or province_names.get(code[:2], '')
        city = row.get('city') or (province if code[:2] in {'11','12','31','50'} else '')
        district = row.get('district') or ''
        return (province, city.removesuffix('市'), district) if province and city and district else None
    for target in rule['targets']:
        if target.get('district_code'):
            key=(target['province_name'],target['city_name'].removesuffix('市'),target.get('district_name') or '')
            aliases.setdefault(key,set()).add(target['district_code'])
    for task in tasks:
        code=task.get('region_code') or ''
        if len(code)==6 and code.isdigit() and not code.endswith('00'):
            key=alias_key(task,code)
            if key:aliases.setdefault(key,set()).add(code)
    uncertain = not tasks
    for index, task in enumerate(tasks):
        code = task.get('region_code') or ''
        if not (len(code) == 6 and code.isdigit()): code = ''
        matched=[];possible=False
        for target in rule['targets']:
            province = task.get('province') or ''
            city = task.get('city') or ''
            district_name = task.get('district') or ''
            province_ok = code[:2] == target['province_code'][:2] if code else province == target['province_name'] if province else None
            district = target.get('district_code')
            city_code = target['city_code']
            city_ok = province_ok if city_code.endswith('0000') else code[:4] == city_code[:4] if code and not code.endswith('0000') else city.removesuffix('市') == target['city_name'].removesuffix('市') if city else None
            checks = [province_ok, city_ok]
            if district:
                checks.append(code == district if code and not code.endswith('00') else district_name == target.get('district_name') if district_name else None)
            if any(value is False for value in checks):
                continue
            if all(value is True for value in checks):
                if not per_region and not per_store:return Decimal(rule['amount'])
                matched.append(target)
            else:possible=True
        if matched:
            if per_store:
                # A store may match a whole city and several overlapping rules.
                # Charge the physical store once, never once per matching rule.
                identity = task.get('store_id') or task.get('id')
                charged.add(('store', identity) if identity else ('stop', index))
                continue
            # A whole-city target charges each actual county, rather than the
            # city checkbox itself. Resolve an explicit county target by names
            # when a legacy store has not stored its code yet.
            actual_code = code if code and not code.endswith('00') else ''
            exact_codes={t['district_code'] for t in matched if t.get('district_code')}
            if not actual_code and len(exact_codes)==1:actual_code=next(iter(exact_codes))
            if not actual_code:
                inferred=aliases.get(alias_key(task,code),set())
                if len(inferred)==1:actual_code=next(iter(inferred))
            if not actual_code:
                raise RoadError('REGION_MISSING', '按区县附加费需要明确的区县资料，请补齐门店区县行政区编码后重新冻结核算')
            charged.add(actual_code)
        elif possible:uncertain=True
    if uncertain:
        raise RoadError('REGION_MISSING', '门店省、市或区县资料不足，无法判断区域附加费，请完善行政区后重新冻结核算')
    return Decimal(rule['amount']) * len(charged) if per_region or per_store else Decimal(0)


def line_fee(quote, meters, stops, tasks=()):
    if not stops:
        return '0.00'
    if quote['mode'] in ['KM', 'KM_REGION']:
        base = Decimal(str(meters)) / 1000 * Decimal(quote['per_km'])
    else:
        base = Decimal(quote['base_price']) + max(stops - quote['included_stops'], 0) * Decimal(quote['extra_stop_price'])
    return money(base + regional_extra(quote, tasks))


def quote_signature(quote):
    keys = ['mode', 'per_km'] if quote['mode'] in ['KM', 'KM_REGION'] else ['mode', 'base_price', 'included_stops', 'extra_stop_price']
    parameters = {k: str(Decimal(str(quote[k])).normalize()) if k != 'mode' else quote[k] for k in keys}
    if quote['mode'].endswith('_REGION'):
        rule = quote.get('region_surcharge') or {}
        parameters['region_surcharge'] = {'amount': str(Decimal(rule.get('amount', '0')).normalize()),
            'targets': sorted(rule.get('targets', []), key=lambda r:(r['province_code'], r['city_code'],r.get('district_code') or ''))}
        if rule.get('billing_mode','ONCE') != 'ONCE':parameters['region_surcharge']['billing_mode']=rule['billing_mode']
    return content_hash(parameters)


def normalize(body):
    """Adapt a frozen S4 input, or an explicitly labelled repository fixture."""
    if 'stores' in body:
        stores = {s['id']: s for s in body['stores']}
        tasks = []
        for row in body['tasks']:
            s = stores[row['store_id']]
            tasks.append({**s, **row, 'id': row['store_id'], 'name': s['name'], 'code': s['code'],
                          'quote': s['dedicated_quote_version'], 'open_at': s['business_open_at'],
                          'close_at': s['business_close_at']})
        vehicles = [{**v, 'warehouse_id': v['day_warehouse_id'], 'available': v['day_available']} for v in body['vehicles']]
    else:
        quotes = body['quotes']
        tasks = [{**t, 'name': t.get('name', t['id']), 'code': t.get('code', t['id']),
                  'quote': quotes.get(t.get('quote_id')), 'open_at': t.get('open_at', t.get('business_open_at')),
                  'close_at': t.get('close_at', t.get('business_close_at'))} for t in body['tasks']]
        vehicles = body['vehicles']
    for t in tasks:
        t['signature'] = quote_signature(t['quote']) if t['quote'] else None
    return {'delivery_date': body['delivery_date'], 'source':body.get('source','FROZEN'), 'tasks': {t['id']: t for t in tasks},
            'vehicles': {v['id']: v for v in vehicles}, 'warehouses': {w['id']: w for w in body['warehouses']}}


def problem(code, message, **refs):
    return {'code': code, 'message': message, **refs}


def evaluate_route(data, vehicle_id, ids, road, control=None):
    tasks, vehicles = data['tasks'], data['vehicles']
    errors, stops, segments = [], [], []
    route = {'vehicle_id': vehicle_id, 'task_ids': ids, 'stops': stops, 'segments': segments, 'errors': errors,
             'meters': 0, 'seconds': 0, 'pieces': 0, 'fee': None, 'road_verified': False}
    v = vehicles.get(vehicle_id)
    if v is None:
        errors.append(problem('UNKNOWN_VEHICLE', '车辆不存在', vehicle_id=vehicle_id)); return route
    route['plate'] = v.get('plate', vehicle_id); route['warehouse_id'] = v['warehouse_id']
    if not ids:
        errors.append(problem('EMPTY_ROUTE', '请移除空线路', vehicle_id=vehicle_id)); return route
    if not v['available']:
        errors.append(problem('VEHICLE_UNAVAILABLE', '车辆当日不可用', vehicle_id=vehicle_id))
    if any(i not in tasks for i in ids):
        errors.append(problem('UNKNOWN_TASK', '线路包含不存在的任务')); return route
    selected = [tasks[i] for i in ids]
    route['pieces'] = sum(t['pieces'] for t in selected)
    if route['pieces'] > v['max_pieces']:
        errors.append(problem('CAPACITY', '装载件数超过车辆容量', vehicle_id=vehicle_id))
    if any(t['warehouse_id'] != v['warehouse_id'] for t in selected):
        errors.append(problem('WAREHOUSE', '线路门店与车辆必须属于同一个出货仓'))
    if selected[0]['signature'] is None or len({t['signature'] for t in selected}) != 1:
        errors.append(problem('QUOTE_INCOMPATIBLE', '线路必须具有相同的完整计费参数'))
    if not errors:
        try:
            regional_extra(selected[0]['quote'], selected)
        except RoadError as exc:
            errors.append(problem(exc.code, exc.message))
    if errors:
        return route
    current = data['warehouses'][v['warehouse_id']]
    leave = datetime.fromisoformat(current['earliest_departure_at'])
    route['departure_at'] = leave.isoformat()
    for t in selected:
        if control: control()
        try:
            segment = road.segment(v, current, t, leave.isoformat())
        except RoadError as exc:
            errors.append(problem(exc.code, exc.message, task_id=t['id'])); break
        segments.append(segment)
        if segment['source'] == 'LIVE' and not segment['verified']:
            errors.append(problem('ROAD_TIME_UNVERIFIED', '尚未验证指定发车时间与完整货车约束', task_id=t['id']))
        try:
            arrival = leave + timedelta(seconds=segment['seconds'])
            opens, closes = datetime.fromisoformat(t['open_at']), datetime.fromisoformat(t['close_at'])
            start = max(arrival, opens)
            leave = start + timedelta(minutes=t['unload_minutes'])
        except OverflowError:
            errors.append(problem('TIME_RANGE', '行驶或卸货时长超出可表示的日期范围', task_id=t['id'])); break
        if t.get('latest_arrival_at') and arrival > datetime.fromisoformat(t['latest_arrival_at']):
            errors.append(problem('LATEST_ARRIVAL', '实际到店晚于最晚到店时间', task_id=t['id']))
        if start > closes:
            errors.append(problem('BUSINESS_CLOSED', '开始卸货时已经超过经营时间', task_id=t['id']))
        stops.append({'task_id': t['id'], 'arrival_at': arrival.isoformat(), 'unload_start_at': start.isoformat(),
                      'departure_at': leave.isoformat(), 'wait_seconds': int((start - arrival).total_seconds()),
                      'unload_minutes': t['unload_minutes'], 'pieces': t['pieces'], 'segment_meters': segment['meters'],
                      'segment_seconds': segment['seconds']})
        route['meters'] += segment['meters']; route['seconds'] += segment['seconds']
        current = t
    if len(segments) == len(ids) and len(stops) == len(ids):
        route['fee'] = line_fee(selected[0]['quote'], route['meters'], len(ids), selected)
        route['regional_surcharge'] = money(regional_extra(selected[0]['quote'], selected))
        route['road_verified'] = all(s['source'] == 'LIVE' and s['verified'] for s in segments)
        route['arrival_end_at'] = leave.isoformat()
    return route


class RoadError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class StopSearch(Exception):
    def __init__(self, reason): self.reason = reason


def check_result(data, result):
    """Independent proof checks: coverage, physical allocation, evidence and arithmetic.

    Does not ask the solver or road service for a second opinion and never changes a result.
    """
    issues = []; seen = Counter(); cars = Counter(); dedicated = Decimal(0); common = Decimal(0)
    for r in result['routes']:
        cars[r['vehicle_id']] += 1; seen.update(r['task_ids'])
        v = data['vehicles'].get(r['vehicle_id'])
        ts = [data['tasks'].get(i) for i in r['task_ids']]
        if not v or any(t is None for t in ts): issues.append(problem('REFERENCE', '线路引用无效')); continue
        if not v['available'] or sum(t['pieces'] for t in ts) > v['max_pieces']:
            issues.append(problem('CAR_CONSTRAINT', '车辆可用性或容量冲突'))
        if any(t['warehouse_id'] != v['warehouse_id'] for t in ts) or len({t['signature'] for t in ts}) != 1 or not ts or ts[0]['quote'] is None:
            issues.append(problem('GROUP_CONSTRAINT', '同仓或报价兼容冲突')); continue
        if r['errors']: issues.extend(r['errors'])
        if len(r['segments']) != len(ts) or len(r['stops']) != len(ts):
            issues.append(problem('MISSING_ROAD', '道路证据或逐店时序不完整')); continue
        meters = sum(s['meters'] for s in r['segments'])
        # Separate arithmetic from the evaluator to detect aggregation/rounding defects.
        q = ts[0]['quote']
        raw = Decimal(meters) * Decimal(q['per_km']) / Decimal(1000) if q['mode'] in ['KM', 'KM_REGION'] else Decimal(q['base_price']) + Decimal(max(0, len(ts)-q['included_stops'])) * Decimal(q['extra_stop_price'])
        try:
            raw += regional_extra(q, ts)
        except RoadError as exc:
            issues.append(problem(exc.code,exc.message))
            continue
        fee = format(raw.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '.2f')
        if r['fee'] != fee or r['meters'] != meters or r['pieces'] != sum(t['pieces'] for t in ts):
            issues.append(problem('FEE_PROOF', '线路金额或件数复算不一致'))
        dedicated += Decimal(fee)
        last = datetime.fromisoformat(data['warehouses'][v['warehouse_id']]['earliest_departure_at'])
        previous_id = v['warehouse_id']
        for t, s, stop in zip(ts, r['segments'], r['stops']):
            request = s['request']
            if request['vehicle_id'] != v['id'] or request['from_id'] != previous_id or request['to_id'] != t['id'] or request['departure_at'] != last.isoformat():
                issues.append(problem('ROAD_BINDING', '道路证据与车辆、顺序或时刻不一致'))
            origin = data['warehouses'][v['warehouse_id']] if previous_id == v['warehouse_id'] else data['tasks'][previous_id]
            if request['origin'] != {k: origin.get(k) for k in ['longitude','latitude','crs']} or request['destination'] != {k: t.get(k) for k in ['longitude','latitude','crs']} or request['source'] != s['source']:
                issues.append(problem('ROAD_COORDINATES', '道路证据与冻结位置或来源不一致'))
            if s['source']=='DRIVING' and (origin.get('crs')!='GCJ02' or t.get('crs')!='GCJ02'):
                from app.coordinates import provider_point
                try:
                    if request.get('coordinate_policy')!='EXPLICIT_REFERENCE_CONVERSION_TO_GCJ02' or request.get('provider_origin')!=provider_point(origin) or request.get('provider_destination')!=provider_point(t):
                        issues.append(problem('ROAD_CONVERSION', '驾车参考坐标转换记录与冻结位置不一致'))
                except (KeyError,TypeError,ValueError):
                    issues.append(problem('ROAD_CONVERSION', '驾车参考坐标转换记录无效'))
            if s['source'] == 'DRIVING':
                issues.append(problem('DRIVING_REFERENCE', '普通驾车参考，未核验货车限行及计划发车时刻', task_id=t['id']))
            elif s['source'] not in ['LIVE','FIXTURE'] or (s['source']=='LIVE' and not s['verified']):
                issues.append(problem('ROAD_NOT_VERIFIED', '实际专车道路尚未通过核验'))
            if content_hash({k: val for k, val in s.items() if k != 'evidence_hash'}) != s['evidence_hash']:
                issues.append(problem('ROAD_INTEGRITY', '道路证据完整性校验失败'))
            from app.road_service import PROFILE
            if request['profile'] != {k: v.get(k) for k in PROFILE}:
                issues.append(problem('ROAD_PROFILE', '道路证据与完整车辆参数不一致'))
            arrival = last + timedelta(seconds=s['seconds']); start = max(arrival, datetime.fromisoformat(t['open_at']))
            last = start + timedelta(minutes=t['unload_minutes']); previous_id = t['id']
            if stop['task_id'] != t['id'] or stop['arrival_at'] != arrival.isoformat() or stop['unload_start_at'] != start.isoformat() or stop['departure_at'] != last.isoformat():
                issues.append(problem('TIME_PROOF', '逐店时序复算不一致'))
            if start > datetime.fromisoformat(t['close_at']) or (t.get('latest_arrival_at') and arrival > datetime.fromisoformat(t['latest_arrival_at'])):
                issues.append(problem('TIME_CONSTRAINT', '到店或卸货开始时间冲突', task_id=t['id']))
    # Shared delivery prices depend only on quantity and unit price. No store
    # business, unloading, latest-arrival or derived arrival fields are read.
    for row in result['common']:
        seen[row['task_id']] += 1; t = data['tasks'].get(row['task_id'])
        expected_common = None if not t or t['common_unit_price'] is None else format((Decimal(t['pieces'])*Decimal(t['common_unit_price'])).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP),'.2f')
        if not t or row['fee'] != expected_common or row['fee'] is None or row['pieces'] != t['pieces']:
            issues.append(problem('COMMON_PRICE', '共配金额或配置无效', task_id=row['task_id']))
        else: common += Decimal(row['fee'])
    for row in result['unassigned']: seen[row['task_id']] += 1
    if set(seen) != set(data['tasks']) or any(count != 1 for count in seen.values()):
        issues.append(problem('TASK_COVERAGE', '每票任务必须且只能分配一次，禁止拆票'))
    if any(count > 1 for count in cars.values()): issues.append(problem('VEHICLE_REUSED', '同一辆实际车辆不能重复使用'))
    total = money(dedicated + common)
    if result['total_fee'] != total: issues.append(problem('TOTAL_PROOF', '总费用复算不一致'))
    if result['unassigned']: issues.append(problem('UNASSIGNED', '仍有未分配任务'))
    return {'valid': not issues, 'issues': issues, 'task_count': len(data['tasks']),
            'total_pieces': sum(t['pieces'] for t in data['tasks'].values()), 'used_vehicles': len(cars),
            'dedicated_fee': money(dedicated), 'common_fee': money(common), 'total_fee': total}


def assemble(data, routes, common_ids, unassigned_ids, reasons=None):
    reasons = reasons or {}
    common = [{'task_id': i, 'pieces': data['tasks'][i]['pieces'], 'unit_price': data['tasks'][i]['common_unit_price'],
               'fee': common_fee(data['tasks'][i]['pieces'], data['tasks'][i]['common_unit_price']),
               'reason': ('选择共配（不校验到店时间）；专车候选说明：'+reasons[i]) if i in reasons else '联合分配选择共配',
               'dedicated_reference_note': reasons.get(i), 'arrival_time_required': False,
               'arrival_at': None, 'time_policy': 'NOT_REQUIRED'} for i in sorted(common_ids)]
    unassigned = [{'task_id': i, 'pieces': data['tasks'][i]['pieces'], 'reason': reasons.get(i, '无可用且通过校验的专车，也未配置共配单价')} for i in sorted(unassigned_ids)]
    result = {'routes': routes, 'common': common, 'unassigned': unassigned,
              'total_fee': money(sum(Decimal(r['fee'] or '0') for r in routes) + sum(Decimal(r['fee'] or '0') for r in common)),
              'engine_version': ENGINE_VERSION, 'common_time_policy': 'NOT_REQUIRED'}
    result['fees_complete'] = not unassigned and all(r['fee'] is not None for r in routes+common)
    result['check'] = check_result(data, result)
    result['confirmable'] = data['source'] != 'FIXTURE' and result['check']['valid'] and all(r['road_verified'] for r in routes)
    result['road_source'] = 'FIXTURE' if data['source'] == 'FIXTURE' else 'DRIVING' if any(s['source'] == 'DRIVING' for r in routes for s in r['segments']) else 'LIVE' if routes and all(r['road_verified'] for r in routes) else 'FIXTURE' if any(s['source'] == 'FIXTURE' for r in routes for s in r['segments']) else 'UNVERIFIED' if routes else 'COMMON_ONLY'
    if result['road_source'] == 'DRIVING':
        result['confirmable'] = False; result['execution_scope'] = 'REFERENCE_ONLY'
    return result


def recalculate(body, draft, road, control=None):
    data = normalize(body); ids = set(data['tasks']); routes = []
    for r in draft['routes']:
        routes.append(evaluate_route(data, r['vehicle_id'], r['task_ids'], road, control))
    common_ids = draft['common_ids']; unassigned_ids = draft['unassigned_ids']
    # Preserve conflicts and all manual choices; do not silently move or repair tasks.
    result = assemble(data, routes, [i for i in common_ids if i in ids], [i for i in unassigned_ids if i in ids])
    unknown = set(common_ids + unassigned_ids) - ids
    if unknown:
        result['check']['issues'].append(problem('UNKNOWN_TASK', '草稿含不存在的任务'))
        result['check']['valid'] = result['confirmable'] = False
    result['algorithm'] = {'mode': 'MANUAL', 'stop_reason': 'MANUAL_RECALCULATED', 'seed': 0}
    return result


def compare_manual(result, previous):
    comparable = result['fees_complete'] and previous.get('fees_complete', True)
    result['comparison'] = {'previous_total': previous['total_fee'], 'current_total': result['total_fee'],
                            'delta': money(Decimal(result['total_fee'])-Decimal(previous['total_fee'])) if comparable else None,
                            'comparable': comparable,
                            'previous_dedicated': previous['check']['dedicated_fee'],
                            'current_dedicated': result['check']['dedicated_fee'],
                            'previous_common': previous['check']['common_fee'],
                            'current_common': result['check']['common_fee']}
    return result


def optimize_manual(body, draft, road, control=None):
    """Minimize route prices while retaining every manual mode and vehicle.

    Up to 8 stops per vehicle are exhaustively ordered. Larger lines use bounded
    relocation/reversal search and report that global optimality is unproven.
    The submitted order is always the incumbent, including on budget expiry.
    """
    data = normalize(body)
    original = recalculate(body, draft, road, control)
    routes = list(original['routes']); candidates = 0; failures = 0
    exact = all(len(route['task_ids']) <= 8 for route in routes)
    stop_reason = 'SEARCH_COMPLETED'
    def blocking(route):
        return [e for e in route['errors'] if e['code'] != 'ROAD_TIME_UNVERIFIED']
    def rank(route):
        return (bool(blocking(route)), Decimal(route['fee']) if route['fee'] is not None else Decimal('Infinity'), route['meters'], route['seconds'])
    try:
        for index, baseline in enumerate(routes):
            ids = list(baseline['task_ids'])
            # Invalid references, allocation and duplicate IDs remain visible.
            if any(i not in data['tasks'] for i in ids) or len(set(ids)) != len(ids):
                exact = False; continue
            best = baseline
            def consider(order):
                nonlocal candidates, best, failures
                if control: control()
                if candidates >= 10000: raise StopSearch('CANDIDATE_BUDGET')
                if order == best['task_ids']: return False
                candidates += 1
                trial = evaluate_route(data, baseline['vehicle_id'], list(order), road, control)
                if trial['fee'] is None:
                    failures += 1
                if rank(trial) < rank(best):
                    best = trial; routes[index] = best; return True
                return False
            if len(ids) <= 8:
                for order in permutations(ids): consider(list(order))
            else:
                for _ in range(3):
                    improved = False
                    order = list(best['task_ids'])
                    for a in range(len(order)):
                        for b in range(a+1, len(order)):
                            if consider(order[:a]+list(reversed(order[a:b+1]))+order[b+1:]): improved = True
                    order = list(best['task_ids'])
                    for a in range(len(order)):
                        for b in range(len(order)):
                            moved = list(order); task = moved.pop(a); moved.insert(b, task)
                            if consider(moved): improved = True
                    if not improved: break
    except StopSearch as exc:
        if exc.reason == 'CANCELLED': raise
        stop_reason = exc.reason
    result = assemble(data, routes, [i['task_id'] for i in original['common']], [i['task_id'] for i in original['unassigned']])
    if any(e['code'] == 'UNKNOWN_TASK' for e in original['check']['issues']):
        result['check']['issues'].append(problem('UNKNOWN_TASK','草稿含不存在的任务'))
        result['check']['valid'] = result['confirmable'] = False
    result['algorithm'] = {'mode':'MANUAL_OPTIMIZED', 'stop_reason':stop_reason, 'seed':0,
        'candidate_count':candidates, 'optimality_proven':exact and not failures and stop_reason == 'SEARCH_COMPLETED' and result['check']['valid'],
        'manual_order_total':original['total_fee'], 'allocation_preserved':True}
    return result


def optimize_legacy(body, road, budget_seconds=15, seed=0, cancelled=lambda: False):
    data = normalize(body); tasks = data['tasks']; start = time.monotonic(); evaluations = 0; failures = 0
    common_ids = {i for i, t in tasks.items() if t['common_unit_price'] is not None}
    unassigned = set(tasks) - common_ids; reasons = {}; candidates = []
    best = assemble(data, [], common_ids, unassigned)
    baseline = best['total_fee'] if not unassigned else None
    stop_reason = 'SEARCH_COMPLETED'; max_evaluations = 20000
    # Reserve a small part of the wall budget for the final independent proof.
    search_seconds = max(0, budget_seconds-min(.25,budget_seconds*.05))
    def control():
        if cancelled(): raise StopSearch('CANCELLED')
        if time.monotonic() - start >= search_seconds: raise StopSearch('TIME_BUDGET')
        if evaluations >= max_evaluations: raise StopSearch('CANDIDATE_BUDGET')
    def evaluate(v, ids):
        nonlocal evaluations, failures
        control(); evaluations += 1
        r = evaluate_route(data, v['id'], ids, road, control)
        if r['errors']:
            failures += 1
            for i in ids: reasons[i] = '；'.join(dict.fromkeys(e['message'] for e in r['errors']))
        elif r['fee'] is not None:
            candidates.append(r)
            if exact: keep_candidate(r)
        return r
    cars = sorted([v for v in data['vehicles'].values() if v['available']], key=lambda v: (v['max_pieces'], v['id']))
    if hasattr(road,'preflight'):
        ready=[]
        for v in cars:
            try:road.preflight(v,data['warehouses'][v['warehouse_id']]);ready.append(v)
            except RoadError as exc:
                failures+=1
                for i,t in tasks.items():
                    if t['warehouse_id']==v['warehouse_id']:reasons[i]=exc.message
        cars=ready
    exact = len(tasks) <= 8 and len(cars) <= 8
    def rank(result):
        return (len(result['unassigned']), Decimal(result['total_fee']), len(result['routes']))
    def keep_candidate(candidate):
        nonlocal best
        covered=set(candidate['task_ids'])
        selected=[r for r in best['routes'] if r['vehicle_id'] != candidate['vehicle_id'] and not covered.intersection(r['task_ids'])]+[candidate]
        used={i for r in selected for i in r['task_ids']}
        trial=assemble(data,selected,common_ids-used,unassigned-used,reasons)
        if rank(trial)<rank(best):best=trial
    pending_routes = None
    try:
        if exact:
            groups = {}
            for i, t in tasks.items():
                if t['signature']: groups.setdefault((t['warehouse_id'], t['signature']), []).append(i)
            # Seed complete feasible routes before factorial enumeration. A budget
            # stop during candidate generation must not discard routes already found.
            for v in cars:
                for (wh,_),group in sorted(groups.items()):
                    if wh != v['warehouse_id']:continue
                    chain=[];pieces=0
                    for i in sorted(group,key=lambda i:(tasks[i]['common_unit_price'] is not None,tasks[i].get('latest_arrival_at') or tasks[i]['close_at'],i)):
                        if pieces+tasks[i]['pieces']>v['max_pieces']:continue
                        trial=evaluate(v,chain+[i])
                        if not trial['errors']:chain=chain+[i];pieces+=tasks[i]['pieces']
            for v in cars:
                for (wh, _), group in sorted(groups.items()):
                    if wh != v['warehouse_id']: continue
                    for size in range(1, len(group) + 1):
                        for order in permutations(sorted(group), size):
                            if sum(tasks[i]['pieces'] for i in order) <= v['max_pieces']: evaluate(v, list(order))
            # Dynamic programming over the single physical vehicle pool, across every quote group.
            indices = {i: 1 << n for n, i in enumerate(sorted(tasks))}
            choices = {}
            for r in candidates:
                mask = sum(indices[i] for i in r['task_ids']); key = (r['vehicle_id'], mask)
                if key not in choices or Decimal(r['fee']) < Decimal(choices[key]['fee']): choices[key] = r
            states = {0: (Decimal(0), [])}
            for v in cars:
                control(); following = dict(states)
                for (vid, mask), r in choices.items():
                    if vid != v['id']: continue
                    for used, (cost, selected) in states.items():
                        if used & mask: continue
                        value = cost + Decimal(r['fee']); target = used | mask
                        if target not in following or value < following[target][0]: following[target] = (value, selected + [r])
                states = following
                # Keep a complete incumbent after every physical vehicle, including on timeout.
                for _, (_, selected) in states.items():
                    used = {i for r in selected for i in r['task_ids']}
                    result = assemble(data, selected, common_ids - used, unassigned - used, reasons)
                    if rank(result) < rank(best): best = result
        else:
            # Bounded greedy insertion, then exchanges via a fresh complete vehicle reallocation.
            # Geometry only orders candidate queries; road measurements always determine cost/time.
            def proximity(origin, task):
                try: return (float(origin['longitude'])-float(task['longitude']))**2 + (float(origin['latitude'])-float(task['latitude']))**2
                except (KeyError, TypeError): return 0
            for pass_no in range(2):
                selected = []; remaining = set(tasks)
                ordered_cars = cars if pass_no == 0 else sorted(cars, key=lambda v: (-v['max_pieces'], v['id']))
                for v in ordered_cars:
                    control(); wh = data['warehouses'][v['warehouse_id']]
                    available = sorted([i for i in remaining if tasks[i]['warehouse_id'] == v['warehouse_id'] and tasks[i]['signature'] and tasks[i]['pieces'] <= v['max_pieces']],
                                       key=lambda i: (tasks[i]['common_unit_price'] is not None, proximity(wh,tasks[i]), i))
                    winner = None; winner_score = (0, Decimal(0))
                    def consider(r):
                        nonlocal winner, winner_score, pending_routes
                        saved = sum(Decimal(common_fee(tasks[i]['pieces'], tasks[i]['common_unit_price']) or '0') for i in r['task_ids']) - Decimal(r['fee'])
                        score = (sum(tasks[i]['common_unit_price'] is None for i in r['task_ids']), saved)
                        if score > winner_score:
                            winner, winner_score = r, score
                            pending_routes=selected+[winner]
                    for first in available[:4]:
                        chain = [first]; r = evaluate(v, chain)
                        if r['errors']: continue
                        consider(r)
                        used_pieces = tasks[first]['pieces']
                        pool = [i for i in available if tasks[i]['signature'] == tasks[first]['signature'] and i != first]
                        for _ in range(min(len(pool), 30)):
                            control()
                            fitting = sorted([i for i in pool if used_pieces + tasks[i]['pieces'] <= v['max_pieces']], key=lambda i: (tasks[i]['common_unit_price'] is not None, proximity(tasks[chain[-1]], tasks[i]), i))
                            extended = None
                            for i in fitting[:3]:
                                trial = evaluate(v, chain + [i])
                                if not trial['errors']: extended = (i, trial); break
                            if extended is None: break
                            i, r = extended; chain = chain + [i]; pool.remove(i); used_pieces += tasks[i]['pieces']
                            consider(r)
                    if winner:
                        selected.append(winner); remaining.difference_update(winner['task_ids'])
                    pending_routes=None
                    result = assemble(data, selected, common_ids & remaining, unassigned & remaining, reasons)
                    if rank(result) < rank(best): best = result
            # Reassign physical vehicles across quote groups and exchange overlapping
            # lines. Every accepted exchange is a complete checked allocation.
            for _ in range(2):
                improved = False
                for candidate in candidates:
                    control(); covered = set(candidate['task_ids'])
                    removed = [r for r in best['routes'] if r['vehicle_id'] == candidate['vehicle_id'] or covered.intersection(r['task_ids'])]
                    freed = {i for r in removed for i in r['task_ids']}-covered
                    previous_common = {r['task_id'] for r in best['common']}
                    missing = ({r['task_id'] for r in best['unassigned']} | (freed & unassigned))-covered
                    projected = Decimal(best['total_fee']) + Decimal(candidate['fee']) - sum(Decimal(r['fee']) for r in removed)
                    projected += sum(Decimal(common_fee(tasks[i]['pieces'],tasks[i]['common_unit_price'])) for i in freed & common_ids)
                    projected -= sum(Decimal(common_fee(tasks[i]['pieces'],tasks[i]['common_unit_price'])) for i in covered & previous_common)
                    projected_rank=(len(missing),projected,len(best['routes'])-len(removed)+1)
                    if projected_rank < rank(best):
                        selected = [r for r in best['routes'] if r not in removed]+[candidate]
                        used = {i for r in selected for i in r['task_ids']}
                        trial = assemble(data, selected, common_ids-used, unassigned-used, reasons)
                        if rank(trial) < rank(best): best=trial; improved=True
                if not improved: break
    except StopSearch as exc:
        stop_reason = exc.reason
    if pending_routes:
        used={i for r in pending_routes for i in r['task_ids']}
        trial=assemble(data,pending_routes,common_ids-used,unassigned-used,reasons)
        if rank(trial)<rank(best):best=trial
    best = assemble(data, best['routes'], {r['task_id'] for r in best['common']}, {r['task_id'] for r in best['unassigned']}, reasons)
    best['algorithm'] = {'mode': 'EXACT_SMALL' if exact and stop_reason == 'SEARCH_COMPLETED' else 'BOUNDED_HEURISTIC',
                         'seed': seed, 'stop_reason': stop_reason, 'candidate_count': evaluations,
                         'elapsed_seconds': round(time.monotonic() - start, 4), 'budget_seconds': budget_seconds,
                         'search_budget_seconds': search_seconds,
                         'all_common_baseline': baseline, 'optimality_proven': exact and not failures and stop_reason == 'SEARCH_COMPLETED',
                         'infeasibility_proven': False, 'road_stats': road.stats()}
    return best


def optimize(body, road, budget_seconds=15, seed=0, cancelled=lambda:False, *, initial_result=None):
    from app.route_portfolio import optimize_hybrid, optimize_alns
    settings=getattr(road,'settings',None)
    profile=getattr(settings,'planning_optimizer_profile','HYBRID')
    # Preserve exact enumeration on tiny inputs, whatever study profile is
    # selected for larger days. Forced refresh must revalidate old quotations.
    if len(body.get('tasks',[]))<=8 and len(body.get('vehicles',[]))<=8:
        return optimize_hybrid(body,road,budget_seconds,seed,cancelled,legacy=optimize_legacy,initial_result=initial_result)
    if profile=='ALNS':
        return optimize_alns(body,road,budget_seconds,seed,cancelled,legacy=optimize_legacy,initial_result=initial_result)
    if profile=='V3_REFERENCE' and not getattr(road,'force_refresh',False):
        from app.route_baseline import optimize_joint
        return optimize_joint(body,road,budget_seconds,seed,cancelled,legacy=optimize_legacy,initial_result=initial_result)
    if profile in {'GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH'}:
        from app.route_ortools import optimize_ortools
        return optimize_ortools(body,road,budget_seconds,seed,cancelled,initial_result=initial_result,
            strategy=profile,common_penalty_multiplier=getattr(settings,'planning_common_penalty_multiplier',1))
    return optimize_hybrid(body,road,budget_seconds,seed,cancelled,legacy=optimize_legacy,initial_result=initial_result)
