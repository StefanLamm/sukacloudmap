from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import gzip
import json
import os
import sqlite3
import time

import pytest

from app.planning_inputs import content_hash, encode
from app.road_graph import ReferenceGraph, SCHEMA
from app.road_service import DrivingRoad, evidence, road_request


def sample(*, age=0, meters=1000, origin=None, destination=None):
    a = origin or {'id': 'W', 'longitude': '117.0', 'latitude': '31.0', 'crs': 'GCJ02'}
    b = destination or {'id': 'S', 'longitude': '117.1', 'latitude': '31.1', 'crs': 'GCJ02'}
    request = road_request({'id': 'V', 'plate': 'TEST1'}, a, b, '2026-11-13T08:00:00+08:00', 'DRIVING', strategy=32)
    item = evidence(request, meters, 60, 'DRIVING', False,
                    [[float(a['longitude']), float(a['latitude'])], [float(b['longitude']), float(b['latitude'])]],
                    'TRUCK_NOT_VERIFIED', time_status='CURRENT_TRAFFIC_REFERENCE', received_at=int(time.time()) - age)
    key = content_hash(DrivingRoad.reference_request(a, b))
    return key, item


def raw_write(path, key, item, *, content=None, overrides=None):
    with sqlite3.connect(path) as connection:
        connection.execute(SCHEMA)
        row = {**item, **(overrides or {})}
        connection.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',
                           (key, 'W', 'S', row['meters'], row['seconds'], row['received_at'], row['evidence_hash'],
                            content if content is not None else gzip.compress(encode(item).encode())))
        connection.commit()


def test_roundtrip_keeps_original_age_and_evidence_hash_with_private_file(tmp_path):
    key, item = sample(age=15)
    path = tmp_path / 'reference.db';graph = ReferenceGraph(path)
    assert not path.exists() and graph.get_metrics(key) is None
    assert graph.put_reference(key, item)
    assert os.stat(path).st_mode & 0o777 == 0o600
    metric = graph.get_metrics(key)
    assert metric['meters'] == item['meters'] and metric['heuristic_only'] and metric['fresh']
    actual = graph.get_recent(key)
    assert actual == item and actual['received_at'] == item['received_at']
    assert graph.get_recent(key, ttl_seconds=0) is None
    graph.close()


def test_expired_graph_metrics_are_hints_and_cannot_be_current_evidence(tmp_path):
    key, item = sample(age=3601)
    path = tmp_path / 'reference.db';raw_write(path, key, item)
    graph = ReferenceGraph(path)
    metric = graph.get_metrics(key)
    assert metric['heuristic_only'] and not metric['fresh']
    assert graph.get_recent(key) is None
    assert not graph.put_reference(key, item)
    graph.close()


def test_full_source_hash_and_canonical_provider_binding_are_required(tmp_path):
    key, item = sample();bad = deepcopy(item);bad['meters'] += 1
    path = tmp_path / 'reference.db';raw_write(path, key, bad)
    graph = ReferenceGraph(path)
    assert graph.get_recent(key) is None and not graph.put_reference(key, bad)
    wrong = 'a' * 64
    assert wrong != key
    assert not graph.put_reference(wrong, item)
    raw_write(path, wrong, item)
    assert graph.get_recent(wrong) is None
    graph.close()


@pytest.mark.parametrize('change', [
    {'source': 'LIVE'}, {'verified': True}, {'time_status': 'TIME_UNVERIFIED'},
    {'meters': True}, {'received_at': 'FUTURE'},
])
def test_nonordinary_or_invalid_reference_is_never_saved(tmp_path, change):
    key, item = sample();item.update({k: int(time.time()) + 100 if value == 'FUTURE' else value for k, value in change.items()})
    item['evidence_hash'] = content_hash({k: value for k, value in item.items() if k != 'evidence_hash'})
    graph = ReferenceGraph(tmp_path / 'reference.db')
    assert not graph.put_reference(key, item)
    assert graph.get_recent(key) is None
    graph.close()


def test_metadata_and_compressed_evidence_must_match(tmp_path):
    key, item = sample();path = tmp_path / 'reference.db'
    raw_write(path, key, item, overrides={'meters': item['meters'] + 1})
    graph = ReferenceGraph(path)
    assert graph.get_recent(key) is None
    # A bad newer metadata row cannot permanently block a correct fresh write.
    assert graph.put_reference(key, item)
    assert graph.get_recent(key) == item
    graph.close()


