"""Offline evidence and a source version's production search policy are distinct."""
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.routing_engine import RoadError, StopSearch, assemble, normalize
from app.route_search import JointSearch
from app.search_policy import production_search_control
from scripts.benchmark_algorithms import FrozenEvidenceRoad
import scripts.evaluate_delivery_stages as study
import scripts.evaluate_controlled_general as controlled
from test_delivery_stage_evaluation import frozen_case, valid_row
from test_algorithm_benchmark import problem


def test_policy_is_explicit_and_research_cli_default_is_unchanged():
    assert study.parser().parse_args([]).production_search_policy is False
    assert study.parser().parse_args(['--production-search-policy']).production_search_policy is True
    research = study.road_execution_policy(False)
    production = study.road_execution_policy(True)
    assert research['search_policy'] == 'RESEARCH_DEFAULT'
    assert production['search_policy'] == 'VERSION_NATIVE_PRODUCTION'
    assert production['production_planning'] and not production['http_enabled']
    assert production['wall_deadline_policy'] == 'WORKER_LAUNCH_PLUS_WAIT_SECONDS'
    assert production['unknown_edges'] == 'REJECT_NO_ESTIMATION_OR_COLLECTION'
    for bad in (None, 1, 'false'):
        with pytest.raises(ValueError, match='boolean'):
            study.road_execution_policy(bad)


def test_research_application_adds_no_production_attributes(tmp_path):
    case_path, _ = frozen_case(tmp_path)
    road = FrozenEvidenceRoad(study.load(case_path)['road_database'])
    before = dict(vars(road))
    try:
        policy = study.apply_road_execution_policy(road, False, wall_deadline=None, cancelled=lambda: False)
        assert policy == study.road_execution_policy(False)
        assert vars(road) == before
    finally:
        road.close()


def test_production_marker_does_not_enable_http_change_metrics_hints_or_make_graph_writable(tmp_path):
    case_path, body = frozen_case(tmp_path)
    graph = Path(study.load(case_path)['road_database'])
    before_file = hashlib.sha256(graph.read_bytes()).hexdigest()
    road = FrozenEvidenceRoad(graph)
    road.settings = SimpleNamespace(planning_optimizer_profile='HYBRID', road_calls_enabled=True)
    metric = deepcopy(road.known_segment('W', 'T1'))
    hints = deepcopy(road.route_seed_samples)
    try:
        study.apply_road_execution_policy(road, True, wall_deadline=study.time.monotonic() + 10, cancelled=lambda: False)
        assert road.production_planning is True and road.timer is study.time.monotonic
        assert road.settings.road_calls_enabled is False
        assert not hasattr(road, 'call_limit')  # Do not introduce a zero-allowance search branch.
        assert road.stats()['call_limit'] == 0 and road.stats()['calls'] == 0
        assert road.known_segment('W', 'T1') == metric and road.route_seed_samples == hints
        assert road.known_segment('T1', 'W') is None
        data = normalize(body)
        with pytest.raises(RoadError) as error:
            road.segment(data['vehicles']['V1'], data['tasks']['T1'], data['warehouses']['W'], '2026-11-13T08:00:00+08:00')
        assert error.value.code == 'ROAD_EDGE_NOT_FROZEN'
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            road.connection.execute("INSERT INTO metadata VALUES ('policy-write-probe','true')")
        assert not hasattr(road, 'transport') and not hasattr(road, 'client')
    finally:
        road.close()
    assert hashlib.sha256(graph.read_bytes()).hexdigest() == before_file


def test_marker_uses_native_search_count_rule_while_absolute_wall_and_cancel_still_stop(tmp_path, monkeypatch):
    case_path, body = frozen_case(tmp_path)
    now = [100.]
    monkeypatch.setattr(study.time, 'monotonic', lambda: now[0])
    roads = [FrozenEvidenceRoad(study.load(case_path)['road_database']) for _ in range(2)]
    cancelled = [False]
    try:
        research = JointSearch(normalize(body), roads[0], 20, 0, lambda: False)
        research.evaluations = research.max_evaluations
        with pytest.raises(StopSearch, match='CANDIDATE_BUDGET'):
            research.control()
        study.apply_road_execution_policy(roads[1], True, wall_deadline=110., cancelled=lambda: cancelled[0])
        production = JointSearch(normalize(body), roads[1], 20, 0, lambda: cancelled[0])
        production.evaluations = production.max_evaluations
        production.control()  # No mutation of max_evaluations or source functions.
        assert production.max_evaluations == research.max_evaluations
        bounded = production_search_control(roads[1])
        bounded()
        cancelled[0] = True
        with pytest.raises(StopSearch, match='CANCELLED'):
            bounded()
        cancelled[0] = False
        now[0] = 110.
        with pytest.raises(StopSearch, match='WALL_BUDGET'):
            production.control()
        with pytest.raises(StopSearch, match='WALL_BUDGET'):
            bounded()
    finally:
        for road in roads:
            road.close()


