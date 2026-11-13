"""Small compatible groups still get exhaustive columns inside large days."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.route_search import JointSearch
from app.route_small_groups import seed_small_groups
from app.routing_engine import StopSearch, check_result, normalize
from test_solver_oracle import oracle, scenario


@pytest.mark.parametrize('seed', range(40))
def test_small_groups_match_independent_allocation_oracle(seed):
    body, road, roads = scenario(seed + 200)
    # The measured-metric API is vehicle independent. Preserve distinct
    # capacities/availability but bind each car's fixture to that same matrix.
    metrics = {}
    for (_, a, b), pair in roads.items():
        metrics.setdefault((a, b), pair)
    for (vid, a, b) in roads:
        roads[vid, a, b] = metrics[a, b]
    road.known_segment = lambda a, b: dict(zip(('meters', 'seconds'), metrics[a['id'], b['id']]))
    expected = oracle(body, roads)
    data = normalize(body)
    search = JointSearch(data, road, 10, seed, lambda: False)
    search.setup_fleet()
    diagnostics = seed_small_groups(search)
    actual = search.best
    assert (len(actual['unassigned']), int(Decimal(actual['total_fee']) * 100)) == expected
    assert check_result(data, actual)['valid'] == (expected[0] == 0)
    assert diagnostics['completed_groups'] == diagnostics['eligible_groups']
    assert not diagnostics['global_optimality_proven']


def test_large_day_retains_small_group_full_order_and_capacity_alternatives():
    body, road, roads = scenario(201)
    template = deepcopy(body['tasks'][0])
    body['tasks'] = [{**deepcopy(template), 'id': str(i), 'store_id': str(i),
                      'pieces': 1, 'quote_id': 'K', 'warehouse_id': 'W',
                      'common_unit_price': '100', 'unload_minutes': 0,
                      'latest_arrival_at': None} for i in range(5)]
    body['tasks'] += [{**deepcopy(template), 'id': 'C' + str(i), 'store_id': 'C' + str(i),
                       'pieces': 1, 'quote_id': None, 'common_unit_price': '1'} for i in range(4)]
    body['vehicles'] = [{'id': 'V' + str(i), 'warehouse_id': 'W', 'available': True,
                         'max_pieces': 5 if i == 0 else 3} for i in range(2)]
    body['quotes']['K'] = {'mode': 'KM', 'per_km': '1'}
    order = ['2', '0', '4', '1', '3']
    good = set(zip(['W'] + order, order))
    from app.road_service import evidence, road_request
    road.known_segment = lambda a, b: {'meters': 1000 if (a['id'], b['id']) in good else 10000, 'seconds': 60}
    road.segment = lambda v, a, b, at: evidence(road_request(v, a, b, at, 'FIXTURE'), **road.known_segment(a, b), source='FIXTURE')
    data = normalize(body)
    search = JointSearch(data, road, 10, 0, lambda: False)
    search.setup_fleet()
    seed_small_groups(search)
    assert search.best['total_fee'] == '9.00'
    assert len(search.best['routes']) == 1
    assert search.best['routes'][0]['task_ids'] == order
    assert search.best['routes'][0]['vehicle_id'] == 'V0'
    assert check_result(data, search.best)['valid']


def test_missing_metrics_do_not_trigger_network_or_invent_a_cost():
    body, road, _ = scenario(201)
    road.known_segment = lambda a, b: None
    def forbidden(*args):
        raise AssertionError('Small-group candidate generation must not fetch roads')
    road.segment = forbidden
    search = JointSearch(normalize(body), road, 10, 0, lambda: False)
    search.setup_fleet()
    before = deepcopy(search.best)
    diagnostics = seed_small_groups(search)
    assert search.best == before
    assert diagnostics['pruned_missing_prefixes'] > 0
    assert diagnostics['orders_checked'] == 0


def test_small_group_enumeration_obeys_cancel_and_budget():
    body, road, _ = scenario(201)
    road.known_segment = lambda a, b: {'meters': 1000, 'seconds': 60}
    for seconds, cancelled, reason in [(0, lambda: False, 'TIME_BUDGET'), (10, lambda: True, 'CANCELLED')]:
        search = JointSearch(normalize(body), road, seconds, 0, cancelled)
        before = deepcopy(search.best)
        with pytest.raises(StopSearch) as exc:
            seed_small_groups(search)
        assert exc.value.reason == reason
        assert search.best == before


def sparse_problem(count):
    body, road, _ = scenario(201)
    template = body['tasks'][0]
    body['tasks'] = [{**deepcopy(template), 'id': str(i), 'store_id': str(i),
                      'warehouse_id': 'W', 'quote_id': 'K', 'pieces': 1,
                      'common_unit_price': None, 'latest_arrival_at': None,
                      'unload_minutes': 0} for i in range(count)]
    body['vehicles'] = [{'id': 'V', 'warehouse_id': 'W', 'max_pieces': count, 'available': True}]
    body['quotes']['K'] = {'mode': 'KM', 'per_km': '1'}
    edges = set(zip(['W'] + [str(i) for i in range(count - 1)], [str(i) for i in range(count)]))
    lookups = [0]
    def lookup(a, b):
        lookups[0] += 1
        return {'meters': 1000, 'seconds': 60} if (a['id'], b['id']) in edges else None
    from app.road_service import evidence, road_request
    road.known_segment = lookup
    road.segment = lambda v, a, b, at: evidence(road_request(v, a, b, at, 'FIXTURE'), **lookup(a, b), source='FIXTURE')
    return body, road, lookups


def test_seven_stop_sparse_graph_does_not_scan_factorial_missing_orders():
    body, road, lookups = sparse_problem(7)
    search = JointSearch(normalize(body), road, 10, 0, lambda: False)
    search.setup_fleet()
    diagnostics = seed_small_groups(search)
    assert not search.best['unassigned'] and search.best['total_fee'] == '7.00'
    assert diagnostics['orders_checked'] == 7
    # Seven known prefixes suffice, rather than thousands of impossible orders.
    assert lookups[0] < 150
    assert diagnostics['pruned_missing_prefixes'] > 0


def test_uncertain_regional_prefix_can_be_resolved_by_a_later_stop():
    body, road, _ = sparse_problem(2)
    body['quotes']['K'] = {'mode': 'KM_REGION', 'per_km': '1', 'region_surcharge': {
        'amount': '10', 'billing_mode': 'ONCE', 'targets': [{
            'province_code': '320000', 'province_name': '江苏省',
            'city_code': '320800', 'city_name': '淮安市',
            'district_code': '320803', 'district_name': '淮安区'}]}}
    body['tasks'][1].update(province='江苏省', city='淮安市', district='淮安区', region_code='320803')
    search = JointSearch(normalize(body), road, 10, 0, lambda: False)
    search.setup_fleet()
    seed_small_groups(search)
    assert search.best['total_fee'] == '12.00'
    assert search.best['routes'][0]['task_ids'] == ['0', '1']
    assert not search.best['unassigned'] and check_result(search.data, search.best)['valid']


def city_problem(counts=(5, 2, 3), cars=3):
    """A compatible ten-store day with three measured city chains."""
    body, road, _ = sparse_problem(sum(counts))
    body['vehicles'] = [{'id': 'V' + str(i), 'warehouse_id': 'W', 'max_pieces': max(counts),
                         'available': True} for i in range(cars)]
    edges = set()
    offset = 0
    for city, count in enumerate(counts):
        ids = [str(i) for i in range(offset, offset + count)]
        edges.update(zip(['W'] + ids[:-1], ids))
        for item in body['tasks'][offset:offset + count]:
            item.update(province='省A', city='城市' + str(city), common_unit_price='100')
        offset += count
    def lookup(a, b):
        return {'meters': 1000, 'seconds': 60} if (a['id'], b['id']) in edges else None
    from app.road_service import evidence, road_request
    road.known_segment = lookup
    road.segment = lambda v, a, b, at: evidence(road_request(v, a, b, at, 'FIXTURE'), **lookup(a, b), source='FIXTURE')
    return body, road


def solve_small_groups(body, road):
    search = JointSearch(normalize(body), road, 10, 0, lambda: False)
    search.setup_fleet()
    return search, seed_small_groups(search)


def test_ten_compatible_stores_get_five_two_three_city_subgroup_candidates():
    body, road = city_problem()
    # Whitespace/full-width normalization must not fragment an explicit city.
    body['tasks'][0].update(province=' 省Ａ ', city=' 城市０ ')
    search, diagnostics = solve_small_groups(body, road)
    assert search.best['total_fee'] == '10.00' and not search.best['common']
    assert len(search.best['routes']) == 3
    assert any(r['task_ids'] == ['0', '1', '2', '3', '4'] for r in search.best['routes'])
    assert len({r['vehicle_id'] for r in search.best['routes']}) == 3
    assert diagnostics['compatible_groups_seen'] == diagnostics['large_compatible_groups'] == 1
    assert diagnostics['full_small_groups'] == 0 and diagnostics['city_subgroups'] == 3
    assert diagnostics['completed_groups'] == diagnostics['eligible_groups'] == 3
    assert diagnostics['city_scope_is_candidate_only']
    assert check_result(search.data, search.best)['valid']


@pytest.mark.parametrize('unknown_field', ['city', 'province'])
def test_unknown_city_or_province_does_not_create_a_shared_unknown_group(unknown_field):
    body, road = city_problem((5, 5), cars=2)
    for row, unknown in zip(body['tasks'][5:], [None, '', '  ', '未知', 'unknown']):
        row[unknown_field] = unknown
    search, diagnostics = solve_small_groups(body, road)
    assert diagnostics['city_subgroups'] == 1 and diagnostics['unclassified_city_tasks'] == 5
    assert search.best['total_fee'] == '505.00'
    assert {r['task_id'] for r in search.best['common']} == {str(i) for i in range(5, 10)}
    assert len(search.best['routes']) == 1


def test_identical_city_names_in_different_provinces_are_separate_scopes():
    body, road = city_problem((3, 3, 4))
    for row in body['tasks'][:6]:
        row['city'] = '同名城市'
    for row in body['tasks'][3:6]:
        row['province'] = '省B'
    for row in body['tasks'][6:]:
        row['city'] = ''
    search, diagnostics = solve_small_groups(body, road)
    assert diagnostics['city_subgroups'] == 2 and diagnostics['unclassified_city_tasks'] == 4
    assert {tuple(r['task_ids']) for r in search.best['routes']} == {('0', '1', '2'), ('3', '4', '5')}


def test_small_complete_group_remains_cross_city_and_is_not_enumerated_twice():
    body, road, _ = sparse_problem(6)
    for n, task in enumerate(body['tasks']):
        task.update(province='省A', city='城市' + str(n // 3))
    search, diagnostics = solve_small_groups(body, road)
    assert search.best['total_fee'] == '6.00' and not search.best['unassigned']
    assert search.best['routes'][0]['task_ids'] == [str(i) for i in range(6)]
    assert diagnostics['full_small_groups'] == 1 and diagnostics['city_subgroups'] == 0
    assert diagnostics['completed_groups'] == diagnostics['eligible_groups'] == 1


def test_city_subgroups_compete_for_the_same_physical_vehicle_globally():
    body, road = city_problem(cars=1)
    search, diagnostics = solve_small_groups(body, road)
    assert diagnostics['city_subgroups'] == 3
    assert search.best['total_fee'] == '505.00'
    assert len(search.best['routes']) == 1 and search.best['routes'][0]['vehicle_id'] == 'V0'
    assert search.best['routes'][0]['task_ids'] == ['0', '1', '2', '3', '4']
    assert len(search.best['common']) == 5 and check_result(search.data, search.best)['valid']


def test_oversized_city_and_singletons_do_not_expand_the_bounded_scope():
    body, road = city_problem((8, 1, 2), cars=3)
    _, diagnostics = solve_small_groups(body, road)
    assert diagnostics['city_subgroups'] == 1 and diagnostics['city_subgroups_over_limit'] == 1
    assert diagnostics['singleton_city_groups'] == 1
    assert diagnostics['eligible_groups'] == 1


def test_city_with_no_measured_warehouse_departure_is_skipped_without_building_its_matrix():
    body, road = city_problem()
    original = road.known_segment
    lookups = []
    def lookup(a, b):
        lookups.append((a['id'], b['id']))
        return None if b['id'] in {'5', '6'} and a['id'] == 'W' else original(a, b)
    road.known_segment = lookup
    _, diagnostics = solve_small_groups(body, road)
    assert diagnostics['groups_without_measured_departure'] == 1
    assert not any(a in {'5', '6'} for a, _ in lookups)
    assert diagnostics['completed_groups'] == diagnostics['eligible_groups']
