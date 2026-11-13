"""Tiny finite-pool CP-SAT cases; run after the private API timing slot only."""
from copy import deepcopy
from types import SimpleNamespace
import signal
import subprocess

import pytest

from app.routing_engine import assemble,normalize,quote_signature
from scripts.prepare_corrected_application import ApplicationFrozenRoad,prepare_application
from scripts.select_application_columns_cpsat import (exact_vehicle_classes,extract_orders,
    atomic_private_json,install_parent_stop_handlers,reprice_vehicle_columns,select_and_materialize,
    solve_columns,stop_process,worker_environment,guard_database_path)
from test_corrected_application import private_case


@pytest.fixture
def small_pool(private_case,tmp_path):
    prepared=prepare_application(private_case['inspection'],tmp_path/'pool')
    data=normalize(private_case['new']);data['vehicles']['VX']['max_pieces']=1
    road=ApplicationFrozenRoad(prepared['private_graph'])
    try:yield data,road
    finally:road.close()


def test_two_exactly_equivalent_cars_can_take_two_different_patterns(small_pool):
    data,road=small_pool
    data['vehicles']['VX2']={**data['vehicles']['VX'],'id':'VX2'}
    orders={('S0',):[],('S1',):[],('S2',):[]}
    columns,_=reprice_vehicle_columns(data,orders,road,lambda:None)
    classes,_=exact_vehicle_classes(data,columns)
    assert any(c['members']==['VX','VX2'] and len(c['patterns'])==2 for c in classes)
    incumbent=assemble(data,[],list(data['tasks']),[])
    result,proof,diag,_,_=select_and_materialize(data,columns,incumbent,road,1)
    assert result['total_fee']=='3.00' and proof['audit']['valid']
    assert {r['vehicle_id'] for r in result['routes']}=={'VW','VX','VX2'}
    assert diag['finite_pool_cost_optimal'] and diag['num_search_workers']==1
    assert all(not r.get('_estimated') and r['segments'] for r in result['routes'])


def test_one_car_class_cannot_cover_two_routes(small_pool):
    data,road=small_pool
    columns,_=reprice_vehicle_columns(data,{('S0',):[],('S1',):[],('S2',):[]},road,lambda:None)
    incumbent=assemble(data,[],list(data['tasks']),[])
    result,proof,diag,_,_=select_and_materialize(data,columns,incumbent,road,1)
    assert result['total_fee']=='102.00' and proof['audit']['valid']
    assert sum(r['vehicle_id']=='VX' for r in result['routes'])==1 and len(result['common'])==1
    assert diag['integer_objective_cents']==10200


@pytest.mark.parametrize('difference',['fee','meters','seconds','pieces','missing-pattern'])
def test_nonidentical_complete_column_sets_are_never_merged(small_pool,difference):
    data,road=small_pool;data['vehicles']['VX2']={**data['vehicles']['VX'],'id':'VX2'}
    columns,_=reprice_vehicle_columns(data,{('S1',):[],('S2',):[]},road,lambda:None)
    if difference=='missing-pattern':columns['VX2'].pop(('S2',))
    else:
        row=columns['VX2'][('S1',)]
        if difference=='fee':row['fee']='1.01'
        else:row[difference]+=1
    classes,_=exact_vehicle_classes(data,columns)
    assert not any({'VX','VX2'}.issubset(set(c['members'])) for c in classes)


def test_mandatory_store_has_no_common_variable_and_infeasible_pool_is_detected(small_pool):
    data,road=small_pool;data['tasks']={key:data['tasks'][key] for key in ['S1','S2']}
    data['tasks']['S1']['common_unit_price']=None
    columns,_=reprice_vehicle_columns(data,{('S1',):[],('S2',):[]},road,lambda:None)
    classes,_=exact_vehicle_classes(data,columns)
    chosen,diag=solve_columns(data,classes,solver_seconds=1)
    assert chosen is not None and any('S1' in r['task_ids'] for r in chosen)
    assert diag['common_variables']==1
    data['tasks']['S2']['common_unit_price']=None
    chosen,diag=solve_columns(data,classes,solver_seconds=1)
    assert chosen is None and diag['status']=='INFEASIBLE'


def test_one_cent_real_fee_is_not_rounded_or_replaced_by_surrogate(small_pool):
    data,road=small_pool;data['tasks']={key:data['tasks'][key] for key in ['S1','S2']}
    data['vehicles']['VX']['max_pieces']=2
    quote={'id':'Q1','mode':'BASE_STOPS','base_price':'0.01','included_stops':2,'extra_stop_price':'0.00'}
    for row in data['tasks'].values():
        row.update(quote=quote,signature=quote_signature(quote),common_unit_price='0.02')
    columns,_=reprice_vehicle_columns(data,{('S1','S2'):[]},road,lambda:None)
    classes,_=exact_vehicle_classes(data,columns)
    chosen,diag=solve_columns(data,classes,solver_seconds=1)
    assert len(chosen)==1 and chosen[0]['fee']=='0.01' and diag['integer_objective_cents']==1


@pytest.mark.parametrize('status_name',['UNKNOWN','INFEASIBLE'])
def test_solver_timeout_or_no_solution_retains_the_proved_complete_incumbent(small_pool,status_name):
    from ortools.sat.python import cp_model
    data,road=small_pool
    columns,_=reprice_vehicle_columns(data,{('S0',):[],('S1',):[],('S2',):[]},road,lambda:None)
    incumbent=assemble(data,[],list(data['tasks']),[])
    class NoSolution:
        parameters=SimpleNamespace()
        def solve(self,_):return getattr(cp_model,status_name)
        def status_name(self,_):return status_name
    result,proof,diag,_,_=select_and_materialize(data,columns,incumbent,road,1,solver_factory=NoSolution)
    assert result['total_fee']==incumbent['total_fee'] and proof['audit']['valid']
    assert not diag['finite_pool_cost_optimal'] and diag['fallback_reason']=='NO_VALID_CP_SAT_SOLUTION'


