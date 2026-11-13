"""Independent monetary/time differential checks for compact road proposals.

All distances below belong to explicitly synthetic, asymmetric road matrices.
Coordinate hints are constant and cannot provide any of the expected prices.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import random

import pytest

from app.road_service import evidence, road_request
from app.route_metrics import evaluate_candidate
from app.route_search import JointSearch, optimize_joint
from app.routing_engine import RoadError, StopSearch, assemble, evaluate_route, normalize


class MeasuredMatrix:
    def __init__(self, rows):
        self.rows = rows
        self.strict_reads = self.metric_reads = 0

    def known_segment(self, a, b):
        self.metric_reads += 1
        return self.rows.get((a['id'], b['id']))

    def segment(self, vehicle, a, b, departure):
        self.strict_reads += 1
        row = self.rows.get((a['id'], b['id']))
        if row is None:
            raise RoadError('ROAD_CALL_BUDGET', 'Unmeasured test edge')
        return evidence(road_request(vehicle, a, b, departure, 'FIXTURE'),
                        row['meters'], row['seconds'], 'FIXTURE')

    def stats(self):
        return {'calls': 0, 'fixture_reads': self.strict_reads}


REGIONS = [
    ('安徽省', '合肥市', '蜀山区', '340104'),
    ('安徽省', '合肥市', '包河区', '340111'),
    ('安徽省', '淮南市', '田家庵区', '340403'),
    ('浙江省', '杭州市', '西湖区', '330106'),
    ('浙江省', '杭州市', '余杭区', '330110'),
]
TARGETS = [
    {'province_code': '340000', 'province_name': '安徽省', 'city_code': '340100',
     'city_name': '合肥市', 'district_code': '', 'district_name': ''},
    {'province_code': '340000', 'province_name': '安徽省', 'city_code': '340100',
     'city_name': '合肥市', 'district_code': '340104', 'district_name': '蜀山区'},
    {'province_code': '330000', 'province_name': '浙江省', 'city_code': '330100',
     'city_name': '杭州市', 'district_code': '330106', 'district_name': '西湖区'},
]
PRICES = [('KM', None), ('BASE_STOPS', None)] + [
    (mode, policy) for mode in ('KM_REGION', 'BASE_STOPS_REGION')
    for policy in ('ONCE', 'PER_REGION', 'PER_STORE')]


def money(value):
    return str(Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def instance(seed=0, mode='KM', policy=None, n=7, cars=1):
    rng = random.Random(seed)
    day = datetime.fromisoformat('2026-11-13T00:00:00+08:00')
    departure = day + timedelta(hours=22 if seed % 3 == 0 else 6, minutes=seed % 5)
    quote = {'mode': mode, 'per_km': '5.80', 'base_price': '85.35',
             'included_stops': 2, 'extra_stop_price': '13.50'}
    if policy:
        quote['region_surcharge'] = {'amount': '17.25', 'targets': deepcopy(TARGETS), 'billing_mode': policy}
    tasks = []
    for i in range(n):
        province, city, district, code = REGIONS[(i + seed) % len(REGIONS)]
        opens = departure + timedelta(minutes=rng.randint(-60, 180))
        closes = opens + timedelta(minutes=rng.randint(45, 600))
        latest = departure + timedelta(minutes=rng.randint(30, 600)) if (i + seed) % 4 == 0 else None
        tasks.append({'id': f'S{i}', 'store_id': f'S{i}', 'warehouse_id': 'W',
                      'pieces': rng.randint(1, 40), 'quote_id': 'Q', 'common_unit_price': '7.50',
                      'longitude': 0, 'latitude': 0, 'open_at': opens.isoformat(),
                      'close_at': closes.isoformat(), 'latest_arrival_at': latest.isoformat() if latest else None,
                      'unload_minutes': rng.randint(0, 45), 'province': province, 'city': city,
                      'district': district, 'region_code': code})
    body = {'source': 'FIXTURE', 'delivery_date': '2026-11-13',
            'warehouses': [{'id': 'W', 'longitude': 0, 'latitude': 0,
                            'earliest_departure_at': departure.isoformat()}],
            'vehicles': [{'id': f'V{i}', 'warehouse_id': 'W', 'available': True,
                          'max_pieces': 45 + (seed % 8) * 45} for i in range(cars)],
            'quotes': {'Q': quote}, 'tasks': tasks}
    ids = ['W'] + [t['id'] for t in tasks]
    rows = {(a, b): {'meters': rng.randint(0, 180000), 'seconds': rng.randint(0, 3600)}
            for a in ids for b in ids if a != b}
    return body, MeasuredMatrix(rows)


def oracle_price(quote, ids, data, meters):
    if quote['mode'] in ('KM', 'KM_REGION'):
        total = Decimal(meters) * Decimal(quote['per_km']) / 1000
    else:
        extra = max(0, len(ids) - quote['included_stops'])
        total = Decimal(quote['base_price']) + extra * Decimal(quote['extra_stop_price'])
    if quote['mode'].endswith('_REGION'):
        matching = [data['tasks'][i] for i in ids
                    if data['tasks'][i]['region_code'].startswith('3401')
                    or data['tasks'][i]['region_code'] == '330106']
        rule = quote['region_surcharge']
        if rule['billing_mode'] == 'ONCE':
            count = bool(matching)
        elif rule['billing_mode'] == 'PER_REGION':
            count = len({row['region_code'] for row in matching})
        else:
            count = len({row['store_id'] for row in matching})
        total += Decimal(rule['amount']) * count
    return money(total)


@pytest.mark.parametrize('mode,policy', PRICES)
@pytest.mark.parametrize('seed', range(30))
def test_compact_route_matches_full_directed_evidence_and_independent_oracle(mode, policy, seed):
    body, road = instance(seed, mode, policy)
    data = normalize(body)
    ids = list(data['tasks'])
    random.Random(seed + 901).shuffle(ids)
    ids = ids[:1 + seed % len(ids)]
    fast = evaluate_candidate(data, 'V0', ids, road)
    assert fast is not None and fast['_estimated']
    assert road.strict_reads == 0
    full = evaluate_route(data, 'V0', ids, road)
    for key in ('fee', 'meters', 'seconds', 'pieces', 'errors'):
        assert fast[key] == full[key], (seed, mode, policy, key)
    assert fast.get('arrival_end_at') == full.get('arrival_end_at')
    assert 'segments' not in fast and 'polyline' not in fast
    if fast['fee'] is None:
        assert {e['code'] for e in fast['errors']} == {'CAPACITY'}
        return
    meters = seconds = 0
    origin = 'W'
    leave = datetime.fromisoformat(data['warehouses']['W']['earliest_departure_at'])
    late, closed = set(), set()
    for item in ids:
        edge = road.rows[(origin, item)]
        meters += edge['meters']; seconds += edge['seconds']
        task = data['tasks'][item]
        arrival = leave + timedelta(seconds=edge['seconds'])
        begin = max(arrival, datetime.fromisoformat(task['open_at']))
        if task['latest_arrival_at'] and arrival > datetime.fromisoformat(task['latest_arrival_at']):
            late.add(item)
        if begin > datetime.fromisoformat(task['close_at']):
            closed.add(item)
        leave = begin + timedelta(minutes=task['unload_minutes'])
        origin = item
    assert fast['meters'] == meters and fast['seconds'] == seconds
    assert fast['arrival_end_at'] == leave.isoformat()
    assert fast['fee'] == oracle_price(body['quotes']['Q'], ids, data, meters)
    assert {e['task_id'] for e in fast['errors'] if e['code'] == 'LATEST_ARRIVAL'} == late
    assert {e['task_id'] for e in fast['errors'] if e['code'] == 'BUSINESS_CLOSED'} == closed


@pytest.mark.parametrize('seconds,expected', [(60, set()), (61, {'LATEST_ARRIVAL', 'BUSINESS_CLOSED'})])
def test_cross_midnight_arrival_boundary_and_waiting_unload_are_exact(seconds, expected):
    body, road = instance(0, n=2)
    body['vehicles'][0]['max_pieces'] = 400
    body['warehouses'][0]['earliest_departure_at'] = '2026-11-13T23:59:00+08:00'
    body['tasks'][0].update(open_at='2026-11-14T00:00:00+08:00', close_at='2026-11-14T00:00:00+08:00',
                            latest_arrival_at='2026-11-14T00:00:00+08:00', unload_minutes=15)
    body['tasks'][1].update(open_at='2026-11-14T01:00:00+08:00', close_at='2026-11-14T02:00:00+08:00',
                            latest_arrival_at=None, unload_minutes=30)
    road.rows[('W', 'S0')] = {'meters': 1200, 'seconds': seconds}
    road.rows[('S0', 'S1')] = {'meters': 800, 'seconds': 60}
    data = normalize(body)
    fast = evaluate_candidate(data, 'V0', ['S0', 'S1'], road)
    full = evaluate_route(data, 'V0', ['S0', 'S1'], road)
    assert fast['arrival_end_at'] == full['arrival_end_at'] == '2026-11-14T01:30:00+08:00'
    assert fast['fee'] == full['fee'] == '11.60'
    assert {e['code'] for e in fast['errors']} == expected


@pytest.mark.parametrize('fault', ['unavailable', 'capacity', 'warehouse', 'quote'])
def test_invalid_resource_candidates_are_rejected_before_road_metrics(fault):
    body, road = instance(1, n=2)
    if fault == 'unavailable':
        body['vehicles'][0]['available'] = False
    elif fault == 'capacity':
        body['vehicles'][0]['max_pieces'] = 0
    elif fault == 'warehouse':
        body['tasks'][1]['warehouse_id'] = 'OTHER'
    else:
        body['tasks'][1]['quote_id'] = None
    data = normalize(body)
    fast = evaluate_candidate(data, 'V0', ['S0', 'S1'], road)
    full = evaluate_route(data, 'V0', ['S0', 'S1'], road)
    assert fast['errors'] == full['errors']
    assert fast['fee'] is None and road.metric_reads == road.strict_reads == 0


@pytest.mark.parametrize('bad', [None, {}, [], 'invalid', 123,
    {'meters': 12}, {'seconds': 2}, {'meters': -1, 'seconds': 2},
    {'meters': 1, 'seconds': -2}, {'meters': True, 'seconds': 2},
    {'meters': 1, 'seconds': False}, {'meters': 1.5, 'seconds': 2},
    {'meters': 1, 'seconds': float('nan')}, {'meters': float('inf'), 'seconds': 2}])
def test_unknown_or_malformed_metrics_never_produce_a_priced_candidate(bad):
    body, road = instance(1, n=1)
    road.rows[('W', 'S0')] = bad
    assert evaluate_candidate(normalize(body), 'V0', ['S0'], road) is None
    assert road.strict_reads == 0


def test_unknown_fast_edge_falls_back_to_the_strict_road_adapter():
    body, road = instance(1, n=1)
    road.known_segment = lambda a, b: None
    host = JointSearch(normalize(body), road, 30, 1, lambda: False)
    candidate = host.evaluate('V0', ['S0'])
    assert candidate is not None
    assert road.strict_reads == 1
    expected = Decimal(road.rows[('W', 'S0')]['meters']) * Decimal('5.80') / 1000
    assert candidate['fee'] == money(expected)


def test_metrics_cannot_turn_a_missing_strict_edge_into_execution_evidence():
    body, road = instance(1, n=1)
    road.known_segment = lambda a, b: {'meters': 100, 'seconds': 1}
    road.rows.clear()
    data = normalize(body)
    host = JointSearch(data, road, 30, 1, lambda: False)
    candidate = host.evaluate('V0', ['S0'])
    # Even if a proposal adapter claims metrics, publication must obtain actual
    # road evidence. No polyline/geometry may be fabricated to close that gap.
    if candidate is not None:
        assert not host.consider([candidate])
    assert host.best['routes'] == [] and len(host.best['common']) == 1


def test_materialization_reprices_mismatched_metrics_before_accepting_cheaper_plan():
    body, road = instance(1, n=1)
    body['tasks'][0]['pieces'] = 1
    road.rows[('W', 'S0')] = {'meters': 10000, 'seconds': 60}
    road.known_segment = lambda a, b: {'meters': 100, 'seconds': 1}
    host = JointSearch(normalize(body), road, 30, 1, lambda: False)
    candidate = host.evaluate('V0', ['S0'])
    assert candidate is not None
    assert not host.consider([candidate])
    # The claimed proposal would be 0.58, but actual strict-road evidence costs
    # 58.00 against a 7.50 common delivery. Only the real evidence can publish.
    assert host.best['total_fee'] == '7.50'
    assert host.best['routes'] == [] and road.strict_reads >= 1


@pytest.mark.parametrize('cars', [25, 50])
@pytest.mark.parametrize('cancelled,reason', [(False, 'TIME_BUDGET'), (True, 'CANCELLED')])
def test_large_initial_fleet_survives_raw_lru_and_stopping(cars, cancelled, reason):
    body, road = instance(2, n=cars, cars=cars)
    for row in body['tasks']:
        row.update(pieces=1, common_unit_price=None, open_at='2026-11-13T06:00:00+08:00', close_at='2026-11-13T23:59:00+08:00',
                   latest_arrival_at=None, unload_minutes=0)
    for vehicle in body['vehicles']:
        vehicle['max_pieces'] = 1
    data = normalize(body)
    routes = [evaluate_route(data, f'V{i}', [f'S{i}'], road) for i in range(cars)]
    initial = assemble(data, routes, [], [])
    assert initial['check']['valid']
    reads = road.strict_reads
    result = optimize_joint(body, road, 0, seed=4, cancelled=lambda: cancelled, initial_result=initial)
    assert result['total_fee'] == initial['total_fee']
    assert len(result['routes']) == cars and not result['unassigned'] and not result['common']
    assert result['check']['valid'] and road.strict_reads == reads
    assert result['algorithm']['stop_reason'] == reason


def test_same_total_fee_prefers_shorter_roads_even_with_more_vehicles():
    body, road = instance(2, n=6, cars=2)
    body['quotes']['Q'] = {'mode': 'BASE_STOPS', 'base_price': '0', 'included_stops': 0, 'extra_stop_price': '1'}
    for row in body['tasks']:
        row.update(pieces=1, open_at='2026-11-13T06:00:00+08:00', close_at='2026-11-13T23:59:00+08:00',
                   latest_arrival_at=None, unload_minutes=0)
    for vehicle in body['vehicles']:
        vehicle['max_pieces'] = 6
    for key, row in road.rows.items():
        a, b = key
        row.update(meters=10000 if 'W' in (a,b) else 2000 if int(a[1:]) // 3 == int(b[1:]) // 3 else 100000, seconds=1)
    data = normalize(body)
    long = evaluate_route(data, 'V0', list(data['tasks']), road)
    short = [evaluate_route(data, 'V0', ['S0', 'S1', 'S2'], road),
             evaluate_route(data, 'V1', ['S3', 'S4', 'S5'], road)]
    initial = assemble(data, [long], [], [])
    assert initial['total_fee'] == '6.00' and long['pieces'] == 6
    host = JointSearch(data, road, 30, 2, lambda: False, initial)
    assert sum(Decimal(r['fee']) for r in short) == Decimal('6.00')
    assert host.consider(short)
    assert len(host.best['routes']) == 2
    assert sum(r['meters'] for r in host.best['routes']) == 28000


def test_more_expensive_but_shorter_or_fully_loaded_route_never_wins():
    body, road = instance(2, n=3, cars=2)
    for row in body['tasks']:
        row.update(pieces=1, common_unit_price='1', open_at='2026-11-13T06:00:00+08:00',
                   close_at='2026-11-13T23:59:00+08:00', latest_arrival_at=None, unload_minutes=0)
    for vehicle in body['vehicles']:
        vehicle['max_pieces'] = 3
    for row in road.rows.values():
        row.update(meters=1000, seconds=1)
    data = normalize(body)
    host = JointSearch(data, road, 30, 2, lambda: False)
    fully_loaded = evaluate_route(data, 'V0', list(data['tasks']), road)
    assert fully_loaded['pieces'] == 3 and fully_loaded['fee'] == '17.40'
    assert not host.consider([host.remember(fully_loaded)])
    assert host.best['total_fee'] == '3.00' and not host.best['routes']


def test_compact_candidate_cancellation_checks_before_each_measured_segment():
    body, road = instance(1, n=3)
    body['vehicles'][0]['max_pieces'] = 400
    calls = 0
    def cancel_second_segment():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch) as caught:
        evaluate_candidate(normalize(body), 'V0', ['S0', 'S1', 'S2'], road, cancel_second_segment)
    assert caught.value.reason == 'CANCELLED'
    assert road.metric_reads == 1 and road.strict_reads == 0


def test_unrepresentable_measured_duration_is_not_a_complete_price():
    body, road = instance(1, n=1)
    road.rows[('W', 'S0')] = {'meters': 1, 'seconds': 10 ** 30}
    data = normalize(body)
    fast = evaluate_candidate(data, 'V0', ['S0'], road)
    full = evaluate_route(data, 'V0', ['S0'], road)
    assert fast['fee'] is full['fee'] is None
    assert fast['meters'] == full['meters'] == 0
    assert fast['seconds'] == full['seconds'] == 0
    assert fast['errors'] == full['errors']
    assert {row['code'] for row in fast['errors']} == {'TIME_RANGE'}


def test_exact_cent_rounding_and_arrival_deadline_are_independent_of_waiting():
    body, road = instance(1, n=1)
    body['warehouses'][0]['earliest_departure_at'] = '2026-11-13T06:00:00+08:00'
    body['tasks'][0].update(open_at='2026-11-13T08:00:00+08:00', close_at='2026-11-13T12:00:00+08:00',
                            latest_arrival_at='2026-11-13T06:01:00+08:00', unload_minutes=15)
    road.rows[('W', 'S0')] = {'meters': 10025, 'seconds': 60}
    data = normalize(body)
    fast = evaluate_candidate(data, 'V0', ['S0'], road)
    full = evaluate_route(data, 'V0', ['S0'], road)
    # 10.025 km * 5.80 = 58.145 rounds HALF_UP to 58.15; actual arrival
    # is exactly the deadline even though unloading begins two hours later.
    assert fast['fee'] == full['fee'] == '58.15'
    assert fast['errors'] == full['errors'] == []
    assert fast['arrival_end_at'] == full['arrival_end_at'] == '2026-11-13T08:15:00+08:00'