def test_pairs_reject_mixed_or_missing_policy_and_groups_do_not_pool_them():
    old, new = valid_row('baseline', '100.00'), valid_row('candidate', '99.00')
    research_pair = study.pair_summary(old, new)
    new.update(production_search_policy=True, road_execution_policy=study.road_execution_policy(True))
    with pytest.raises(study.IntegrityError, match='unequal road'):
        study.pair_summary(old, new)
    old.update(production_search_policy=True, road_execution_policy=study.road_execution_policy(True))
    production_pair = study.pair_summary(old, new)
    assert len(study.summarize([research_pair, production_pair], [])['groups']) == 2
    del new['road_execution_policy']
    with pytest.raises(study.IntegrityError, match='Missing'):
        study.pair_summary(old, new)


@pytest.mark.parametrize('side', ['baseline', 'candidate'])
def test_worker_passes_production_contract_to_each_pinned_version_without_altering_source(tmp_path, monkeypatch, side):
    case_path, body = frozen_case(tmp_path)
    now = [100.]
    monkeypatch.setattr(study.time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    seen = []
    # Old entrypoints need no new keyword: the caller contract lives on road.
    def optimizer(body, road, budget, seed, cancelled, *, initial_result=None):
        seen.append(road.production_planning)
        assert road.wall_deadline == 110. and road.timer is study.time.monotonic
        assert road.settings.road_calls_enabled is False and not hasattr(road, 'call_limit')
        assert not cancelled()
        return assemble(normalize(body), [], [task['id'] for task in body['tasks']], [])
    real_import = importlib.import_module
    monkeypatch.setattr(study.importlib, 'import_module', lambda name, *args, **kw:
        SimpleNamespace(optimizer=optimizer) if name == 'production_policy_probe' else real_import(name, *args, **kw))
    folder = tmp_path / 'run';folder.mkdir()
    spec = {'source_root': str(study.ROOT), 'source_hash': study.source_manifest(study.ROOT)['sha256'],
        'case_file': str(case_path), 'identity': study.case_identity(case_path), 'side': side, 'profile_name': 'policy',
        'compute_seconds': 1, 'wait_seconds': 10, 'seed': 0, 'hash_seed': 0,
        'entrypoint': 'production_policy_probe:optimizer', 'options': {}, 'run_folder': str(folder),
        'production_search_policy': True, 'road_execution_policy': study.road_execution_policy(True)}
    study.save(folder / 'spec.json', spec)
    row = study.worker(folder / 'spec.json')
    assert row['status'] == 'RETURNED' and seen == [True]
    assert row['production_search_policy'] is True and row['road_production_planning'] is True
    assert row['road_execution_policy'] == spec['road_execution_policy']
    assert row['road_wall_deadline_monotonic'] == row['worker_launched_monotonic'] + spec['wait_seconds']
    assert row['road_timer'] == 'time.monotonic' and row['road_stats']['calls'] == 0
    assert row['source_unchanged']


def test_controlled_formula_worker_reuses_the_same_policy_and_cli_option(tmp_path, monkeypatch):
    body = problem()
    body['road_fixture'] = {'formula_locked': True, 'scope': 'SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE', 'rule': controlled.FORMULA}
    path = tmp_path / 'input.json';study.save(path, body)
    road = controlled.FormulaRoad(body)
    case = {'name': 'formula-policy', 'input_file': str(path), 'input_hash': controlled.digest(body),
            'road_graph': None, 'graph_hash': road.graph_hash, 'road_scope': 'SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE'}
    policy = controlled.controlled_execution_policy(True, case)
    assert policy == study.road_execution_policy(True, adapter='FormulaRoad', evidence_scope=case['road_scope'])
    now = [200.]
    monkeypatch.setattr(study.time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    def optimizer(body, road, budget, seed, cancelled, *, initial_result=None, wall_budget_seconds=None):
        assert road.production_planning and road.wall_deadline == 210. and wall_budget_seconds == 10.
        assert road.timer is study.time.monotonic and road.settings.road_calls_enabled is False
        return assemble(normalize(body), [], [task['id'] for task in body['tasks']], [])
    real_import = importlib.import_module
    monkeypatch.setattr(study.importlib, 'import_module', lambda name, *args, **kw:
        SimpleNamespace(optimizer=optimizer) if name == 'formula_policy_probe' else real_import(name, *args, **kw))
    spec = {'source': controlled.source_identity(controlled.ROOT), 'case': case, 'side': 'candidate',
            'compute_seconds': 1, 'wait_seconds': 10, 'seed': 0, 'hash_seed': 0,
            'entrypoint': 'formula_policy_probe:optimizer', 'options': {}, 'production_search_policy': True,
            'road_execution_policy': policy}
    study.save(tmp_path / 'spec.json', spec)
    controlled.worker(tmp_path / 'spec.json')
    row = study.load(tmp_path / 'worker.json')
    assert row['status'] == 'RETURNED' and row['road_execution_policy'] == policy
    assert row['road_production_planning'] and row['road_wall_deadline_monotonic'] == 210.
    captured = []
    monkeypatch.setattr(controlled, 'run', lambda *args, **kwargs: captured.append(kwargs) or
        {'finished': True, 'runs': [], 'all_failures': [], 'certificate_failures': []})
    controlled.main(['run', '--prepared', 'input', '--output', 'out', '--baseline-root', 'old',
        '--candidate-root', 'new', '--profile', 'profile', '--compute-seconds', '1', '--wait-seconds', '10',
        '--production-search-policy'])
    assert captured[0]['production_search_policy'] is True
