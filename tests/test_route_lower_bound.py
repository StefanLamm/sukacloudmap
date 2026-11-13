"""Independent route enumeration and adversarial dual checks for cost bounds."""
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

import pytest

from app.route_lower_bound import _lagrangian_certificate, compute_lower_bound
from app.routing_engine import StopSearch
from test_route_ortools import problem, oracle
from test_solver_oracle import scenario, oracle as timed_oracle


@pytest.mark.parametrize('quote',[
    {'mode':'KM','per_km':'1'},
    {'mode':'BASE_STOPS','base_price':'10','included_stops':1,'extra_stop_price':'2'},
    {'mode':'BASE_STOPS','base_price':'10','included_stops':5,'extra_stop_price':'0'},
])
def test_exact_small_finite_problem_bounds_never_exceed_independent_optimum(quote):
    body,road,pairs=problem(quote=quote)
    exact=oracle(body,pairs)[1]
    bound=compute_lower_bound(body,road,3,include_certificate=True)
    assert Decimal(bound['cost_lower_bound'])<=exact
    assert Decimal(bound['cost_lower_bound'])>=Decimal(bound['analytical_lower_bound'])
    assert bound['lp']['certified'] and bound['lp']['certificate']['exact_lagrangian_value']
    assert not bound['global_optimality_proven'] and not bound['feasibility_proven']
    assert bound['scope']=='FROZEN_MEASURED_GRAPH_RELAXATION'
    assert road.evaluated==0


@pytest.mark.parametrize('seed',range(25))
def test_randomized_mixed_fleet_bounds_below_independent_timed_enumeration(seed):
    body,road,roads=scenario(seed)
    # Every scenario has a complete paid common alternative, while native
    # vehicle-specific measurements still differ and time windows remain real.
    for task in body['tasks']:
        if task['common_unit_price'] is None:task['common_unit_price']='25'
    expected=timed_oracle(body,roads)
    assert expected[0]==0
    def measured(vehicle,origin,destination):
        key=vehicle['id'],origin['id'],destination['id']
        if sum(map(ord,''.join(key)))%7==0:return None
        m,s=roads[key];return {'meters':m,'seconds':s}
    bound=compute_lower_bound(body,road,2,edge_lookup=measured,vehicle_independent=False)
    assert Decimal(bound['cost_lower_bound'])<=Decimal(expected[1])/100,(seed,bound,expected)
    assert Decimal(bound['cost_lower_bound'])>=0
    assert bound['round_down_to_cents']


def test_shared_physical_fleet_is_shared_across_quote_groups_in_relaxation():
    body,road,pairs=problem(n=2,cars=1,quote={'mode':'BASE_STOPS','base_price':'1','included_stops':1,'extra_stop_price':'0'})
    body['quotes']['B']={'mode':'BASE_STOPS','base_price':'1','included_stops':2,'extra_stop_price':'0'}
    body['tasks'][1]['quote_id']='B'
    expected=oracle(body,pairs)[1]
    assert expected==Decimal('101.00')
    bound=compute_lower_bound(body,road,3)
    assert Decimal('100.98')<=Decimal(bound['cost_lower_bound'])<=expected
    assert Decimal(bound['lp_lower_bound'])>Decimal(bound['analytical_lower_bound'])


def test_global_load_relaxation_strengthens_bound_without_exceeding_real_optimum():
    body,road,pairs=problem(n=2,cars=1,capacity=10)
    for row in body['tasks']:row['pieces']=9;row['common_unit_price']='11'
    exact=oracle(body,pairs)[1]
    bound=compute_lower_bound(body,road,3)
    assert Decimal(bound['cost_lower_bound'])<=exact
    assert Decimal(bound['lp_lower_bound'])>Decimal(bound['analytical_lower_bound'])+50


def test_unknown_incoming_edges_are_zero_not_ignored_in_the_minimum():
    body,road,pairs=problem(n=2,cars=1,common=None)
    pairs['W','0']=(1,1);pairs['0','1']=(1,1)
    pairs['W','1']=(100000,1);pairs['1','0']=(100000,1)
    def partial(vehicle,origin,destination):
        if (origin['id'],destination['id'])==('W','0'):return None
        return road.known_segment(origin,destination)
    bound=compute_lower_bound(body,road,3,edge_lookup=partial,vehicle_independent=True)
    assert bound['cost_lower_bound']=='0.00'
    assert bound['unknown_cost_edges']==1 and bound['unknown_roads_cost_zero']


def test_per_route_rounding_allowance_is_deducted_before_flooring():
    body,road,pairs=problem(n=1,cars=1,common=None,quote={'mode':'KM','per_km':'4'})
    pairs['W','0']=(1,1)  # unrounded .004 yuan, actual HALF_UP quote .00
    bound=compute_lower_bound(body,road,3)
    assert bound['cost_lower_bound']=='0.00'
    assert bound['rounding_allowance']=='0.005'


