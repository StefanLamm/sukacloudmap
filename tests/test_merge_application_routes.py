"""Tiny offline acceptance cases; run native selection only after formal study."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import pytest

from app.planning_inputs import content_hash
from app.route_selection import select_candidate_routes
from app.routing_engine import assemble,evaluate_route,normalize
from scripts.benchmark_algorithms import private_json
from scripts.merge_application_routes import (SCOPE,compact,materialize_selection,plan_recipes,
    recipe_problem,repaired_recipe,reprice_pool,run_merge)
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    prepare_application,verified_application_result)
from test_corrected_application import application_study,input_pair,private_case


def test_old_economics_are_not_retained_in_route_recipes():
    raw={'routes':[{'vehicle_id':'V','task_ids':['S'],'fee':'0.00','pieces':999999,
                    'meters':0,'segments':[{'verified':True}]}],'common':[],'unassigned':[],'total_fee':'0.00'}
    source=plan_recipes(raw,'/tmp/recipe.json','old','LEGACY_RECIPE_ONLY')
    assert source['recipes']==[{'vehicle_id':'V','task_ids':['S']}]
    assert source['old_total_fee_display_only']=='0.00'


@pytest.mark.parametrize('recipe,code',[
    ({'vehicle_id':'missing','task_ids':['S0']},'UNKNOWN_VEHICLE'),
    ({'vehicle_id':'VW','task_ids':['S0','S0']},'DUPLICATE_TASK_IN_RECIPE'),
    ({'vehicle_id':'VW','task_ids':['not-in-current-demand']},'UNKNOWN_CURRENT_TASK'),
    ({'vehicle_id':'VW','task_ids':'S0'},'MALFORMED_TASK_IDS'),
])
def test_bad_historical_recipe_is_rejected_before_road_lookup(recipe,code):
    assert recipe_problem(normalize(input_pair()[1]),recipe)==code


def test_only_wrong_warehouse_stops_are_repaired_to_priced_common():
    old,new,_=input_pair();data=normalize(new)
    source=plan_recipes({'routes':[{'vehicle_id':'VW','task_ids':['S0','S1']},
        {'vehicle_id':'VX','task_ids':['S2']}],'common':[],'unassigned':[]},'/tmp/old-plan.json','legacy','LEGACY_RECIPE_ONLY')
    repaired,removed=repaired_recipe(data,source)
    assert removed==['S1'] and repaired['common_ids']==['S1']
    assert repaired['recipes']==[{'vehicle_id':'VW','task_ids':['S0']},{'vehicle_id':'VX','task_ids':['S2']}]
    data['tasks']['S1']['common_unit_price']=None
    with pytest.raises(ApplicationGuardError):repaired_recipe(data,source)


@pytest.fixture
def merge_case(private_case,tmp_path,monkeypatch):
    import scripts.merge_application_routes as module
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,_=application_study(private_case,prepared,monkeypatch)
    data=normalize(private_case['new']);road=ApplicationFrozenRoad(prepared['private_graph'])
    reports=[]
    try:
        for name,routes,common in [('A',[('VW',['S0'])],['S1','S2']),('B',[('VX',['S1','S2'])],['S0'])]:
            result=assemble(data,[evaluate_route(data,vid,ids,road) for vid,ids in routes],common,[])
            proof=verified_application_result(data,result,road)
            path=tmp_path/(name+'-result.json');private_json(path,result)
            report={'source_hashes_unchanged':True,'external_calls':0,'source_business_database_written':False,
                'corrected_input_hash':content_hash(private_case['new']),'private_graph_hash':prepared['private_graph_hash'],
                'verification_script_sha256':prepared['verification_script_sha256'],'result_hash':content_hash(result),
                'result_file':str(path),'proof':proof}
            report_path=tmp_path/(name+'-report.json');private_json(report_path,report);reports.append(report_path)
    finally:road.close()
    monkeypatch.setattr(module,'main_guard',lambda *_:deepcopy(private_case['guard']))
    return prepared,study_file,reports


def test_joint_pool_combines_two_plans_and_respects_one_route_per_physical_vehicle(merge_case):
    prepared,study,reports=merge_case
    originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in [study,*reports]}
    report=run_merge(prepared['case_file'],study,reports,budget_seconds=10,selection_seconds=2)
    assert report['before_current_v3_fee']=='102.00' and report['merged_fee']=='3.00'
    assert report['delta_from_current_v3']=='-99.00' and report['physical_vehicle_reuse']==0
    assert report['task_count']==3 and report['proof']['audit']['valid']
    assert report['selection_diagnostics']['scope']=='CANDIDATE_POOL_ONLY'
    assert report['pool']['physical_vehicle_column_counts']['VX']>=2
    assert report['pool']['rejected_reason_counts']['WAREHOUSE']>=1
    assert report['external_calls']==0 and not report['source_business_database_written']
    assert report['scope']==SCOPE and not report['global_225_task_optimality_proven']
    result=json.loads(Path(report['result_file']).read_text())
    assert result['algorithm']['mode']=='HYBRID_COST_PORTFOLIO'
    assert result['algorithm']['selection_scope']==SCOPE and not result['algorithm']['optimality_proven']
    assert result['algorithm']['stop_reason'] in {'SEARCH_COMPLETED','TIME_BUDGET'}
    assert result['algorithm']['budget_seconds']==10
    assert result['algorithm']['distinct_route_count']==report['pool']['unique_ordered_columns']
    assert result['algorithm']['road_stats']['calls']==0
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in originals.items())


def test_running_formal_study_rejects_before_route_selection(merge_case,monkeypatch):
    import scripts.merge_application_routes as module
    prepared,study,reports=merge_case;summary=json.loads(study.read_text());summary['status']='RUNNING';private_json(study,summary)
    monkeypatch.setattr(module,'select_candidate_routes',lambda *_a,**_k:pytest.fail('Must not run selection during study'))
    with pytest.raises(ApplicationGuardError,match='not complete'):
        run_merge(prepared['case_file'],study,reports,budget_seconds=10,selection_seconds=2)
    assert not (Path(prepared['case_file']).parent/'cross-run-merged').exists()


def test_late_head_change_retains_valid_private_result_but_refuses_append(merge_case,monkeypatch):
    import scripts.merge_application_routes as module
    prepared,study,reports=merge_case;guard=deepcopy(prepared['main_guard']);calls=[]
    def changed(*_):
        calls.append(1);value=deepcopy(guard)
        if len(calls)>1:value['guard_token']='OTHER_HEAD'
        return value
    monkeypatch.setattr(module,'main_guard',changed)
    report=run_merge(prepared['case_file'],study,reports,budget_seconds=10,selection_seconds=2)
    assert report['proof']['audit']['valid'] and not report['eligible_for_guarded_append']
    assert report['status']=='MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND'


def test_source_file_change_during_selection_is_fail_closed(merge_case,monkeypatch):
    import scripts.merge_application_routes as module
    prepared,study,reports=merge_case
    def changed(_data,_columns,incumbent,*_args,**_kwargs):
        reports[0].write_text('changed after source capture')
        return incumbent,{'scope':'CANDIDATE_POOL_ONLY','fallback':True}
    monkeypatch.setattr(module,'select_candidate_routes',changed)
    with pytest.raises(ApplicationGuardError,match='Source changed during merge'):
        run_merge(prepared['case_file'],study,reports,budget_seconds=10,selection_seconds=2)
    assert not (Path(prepared['case_file']).parent/'cross-run-merged').exists()


def test_legacy_manifest_cannot_substitute_another_source_file(merge_case,tmp_path):
    prepared,study,reports=merge_case
    manifest={'formal_original_input_hash':prepared['transition']['original_input_hash'],
        'corrected_input_hash':prepared['transition']['corrected_input_hash'],
        'formal_original_graph_hash':prepared['base_graph']['graph_hash'],'corrected_graph_hash':prepared['private_graph_hash'],
        'candidates':[{'result_file':json.loads(reports[0].read_text())['result_file'],'result_file_sha256':'0'*64}]}
    path=tmp_path/'legacy.json';private_json(path,manifest)
    with pytest.raises(ApplicationGuardError,match='Source file hash differs'):
        run_merge(prepared['case_file'],study,reports,legacy_manifest=path,budget_seconds=10,selection_seconds=2)


def test_same_source_vehicle_recipes_can_be_reassigned_to_distinct_physical_cars(private_case,tmp_path):
    prepared=prepare_application(private_case['inspection'],tmp_path/'reassignment')
    data=normalize(private_case['new']);data['vehicles']['VX']['max_pieces']=1
    for vid,capacity,available in [('VX2',1,True),('SMALL',0,True),('OFF',1,False)]:
        data['vehicles'][vid]={**data['vehicles']['VX'],'id':vid,'max_pieces':capacity,'available':available}
    sources=[plan_recipes({'routes':[{'vehicle_id':'VX','task_ids':[sid]}]},'/tmp/'+sid+'.json',sid,'SOURCE')
             for sid in ['S1','S2']]
    sources.append(plan_recipes({'routes':[{'vehicle_id':'VW','task_ids':['S0']}]},'/tmp/S0.json','S0','SOURCE'))
    road=ApplicationFrozenRoad(prepared['private_graph'])
    try:
        incumbent=assemble(data,[evaluate_route(data,'VW',['S0'],road),evaluate_route(data,'VX',['S1'],road)],['S2'],[])
        assert incumbent['total_fee']=='102.00'
        columns,ledger,stats=reprice_pool(data,sources,road,lambda:None)
        assert all(c.get('_estimated') for c in columns)
        assert stats['vehicle_assignment_scope']=='EVERY_CURRENT_PHYSICAL_VEHICLE_REVALIDATED'
        assert any(e['source_vehicle_id']=='VX' and e.get('candidate_vehicle_id')=='VX2'
                   and e.get('vehicle_reassigned') and e['status']=='REPRICED_CURRENT_V3' for e in ledger)
        for vid,reason in [('SMALL','CAPACITY'),('OFF','VEHICLE_UNAVAILABLE'),('VW','WAREHOUSE')]:
            assert any(e.get('candidate_vehicle_id')==vid and reason in e.get('reasons',[]) for e in ledger)
        chosen,diagnostics=select_candidate_routes(data,columns,[compact(r) for r in incumbent['routes']],2)
        result,proof,diagnostics=materialize_selection(data,chosen,incumbent,road,diagnostics)
        assert result['total_fee']=='3.00' and proof['audit']['valid']
        assert {r['vehicle_id'] for r in result['routes']}=={'VW','VX','VX2'}
        assert len(result['routes'])==len({r['vehicle_id'] for r in result['routes']})
        assert all(not r.get('_estimated') and r['segments'] for r in result['routes'])
    finally:road.close()


@pytest.mark.parametrize('failure',['metrics-mismatch','missing-full-evidence'])
def test_search_columns_cannot_bypass_full_evidence_and_fall_back(private_case,tmp_path,monkeypatch,failure):
    import scripts.merge_application_routes as module
    prepared=prepare_application(private_case['inspection'],tmp_path/'fallback')
    data=normalize(private_case['new']);road=ApplicationFrozenRoad(prepared['private_graph'])
    try:
        incumbent=assemble(data,[],list(data['tasks']),[])
        route=evaluate_route(data,'VX',['S1'],road);column=compact(route);column['_estimated']=True
        if failure=='metrics-mismatch':column['fee']='0.00'
        else:
            invalid=deepcopy(route);invalid['segments']=[]
            monkeypatch.setattr(module,'evaluate_route',lambda *_a,**_k:invalid)
        result,proof,diag=materialize_selection(data,[column],incumbent,road,
            {'scope':'CANDIDATE_POOL_ONLY','candidate_pool_optimal':True,'cost_candidate_pool_optimal':True})
        assert result['total_fee']==incumbent['total_fee'] and proof['audit']['valid']
        assert diag['fallback_reason']=='SELECTED_ROUTE_MATERIALIZATION_REJECTED'
        assert not diag['candidate_pool_optimal'] and not diag['cost_candidate_pool_optimal']
        assert all(not r.get('_estimated') for r in result['routes'])
    finally:road.close()


def test_missing_measured_metrics_are_rejected_without_full_route_or_network_lookup(monkeypatch):
    import scripts.merge_application_routes as module
    data=normalize(input_pair()[1]);source=plan_recipes({'routes':[{'vehicle_id':'old-uuid','task_ids':['S1']}]},
        '/tmp/old.json','old','SOURCE')
    class MissingRoad:
        def known_segment(self,*_):return None
        def segment(self,*_):pytest.fail('Search may not fetch geometry or use a provider')
    monkeypatch.setattr(module,'evaluate_route',lambda *_a,**_k:pytest.fail('Search must use measured metrics only'))
    columns,ledger,_=reprice_pool(data,[source],MissingRoad(),lambda:None)
    assert not columns
    assert any('MEASURED_ROUTE_METRICS_MISSING' in e.get('reasons',[]) for e in ledger)


@pytest.mark.parametrize('extra_kind',['valid-cheaper','incomplete','unknown-vehicle'])
def test_complete_extra_incumbent_is_protected_before_pool_budget_exhaustion(merge_case,tmp_path,monkeypatch,extra_kind):
    import scripts.merge_application_routes as module
    prepared,study,reports=merge_case
    body=json.loads((Path(prepared['case_file']).parent/'input.json').read_text());data=normalize(body)
    road=ApplicationFrozenRoad(prepared['private_graph'])
    try:
        extra=assemble(data,[evaluate_route(data,'VW',['S0'],road),evaluate_route(data,'VX',['S1','S2'],road)],[],[])
    finally:road.close()
    # No old economic value is trusted, even for a complete extra allocation.
    extra['total_fee']='0.00'
    for route in extra['routes']:route.update(fee='0.00',pieces=999999,meters=0,seconds=0)
    if extra_kind=='incomplete':extra['routes'].pop()
    if extra_kind=='unknown-vehicle':extra['routes'][0]['vehicle_id']='not-a-current-physical-car'
    path=tmp_path/'extra-result.json';private_json(path,extra)
    monkeypatch.setattr(module,'reprice_pool',lambda *_a,**_k:([],[],{
        'repricing_stop_reason':'REPRICING_TIME_BUDGET','unique_ordered_columns':0}))
    monkeypatch.setattr(module,'select_candidate_routes',lambda _d,_c,inc,*_a,**_k:(inc,{
        'scope':'CANDIDATE_POOL_ONLY','fallback':True,'fallback_reason':'TIME_LIMIT'}))
    report=run_merge(prepared['case_file'],study,reports,extra_results=[path],budget_seconds=10,selection_seconds=2)
    assert report['proof']['audit']['valid'] and report['before_current_v3_fee']=='102.00'
    if extra_kind=='valid-cheaper':
        assert report['merged_fee']=='3.00' and report['incumbent_origin']=='additional:0'
        assert any(row.get('status')=='REPRICED_EXTRA_COMPLETE_BEFORE_POOL' and row['current_fee']=='3.00'
                   and row['became_incumbent'] for row in report['whole_plan_audits'])
    else:
        assert report['merged_fee']=='102.00' and report['incumbent_origin']!='additional:0'
        assert any(row.get('status')=='EXTRA_NOT_USABLE_AS_WHOLE_INCUMBENT' for row in report['whole_plan_audits'])
