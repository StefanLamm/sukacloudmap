"""Mocked fixed-route fresh-reference acceptance; no real provider requests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.planning_inputs import content_hash
from app.routing_engine import assemble,evaluate_route,normalize
from scripts.benchmark_algorithms import private_json
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    prepare_application,verified_application_result)
from scripts.refresh_application_roads import APPLICATION_SCOPE,refresh_application
from test_corrected_application import application_study,private_case


class NoWaitLimiter:
    def __init__(self):self.acquisitions=[];self.retries=[]
    def acquire(self,service,**_):self.acquisitions.append(service)
    def defer(self,service,attempt):self.retries.append((service,attempt))


@pytest.fixture
def fresh_case(private_case,tmp_path,monkeypatch):
    import scripts.refresh_application_roads as module
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study,_=application_study(private_case,prepared,monkeypatch)
    data=normalize(private_case['new']);road=ApplicationFrozenRoad(prepared['private_graph'])
    try:
        result=assemble(data,[evaluate_route(data,'VW',['S0'],road),evaluate_route(data,'VX',['S1','S2'],road)],[],[])
        proof=verified_application_result(data,result,road)
    finally:road.close()
    result['algorithm']={'mode':'HYBRID_COST_PORTFOLIO','optimality_proven':False,
        'candidate_pool_selection':{'candidate_pool_optimal':True,'cost_candidate_pool_optimal':True,'cost_mip_gap':0},
        'correction_application':{'measured_graph_hash':prepared['private_graph_hash'],'global_corrected_input_optimality_proven':False}}
    result_path=tmp_path/'source-result.json';private_json(result_path,result)
    summary=json.loads(study.read_text())
    report={'status':'VALIDATED_READY_FOR_GUARDED_APPEND','scope':APPLICATION_SCOPE,'case_hash':content_hash(prepared),
        'original_input_hash':prepared['transition']['original_input_hash'],'study_summary_hash':content_hash(summary),
        'profile_rank':1,'selected_profile':{'name':'hybrid','entrypoint':'app.route_portfolio:optimize_hybrid','options':{}},
        'eligible_for_guarded_append':True,'source_hashes_unchanged':True,'external_calls':0,
        'source_business_database_written':False,'corrected_input_hash':content_hash(private_case['new']),
        'private_graph_hash':prepared['private_graph_hash'],'verification_script_sha256':prepared['verification_script_sha256'],
        'result_file':str(result_path),'result_hash':content_hash(result),'proof':proof}
    application=tmp_path/'application-report.json';private_json(application,report)
    settings=Settings(_env_file=None,environment='test',planning_route_mode='DRIVING',planning_road_source='LIVE',
        driving_live_calls_enabled=True,amap_web_service_key=SecretStr('fixture-not-a-real-key'),
        amap_rate_limit_path=tmp_path/'shared-pacing.db',amap_reference_graph_path=tmp_path/'do-not-touch-production.db')
    guard=deepcopy(private_case['guard']);guard['head_total_fee']=guard['head_fee_summary']['total_fee']
    monkeypatch.setattr(module,'main_guard',lambda *_:deepcopy(guard))
    return {'prepared':prepared,'study':study,'application':application,'result':result_path,
            'settings':settings,'guard':guard,'tmp':tmp_path}


def provider_response(request,*,duration=120):
    origin=request.url.params['origin'];destination=request.url.params['destination']
    return httpx.Response(200,json={'status':'1','infocode':'10000','route':{'paths':[
        {'distance':'2000','cost':{'duration':str(duration)},'steps':[{'polyline':origin+';'+destination}]}]}})


def execute(case,handler,**kwargs):
    return refresh_application(case['prepared']['case_file'],case['study'],case['application'],
        budget_seconds=30,settings=case['settings'],transport=httpx.MockTransport(handler),limiter=NoWaitLimiter(),**kwargs)


def test_fresh_fixed_routes_have_new_actual_sources_full_proof_and_exact_fee_bridge(fresh_case):
    originals={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in [fresh_case['prepared']['private_graph'],
        fresh_case['study'],fresh_case['application'],fresh_case['result']]}
    requests=[]
    def handle(request):requests.append(request);return provider_response(request)
    report=execute(fresh_case,handle)
    assert report['status']=='READY' and report['eligible_for_guarded_append']
    assert report['actual_external_calls']==len(requests)==3
    assert report['external_calls']==report['offline_proof_external_calls']==0
    assert report['network_stats']['historical_segment_hits']==0
    assert report['network_stats']['call_limit'] is None
    assert report['network_stats']['total_call_policy']=='PRODUCTION_UNLIMITED'
    assert report['network_stats']['wall_deadline'] is not None
    assert report['selected_provider_keys']==report['freshened_provider_keys']
    assert len(report['selected_provider_sources'])==3
    assert report['proof']['audit']['valid'] and report['proof']['audit']['unassigned_tasks']==0
    assert report['proof']['road_evidence']['historical_reference_segments']==0
    assert report['fee_bridge']['corrected_frozen_fee']=='3.00'
    assert report['fee_bridge']['fresh_fixed_order_fee']=='6.00' and report['fee_bridge']['fresh_reference_delta']=='3.00'
    assert report['fee_bridge']['delta_from_current_head']=='-294.00'
    assert report['fresh_graph_hash']!=report['parent_private_graph_hash']
    assert report['fresh_graph_sha256']==hashlib.sha256(Path(report['fresh_graph_file']).read_bytes()).hexdigest()
    result=json.loads(Path(report['result_file']).read_text());old=json.loads(fresh_case['result'].read_text())
    assert all(s['frozen_at']>=s['received_at'] for r in result['routes'] for s in r['segments'])
    assert [(r['vehicle_id'],r['task_ids']) for r in result['routes']]==[(r['vehicle_id'],r['task_ids']) for r in old['routes']]
    assert result['comparison']['input_changed'] and result['comparison']['delta']=='-294.00'
    assert result['algorithm']['old_graph_certificates_cleared'] and not result['algorithm']['cost_candidate_pool_optimal']
    assert result['algorithm']['stop_reason']=='SELECTED_ROADS_REFRESHED'
    assert result['algorithm']['fresh_reference']['fresh_graph_hash']==report['fresh_graph_hash']
    assert result['algorithm']['origin_algorithm_metadata']['candidate_pool_selection']['cost_mip_gap'] is None
    assert not result['algorithm']['origin_algorithm_metadata']['candidate_pool_selection']['candidate_pool_optimal']
    assert not result['confirmable'] and result['road_source']=='DRIVING'
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in originals.items())
    assert not fresh_case['settings'].amap_reference_graph_path.exists()
    assert Path(report['fresh_graph_file']).stat().st_mode&0o777==0o600
    assert not Path(report['fresh_graph_file']+'-wal').exists()


def test_provider_qps_retry_is_counted_separately_from_offline_proof(fresh_case):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(429) if len(requests)==1 else provider_response(request)
    report=execute(fresh_case,handle)
    assert report['status']=='READY' and report['actual_external_calls']==4
    assert len(report['selected_provider_keys'])==3 and report['offline_proof_external_calls']==0


@pytest.mark.parametrize('failure',['graph-write-skipped','provider-error','arrival-infeasible'])
def test_failure_never_publishes_ready_or_final_result(fresh_case,monkeypatch,failure):
    if failure=='graph-write-skipped':monkeypatch.setattr('app.road_graph.ReferenceGraph.put_reference',lambda *_:False)
    def handle(request):
        if failure=='provider-error':return httpx.Response(403)
        return provider_response(request,duration=100*3600 if failure=='arrival-infeasible' else 120)
    with pytest.raises(ApplicationGuardError):execute(fresh_case,handle)
    output=Path(fresh_case['prepared']['case_file']).parent/'selected-roads-refreshed'
    report=json.loads((output/'refresh-report.json').read_text())
    assert report['status']=='FAILED' and not report['eligible_for_guarded_append']
    assert not report['source_business_database_written'] and not (output/'fresh-result.json').exists()


def test_late_head_change_keeps_private_proof_but_prevents_ready(fresh_case,monkeypatch):
    import scripts.refresh_application_roads as module
    calls=[]
    def changed(*_):
        calls.append(1);guard=deepcopy(fresh_case['guard'])
        if len(calls)>1:guard['guard_token']='LATER_HEAD'
        return guard
    monkeypatch.setattr(module,'main_guard',changed)
    report=execute(fresh_case,provider_response)
    assert report['status']=='MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND'
    assert not report['eligible_for_guarded_append'] and report['proof']['audit']['valid']


def test_changed_source_artifact_is_fail_closed_after_actual_network(fresh_case):
    def handle(request):
        if not getattr(handle,'changed',False):
            fresh_case['application'].write_text(fresh_case['application'].read_text()+'\n');handle.changed=True
        return provider_response(request)
    with pytest.raises(ApplicationGuardError,match='input artifact changed'):execute(fresh_case,handle)
    output=Path(fresh_case['prepared']['case_file']).parent/'selected-roads-refreshed'
    assert json.loads((output/'refresh-report.json').read_text())['status']=='FAILED'
    assert not (output/'fresh-result.json').exists()


def test_live_calls_require_explicit_flag_and_refuse_main_db_as_pacer(fresh_case):
    with pytest.raises(ApplicationGuardError,match='explicit --execute-live'):
        refresh_application(fresh_case['prepared']['case_file'],fresh_case['study'],fresh_case['application'],
            settings=fresh_case['settings'],budget_seconds=30)
    fresh_case['settings'].amap_rate_limit_path=Path(fresh_case['prepared']['paths']['database'])
    with pytest.raises(ApplicationGuardError,match='rate limiter must not write'):
        execute(fresh_case,provider_response)
    assert not (Path(fresh_case['prepared']['case_file']).parent/'selected-roads-refreshed').exists()


def test_merged_application_origin_is_strictly_supported_without_profile_rank(fresh_case):
    from scripts.merge_application_routes import SCOPE
    report=json.loads(fresh_case['application'].read_text());prepared=fresh_case['prepared']
    root=Path(prepared['case_file']).parent
    origins=[Path(prepared['case_file']),fresh_case['study'],root/'input.json',root/'original-input.json',fresh_case['result']]
    report.update(scope=SCOPE,merge_script_sha256=hashlib.sha256(Path('scripts/merge_application_routes.py').read_bytes()).hexdigest(),
        origin_files={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in origins})
    for field in ['profile_rank','selected_profile','original_input_hash','study_summary_hash']:report.pop(field)
    private_json(fresh_case['application'],report)
    refreshed=execute(fresh_case,provider_response)
    assert refreshed['status']=='READY' and refreshed['source_application_scope']==SCOPE
    assert set(report['origin_files']).issubset(refreshed['origin_files_sha256'])


@pytest.mark.parametrize('change',['unknown-scope','unregistered-profile','merge-script-sha'])
def test_unbound_application_origin_is_rejected_before_network(fresh_case,change):
    from scripts.merge_application_routes import SCOPE
    report=json.loads(fresh_case['application'].read_text())
    if change=='unknown-scope':report['scope']='arbitrary unverified candidate'
    elif change=='unregistered-profile':report['selected_profile']['entrypoint']='other.module:run'
    else:report.update(scope=SCOPE,merge_script_sha256='0'*64)
    private_json(fresh_case['application'],report)
    with pytest.raises(ApplicationGuardError):execute(fresh_case,lambda *_:pytest.fail('Origin must reject before HTTP'))
    assert not (Path(fresh_case['prepared']['case_file']).parent/'selected-roads-refreshed').exists()
