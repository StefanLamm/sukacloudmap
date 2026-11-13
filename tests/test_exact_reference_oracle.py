from copy import deepcopy

import pytest

from app.routing_engine import assemble, evaluate_route, normalize
from scripts.benchmark_algorithms import FrozenEvidenceRoad, write_graph
from scripts.exact_reference_oracle import exact_reference_oracle
from test_algorithm_benchmark import measured_edges, problem


def test_oracle_exhausts_routes_and_all_vehicle_common_allocations(tmp_path):
    body = problem();write_graph(tmp_path / 'roads.db', body, measured_edges(body))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db')
    result, proof = exact_reference_oracle(body, road)
    assert result['total_fee'] == '12.00' and not result['common'] and not result['unassigned']
    assert proof['audit']['valid'] and proof['frozen_model_optimality_proven']
    assert proof['enumerated_order_prefixes'] == 15 and proof['unique_routes'] == 7
    assert proof['physical_vehicle_columns'] == 14 and proof['allocation_masks'] == 8
    assert not proof['real_world_global_optimality_proven']
    assert not result['algorithm']['optimality_proven']
    assert proof['external_calls'] == 0
    road.close()


def test_physical_capacity_cannot_be_reused_or_split(tmp_path):
    body = problem()
    for row in body['vehicles']:row['max_pieces'] = 10
    write_graph(tmp_path / 'roads.db', body, measured_edges(body))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db')
    result, proof = exact_reference_oracle(body, road)
    assert result['total_fee'] == '21.00'
    assert len(result['routes']) == 2 and len({r['vehicle_id'] for r in result['routes']}) == 2
    assert all(r['pieces'] <= 10 for r in result['routes'])
    assert proof['frozen_model_optimality_proven']
    road.close()


def test_unknown_relevant_edges_prevent_optimum_claim(tmp_path):
    body = problem();write_graph(tmp_path / 'roads.db', body, measured_edges(body, [('W', 'T1')]))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db')
    result, proof = exact_reference_oracle(body, road)
    assert proof['audit']['valid'] and not result['unassigned']
    assert proof['unknown_relevant_edges'] > 0 and not proof['frozen_model_optimality_proven']
    road.close()


def test_every_task_retained_when_model_proves_full_delivery_infeasible(tmp_path):
    body = problem();body['vehicles'] = [{**body['vehicles'][0], 'max_pieces': 10}]
    for task in body['tasks']:task['common_unit_price'] = None
    write_graph(tmp_path / 'roads.db', body, measured_edges(body))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db')
    result, proof = exact_reference_oracle(body, road)
    assert len(result['unassigned']) == 1
    assert len(result['routes'][0]['task_ids']) + len(result['unassigned']) == 3
    assert proof['frozen_model_delivery_infeasible_proven']
    assert not proof['frozen_model_optimality_proven']
    road.close()


@pytest.mark.parametrize('kwargs,reason', [({'budget_seconds': 0}, 'TIME_BUDGET'), ({'cancelled': lambda: True}, 'CANCELLED')])
def test_stopped_oracle_retains_valid_warm_start_without_claim(kwargs, reason, tmp_path):
    body = problem();write_graph(tmp_path / 'roads.db', body, measured_edges(body))
    road = FrozenEvidenceRoad(tmp_path / 'roads.db');data = normalize(body)
    initial = assemble(data, [evaluate_route(data, 'V1', ['T1', 'T2', 'T3'], road)], [], [])
    result, proof = exact_reference_oracle(body, road, initial_result=deepcopy(initial), **kwargs)
    assert result['total_fee'] == '12.00' and proof['audit']['valid']
    assert proof['stop_reason'] == reason and not proof['frozen_model_optimality_proven']
    road.close()
