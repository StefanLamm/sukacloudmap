"""Analytical counterexamples for the independently implemented oracle."""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.routing_engine import normalize
from scripts.evaluate_controlled_general import FormulaRoad,FORMULA,exact_oracle,region_charge,route_model
from test_algorithm_benchmark import problem


class Measured:
    graph_hash='PINNED_TEST_MATRIX'
    def known_segment(self,a,b):
        return {'meters':10000 if a['id']=='W' else 1000,'seconds':600 if a['id']=='W' else 60}


def test_exact_enumeration_is_independent_of_production_pricing(monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('Independent oracle must not call the production fee evaluator')
    monkeypatch.setattr('app.routing_engine.line_fee',forbidden)
    monkeypatch.setattr('app.routing_engine.regional_extra',forbidden)
    body=problem();result=exact_oracle(body,Measured())
    # One 10km departure followed by two 1km links at one currency unit/km.
    assert result['frozen_model_optimality_proven'] and result['optimal_fee']=='12.00'
    assert len(result['routes'])==1 and not result['common_ids']


def test_exact_allocation_cannot_reuse_a_car_or_split_a_ticket():
    body=problem()
    for car in body['vehicles']:car['max_pieces']=10
    result=exact_oracle(body,Measured())
    assert result['optimal_fee']=='21.00' and len(result['routes'])==2
    assert len({r['vehicle_id'] for r in result['routes']})==2
    body['tasks'][0].update(pieces=11,common_unit_price=None)
    negative=exact_oracle(body,Measured())
    assert negative['delivery_infeasible_proven'] and negative['minimum_unassigned']==1
    assert not negative['frozen_model_optimality_proven']


def test_missing_measurement_prevents_exact_certificate():
    class Partial(Measured):
        def known_segment(self,a,b):return None if a['id']=='T1' and b['id']=='T2' else super().known_segment(a,b)
    result=exact_oracle(problem(),Partial())
    assert result['unknown_edges'] and result['optimal_fee'] is None
    assert not result['frozen_model_optimality_proven'] and not result['delivery_infeasible_proven']


@pytest.mark.parametrize('mode,amount',[('ONCE','60'),('PER_REGION','120'),('PER_STORE','180')])
def test_surcharge_counts_actual_regions_or_stores_once(mode,amount):
    quote={'mode':'KM_REGION','per_km':'1','region_surcharge':{'amount':'60','billing_mode':mode,
        'targets':[{'province_code':'320000','city_code':'320100'}]}}
    tasks=[{'id':'A','store_id':'A','region_code':'320102'},
           {'id':'B','store_id':'B','region_code':'320102'},
           {'id':'C','store_id':'C','region_code':'320104'}]
    assert region_charge(quote,tasks)==Decimal(amount)
    # Duplicate matching targets cannot multiply the amount.
    quote['region_surcharge']['targets']*=2
    assert region_charge(quote,tasks)==Decimal(amount)


def test_independent_region_oracle_refuses_unresolved_geography():
    quote={'mode':'KM_REGION','per_km':'1','region_surcharge':{'amount':'60','targets':[{'province_code':'320000','city_code':'320100'}]}}
    with pytest.raises(ValueError,match='FULL_COUNTY_CODES'):region_charge(quote,[{'id':'A','region_code':None}])


def test_time_model_uses_calendar_days_and_waits_before_unloading():
    body=problem();body['warehouses'][0]['earliest_departure_at']='2026-11-13T23:30:00+08:00'
    for task in body['tasks']:
        task.update(open_at='2026-11-13T23:00:00+08:00',close_at='2026-11-14T02:00:00+08:00',unload_minutes=20)
    data=normalize(body);model,errors=route_model(data,data['vehicles']['V1'],['T1','T2','T3'],Measured())
    assert not errors and model['stops'][1]['arrival_at'].startswith('2026-11-14T00:')
    data['tasks']['T1']['open_at']='2026-11-14T00:00:00+08:00'
    waited,errors=route_model(data,data['vehicles']['V1'],['T1'],Measured())
    assert not errors and waited['stops'][0]['unload_start_at']=='2026-11-14T00:00:00+08:00'


def test_synthetic_matrix_uses_row_order_never_ids_or_coordinates():
    body=problem();body['road_fixture']={'formula_locked':True,'scope':'SYNTHETIC_MATRIX_NOT_AMAP_EVIDENCE','rule':FORMULA}
    road=FormulaRoad(body)
    assert road.known_segment({'id':'W'},{'id':'T1'})=={'meters':1025,'seconds':305}
    assert road.known_segment({'id':'T1'},{'id':'T3'})=={'meters':1050,'seconds':310}
    changed=deepcopy(body);changed['tasks']=list(reversed(changed['tasks']))
    assert FormulaRoad(changed).graph_hash!=road.graph_hash
    assert road.stats()['calls']==0 and road.stats()['source']=='FIXTURE'
