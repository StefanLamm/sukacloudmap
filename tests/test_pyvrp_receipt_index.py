"""Verify the optional same-job directional scalar index with sealed receipts."""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import time

import pytest

from app import road_service
from app.planning_inputs import content_hash
from app.road_service import DrivingRoad, evidence, road_request
from app.route_pyvrp_tail import _measure
from app.routing_engine import StopSearch


def point(key, lng):
    return {'id': key, 'warehouse_id': 'W', 'longitude': lng, 'latitude': 31., 'crs': 'GCJ02'}


def setup(*, indexed=True):
    road = DrivingRoad.__new__(DrivingRoad)
    road.fresh_only = True
    road.batch_started_at = int(time.time())
    road._batch_receipts = {}
    road._batch_receipt_indexing = None
    road.metric_hints = {}
    road.deep_experiments = SimpleNamespace(pyvrp_indexed_current_receipts=indexed)

    def forbid(*args):
        pytest.fail('Do not call the live driving adapter or use unsigned metric hints.')

    road.segment = road.known_segment = forbid
    warehouse = point('W', 118.)
    tasks = {i: point(i, lng) for i, lng in [('C', 118.02), ('B', 118.01), ('A', 118.01)]}
    return road, {'warehouses': {'W': warehouse}, 'tasks': tasks}


def raw(road, a, b, meters):
    request = road_request({'id': 'old-car'}, a, b, '2026-11-22T08:00:00+08:00', 'DRIVING', strategy=32)
    item = evidence(request, meters, 5, 'DRIVING', polyline=[[a['longitude'], a['latitude']],
        [b['longitude'], b['latitude']]], received_at=road.batch_started_at, time_status='CURRENT_TRAFFIC_REFERENCE')
    provider = DrivingRoad.reference_request(a, b)
    return content_hash(provider), provider, item


def extract(road, data):
    return _measure(road, data, fresh=True, control=lambda: None)


def test_exact_order_bindings_and_missing_heuristic_only_edges_with_coincident_aliases(monkeypatch):
    road, data = setup()
    w = data['warehouses']['W']
    a, b, c = (data['tasks'][i] for i in 'ABC')
    raw_items = [raw(road, a, c, 2), raw(road, c, w, 3), raw(road, a, b, 0),
                 raw(road, w, a, 1), raw(road, point('outside1', 118.03), point('outside2', 118.04), 4)]
    for item in raw_items:
        road._seal_batch_receipt(*item)
    road.metric_hints[content_hash(DrivingRoad.reference_request(c, a))] = {'meters': 1, 'seconds': 1,
                                                                          'heuristic_only': True}
    decompressed = []
    original = road.batch_receipt

    def count(key):
        decompressed.append(key)
        return original(key)

    monkeypatch.setattr(road, 'batch_receipt', count)
    indexed, rows = extract(road, data)
    assert indexed.indexed_current_receipts and not decompressed
    assert rows == [(None, 'A', 1, 5), (None, 'B', 1, 5), ('A', 'B', 0, 5),
                    ('A', 'C', 2, 5), ('B', 'A', 0, 5), ('B', 'C', 2, 5)]
    assert indexed.known_segment(c, a) is None
    road.deep_experiments.pyvrp_indexed_current_receipts = False
    ordinary, old_rows = extract(road, data)
    assert rows == old_rows and ordinary.metrics == indexed.metrics
    assert decompressed and not ordinary.indexed_current_receipts
    for origin, target in [(w, b), (b, a), (b, c)]:
        vehicle, departure = {'id': 'different-car'}, '2026-11-22T09:15:00+08:00'
        new = indexed.segment(vehicle, origin, target, departure)
        assert new == ordinary.segment(vehicle, origin, target, departure)
        assert new['request']['vehicle_id'] == vehicle['id'] and new['request']['to_id'] == target['id']
        assert new['request']['departure_at'] == departure
    assert [road.batch_receipt(item[0]) for item in raw_items] == [item[2] for item in raw_items]


def test_signed_zero_provider_request_keys_stay_distinct():
    road, data = setup()
    data['tasks']['A']['longitude'] = 0.0
    data['tasks']['B']['longitude'] = -0.0
    w, b = data['warehouses']['W'], data['tasks']['B']
    road._seal_batch_receipt(*raw(road, w, b, 1))
    indexed, rows = extract(road, data)
    assert indexed.indexed_current_receipts and rows == [(None, 'B', 1, 5)]
    road.deep_experiments.pyvrp_indexed_current_receipts = False
    assert extract(road, data)[1] == rows


@pytest.mark.parametrize('late', [False, True])
def test_partial_index_or_late_opt_in_falls_back_without_dropping_measured_edges(late):
    road, data = setup(indexed=not late)
    legacy_road, _ = setup(indexed=False)
    w, a, c = data['warehouses']['W'], data['tasks']['A'], data['tasks']['C']
    first, later = raw(road, w, a, 1), raw(road, a, c, 2)
    road._seal_batch_receipt(*first)
    road.deep_experiments.pyvrp_indexed_current_receipts = True
    if late:
        road._seal_batch_receipt(*later)
        assert all(isinstance(value, bytes) for value in road._batch_receipts.values())
    else:
        legacy_road._seal_batch_receipt(*later)
        key = later[0]
        road._batch_receipts[key] = legacy_road._batch_receipts[key]
    assert road.indexed_batch_receipts(lambda: None) is None
    measured, rows = extract(road, data)
    assert not measured.indexed_current_receipts
    assert rows == [(None, 'A', 1, 5), (None, 'B', 1, 5), ('A', 'C', 2, 5), ('B', 'C', 2, 5)]


def test_shared_seal_is_atomic_on_compression_failure_and_immutable_afterward(monkeypatch):
    road, data = setup()
    item = raw(road, data['warehouses']['W'], data['tasks']['A'], 37)

    def fail(*args, **kwargs):
        raise ValueError('compression failed')

    with monkeypatch.context() as patch:
        patch.setattr(road_service.zlib, 'compress', fail)
        with pytest.raises(ValueError, match='compression failed'):
            road._seal_batch_receipt(*item)
    assert not road._batch_receipts and road._batch_receipt_indexing is None
    road._seal_batch_receipt(*item)
    key, metric = road.indexed_batch_receipts(lambda: None)[0]
    assert key == item[0] and metric.evidence_hash == item[2]['evidence_hash']
    assert road.batch_receipt(key) == item[2]
    with pytest.raises(FrozenInstanceError):
        metric.meters = 0


def test_index_scan_uses_the_same_hard_control():
    road, data = setup()
    road._seal_batch_receipt(*raw(road, data['warehouses']['W'], data['tasks']['A'], 37))

    def stop():
        raise StopSearch('WALL_BUDGET')

    with pytest.raises(StopSearch) as caught:
        _measure(road, data, fresh=True, control=stop)
    assert caught.value.reason == 'WALL_BUDGET'