@pytest.mark.parametrize('mode',['ONCE','PER_REGION','PER_STORE'])
def test_regional_surcharge_relaxations_are_conservative(mode):
    rule={'amount':'10','billing_mode':mode,'targets':[{'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'','district_name':''}]}
    body,road,pairs=problem(n=2,cars=1,common=None,quote={'mode':'KM_REGION','per_km':'1','region_surcharge':rule})
    for row in body['tasks']:row.update(province='安徽省',city='合肥市',district='蜀山区',region_code='340104')
    bound=compute_lower_bound(body,road,3)
    exact=Decimal('2.00')+Decimal(20 if mode=='PER_STORE' else 10)
    assert Decimal(bound['cost_lower_bound'])<=exact
    if mode=='PER_STORE':assert Decimal(bound['cost_lower_bound'])>=Decimal('21.99')
    else:assert Decimal(bound['cost_lower_bound'])<=Decimal('2.00')


def test_repeated_physical_store_ids_do_not_double_count_per_store_lower_charge():
    rule={'amount':'10','billing_mode':'PER_STORE','targets':[{'province_code':'340000','province_name':'安徽省','city_code':'340100','city_name':'合肥市','district_code':'','district_name':''}]}
    body,road,pairs=problem(n=2,cars=1,common=None,quote={'mode':'KM_REGION','per_km':'1','region_surcharge':rule})
    for row in body['tasks']:
        row.update(store_id='SAME_PHYSICAL_STORE',province='安徽省',city='合肥市',district='蜀山区',region_code='340104')
    # Both task IDs on one route cost two road kilometers plus ONE store fee.
    bound=compute_lower_bound(body,road,3)
    assert Decimal(bound['cost_lower_bound'])<=Decimal('12.00')


def test_no_available_dedicated_fleet_has_exact_common_delivery_bound():
    body,road,pairs=problem(n=2,cars=0)
    bound=compute_lower_bound(body,road,3)
    assert bound['cost_lower_bound']=='200.00' and bound['rounding_allowance']=='0'


def test_unpriced_mandatory_tasks_do_not_turn_bound_into_a_feasibility_claim():
    body,road,pairs=problem(n=1,cars=0,common=None)
    body['tasks'][0]['quote_id']=None
    bound=compute_lower_bound(body,road,3)
    assert bound['cost_lower_bound']=='0.00'
    assert bound['unpriced_mandatory_tasks']==1
    assert not bound['feasibility_proven']


def test_inaccurate_duals_are_repaired_by_exact_box_lagrangian_arithmetic():
    # min x+2y subject to x+y>=1, x,y in[0,1] has optimum1.
    # mu=-3 is dual-infeasible for the unbounded form. The exact box correction
    # repairs its raw value3 down to0, so it cannot claim a lower bound of3.
    value,witness=_lagrangian_certificate([Fraction(1),Fraction(2)],
            [(Fraction(0),Fraction(1))]*2,[],[({0:Fraction(-1),1:Fraction(-1)},Fraction(-1))],[],[-3.0])
    assert value==0 and witness['reduced_cost_box_correction']=='-3'
    value,_=_lagrangian_certificate([Fraction(1),Fraction(2)],
            [(Fraction(0),Fraction(1))]*2,[],[({0:Fraction(-1),1:Fraction(-1)},Fraction(-1))],[],[100000.0])
    assert value==0  # Positive inequality values are safely clamped to zero.


def test_timeout_primal_objective_is_never_misreported_as_a_lower_bound(monkeypatch):
    import app.route_lower_bound as module
    np,_,matrix=module._lp_api()
    def timeout(costs,**kwargs):
        return SimpleNamespace(status=1,fun=10**30,
               eqlin=SimpleNamespace(marginals=None),ineqlin=SimpleNamespace(marginals=None))
    monkeypatch.setattr(module,'_lp_api',lambda:(np,timeout,matrix))
    body,road,pairs=problem()
    bound=compute_lower_bound(body,road,3)
    assert bound['lp']['status']=='TIME_LIMIT' and not bound['lp']['certified']
    assert bound['cost_lower_bound']==bound['analytical_lower_bound']
    assert bound['lp_lower_bound'] is None


def test_timeout_with_arbitrary_finite_duals_still_requires_an_exact_certificate(monkeypatch):
    import app.route_lower_bound as module
    np,_,matrix=module._lp_api()
    def timeout(costs,**kwargs):
        return SimpleNamespace(status=1,fun=10**30,
               eqlin=SimpleNamespace(marginals=np.ones(len(kwargs['b_eq']))*10**6),
               ineqlin=SimpleNamespace(marginals=np.ones(len(kwargs['b_ub']))*10**6))
    monkeypatch.setattr(module,'_lp_api',lambda:(np,timeout,matrix))
    body,road,pairs=problem()
    bound=compute_lower_bound(body,road,3)
    assert bound['lp']['certified']
    assert Decimal(bound['cost_lower_bound'])<=oracle(body,pairs)[1]


def test_zero_budget_returns_a_safe_weak_bound_without_road_or_solver_calls(monkeypatch):
    import app.route_lower_bound as module
    def forbidden():raise AssertionError('No native solver when budget is zero')
    monkeypatch.setattr(module,'_lp_api',forbidden)
    body,road,pairs=problem()
    bound=compute_lower_bound(body,road,0)
    assert bound['cost_lower_bound']=='0.00' and road.evaluated==0


def test_cancellation_raises_stop_instead_of_publishing_a_new_bound():
    body,road,pairs=problem()
    with pytest.raises(StopSearch) as error:compute_lower_bound(body,road,3,cancelled=lambda:True)
    assert error.value.reason=='CANCELLED'


def test_negative_quote_parameters_cannot_receive_a_nonnegative_cost_certificate():
    body,road,pairs=problem(quote={'mode':'KM','per_km':'-1'})
    with pytest.raises(ValueError):compute_lower_bound(body,road,3)
