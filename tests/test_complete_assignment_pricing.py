"""Whole returned allocations precede ordinary row batches; no live roads."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.route_assignment_pricing import assignment_callback,price_complete_assignment
from app.route_ortools import generate_ortools_candidates,optimize_ortools
from app.route_search import JointSearch
from app.routing_engine import StopSearch,check_result,normalize
from scripts.benchmark_algorithms import independent_audit
from test_route_columns import problem
from test_route_joint_reoptimization import independent_fee,prepared


def specs(*routes):
    return [{'vehicle_id':vid,'task_ids':list(order)} for vid,order in routes]


def test_two_required_lines_are_compactly_priced_then_considered_once_against_exact_oracle(monkeypatch):
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,initial=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    calls=[];consider=search.consider
    def once(rows,**kwargs):calls.append(rows);return consider(rows,**kwargs)
    monkeypatch.setattr(search,'consider',once)
    row=price_complete_assignment(search,specs(('V0',['0','1']),('V1',['2','3'])))
    assert len(calls)==row['consider_calls']==1 and not row['global_selection_calls']
    assert search.best['total_fee']==independent_fee(body,road.matrix)=='22.00'
    assert initial['total_fee']=='220.00' and row['direct_fee_reduction']=='198.00'
    assert row['complete_after'] and row['accepted_complete']
    assert check_result(search.data,search.best)['valid']


def test_actual_net_filters_optional_lines_and_keeps_valid_required_positive_cost_line():
    body,road=problem(n=3,cars=3,common='100')
    body['tasks'][0]['common_unit_price']=None
    body['tasks'][1]['common_unit_price']='1'
    search,_=prepared(body,road,[('V0',['0'])])
    row=price_complete_assignment(search,specs(('V0',['0']),('V1',['1']),('V2',['2'])))
    assert row['kept_mandatory_routes']==row['kept_economic_optional_routes']==1
    assert row['dropped_optional_routes']==1
    assert search.best['total_fee']=='21.00'
    assert {line['vehicle_id'] for line in search.best['routes']}=={'V0','V2'}
    assert [task['task_id'] for task in search.best['common']]==['1']
    assert check_result(search.data,search.best)['valid']


@pytest.mark.parametrize('rows,reason',[
    (specs(('V0',['0']),('V0',['1'])),'VEHICLE_REUSED'),
    (specs(('V0',['0','1']),('V1',['1'])),'TASK_OVERLAP'),
    ([{'vehicle_id':'V0','task_ids':[['not-a-string']]}],'INVALID_ORDER')])
def test_invalid_assignment_never_partly_admits_its_first_line(rows,reason):
    body,road=problem(n=4,cars=2)
    search,_=prepared(body,road,[])
    original=deepcopy(search.best);pool=deepcopy(search.pool)
    row=price_complete_assignment(search,rows)
    assert row['stop_reason']=='INVALID_PHYSICAL_ASSIGNMENT'
    assert reason in row['rejection_codes'] and not row['priced_routes']
    assert search.best==original and search.pool==pool


def test_cancellation_after_first_price_retains_the_complete_incumbent(monkeypatch):
    body,road=problem(n=4,cars=2)
    search,_=prepared(body,road,[])
    original=deepcopy(search.best);observed=[]
    road.priced_candidate_observer=observed.append
    control=search.control
    def cancelled():
        control()
        if observed:raise StopSearch('CANCELLED')
    monkeypatch.setattr(search,'control',cancelled)
    with pytest.raises(StopSearch,match='CANCELLED'):
        price_complete_assignment(search,specs(('V0',['0','1']),('V1',['2','3'])))
    assert search.best==original
    assert search.assignment_pricing_diagnostics[-1]['stop_reason']=='CANCELLED'
    assert search.assignment_pricing_diagnostics[-1]['complete_after']


def test_proved_win_is_published_even_when_selector_stops_immediately_after_acceptance(monkeypatch):
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    published=[]
    def select_then_stop():
        left=search.evaluate('V0',['0','1']);right=search.evaluate('V1',['2','3'])
        assert search.consider([left,right])
        raise StopSearch('PHASE_BUDGET')
    monkeypatch.setattr(search,'pack',select_then_stop)
    callback=assignment_callback(search,after_accept=lambda:published.append(search.best['total_fee']))
    with pytest.raises(StopSearch,match='PHASE_BUDGET') as stopped:
        callback(specs(('V0',['0','1'])))
    assert published==['22.00'] and search.best['total_fee']=='22.00'
    assert search.assignment_pricing_diagnostics[-1]['accepted_complete']
    assert stopped.value.assignment_pricing_record['direct_fee_reduction']=='198.00'


def test_post_accept_cancellation_carries_proved_gain_and_is_never_hidden():
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    def cancelled():raise StopSearch('CANCELLED')
    with pytest.raises(StopSearch,match='CANCELLED') as stopped:
        assignment_callback(search,after_accept=cancelled)(specs(('V0',['0','1']),('V1',['2','3'])))
    assert stopped.value.assignment_pricing_record['direct_fee_reduction']=='198.00'
    assert search.best['total_fee']=='22.00'


@pytest.mark.parametrize('reason',['CANCELLED','WALL_BUDGET'])
def test_global_stop_after_local_acceptance_never_starts_after_accept_proof(monkeypatch,reason):
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    published=[]
    def select_then_stop():
        assert search.consider([search.evaluate('V0',['0','1']),search.evaluate('V1',['2','3'])])
        raise StopSearch(reason)
    monkeypatch.setattr(search,'pack',select_then_stop)
    with pytest.raises(StopSearch,match=reason) as stopped:
        assignment_callback(search,after_accept=lambda:published.append(True))(specs(('V0',['0','1'])))
    assert published==[] and stopped.value.assignment_pricing_record['accepted_complete']


def test_global_stop_during_publication_supersedes_local_selector_stop(monkeypatch):
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    def select_then_stop():
        assert search.consider([search.evaluate('V0',['0','1']),search.evaluate('V1',['2','3'])])
        raise StopSearch('PHASE_BUDGET')
    def wall_stop():raise StopSearch('WALL_BUDGET')
    monkeypatch.setattr(search,'pack',select_then_stop)
    with pytest.raises(StopSearch,match='WALL_BUDGET') as stopped:
        assignment_callback(search,after_accept=wall_stop)(specs(('V0',['0','1'])))
    assert stopped.value.assignment_pricing_record['accepted_complete']


def test_missing_metric_never_triggers_http_or_replaces_required_coverage(monkeypatch):
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','1']),('V1',['2','3'])])
    del road.matrix['0','2']
    original=deepcopy(search.best);reads=road.strict_reads;selection=[]
    monkeypatch.setattr(search,'pack',lambda:selection.append(True))
    row=price_complete_assignment(search,specs(('V0',['0','2']),('V1',['1','3'])))
    assert row['missing_metric_routes']==1 and row['global_selection_calls']==1
    assert selection and road.strict_reads==reads and search.best==original


def test_quote_scope_preserves_external_whole_line_and_checks_external_car_conflicts(monkeypatch):
    body,road=problem(n=4,cars=3,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['1','0']),('V1',['2','3'])])
    frozen=deepcopy(search.best['routes'][1])
    row=price_complete_assignment(search,specs(('V2',['0','1'])),scope_ids={'0','1'})
    assert row['accepted_complete'] and search.best['total_fee']=='22.00'
    assert next(line for line in search.best['routes'] if line['vehicle_id']=='V1')==frozen
    old=deepcopy(search.best);selection=[]
    monkeypatch.setattr(search,'pack',lambda:selection.append(True))
    row=price_complete_assignment(search,specs(('V1',['0','1'])),scope_ids={'0','1'})
    assert selection and row['global_selection_calls']==1 and search.best==old
    assert len({line['vehicle_id'] for line in search.best['routes']})==len(search.best['routes'])


class FakeModel:
    def __init__(self,*args,**kwargs):
        self.virtual=[({'id':'V0'},None)];self.metrics={};self.unknown_edges=self.invalid_metrics=0
        self.solution_count=1;self.native_slices=0;self.statuses=[]
        self.nonadditive_regions=[];self.scale=self.money_divisor=self.money_multiplier=1
        self.distance_tie_break=False
        self.retention=None
        self.candidate_specs={(str(i),('unrelated',)):{'vehicle_id':str(i),'task_ids':['unrelated']} for i in range(10000)}
        self.disposed=False
    def solve(self,*args):
        self.native_slices+=1
        return object(),specs(('V0',['0']))
    def dispose(self):self.disposed=True


@pytest.mark.parametrize('enabled',[False,True])
def test_assignment_channel_is_before_batch_and_independent_of_full_candidate_cap(monkeypatch,enabled):
    monkeypatch.setattr('app.route_ortools._MeasuredModel',FakeModel)
    body,road=problem(n=1,cars=1)
    calls=[]
    def assignment(rows):calls.append(('assignment',rows));return {'direct_fee_reduction':'1.23'}
    def batch(rows):calls.append(('batch',len(rows)))
    _,diag=generate_ortools_candidates(normalize(body),lambda v,a,b:road.known_segment(a,b),1,
        strategy='GREEDY_DESCENT',complete_assignment_pricing=enabled,on_assignment=assignment,on_batch=batch)
    assert [kind for kind,_ in calls]==(['assignment','batch'] if enabled else ['batch'])
    assert diag['complete_assignment_pricing']['assignment_count']==int(enabled)
    assert diag['complete_assignment_pricing']['ordinary_pool_cap_applied'] is False
    if enabled:assert calls[0][1]==specs(('V0',['0']))


def test_native_substatistics_keep_actual_gain_when_assignment_callback_stops(monkeypatch):
    monkeypatch.setattr('app.route_ortools._MeasuredModel',FakeModel)
    body,road=problem(n=1,cars=1)
    batch=[]
    def accepted_then_stop(rows):
        exc=StopSearch('PHASE_BUDGET')
        exc.assignment_pricing_record={'accepted_complete':True,'after_fee':'22.00',
                                       'direct_fee_reduction':'198.00'}
        raise exc
    _,diag=generate_ortools_candidates(normalize(body),lambda v,a,b:road.known_segment(a,b),1,
        strategy='GREEDY_DESCENT',complete_assignment_pricing=True,
        on_assignment=accepted_then_stop,on_batch=batch.append)
    details=diag['complete_assignment_pricing']
    assert diag['stop_reason']=='PHASE_BUDGET' and batch==[]
    assert details['assignment_count']==1 and details['direct_fee_reduction']=='198.00'
    assert details['records'][0]['accepted_complete']
    assert details['records'][0]['stop_reason']=='PHASE_BUDGET'


@pytest.mark.parametrize('reason',['CANCELLED','WALL_BUDGET'])
def test_native_global_stop_carries_completed_statistics_and_does_no_batch_work(monkeypatch,reason):
    models=[]
    def model(*args,**kwargs):
        value=FakeModel(*args,**kwargs);models.append(value);return value
    monkeypatch.setattr('app.route_ortools._MeasuredModel',model)
    body,road=problem(n=1,cars=1);batch=[]
    def stop(rows):
        exc=StopSearch(reason)
        exc.assignment_pricing_record={'accepted_complete':True,'direct_fee_reduction':'198.00'}
        raise exc
    with pytest.raises(StopSearch,match=reason) as stopped:
        generate_ortools_candidates(normalize(body),lambda v,a,b:road.known_segment(a,b),1,
            strategy='GREEDY_DESCENT',complete_assignment_pricing=True,
            on_assignment=stop,on_batch=batch.append)
    diagnostic=stopped.value.native_diagnostics
    assert models[0].disposed and not batch
    assert diagnostic['stop_reason']==reason
    assert diagnostic['complete_assignment_pricing']['direct_fee_reduction']=='198.00'


@pytest.mark.parametrize('host',['main','hybrid','adaptive'])
def test_native_hosts_record_interrupted_native_statistics(monkeypatch,host):
    import app.route_ortools as native
    import app.route_portfolio as portfolio
    import app.route_adaptive_cycles as cycles
    body,road=problem(n=9,cars=2,common='100',capacity=9)
    record={'stop_reason':'CANCELLED','complete_assignment_pricing':{'direct_fee_reduction':'12.34'}}
    def cancelled(*args,**kwargs):
        exc=StopSearch('CANCELLED');exc.native_diagnostics=record;raise exc
    monkeypatch.setattr(native,'generate_ortools_candidates',cancelled)
    monkeypatch.setattr(cycles,'generate_ortools_candidates',cancelled)
    if host=='main':
        result=native.optimize_ortools(body,road,.2)
        assert result['algorithm']['routing_diagnostics']==record
        assert result['algorithm']['stop_reason']=='CANCELLED'
    elif host=='hybrid':
        monkeypatch.setattr(portfolio,'_has_readonly_start',lambda search:True)
        monkeypatch.setattr(JointSearch,'seed_cached_paths',lambda search:None)
        result=portfolio.optimize_hybrid(body,road,1)
        assert result['algorithm']['interrupted_native_diagnostics']==[record]
        assert result['algorithm']['stop_reason']=='CANCELLED'
    else:
        search,_=prepared(body,road,[])
        with pytest.raises(StopSearch,match='CANCELLED') as stopped:
            cycles.expand_candidate_cycles(search,max_cycles=1)
        assert stopped.value.candidate_expansion_diagnostics['cycles'][0]['routing']==record


def test_joint_publication_checks_global_stop_before_even_cached_global_proof(monkeypatch):
    import app.route_ortools as native
    from app.route_joint_reoptimization import reoptimize_joint_routes
    body,road=problem(n=9,cars=3,common='100',capacity=9)
    road.deep_experiments=SimpleNamespace(complete_assignment_pricing=True)
    search,_=prepared(body,road,[('V0',['0','1','2','3']),('V1',['4','5','6','7','8'])])
    stopped=[False];after_publish=[];control=search.control
    def global_control():
        if stopped[0]:raise StopSearch('CANCELLED')
        control()
    monkeypatch.setattr(search,'control',global_control)
    monkeypatch.setattr(JointSearch,'pack',lambda self:None)
    # Isolate the publication gate: other tests prove callback pricing/coverage.
    def factory(local,*,scope_ids,after_accept):
        return lambda specs:after_accept()
    monkeypatch.setattr('app.route_assignment_pricing.assignment_callback',factory)
    record={'stop_reason':'CANCELLED','complete_assignment_pricing':{'direct_fee_reduction':'0.00'}}
    def cancel_at_publication(*args,**kwargs):
        stopped[0]=True
        try:kwargs['on_assignment']([])
        except StopSearch as exc:
            exc.native_diagnostics=record
            raise
        after_publish.append(True)
        return [],record
    monkeypatch.setattr(native,'generate_ortools_candidates',cancel_at_publication)
    with pytest.raises(StopSearch,match='CANCELLED') as caught:
        reoptimize_joint_routes(search,seconds=1,max_neighborhoods=1,scalable=True)
    assert after_publish==[]
    details=caught.value.joint_reoptimization_diagnostics
    assert details['stop_reason']=='CANCELLED'
    assert details['neighborhoods'][0]['native_diagnostics']==record


def test_adaptive_false_path_keeps_original_half_remaining_duration(monkeypatch):
    import app.route_adaptive_cycles as cycles
    body,road=problem(n=9,cars=3,common='100',capacity=9)
    search,_=prepared(body,road,[]);durations=[]
    monkeypatch.setattr(search,'remaining',lambda:1.)
    def native(data,lookup,seconds,*args,**kwargs):
        durations.append(seconds);return [],{'stop_reason':'SEARCH_COMPLETED'}
    monkeypatch.setattr(cycles,'generate_ortools_candidates',native)
    cycles.expand_candidate_cycles(search,max_cycles=1)
    assert durations==[.5]


def test_real_small_assignment_is_saved_before_ordinary_batch_budget_stop():
    body,road=problem(n=4,cars=2,common=None,capacity=2)
    search,_=prepared(body,road,[('V0',['0','2']),('V1',['1','3'])])
    def fail_batch(rows):raise StopSearch('PHASE_BUDGET')
    _,diag=generate_ortools_candidates(search.data,lambda v,a,b:road.known_segment(a,b),1,
        vehicle_independent=True,strategy='GREEDY_DESCENT',complete_assignment_pricing=True,
        on_assignment=assignment_callback(search),on_batch=fail_batch)
    assert diag['stop_reason']=='PHASE_BUDGET'
    assert search.best['total_fee']==independent_fee(body,road.matrix)=='22.00'
    assert independent_audit(search.data,search.best,road)['valid']
    assert diag['complete_assignment_pricing']['assignment_count']==1
    assert Decimal(diag['complete_assignment_pricing']['direct_fee_reduction'])>0


def test_all_native_hosts_forward_the_independent_complete_assignment_channel(monkeypatch):
    import app.route_ortools as native
    import app.route_portfolio as portfolio
    import app.route_adaptive_cycles as cycles
    from app.route_joint_reoptimization import reoptimize_joint_routes
    body,road=problem(n=9,cars=3,common='100',capacity=9)
    road.deep_experiments=SimpleNamespace(complete_assignment_pricing=True)
    calls=[]
    def fake(*args,**kwargs):calls.append(kwargs);return [],{'stop_reason':'SEARCH_COMPLETED'}
    monkeypatch.setattr(native,'generate_ortools_candidates',fake)
    monkeypatch.setattr(cycles,'generate_ortools_candidates',fake)
    optimize_ortools(body,road,.2)
    search,_=prepared(body,road,[])
    portfolio._native(search,.1,'GREEDY_DESCENT','ALL_UNPERFORMED',0)
    cycles.expand_candidate_cycles(search,max_cycles=1)
    joint,_=prepared(body,road,[('V0',['0','1','2','3']),('V1',['4','5','6','7','8'])])
    monkeypatch.setattr(JointSearch,'pack',lambda self:None)
    monkeypatch.setattr(JointSearch,'generate',lambda self:None)
    reoptimize_joint_routes(joint,seconds=.3,max_neighborhoods=1,scalable=False)
    assert len(calls)==4
    assert all(call['complete_assignment_pricing'] and callable(call['on_assignment']) for call in calls)
