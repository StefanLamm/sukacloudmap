from copy import deepcopy
import time
import pytest
from app.route_portfolio import optimize_alns,optimize_hybrid
from app.routing_engine import assemble,normalize,evaluate_route,check_result
from test_route_search import clustered_problem


@pytest.mark.parametrize('algorithm',[optimize_alns,optimize_hybrid])
def test_portfolio_increases_cars_when_total_spending_falls(algorithm):
    body,road=clustered_problem();data=normalize(body)
    initial=assemble(data,[evaluate_route(data,'V0',list(data['tasks']),road)],[],[])
    result=algorithm(body,road,2,initial_result=initial)
    assert result['total_fee']=='42.00' and len(result['routes'])==3
    assert not result['common'] and not result['unassigned']
    assert check_result(data,result)['valid']
    assert result['algorithm']['fleet_utilization']['dedicated_pieces']==9
    assert not result['algorithm']['optimality_proven']


@pytest.mark.parametrize('algorithm',[optimize_alns,optimize_hybrid])
@pytest.mark.parametrize('budget,cancelled,reason',[(0,lambda:False,'TIME_BUDGET'),(30,lambda:True,'CANCELLED')])
def test_portfolio_keeps_complete_verified_plan_when_interrupted(algorithm,budget,cancelled,reason):
    body,road=clustered_problem(cars=30);data=normalize(body)
    initial=assemble(data,[evaluate_route(data,f'V{i}',[str(i)],road) for i in range(9)],[],[])
    before=road.calls
    result=algorithm(body,road,budget,cancelled=cancelled,initial_result=deepcopy(initial))
    assert result['total_fee']==initial['total_fee'] and road.calls==before
    assert result['algorithm']['stop_reason']==reason
    assert check_result(data,result)['valid']


@pytest.mark.parametrize('algorithm',[optimize_alns,optimize_hybrid])
def test_no_eligible_dedicated_tasks_does_not_spin_for_long_budget(algorithm):
    body,road=clustered_problem()
    for task in body['tasks']:task['quote_id']=None
    before=time.monotonic();result=algorithm(body,road,24000)
    assert time.monotonic()-before<1
    assert len(result['common'])==9 and result['unassigned']==[]
    assert result['algorithm']['stop_reason']=='NO_DEDICATED_CANDIDATES'


@pytest.mark.parametrize('algorithm',[optimize_alns,optimize_hybrid])
def test_forced_refresh_cannot_retain_a_cheaper_old_road_quotation(algorithm):
    body,old_road=clustered_problem();data=normalize(body)
    initial=assemble(data,[evaluate_route(data,f'V{i}',[str(3*i+j) for j in range(3)],old_road) for i in range(3)],[],[])
    assert initial['total_fee']=='42.00'
    from app.road_service import evidence
    class RefreshedRoad:
        force_refresh=True
        def segment(self,v,a,b,at):
            original=old_road.segment(v,a,b,at)
            return evidence(original['request'],original['meters']*2,original['seconds']*2,'FIXTURE')
        def stats(self):return {'calls':0}
    result=algorithm(body,RefreshedRoad(),2,initial_result=initial)
    assert result['total_fee']=='84.00'
    assert result['algorithm']['initial_reference_revalidation_required']
    assert not result['algorithm']['initial_plan_retained_or_improved']
    assert check_result(data,result)['valid']
    assert initial['total_fee']=='42.00'
