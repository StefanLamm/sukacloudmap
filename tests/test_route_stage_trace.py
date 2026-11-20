"""Trace hooks are opt-in listeners; these tests never start a native solver."""
from types import SimpleNamespace
import json

from app.route_columns import select_portfolio
from app.route_ortools import _MeasuredModel
from app.route_search import JointSearch
from app.routing_engine import assemble,normalize
from app.planning_inputs import content_hash
from scripts.benchmark_algorithms import private_json,write_graph
from scripts.reference_route_trace import ReferenceStageTrace,read
from scripts.trace_cold_reference import run_case
from test_route_columns import problem,plan
from test_algorithm_benchmark import problem as frozen_problem,measured_edges


def reference(order,vehicle='V0'):
    return {'routes':[{'vehicle_id':vehicle,'task_ids':order,'fee':'0.00'}]}


def events(trace,stage):
    return [event for target in trace.snapshot()['targets'] for event in target['events'] if event['stage']==stage]


def test_native_pre_retention_identity_is_observed_even_when_first_pool_is_full():
    trace=ReferenceStageTrace(reference(['a']))
    model=_MeasuredModel.__new__(_MeasuredModel)
    model.stage_trace=trace;model.trace_scope=trace.scope('NATIVE')
    model.virtual=[({'id':'V0'},None)]
    model.routing=SimpleNamespace(Start=lambda vi:0,IsEnd=lambda node:node==2)
    model.manager=SimpleNamespace(IndexToNode=lambda node:node)
    model.task_nodes={1:'a'};model.ids=['a']
    model.candidate_stream_observer=None;model.retention=None
    model.candidate_limit=10000
    model.candidate_specs={(str(i),('other',)):{} for i in range(10000)}
    result=model._collect(lambda node:node+1)
    assert result==[{'vehicle_id':'V0','task_ids':['a']}]
    assert events(trace,'NATIVE_PROPOSED')[0]['exact_order_count']==1
    assert ('V0',('a',)) not in model.candidate_specs




def test_actual_pricing_rejection_has_its_first_visible_boundary():
    body,road=problem(n=2,cars=1,capacity=1)
    trace=ReferenceStageTrace(reference(['0','1']))
    road.route_stage_trace=trace
    search=JointSearch(normalize(body),road,1,0,lambda:False)
    assert search.evaluate('V0',['0','1']) is None
    assert events(trace,'PRICE_REQUEST')
    assert events(trace,'PRICED_REJECTED')[0]['reason_codes']==['CAPACITY']


def test_pool_pricing_column_model_and_materialization_hooks_use_same_feasible_order(monkeypatch):
    body,road=problem(n=2,cars=1,capacity=2)
    trace=ReferenceStageTrace(reference(['0','1']))
    road.route_stage_trace=trace
    def solve(data,classes,incumbent,seconds,cancelled):
        row={**classes[0]['patterns'][0],'vehicle_id':classes[0]['members'][0]}
        return ([row],set()),{'solver_status':'TEST_FAKE','finite_pool_cost_optimal':False}
    monkeypatch.setattr('app.route_columns._solve',solve)
    result=select_portfolio(body,road,[plan(body,road)],seconds=1,
        candidate_recipes=[{'task_ids':['0','1']}])
    assert result['total_fee']=='11.00'
    for stage in ('POOL_ORDER','POOL_PRICE','COLUMN','MODEL_INCLUDED','MODEL_SELECTED','MATERIALIZED','PORTFOLIO_FINAL'):
        assert events(trace,stage),stage
    assert trace.snapshot()['stage_status']['MODEL_SELECTED']['PORTFOLIO_1']['status']=='COMPLETE'


def test_unreached_pool_model_is_explicitly_skipped_on_zero_allowance():
    body,road=problem(n=2,cars=1)
    trace=ReferenceStageTrace(reference(['0','1']))
    road.route_stage_trace=trace
    select_portfolio(body,road,[plan(body,road)],seconds=0)
    assert trace.snapshot()['stage_status']['MODEL_SELECTED']['PORTFOLIO_1']['status']=='SKIPPED'


def frozen_case(tmp_path):
    folder=tmp_path/'case';folder.mkdir()
    body=frozen_problem()
    private_json(folder/'input.json',body)
    graph=folder/'roads.db'
    metadata=write_graph(graph,body,measured_edges(body))
    private_json(folder/'case.json',{'body_file':'input.json','input_hash':content_hash(body),
        'road_database':str(graph),'graph_hash':metadata['graph_hash']})
    return folder/'case.json',body


def test_cold_script_reference_is_observation_only_and_stream_channels_do_not_duplicate(tmp_path):
    case,body=frozen_case(tmp_path)
    ref=tmp_path/'arbitrary-reference.json'
    private_json(ref,reference(['not-a-current-task'],'not-a-current-car'))
    def optimizer(actual,road,seconds,seed,cancelled,**kwargs):
        assert actual==body and kwargs['initial_result'] is None
        assert set(kwargs)=={'initial_result','experiments','wall_budget_seconds'}
        spec={'vehicle_id':'V1','task_ids':['T1']}
        road.candidate_stream_observer({'kind':'candidate',**spec})
        road.candidate_stream_observer({'kind':'priced_candidate',**spec,'retention_fee':'10.00'})
        road.priced_candidate_observer({**spec,'retention_fee':'10.00'})
        road.native_candidate_stream_observer(spec)
        data=normalize(actual)
        result=assemble(data,[],set(data['tasks']),set())
        result['algorithm']={'experiments':kwargs['experiments']}
        return result
    result=run_case(case,tmp_path/'output',reference_file=ref,seconds=1,
        experiments={},capture_stream=True,optimizer=optimizer)
    assert result['status']=='RETURNED' and result['audit']['valid']
    assert result['fee']=='150.00' and not result['reference_injected']
    assert result['timing_scope']=='DIAGNOSTIC_STREAM_CAPTURE'
    assert len(read(result['deep_stream']['file']))==2
    assert len(read(result['price_stream']['file']))==1
    assert len(read(result['native_stream']['file']))==1
    assert result['external_calls']==0 and not result['business_database_written']
    assert read(result['trace_file'])['targets'][0]['task_ids']==['not-a-current-task']
    json.dumps(result,allow_nan=False)


def test_cold_script_preserves_failed_run_and_empty_stream_evidence(tmp_path):
    case,_=frozen_case(tmp_path)
    def fail(*args,**kwargs):raise RuntimeError('synthetic failure without solving')
    result=run_case(case,tmp_path/'output',seconds=1,capture_stream=True,optimizer=fail)
    assert result['status']=='ERROR' and result['exception_type']=='RuntimeError'
    assert read(result['deep_stream']['file'])==[]
    assert read(result['trace_file'])['stage_status']['COLD_RUN']['CURRENT_RUN']['status']=='INTERRUPTED'
