"""Central opt-in attempts include custom macros and direct legacy pricing."""
from copy import deepcopy

from app.route_macro import seed_long_routes
from app.route_metrics import evaluate_candidate
from app.route_search import JointSearch
from app.routing_engine import evaluate_route,normalize,optimize_legacy
from scripts.reference_route_trace import ReferenceStageTrace
from test_route_columns import problem


def listener(order,vehicle='V0'):
    return ReferenceStageTrace({'routes':[{'vehicle_id':vehicle,'task_ids':order,'fee':'0.00'}]})


def stage_events(trace,stage):
    return [event for row in trace.snapshot()['targets'] for event in row['events'] if event['stage']==stage]


def test_custom_macro_evaluator_is_visible_without_jointsearch_evaluate():
    body,road=problem(n=2,cars=1,common='100',capacity=2)
    trace=ReferenceStageTrace({'routes':[{'vehicle_id':'V0','task_ids':[item],'fee':'0.00'} for item in ('0','1')]})
    road.route_stage_trace=trace
    search=JointSearch(normalize(body),road,1,0,lambda:False)
    search.setup_fleet()
    def forbidden(*args):raise AssertionError('Macro owns its separate evaluator')
    search.evaluate=forbidden
    seed_long_routes(search,max_seeds=1,max_stops=2,max_evaluations=20)
    assert stage_events(trace,'CENTRAL_METRIC_REQUEST')
    assert not stage_events(trace,'PRICE_REQUEST')


def test_legacy_direct_road_evaluator_is_visible_without_jointsearch():
    body,road=problem(n=2,cars=1,common='100',capacity=2)
    trace=listener(['0','1']);road.route_stage_trace=trace
    result=optimize_legacy(body,road,budget_seconds=1)
    assert result['total_fee']=='11.00'
    events=stage_events(trace,'CENTRAL_ROAD_REQUEST')
    assert events and events[0]['exact_order_count']>0
    assert events[0]['first_exact_witness']=={'vehicle_id':'V0','task_ids':['0','1']}
    assert not stage_events(trace,'PRICE_REQUEST')


def test_rejected_direct_attempt_keeps_actual_vehicle_order_and_constant_scope():
    body,road=problem(n=2,cars=2,common='100',capacity=1)
    trace=listener(['0','1'],'V0');road.route_stage_trace=trace
    data=normalize(body)
    for _ in range(3):
        metric=evaluate_candidate(data,'V1',['0','1'],road)
        actual=evaluate_route(data,'V1',['0','1'],road)
        assert {issue['code'] for issue in metric['errors']}=={'CAPACITY'}
        assert {issue['code'] for issue in actual['errors']}=={'CAPACITY'}
    for name in ('CENTRAL_METRIC_REQUEST','CENTRAL_ROAD_REQUEST'):
        events=stage_events(trace,name)
        assert len(events)==1 and events[0]['count']==3
        assert events[0]['scope']=='JOB_EVALUATOR_REQUESTS'
        assert events[0]['first_exact_witness']=={'vehicle_id':'V1','task_ids':['0','1']}
    assert not trace.scope_counts


def test_opt_in_listener_does_not_change_business_price_or_feasibility():
    body,road=problem(n=2,cars=1,common='100',capacity=2)
    original=deepcopy(body);data=normalize(body)
    before=evaluate_candidate(data,'V0',['0','1'],road)
    road.route_stage_trace=listener(['0','1'])
    after=evaluate_candidate(data,'V0',['0','1'],road)
    assert before==after and body==original
