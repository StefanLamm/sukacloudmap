"""Boundary regressions discovered by independent portfolio/evidence review."""
from copy import deepcopy
import gzip
from types import SimpleNamespace

import pytest

from app.planning_inputs import content_hash, encode
from app.road_graph import ReferenceGraph
from app.route_portfolio import _phase, optimize_alns
from app.route_search import JointSearch
from app.route_selection import select_candidate_routes
from app.routing_engine import RoadError, StopSearch, check_result, normalize
from test_road_graph import raw_write, sample
from test_route_neighborhood import problem


def test_phase_budget_is_visible_to_nested_selector_remaining_time():
    search = SimpleNamespace(control=lambda: None, remaining=lambda: 1000.0, compute_elapsed=lambda: 0.0,
                             best={'total_fee': '0.00', 'unassigned': []},
                             best_routes=[], pool={})
    original_control, original_remaining = search.control, search.remaining
    captured = []
    _phase(search, 'bounded_selector', 2, lambda: captured.append(search.remaining()))
    assert 0 <= captured[0] <= 2
    assert search.control == original_control and search.remaining == original_remaining


def test_phase_cancellation_restores_nested_control_and_remaining():
    cancelled = [False]
    def control():
        if cancelled[0]:
            raise StopSearch('CANCELLED')
    search = SimpleNamespace(control=control, remaining=lambda: 1000.0, compute_elapsed=lambda: 0.0,
                             best={'total_fee': '0.00', 'unassigned': []},
                             best_routes=[], pool={})
    original_control, original_remaining = search.control, search.remaining
    def action():
        cancelled[0] = True
        search.control()
    with pytest.raises(StopSearch, match='CANCELLED'):
        _phase(search, 'cancelled_phase', 2, action)
    assert search.control == original_control and search.remaining == original_remaining


def test_corrupt_deflate_block_is_an_optional_graph_miss_not_an_exception(tmp_path):
    key, item = sample()
    compressed = bytearray(gzip.compress(encode(item).encode()))
    # A syntactically valid GZIP header followed by the reserved DEFLATE block
    # type raises zlib.error, not gzip.BadGzipFile or EOFError.
    compressed[10] = compressed[10] & ~7 | 7
    path = tmp_path / 'bad-deflate.db'
    raw_write(path, key, item, content=bytes(compressed))
    graph = ReferenceGraph(path)
    try:
        assert graph.get_recent(key) is None
        assert graph.put_reference(key, item)
        assert graph.get_recent(key) == item
    finally:
        graph.close()


@pytest.mark.parametrize('bad_request', [[], 'invalid', 123])
def test_coherently_hashed_but_malformed_request_is_rejected_without_crash(tmp_path, bad_request):
    key, item = sample()
    malformed = deepcopy(item)
    malformed['request'] = bad_request
    malformed['evidence_hash'] = content_hash({k: v for k, v in malformed.items() if k != 'evidence_hash'})
    path = tmp_path / 'bad-request.db'
    raw_write(path, key, malformed)
    graph = ReferenceGraph(path)
    try:
        assert graph.get_recent(key) is None
        assert not graph.put_reference(key, malformed)
    finally:
        graph.close()


@pytest.mark.parametrize('point_key', ['origin', 'destination'])
def test_coherently_hashed_but_nonmapping_coordinate_object_is_an_optional_miss(tmp_path, point_key):
    key, malformed = sample()
    malformed['request'][point_key] = []
    malformed['evidence_hash'] = content_hash({k: v for k, v in malformed.items() if k != 'evidence_hash'})
    path = tmp_path / 'bad-point.db'
    raw_write(path, key, malformed)
    graph = ReferenceGraph(path)
    try:
        assert graph.get_recent(key) is None
        assert not graph.put_reference(key, malformed)
    finally:
        graph.close()


def test_zero_call_domain_with_no_known_warehouse_edges_does_not_spin_24000_seconds(monkeypatch):
    body, _ = problem()
    class MissingRoad:
        call_limit = 0
        def known_segment(self, a, b):
            return None
        def segment(self, *args):
            raise RoadError('ROAD_CALL_BUDGET', 'No measured edge and no HTTP allowance')
        def stats(self):
            return {'calls': 0, 'call_limit': 0}
    original = JointSearch.control
    controls = [0]
    def guarded(self):
        controls[0] += 1
        if controls[0] > 10000:
            raise StopSearch('REVIEW_NONSPIN_GUARD')
        return original(self)
    monkeypatch.setattr(JointSearch, 'control', guarded)
    result = optimize_alns(body, MissingRoad(), 24000)
    assert result['algorithm']['stop_reason'] != 'REVIEW_NONSPIN_GUARD'
    assert controls[0] <= 10000
    assert not result['routes'] and len(result['common']) == 9 and not result['unassigned']
    assert not result['algorithm']['optimality_proven']


def test_rebinding_specs_unlocks_disjoint_routes_without_cloning_evidence():
    body, road = problem(n=4, cars=2, capacity=2, common='100')
    for (a, b) in road.costs:
        road.costs[a, b] = 10000 if a == 'W' else 0 if int(a) // 2 == int(b) // 2 else 100000
    data = normalize(body)
    search = JointSearch(data, road, 30, 4, lambda: False)
    search.setup_fleet()
    search.evaluate('V0', ['0', '1'])
    search.evaluate('V0', ['2', '3'])
    selected, diagnostic = select_candidate_routes(data, list(search.pool.values()), [], 2)
    assert diagnostic['total_fee'] == '210.00' and len(selected) == 1
    # Transfer only the ordered task specification. The host re-evaluates V1;
    # geometry and its evidence hash are never copied or relabelled from V0.
    search.evaluate('V1', ['2', '3'])
    selected, diagnostic = select_candidate_routes(data, list(search.pool.values()), [], 2)
    assert diagnostic['total_fee'] == '20.00' and len(selected) == 2
    assert search.consider(selected)
    assert check_result(data, search.best)['valid']
    rebound = next(r for r in search.best['routes'] if r['vehicle_id'] == 'V1')
    assert all(s['request']['vehicle_id'] == 'V1' for s in rebound['segments'])


def test_failed_full_materialization_reports_road_unknown_and_preserves_incumbent():
    body, road = problem(n=3, cars=1)
    def unavailable(*args):
        raise RoadError('ROAD_CALL_BUDGET', 'Full evidence cannot be refreshed')
    road.segment = unavailable
    search = JointSearch(normalize(body), road, 30, 4, lambda: False)
    before = deepcopy(search.best)
    candidate = search.evaluate('V0', ['0', '1'])
    assert candidate is not None and candidate.get('_estimated')
    assert not search.consider([candidate])
    assert search.best == before
    assert search.failures == search.network_failures == 1
    assert all('Full evidence' in search.reasons[i] for i in ['0', '1'])
