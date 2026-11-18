"""Large thawed scopes require complementary columns, not tiny enumeration."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.route_joint_reoptimization import reoptimize_joint_routes
from app.routing_engine import check_result
from test_route_columns import problem
from test_route_joint_reoptimization import prepared


def chain(road, order, meters=1000):
    for left,right in zip(order,order[1:]):
        road.matrix[left,right]['meters']=meters


def complete(search):
    assert check_result(search.data,search.best)['valid']
    assert not search.best['unassigned']
    assert len(search.best['routes'])==len({row['vehicle_id'] for row in search.best['routes']})


def test_large_tail_exchange_replaces_two_whole_lines_together_without_native():
    body,road=problem(n=10,cars=2,common=None,capacity=5)
    original=[['0','1','2','3','4'],['5','6','7','8','9']]
    for order in original:chain(road,order)
    road.matrix['1','2']['meters']=100000
    road.matrix['6','7']['meters']=100000
    chain(road,['0','1','7','8','9']);chain(road,['5','6','2','3','4'])
    search,initial=prepared(body,road,[('V0',original[0]),('V1',original[1])])
    details=reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,native=False)
    assert initial['total_fee']=='226.00'
    assert search.best['total_fee']=='28.00'
    assert details['neighborhoods'][0]['task_count']==10
    assert details['neighborhoods'][0]['scalable_methods'][0]['generated_orders']>0
    complete(search)


def test_large_interior_block_relocation_supplies_the_complementary_remainder():
    body,road=problem(n=9,cars=2,common=None,capacity=6)
    chain(road,['0','1','2','3','4']);chain(road,['5','6','7','8'])
    road.matrix['0','1']['meters']=100000
    road.matrix['2','3']['meters']=100000
    chain(road,['0','3','4']);chain(road,['5','6','1','2','7','8'])
    search,initial=prepared(body,road,[('V0',['0','1','2','3','4']),('V1',['5','6','7','8'])])
    details=reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,native=False)
    assert search.best['total_fee']=='27.00'
    assert Decimal(search.best['total_fee'])<Decimal(initial['total_fee'])
    methods=details['neighborhoods'][0]['scalable_methods']
    assert next(row for row in methods if row['method']=='block_relocate_and_split')['generated_orders']>0
    complete(search)


def test_large_scope_can_enable_third_physical_vehicle_for_detached_block():
    body,road=problem(n=9,cars=3,common=None,capacity=5)
    chain(road,['0','1','2','3','4']);chain(road,['5','6','7','8'])
    road.matrix['1','2']['meters']=100000
    search,initial=prepared(body,road,[('V0',['0','1','2','3','4']),('V1',['5','6','7','8'])])
    reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,native=False)
    assert search.best['total_fee']=='36.00'
    assert Decimal(search.best['total_fee'])<Decimal(initial['total_fee'])
    assert len(search.best['routes'])==3
    complete(search)


def test_common_refill_in_large_cross_city_scope_is_measured_and_whole_ticket():
    body,road=problem(n=10,cars=2,common=None,capacity=5)
    body['tasks'][9]['common_unit_price']='100'
    for index,task in enumerate(body['tasks']):task['city']='city-'+str(index)
    for (origin,_),metric in road.matrix.items():metric['meters']=10000 if origin=='W' else 1000
    search,initial=prepared(body,road,[('V0',['0','1','2','3','4']),('V1',['5','6','7','8'])])
    details=reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,native=False)
    assert search.best['total_fee']=='28.00' and initial['total_fee']=='127.00'
    assert not search.best['common']
    assert details['neighborhoods'][0]['related_common_tasks']==1
    assert any(len(row['task_ids'])>1 for row in search.best['routes'])
    complete(search)


def test_idle_growth_crosses_unprofitable_prefixes_without_a_seven_store_cutoff():
    body,road=problem(n=12,cars=3,common=None,capacity=6)
    for task in body['tasks'][4:]:task['common_unit_price']='10'
    chain(road,['0','1']);chain(road,['2','3'])
    optional=[str(i) for i in range(4,12)]
    for origin in optional:
        road.matrix['W',origin]['meters']=50000
        for destination in optional:
            if origin!=destination:road.matrix[origin,destination]['meters']=1000
    search,initial=prepared(body,road,[('V0',['0','1']),('V1',['2','3'])])
    details=reoptimize_joint_routes(search,seconds=1.5,max_neighborhoods=1,native=False)
    # Any <=5-stop new line is dearer than its common alternative. A six-stop
    # line saves five and keeps both mandatory lines on their actual vehicles.
    assert search.best['total_fee']=='97.00' and initial['total_fee']=='102.00'
    assert len(search.best['routes'])==3
    assert any(len(row['task_ids'])==6 for row in search.best['routes'])
    assert details['neighborhoods'][0]['task_count']==12
    complete(search)




def test_scalable_false_keeps_legacy_large_generation_path(monkeypatch):
    body,road=problem(n=9,cars=3,common=None,capacity=9)
    search,initial=prepared(body,road,[('V0',['0','1','2','3']),('V1',['4','5','6','7','8'])])
    generated=[]
    monkeypatch.setattr('app.route_search.JointSearch.pack',lambda local:None)
    monkeypatch.setattr('app.route_search.JointSearch.generate',lambda local:generated.append(tuple(local.tasks)))
    def forbidden(*args):raise AssertionError('New columns must stay off with scalable=False')
    monkeypatch.setattr('app.route_joint_reoptimization._scalable_columns',forbidden)
    details=reoptimize_joint_routes(search,seconds=.5,max_neighborhoods=1,native=False,scalable=False)
    assert generated and details['scalable_search_enabled'] is False
    assert search.best['total_fee']==initial['total_fee']
    complete(search)


@pytest.mark.parametrize('production',[False,True])
def test_missing_directed_bridge_is_collected_only_in_production_outside_native(production):
    body,road=problem(n=10,cars=2,common=None,capacity=5)
    for order in (['0','1','2','3','4'],['5','6','7','8','9']):chain(road,order)
    road.matrix['1','2']['meters']=100000
    road.matrix['6','7']['meters']=100000
    chain(road,['0','1','7','8','9']);chain(road,['5','6','2','3','4'])
    search,initial=prepared(body,road,[('V0',['0','1','2','3','4']),('V1',['5','6','7','8','9'])])
    full=deepcopy(road.matrix)
    del road.matrix['1','7']
    road.production_planning=production
    calls=[]
    segment=road.segment
    def collect(vehicle,a,b,at):
        if (a['id'],b['id']) not in road.matrix:
            assert production
            calls.append((a['id'],b['id']))
            road.matrix[a['id'],b['id']]=full[a['id'],b['id']]
        return segment(vehicle,a,b,at)
    road.segment=collect
    details=reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,native=False)
    assert bool(calls)==production
    if production:
        assert search.best['total_fee']=='28.00'
        assert details['neighborhoods'][0]['live_connection_evaluations']>0
    else:
        assert not details['neighborhoods'][0]['live_connection_evaluations']
    complete(search)