def test_multiple_instances_keep_newest_valid_reference(tmp_path):
    key, old = sample(age=30, meters=1000);_, new = sample(age=5, meters=1200)
    path = tmp_path / 'reference.db';first = ReferenceGraph(path);second = ReferenceGraph(path)
    assert first.put_reference(key, new)
    assert second.put_reference(key, old)
    assert second.get_recent(key)['meters'] == 1200
    # Returned evidence cannot mutate an instance's LRU contents.
    copied = first.get_recent(key);copied['meters'] = 0;copied['polyline'].clear()
    assert first.get_recent(key) == new
    first.close();second.close()


def test_older_write_updates_both_hint_and_geometry_lru_from_newer_shared_row(tmp_path):
    key, old = sample(age=30, meters=1000);_, new = sample(age=5, meters=1200)
    path = tmp_path / 'reference.db';first = ReferenceGraph(path);second = ReferenceGraph(path)
    assert first.put_reference(key, old) and first.get_recent(key)['meters'] == 1000
    assert second.put_reference(key, new)
    assert first.put_reference(key, old)
    assert first.get_metrics(key)['meters'] == first.get_recent(key)['meters'] == 1200
    first.close();second.close()


def test_schema_corruption_and_locked_writes_fail_without_blocking(tmp_path):
    key, item = sample();path = tmp_path / 'reference.db';graph = ReferenceGraph(path)
    assert graph.put_reference(key, item)
    with sqlite3.connect(path, timeout=.1) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        assert not ReferenceGraph(path).put_reference(key, item)
        assert time.monotonic() - started < .6
        blocker.rollback()
    graph.close()
    corrupt = tmp_path / 'corrupt.db';corrupt.write_bytes(b'not-sqlite')
    broken = ReferenceGraph(corrupt)
    assert broken.get_recent(key) is None and broken.get_metrics(key) is None
    assert not broken.put_reference(key, item)
    unrelated = tmp_path / 'business.db'
    with sqlite3.connect(unrelated) as connection:connection.execute('CREATE TABLE accounts(id TEXT)')
    other = ReferenceGraph(unrelated)
    assert not other.put_reference(key, item)
    with sqlite3.connect(unrelated) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('accounts',)]
    broken.close();other.close()


def test_broken_gzip_and_bounded_lru_are_safe(tmp_path):
    key, item = sample();path = tmp_path / 'reference.db';raw_write(path, key, item, content=b'\x1f\x8bgarbage')
    graph = ReferenceGraph(path, geometry_cache_size=1)
    assert graph.get_recent(key) is None
    assert graph.put_reference(key, item)
    second_key, second_item = sample(destination={'id': 'T', 'longitude': '117.2', 'latitude': '31.1', 'crs': 'GCJ02'})
    assert graph.put_reference(second_key, second_item)
    assert len(graph._geometry) == 1
    assert graph.get_recent(key) == item
    assert len(graph._geometry) == 1
    graph.close()
    assert graph.get_metrics(key) is None and graph.get_recent(key) is None
    assert not graph.put_reference(key, item)


def test_parallel_shared_sidecar_writes_produce_one_valid_latest_row(tmp_path):
    key, item = sample(age=20);path = tmp_path / 'reference.db';seed = ReferenceGraph(path)
    assert seed.put_reference(key, item);seed.close()
    def write(n):
        graph = ReferenceGraph(path)
        _, value = sample(age=19 - n, meters=1000 + n)
        result = graph.put_reference(key, value)
        graph.close()
        return result
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(write, range(8)))
    final = ReferenceGraph(path)
    assert final.get_recent(key)['meters'] == 1007
    final.close()


def test_directory_path_is_not_chmodded_and_oversized_gzip_fails_closed(tmp_path, monkeypatch):
    import app.road_graph as module
    key, item = sample()
    folder = tmp_path / 'directory';folder.mkdir(mode=0o700)
    graph = ReferenceGraph(folder)
    assert not graph.put_reference(key, item)
    assert os.stat(folder).st_mode & 0o777 == 0o700
    graph.close()
    path = tmp_path / 'large.db';raw_write(path, key, item)
    monkeypatch.setattr(module, 'MAX_EVIDENCE_BYTES', 20)
    graph = ReferenceGraph(path)
    assert graph.get_recent(key) is None
    graph.close()
