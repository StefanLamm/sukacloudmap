"""Isolated synthetic-provider tests for reproducible offline experiments."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import sqlite3
import time

import pytest

from app.planning_inputs import content_hash, encode
from app.road_service import DrivingRoad, evidence, road_request
from app.routing_engine import assemble, evaluate_route, normalize
from scripts.benchmark_algorithms import (Algorithm, FrozenEvidenceRoad, freeze_case,
                                          freeze_historical_case, independent_audit, private_json,
                                          run_benchmark, write_graph)


def problem():
    body = {'source': 'FIXTURE', 'delivery_date': '2026-11-13',
            'warehouses': [{'id': 'W', 'longitude': '117.0', 'latitude': '31.0', 'crs': 'GCJ02',
                            'earliest_departure_at': '2026-11-13T08:00:00+08:00'}],
            'vehicles': [{'id': 'V1', 'plate': 'TEST1', 'warehouse_id': 'W', 'max_pieces': 20, 'available': True},
                         {'id': 'V2', 'plate': 'TEST2', 'warehouse_id': 'W', 'max_pieces': 20, 'available': True}],
            'quotes': {'Q': {'mode': 'KM', 'per_km': '1'}},
            'tasks': [{'id': f'T{i}', 'store_id': f'T{i}', 'warehouse_id': 'W', 'pieces': 5,
                       'quote_id': 'Q', 'common_unit_price': '10', 'longitude': str(117 + i / 100),
                       'latitude': '31.1', 'crs': 'GCJ02', 'open_at': '2026-11-13T08:00:00+08:00',
                       'close_at': '2026-11-13T23:00:00+08:00', 'latest_arrival_at': None,
                       'unload_minutes': 5} for i in range(1, 4)]}
    return body


def measured_edges(body, pairs=None):
    data = normalize(body)
    points = {**data['tasks'], **data['warehouses']}
    pairs = pairs if pairs is not None else [('W', i) for i in data['tasks']] + [(a, b) for a in data['tasks'] for b in data['tasks'] if a != b]
    result = []
    for a, b in pairs:
        request = road_request(data['vehicles']['V1'], points[a], points[b], '2026-11-13T08:00:00+08:00', 'DRIVING', strategy=32)
        result.append(evidence(request, 10000 if a == 'W' else 1000, 600 if a == 'W' else 60,
                               'DRIVING', False, [[float(points[a]['longitude']), float(points[a]['latitude'])],
                                                  [float(points[b]['longitude']), float(points[b]['latitude'])]],
                               'TRUCK_NOT_VERIFIED', time_status='CURRENT_TRAFFIC_REFERENCE',
                               received_at=int(time.time()) - 7200))
    return result


def minimal_database(path, body, cache):
    with sqlite3.connect(path) as connection:
        connection.executescript('''
        CREATE TABLE demand_groups(id TEXT,name TEXT,delivery_date TEXT);
        CREATE TABLE input_snapshots(id TEXT,group_id TEXT,version INTEGER,content TEXT,content_hash TEXT);
        CREATE TABLE plan_versions(id TEXT,group_id TEXT,snapshot_id TEXT,version INTEGER,content TEXT,content_hash TEXT);
        CREATE TABLE road_cache(key TEXT,content TEXT);
        ''')
        connection.execute('INSERT INTO demand_groups VALUES (?,?,?)', ('G', 'isolated-test', body['delivery_date']))
        connection.execute('INSERT INTO input_snapshots VALUES (?,?,?,?,?)', ('S', 'G', 1, encode(body), content_hash(body)))
        connection.executemany('INSERT INTO road_cache VALUES (?,?)', [(content_hash(item['request']), encode(item)) for item in cache])
        connection.commit()


def test_freeze_never_changes_source_database_and_filters_invalid_evidence(tmp_path):
    body = problem()
    rows = measured_edges(body, [('W', 'T1')])
    bad = deepcopy(rows[0]);bad['meters'] += 100
    source = tmp_path / 'business.db'
    minimal_database(source, body, [*rows, bad])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    case = freeze_case(source, tmp_path / 'case', 'G')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert case['road_stats']['known_directed_edges'] == 1
    assert case['required_directed_edges'] == 9
    road = FrozenEvidenceRoad(case['road_database'])
    data = normalize(body)
    assert road.known_segment('W', 'T1')['meters'] == 10000
    assert road.known_segment('T1', 'W') is None
    with pytest.raises(Exception) as error:
        road.segment(data['vehicles']['V1'], data['tasks']['T1'], data['warehouses']['W'], '2026-11-13T09:00:00+08:00')
    assert error.value.code == 'ROAD_EDGE_NOT_FROZEN'
    assert road.stats()['calls'] == 0
    road.close()


def test_frozen_evidence_rebinds_vehicle_and_time_without_promoting_old_reference(tmp_path):
    body = problem();source = measured_edges(body, [('W', 'T1')])[0]
    write_graph(tmp_path / 'roads.db', body, [source])
    road = FrozenEvidenceRoad(tmp_path / 'roads.db');data = normalize(body)
    actual = road.segment(data['vehicles']['V2'], data['warehouses']['W'], data['tasks']['T1'], '2026-11-13T12:00:00+08:00')
    assert actual['request']['vehicle_id'] == 'V2'
    assert actual['request']['departure_at'] == '2026-11-13T12:00:00+08:00'
    assert actual['received_at'] == source['received_at']
    assert actual['polyline'] == source['polyline']
    assert actual['provider_evidence_hash'] == source['evidence_hash']
    assert not actual['verified'] and actual['time_status'] == 'CURRENT_TRAFFIC_REFERENCE'
    assert actual['evidence_hash'] == content_hash({k: v for k, v in actual.items() if k != 'evidence_hash'})
    moved = {**data['tasks']['T1'], 'longitude': '118.0'}
    assert road.known_segment(data['warehouses']['W'], moved) is None
    assert road.stats()['calls'] == 0
    road.close()


def test_independent_audit_checks_complete_money_capacity_and_reference_graph(tmp_path):
    body = problem();write_graph(tmp_path / 'roads.db', body, measured_edges(body))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db');data = normalize(body)
    route = evaluate_route(data, 'V1', ['T1', 'T2', 'T3'], road)
    result = assemble(data, [route], [], [])
    assert independent_audit(data, result, road)['valid']
    assert result['total_fee'] == '12.00'
    bad = deepcopy(result);bad['total_fee'] = '0.00'
    assert 'TOTAL_FEE' in independent_audit(data, bad, road)['errors']
    bad = deepcopy(result);bad['common'].append({'task_id': 'T1', 'pieces': 5, 'fee': '50.00'})
    assert 'COVERAGE' in independent_audit(data, bad, road)['errors']
    bad = deepcopy(result);bad['routes'][0]['segments'][0]['meters'] += 1
    assert 'ROAD_GRAPH_MISMATCH' in independent_audit(data, bad, road)['errors']
    # Common has no arrival or opening-time requirements.
    data['tasks']['T1']['close_at'] = '2026-11-12T00:00:00+08:00'
    common = assemble(data, [], list(data['tasks']), [])
    assert independent_audit(data, common, road)['valid']
    road.close()


def test_runner_uses_same_input_graph_and_cost_precedes_vehicle_utilization(tmp_path, monkeypatch):
    body = problem();source = tmp_path / 'business.db';minimal_database(source, body, measured_edges(body))
    freeze_case(source, tmp_path / 'case', 'G')
    def algorithm_function(spec):
        def run(body, road, budget, seed, cancelled, *, initial_result=None):
            data = normalize(body)
            if spec.name == 'expensive-full-load':
                return assemble(data, [], list(data['tasks']), [])
            routes = [evaluate_route(data, 'V1', ['T1', 'T2', 'T3'], road)]
            result = assemble(data, routes, [], [])
            if spec.name == 'invalid-cheapest':result['total_fee'] = '0.01'
            return result
        return run
    monkeypatch.setattr(Algorithm, 'function', algorithm_function)
    report = run_benchmark(tmp_path / 'case/case.json', [Algorithm('expensive-full-load', 'a:b'), Algorithm('measured-route', 'a:b'), Algorithm('invalid-cheapest', 'a:b')], seeds=(0, 1), budgets=(1,))
    assert report['best']['algorithm'] == 'measured-route'
    assert report['best']['audit']['total_fee'] == '12.00'
    assert report['all_runs_same_input_and_graph']
    assert report['external_calls'] == 0 and not report['source_business_database_written']
    assert [r['status'] for r in report['runs']].count('INVALID') == 2


def test_runner_rejects_a_changed_graph_before_comparing_algorithms(tmp_path):
    body = problem();source = tmp_path / 'business.db';minimal_database(source, body, measured_edges(body))
    freeze_case(source, tmp_path / 'case', 'G')
    with sqlite3.connect(tmp_path / 'case/frozen-roads.db') as connection:
        connection.execute('UPDATE metrics SET meters=meters+1')
        connection.commit()
    with pytest.raises(ValueError, match='road graph changed'):
        run_benchmark(tmp_path / 'case/case.json', [])


def test_saved_baseline_is_repriced_on_same_graph_before_warm_start(tmp_path, monkeypatch):
    body = problem();data = normalize(body)
    old_edges = measured_edges(body)
    for item in old_edges:
        item['meters'] *= 2
        item['evidence_hash'] = content_hash({k: v for k, v in item.items() if k != 'evidence_hash'})
    write_graph(tmp_path / 'old.db', body, old_edges)
    old_road = FrozenEvidenceRoad(tmp_path / 'old.db')
    old_result = assemble(data, [evaluate_route(data, 'V1', ['T1', 'T2', 'T3'], old_road)], [], [])
    assert old_result['total_fee'] == '24.00'
    old_road.close()
    source = tmp_path / 'business.db';minimal_database(source, body, measured_edges(body))
    content = {'result': old_result}
    with sqlite3.connect(source) as connection:
        connection.execute('INSERT INTO plan_versions VALUES (?,?,?,?,?,?)', ('P', 'G', 'S', 1, encode(content), content_hash(content)))
        connection.commit()
    freeze_case(source, tmp_path / 'case', 'G')
    observed = []
    def factory(_):
        def use_initial(body, road, budget, seed, cancelled, *, initial_result=None):
            observed.append(initial_result['total_fee'])
            return initial_result
        return use_initial
    monkeypatch.setattr(Algorithm, 'function', factory)
    report = run_benchmark(tmp_path / 'case/case.json', [Algorithm('initial', 'a:b')], budgets=(1,))
    assert observed == ['12.00']
    assert report['baseline_audit']['total_fee'] == '12.00'
    assert report['runs'][0]['warm_start'] and report['best']['audit']['total_fee'] == '12.00'


def test_runner_detects_graph_mutation_between_runs(tmp_path, monkeypatch):
    body = problem();source = tmp_path / 'business.db';minimal_database(source, body, measured_edges(body))
    freeze_case(source, tmp_path / 'case', 'G')
    def factory(_):
        def mutate(body, road, budget, seed, cancelled, *, initial_result=None):
            result = assemble(normalize(body), [], ['T1', 'T2', 'T3'], [])
            with sqlite3.connect(tmp_path / 'case/frozen-roads.db') as connection:
                connection.execute('UPDATE metrics SET meters=meters+1');connection.commit()
            return result
        return mutate
    monkeypatch.setattr(Algorithm, 'function', factory)
    with pytest.raises(ValueError, match='between algorithms'):
        run_benchmark(tmp_path / 'case/case.json', [Algorithm('changes-graph', 'a:b')], budgets=(1,), seeds=(0, 1))


def test_planned_alias_without_measurement_is_not_reported_as_known_edge(tmp_path):
    body = problem();path = tmp_path / 'roads.db'
    write_graph(path, body, measured_edges(body, [('W', 'T1')]))
    with sqlite3.connect(path) as connection:
        connection.execute('INSERT INTO edges VALUES (?,?,?)', ('T1', 'T2', 'f' * 64));connection.commit()
    road = FrozenEvidenceRoad(path)
    assert road.stats()['known_directed_edges'] == 1
    assert road.stats()['planned_directed_edges'] == 2
    assert road.known_segment('T1', 'T2') is None
    road.close()


def test_historical_replay_maps_every_row_and_preserves_quantities_and_day_offsets(tmp_path):
    body = problem();data = normalize(body);template = deepcopy(body)
    template['stores'] = [{**row, 'dedicated_quote_version': row['quote'],
                            'business_open_at': row['open_at'], 'business_close_at': row['close_at']}
                           for row in data['tasks'].values()]
    template['vehicles'] = [{**row, 'day_warehouse_id': row['warehouse_id'], 'day_available': True} for row in body['vehicles']]
    graph = tmp_path / 'roads.db';write_graph(graph, body, measured_edges(body))
    history = tmp_path / 'history.db'
    with sqlite3.connect(history) as connection:
        connection.executescript('CREATE TABLE stores(id TEXT,name TEXT);CREATE TABLE daily_demands(id TEXT,store_id TEXT,import_id TEXT,pieces INTEGER,source_rows TEXT,delivery_date TEXT);')
        connection.executemany('INSERT INTO stores VALUES (?,?)', [('OLD1', 'T1'), ('OLD2', 'T2'), ('UNMAPPED', 'missing-current-location')])
        connection.executemany('INSERT INTO daily_demands VALUES (?,?,?,?,?,?)',
                               [('D1', 'OLD1', 'I', 8, '[1]', '2026-10-03'),
                                ('D2', 'OLD2', 'I', 2, '[2]', '2026-10-03'),
                                ('D3', 'OLD2', 'J', 3, '[3]', '2026-10-03'),
                                ('D4', 'UNMAPPED', 'I', 5, '[4]', '2026-10-04')])
        connection.commit()
    before = hashlib.sha256(history.read_bytes()).hexdigest()
    case = freeze_historical_case(template, history, '2026-10-03', tmp_path / 'case', road_database=graph)
    actual = json.loads((tmp_path / 'case/input.json').read_text());normalized = normalize(actual)
    assert case['scenario']['source_rows_all_mapped'] and case['scenario']['source_rows_discarded'] == 0
    assert case['scenario']['source_demand_rows'] == 3 and actual['totals']['task_count'] == 2
    assert actual['totals']['total_pieces'] == 13
    assert normalized['tasks']['T1']['pieces'] == 8 and normalized['tasks']['T2']['pieces'] == 5
    assert normalized['tasks']['T1']['open_at'] == '2026-10-03T08:00:00+08:00'
    assert normalized['warehouses']['W']['earliest_departure_at'] == '2026-10-03T08:00:00+08:00'
    assert len(normalized['tasks']['T2']['historical_source_demand_ids']) == 2
    assert hashlib.sha256(history.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match='all map unambiguously'):
        freeze_historical_case(template, history, '2026-10-04', tmp_path / 'missing', road_database=graph)
