from copy import deepcopy
from decimal import Decimal
import pytest
from app.route_search import JointSearch
from app.routing_engine import StopSearch,normalize
from test_route_fragment_merging import problem,MatrixRoad

@pytest.mark.parametrize('stop_reason',['TIME_BUDGET','CANCELLED','WALL_BUDGET'])
def test_daily_seed_integrates_required_tickets_and_retains_complete_plan_when_late_pack_stops(monkeypatch,stop_reason):
    from app.route_deep import _Budget,_pre_selection_macros,baseline_result
    from app.route_experiments import DeepExperiments
    from app.routing_engine import check_result
    body=problem(16,capacities=(8,8),mandatory=True);original=deepcopy(body)
    road=MatrixRoad(body);road.deep_experiments=DeepExperiments(fragment_merging=True)
    state={'cancelled':False}
    def stop_pack(search):
        assert not search.best['unassigned'], 'Cheap physical matching must prove a full seed before packing'
        if stop_reason=='CANCELLED':state['cancelled']=True
        elif stop_reason=='WALL_BUDGET':search.road.wall_deadline=0
        raise StopSearch(stop_reason)
    monkeypatch.setattr(JointSearch,'pack',stop_pack)
    data=normalize(body);budget=_Budget(road,60,lambda:state['cancelled'])
    result,details,recipes=_pre_selection_macros(data,road,baseline_result(data),budget,0,lambda:state['cancelled'])
    assert not result['unassigned'] and result['fees_complete']
    assert check_result(data,result)['valid']
    assert Decimal(result['total_fee'])==2*(Decimal(1000000)+7*1000)/1000
    assert len({row['vehicle_id'] for row in result['routes']})==2
    assert details['seed_generator_policy']=='OPEN_FRAGMENT_MERGING' and details['stop_reason']==stop_reason
    physical=next(row for row in details['methods'] if row['method']=='fragment_physical_selection')
    assert physical['action_entered'] and physical['recorded_on_interruption']
    assert physical['stop_reason']==stop_reason and physical['complete_after']
    assert physical['best_fee']==result['total_fee']
    assert physical['details']['complete_proposal_considered']
    assert physical['details']['physical_pack_attempted']
    assert recipes and body==original