def test_materialization_mismatch_rejects_search_cost_and_its_optimality_claim(small_pool,monkeypatch):
    import scripts.select_application_columns_cpsat as module
    data,road=small_pool;actual=module.evaluate_candidate
    def wrong(*args,**kwargs):
        row=actual(*args,**kwargs)
        if row and not row['errors'] and row['fee'] is not None:row['fee']='0.00'
        return row
    monkeypatch.setattr(module,'evaluate_candidate',wrong)
    columns,_=reprice_vehicle_columns(data,{('S0',):[],('S1',):[],('S2',):[]},road,lambda:None)
    incumbent=assemble(data,[],list(data['tasks']),[])
    result,proof,diag,_,_=select_and_materialize(data,columns,incumbent,road,1)
    assert result['total_fee']==incumbent['total_fee'] and proof['audit']['valid']
    assert not diag['finite_pool_cost_optimal'] and diag['fallback_reason']=='SELECTED_ROUTE_MATERIALIZATION_REJECTED'


def test_prior_ledger_prices_quantities_and_vehicle_assignment_are_not_used(small_pool):
    data,_=small_pool
    ledger=[{'status':'REPRICED_CURRENT_V3','task_ids':['S1'],'vehicle_id':'old-car',
             'fee':'0','pieces':999999,'current_quote':{'fee':'0.00','meters':0}},
            {'status':'REPRICED_CURRENT_V3','task_ids':['missing']},
            {'status':'REJECTED','task_ids':['S2']}]
    orders,rejected=extract_orders(ledger,[],data)
    assert set(orders)=={('S1',)} and rejected=={'UNKNOWN_CURRENT_TASK':1}
    assert orders['S1',][0]['source_vehicle_id']=='old-car'


def test_parent_timeout_only_terminates_its_owned_process_group(monkeypatch):
    import scripts.select_application_columns_cpsat as module
    calls=[]
    class Process:
        pid=98765
        def poll(self):return None
        def wait(self,timeout):
            if timeout==1 and len(calls)==1:raise subprocess.TimeoutExpired('owned',timeout)
    monkeypatch.setattr(module.os,'killpg',lambda pid,sig:calls.append((pid,sig)))
    stop_process(Process())
    assert calls==[(98765,signal.SIGTERM),(98765,signal.SIGKILL)]


def test_parent_term_or_interrupt_sets_cooperative_stop_flag(monkeypatch):
    import scripts.select_application_columns_cpsat as module
    callbacks={};stopping=[False]
    monkeypatch.setattr(module.signal,'getsignal',lambda sig:'previous-'+str(sig))
    monkeypatch.setattr(module.signal,'signal',lambda sig,handler:callbacks.__setitem__(sig,handler))
    previous=install_parent_stop_handlers(stopping)
    assert set(previous)=={signal.SIGTERM,signal.SIGINT}
    callbacks[signal.SIGTERM](signal.SIGTERM,None)
    assert stopping[0]
    stopping[0]=False;callbacks[signal.SIGINT](signal.SIGINT,None)
    assert stopping[0]


def test_child_environment_never_inherits_keys_or_tunnel_credentials():
    env=worker_environment({'PATH':'/test/bin','HOME':'/test/home','SUKA_AMAP_WEB_SERVICE_KEY':'never-pass',
        'TUNNEL_SECRET':'never-pass','OPENAI_API_KEY':'never-pass','HTTP_PROXY':'never-pass','OMP_NUM_THREADS':'64'})
    assert env['PATH']=='/test/bin' and env['HOME']=='/test/home' and env['OMP_NUM_THREADS']=='1'
    assert not any(k.startswith(('SUKA_','TUNNEL_')) for k in env)
    assert not any(v=='never-pass' for v in env.values())


def test_failed_checkpoint_swap_preserves_previous_complete_pointer(tmp_path,monkeypatch):
    import json
    import scripts.select_application_columns_cpsat as module
    path=tmp_path/'incumbent-checkpoint.json';atomic_private_json(path,{'result_file':'old-complete-result.json'})
    def failed(*_):raise RuntimeError('swap interrupted')
    monkeypatch.setattr(module.os,'replace',failed)
    with pytest.raises(RuntimeError,match='swap interrupted'):
        atomic_private_json(path,{'result_file':'new-complete-result.json'})
    assert json.loads(path.read_text())=={'result_file':'old-complete-result.json'}
    assert not list(tmp_path.glob('.checkpoint-*.tmp'))


def test_guard_database_override_is_explicit_existing_and_separate(tmp_path):
    from scripts.prepare_corrected_application import ApplicationGuardError
    main=tmp_path/'main.db';shadow=tmp_path/'shadow.db';main.touch();shadow.touch()
    case={'paths':{'database':str(main)}}
    assert guard_database_path(case)==main.resolve()
    assert guard_database_path(case,str(shadow))==shadow.resolve()
    with pytest.raises(ApplicationGuardError):guard_database_path(case,str(main))
    with pytest.raises(ApplicationGuardError):guard_database_path(case,str(tmp_path/'missing.db'))
