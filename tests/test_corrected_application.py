"""Read-only application preparation guards, using tiny private source graphs."""
from copy import deepcopy
from contextlib import ExitStack,closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.planning_inputs import content_hash
from app.road_service import DrivingRoad,evidence
from app.routing_engine import assemble,evaluate_route,normalize
from scripts.benchmark_algorithms import compatible_pairs,decode_evidence,private_json,read_only,write_graph
from scripts.prepare_corrected_application import (ApplicationFrozenRoad,ApplicationGuardError,
    application_run_output,graph_state,inspect_application,main_guard,prepare_application,readonly_session,
    run_application,validate_transition,verified_application_result,verify_source_ready,winner_profile)
from scripts.run_algorithm_study import MATH_SOURCES,RESEARCH_SOURCES,profile_ranking
from test_stage4 import basis


ROOT=Path(__file__).resolve().parents[1]


def input_pair():
    quote={'id':'Q1','mode':'KM','per_km':'1'}
    warehouses=[{'id':wid,'name':wid,'code':wid,'longitude':lng,'latitude':'31','crs':'GCJ02',
                 'earliest_departure_at':'2026-11-12T06:00:00+08:00','store_count':count}
                for wid,lng,count in [('W','117',2),('X','118',1)]]
    stores=[];tasks=[]
    for n,wh in enumerate(['W','W','X']):
        sid='S'+str(n)
        stores.append({'id':sid,'code':sid,'name':sid,'warehouse_id':wh,'warehouse_name':wh,'revision':1,
            'updated_at':'2026-11-12T00:00:00+08:00','longitude':str(117.1+n*.1),'latitude':'31','crs':'GCJ02',
            'business_open_at':'2026-11-12T06:00:00+08:00','business_close_at':'2026-11-12T23:00:00+08:00',
            'unload_minutes':0,'latest_arrival_at':None,'dedicated_quote_version':quote,'common_unit_price':'100'})
        tasks.append({'id':'D'+str(n),'store_id':sid,'warehouse_id':wh,'warehouse_name':wh,'pieces':1,'common_unit_price':'100'})
    def totals(rows):
        return {'task_count':len(rows),'total_pieces':sum(r['pieces'] for r in rows),'warehouses':[]}
    old={'schema_version':1,'source':'FROZEN','group_id':'G','delivery_date':'2026-11-12','stores':stores,'tasks':tasks,
        'warehouses':warehouses,'quotes':[quote],'vehicles':[{'id':'V'+wid,'warehouse_id':wid,'day_warehouse_id':wid,
            'available':True,'day_available':True,'max_pieces':10} for wid in ['W','X']],
        'configuration_hash':'old-config','source_stamp':{'master_revision':7,'stores':[],'vehicles':[],'warehouses':[]},
        'demand_revision':1,'totals':totals(tasks)}
    new=deepcopy(old);new['configuration_hash']='new-config';new['source_stamp']['master_revision']=8
    new['stores'][1].update(warehouse_id='X',warehouse_name='X',revision=2,updated_at='2026-11-13T01:00:00+08:00')
    new['tasks'][1].update(warehouse_id='X',warehouse_name='X')
    new['warehouses'][0]['store_count']-=1;new['warehouses'][1]['store_count']+=1
    new['totals']=totals(new['tasks'])
    correction={'store_id':'S1','before':{'warehouse_id':'W'},'after':{'warehouse_id':'X'},
                'revision_before':1,'revision_after':2,'new_snapshot_id':'SNAP3','new_snapshot_version':3,
                'new_snapshot_hash':content_hash(new)}
    return old,new,correction


def frozen_source(body,pair):
    data=normalize(body);points={**data['warehouses'],**data['tasks']};a,b=[points[key] for key in pair]
    return evidence(DrivingRoad.reference_request(a,b),1000,60,'DRIVING',False,
              [[float(a['longitude']),float(a['latitude'])],[float(b['longitude']),float(b['latitude'])]],
              'TRUCK_NOT_VERIFIED',time_status='CURRENT_TRAFFIC_REFERENCE',received_at=int(time.time())-7200)


def ready_file(path):
    names=set(MATH_SOURCES)|set(RESEARCH_SOURCES)|{'app/route_cache.py','app/road_graph.py','app/route_baseline.py','app/config.py'}
    ready={'status':'READY','deadline_epoch':int(time.time())+3600,'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}}
    private_json(path,ready);return ready


@pytest.fixture
def private_case(tmp_path,monkeypatch):
    import scripts.prepare_corrected_application as module
    old,new,correction=input_pair();old_pairs=compatible_pairs(normalize(old));new_pairs=compatible_pairs(normalize(new))
    for name,value in [('old.json',old),('new.json',new),('correction.json',correction)]:private_json(tmp_path/name,value)
    base=tmp_path/'base.db';extension=tmp_path/'extension.db'
    write_graph(base,old,[frozen_source(old,pair) for pair in sorted(old_pairs)])
    write_graph(extension,new,[frozen_source(new,pair) for pair in sorted(new_pairs-old_pairs)])
    for path in [base,extension]:
        with sqlite3.connect(path) as connection:
            n=connection.execute('SELECT count(*) FROM metrics').fetchone()[0]
            connection.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                  [('complete','true'),('expected_provider_keys',str(n)),('status','"COMPLETED"')])
    summary={'status':'COMPLETED','input_hash':content_hash(new),'errors':[]}
    private_json(tmp_path/'extension.json',summary);ready=ready_file(tmp_path/'ready.json')
    guard={'snapshot_id':'SNAP3','expected_head':4,'head_id':'PLAN4','guard_token':'UNCHANGED','snapshot_hash':content_hash(new),
           'head_input_hash':content_hash(old),'head_fee_summary':{'total_fee':'300.00','fees_complete':True,
                'check':{'dedicated_fee':'0.00','common_fee':'300.00'}}}
    monkeypatch.setattr(module,'main_guard',lambda *_:deepcopy(guard))
    inspection=inspect_application(tmp_path/'main.db',tmp_path/'old.json',tmp_path/'new.json',tmp_path/'correction.json',
                base,extension,tmp_path/'extension.json',tmp_path/'ready.json',3)
    return {'old':old,'new':new,'correction':correction,'inspection':inspection,'base':base,'extension':extension,
            'ready':ready,'guard':guard,'folder':tmp_path}


@pytest.mark.parametrize('change',['quantity','price','coordinate','vehicle','other_store','wrong_store','warehouse_count'])
def test_only_authorized_input_transition_is_accepted(change):
    old,new,correction=input_pair()
    if change=='quantity':new['tasks'][0]['pieces']=2
    if change=='price':new['stores'][0]['common_unit_price']='200'
    if change=='coordinate':new['stores'][1]['longitude']='120'
    if change=='vehicle':new['vehicles'][0]['max_pieces']=999
    if change=='other_store':new['stores'][0]['revision']=2
    if change=='wrong_store':new['tasks'][1]['warehouse_id']='W'
    if change=='warehouse_count':new['warehouses'][0]['store_count']=999
    correction['new_snapshot_hash']=content_hash(new)
    with pytest.raises(ApplicationGuardError):validate_transition(old,new,correction,3)


def test_partial_formal_graph_is_not_prepared(private_case,tmp_path):
    case=deepcopy(private_case['inspection']);case['status']='WAITING_COMPLETE_BASE_GRAPH'
    target=tmp_path/'must-not-exist'
    with pytest.raises(ApplicationGuardError):prepare_application(case,target)
    assert not target.exists()


def test_private_merge_keeps_original_and_extension_bytes_unchanged(private_case,tmp_path):
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in [private_case['base'],private_case['extension']]}
    prepared=prepare_application(private_case['inspection'],tmp_path/'application')
    assert prepared['status']=='PREPARED' and prepared['new_correction_directions']==3
    assert Path(prepared['private_graph']).stat().st_mode&0o777==0o600
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in before.items())
    road=ApplicationFrozenRoad(prepared['private_graph'])
    try:
        data=normalize(private_case['new'])
        assert all(road.known_segment(a,b) is not None for a,b in compatible_pairs(data))
        segment=road.segment(data['vehicles']['VX'],data['warehouses']['X'],data['tasks']['S1'],'2026-11-12T06:00:00+08:00')
        assert segment['time_status']=='HISTORICAL_TRAFFIC_REFERENCE' and not segment['verified']
        assert segment['received_at']==segment['provider_received_at']
    finally:road.close()


def test_wal_sources_are_closed_and_consolidated_before_private_graph_rename(private_case,tmp_path):
    changed={}
    with ExitStack() as stack:
        for source in [private_case['base'],private_case['extension']]:
            writer=stack.enter_context(closing(sqlite3.connect(source)))
            writer.row_factory=sqlite3.Row
            assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
            writer.execute('PRAGMA wal_autocheckpoint=0')
            row=writer.execute('SELECT * FROM metrics ORDER BY provider_key LIMIT 1').fetchone()
            item=decode_evidence(row['content']);item['meters']+=17;item['seconds']+=3
            item['evidence_hash']=content_hash({k:v for k,v in item.items() if k!='evidence_hash'})
            writer.execute('UPDATE metrics SET meters=?,seconds=?,evidence_hash=?,content=? WHERE provider_key=?',
                (item['meters'],item['seconds'],item['evidence_hash'],gzip.compress(json.dumps(item).encode(),mtime=0),row['provider_key']))
            writer.commit();changed[row['provider_key']]=item
            assert Path(str(source)+'-wal').stat().st_size>32
        # Include actual committed but uncheckpointed provider measurements.
        paths=private_case['inspection']['paths']
        inspection=inspect_application(paths['database'],paths['original_input'],paths['corrected_input'],paths['correction'],
            paths['base_graph'],paths['extension_graph'],private_case['folder']/'extension.json',
            private_case['folder']/'ready.json',3)
        source_bytes={p:p.read_bytes() for p in [private_case['base'],private_case['extension'],
            Path(str(private_case['base'])+'-wal'),Path(str(private_case['extension'])+'-wal')]}
        output=tmp_path/'wal-application';prepared=prepare_application(inspection,output)
        assert all(p.read_bytes()==raw for p,raw in source_bytes.items())
        assert not list(output.glob('.merging-*'))
        target=Path(prepared['private_graph'])
        assert not Path(str(target)+'-wal').exists() and not Path(str(target)+'-shm').exists()
        with closing(read_only(target)) as connection:
            assert connection.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
            assert connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            expected=inspection['base_graph']['provider_edges']+inspection['extension_graph']['provider_edges']
            assert connection.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]==expected
            for key,item in changed.items():
                row=connection.execute('SELECT * FROM metrics WHERE provider_key=?',(key,)).fetchone()
                assert row['meters']==item['meters'] and row['evidence_hash']==item['evidence_hash']
                assert decode_evidence(row['content'])==item
        road=ApplicationFrozenRoad(target)
        try:
            assert all(road.known_segment(a,b) is not None for a,b in compatible_pairs(normalize(private_case['new'])))
        finally:road.close()


def test_existing_private_graph_is_not_overwritten(private_case,tmp_path):
    output=tmp_path/'existing';output.mkdir();path=output/'frozen-roads.db';path.write_bytes(b'keep me')
    with pytest.raises(ApplicationGuardError):prepare_application(private_case['inspection'],output)
    assert path.read_bytes()==b'keep me'


def test_source_ready_requires_all_frozen_files_and_correct_hashes(tmp_path):
    path=tmp_path/'ready.json';ready=ready_file(path)
    assert verify_source_ready(path)['source_hashes']==ready['source_hashes']
    ready['source_hashes']['app/config.py']='0'*64;private_json(path,ready)
    with pytest.raises(ApplicationGuardError):verify_source_ready(path)
    private_json(path,{'status':'READY','source_hashes':{}})
    with pytest.raises(ApplicationGuardError):verify_source_ready(path)


def application_study(private_case,prepared,monkeypatch):
    import scripts.prepare_corrected_application as module
    old=private_case['old'];old_data=normalize(old)
    road=ApplicationFrozenRoad(private_case['base'])
    try:
        result=assemble(old_data,[evaluate_route(old_data,'VW',['S0','S1'],road),evaluate_route(old_data,'VX',['S2'],road)],[],[])
    finally:road.close()
    result_path=private_case['folder']/'formal-winner.json';private_json(result_path,result)
    baseline=assemble(old_data,[],list(old_data['tasks']),[])
    baseline_file=private_case['folder']/'baseline.json';private_json(baseline_file,{'input':old,'input_hash':content_hash(old),'result':baseline})
    winner={'case_name':'main225','status':'VALID','audit':{'valid':True,'unassigned_tasks':0,'total_fee':result['total_fee'],
        'road_meters':3000,'used_vehicles':2},
        'input_hash':content_hash(old),'graph_hash':prepared['base_graph']['graph_hash'],'result_file':str(result_path),
        'profile_name':'hybrid','entrypoint':'app.route_portfolio:optimize_hybrid','options':{},'budget_seconds':1,'seed':0}
    summary={'status':'COMPLETED','source_files_unchanged':True,'best_by_case':{'main225':winner},
        'ranking_by_case':{'main225':profile_ranking([winner],'main225')},'runs':[winner],
        'manifest':{'source_hashes':private_case['ready']['source_hashes']},'deadline_epoch':int(time.time())+3600}
    study_file=private_case['folder']/'formal-study'/'study_summary.json';private_json(study_file,summary)
    class StubAlgorithm:
        def __init__(self,*_):pass
        def function(self):
            def calculate(body,road,*args,**kwargs):
                data=normalize(body)
                result=assemble(data,[evaluate_route(data,'VW',['S0'],road),evaluate_route(data,'VX',['S2','S1'],road)],[],[])
                result['algorithm']={'mode':'EXPLICIT_APPLICATION_TEST_FIXTURE','elapsed_seconds':.001}
                return result
            return calculate
    monkeypatch.setattr(module,'Algorithm',StubAlgorithm)
    return study_file,baseline_file


@pytest.mark.parametrize('after_change',[None,'head','input'])
def test_readonly_application_proof_attribution_and_late_main_change_guard(private_case,tmp_path,monkeypatch,after_change):
    import scripts.prepare_corrected_application as module
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    calls=[]
    def guard(*_):
        calls.append(1)
        if len(calls)>1 and after_change=='input':raise ApplicationGuardError('Current inputs changed')
        result=deepcopy(private_case['guard'])
        if len(calls)>1 and after_change=='head':result['guard_token']='NEW_HEAD'
        return result
    monkeypatch.setattr(module,'main_guard',guard)
    report=run_application(prepared['case_file'],study_file,baseline_file)
    assert report['source_business_database_written'] is False and report['external_calls']==0
    assert report['proof']['audit']['valid'] and report['task_count']==3 and report['pieces']==3
    assert report['proof']['road_evidence']['historical_reference_segments']==3
    assert report['proof']['road_evidence']['extension_segments']>0
    assert report['attribution']['road_repricing_delta']=='0.00'
    assert report['attribution']['warehouse_minimal_repair_delta']=='99.00'
    assert report['attribution']['additional_corrected_optimization_delta']=='-99.00'
    assert report['attribution']['total_delta_from_collection_baseline']=='-297.00'
    assert Path(report['result_file']).is_file()
    result=json.loads(Path(report['result_file']).read_text())
    assert result['comparison']['input_changed'] and result['comparison']['delta']=='-297.00'
    assert result['comparison']['previous_input_hash']==content_hash(private_case['old'])
    assert report['status']==('VALIDATED_READY_FOR_GUARDED_APPEND' if after_change is None else 'MAIN_CHANGED_RESULT_RETAINED_DO_NOT_APPEND')
    assert report['append_recipe']['status']=='DRAFT'


def test_wrong_formal_graph_or_expired_deadline_is_rejected_before_optimizer(private_case,tmp_path,monkeypatch):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    summary=json.loads(study_file.read_text());summary['best_by_case']['main225']['graph_hash']='wrong'
    private_json(study_file,summary)
    with pytest.raises(ApplicationGuardError):run_application(prepared['case_file'],study_file,baseline_file)
    summary['best_by_case']['main225']['graph_hash']=prepared['base_graph']['graph_hash'];summary['deadline_epoch']=int(time.time())+1
    private_json(study_file,summary)
    with pytest.raises(ApplicationGuardError):run_application(prepared['case_file'],study_file,baseline_file)


def test_main_guard_session_is_readonly_and_current_master_changes_are_rejected(basis,app):
    from test_demand_groups import create_group,manual
    c,w,q,s,s2,v=basis;group=create_group(c)
    receipt=manual(c,group,[{'store_id':s['id'],'pieces':3}])
    detail=c.get('/api/planning/snapshots/'+receipt['snapshot_id']).json();body=detail['input']
    correction={'new_snapshot_id':detail['id'],'new_snapshot_hash':detail['content_hash'],'new_snapshot_version':detail['version'],
                'store_id':s['id'],'revision_after':s['revision'],'after':{'warehouse_id':w['id']}}
    path=Path(make_url(app.state.settings.database_url.get_secret_value()).database)
    before=main_guard(path,body,correction)
    with readonly_session(path) as session:
        with pytest.raises(OperationalError):session.execute(text('UPDATE system_state SET master_revision=999'))
    assert main_guard(path,body,correction)['guard_token']==before['guard_token']
    response=c.patch('/api/stores/'+s['id'],json={'expected_revision':s['revision'],'city':'外部新修改'})
    assert response.status_code==200
    with pytest.raises(ApplicationGuardError):main_guard(path,body,correction)


def second_ranked_profile(study_file,baseline_file):
    summary=json.loads(study_file.read_text());second=deepcopy(summary['best_by_case']['main225'])
    baseline=json.loads(baseline_file.read_text())['result']
    result_path=study_file.parent/'second-result.json';private_json(result_path,baseline)
    second.update(profile_name='alns',entrypoint='app.route_portfolio:optimize_alns',result_file=str(result_path),seed=7)
    second['audit'].update(total_fee=baseline['total_fee'],road_meters=0,used_vehicles=0)
    summary['runs'].append(second)
    summary['ranking_by_case']['main225']=profile_ranking(summary['runs'],'main225')
    private_json(study_file,summary)
    return summary


def test_rank_two_uses_registered_generator_and_preserves_rank_one_attribution(private_case,tmp_path,monkeypatch):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    second_ranked_profile(study_file,baseline_file)
    second=run_application(prepared['case_file'],study_file,baseline_file,profile_rank=2,run_output='rank2-seed7')
    assert second['profile_rank']==2 and second['selected_profile']['name']=='alns' and second['seed']==7
    assert second['winning_profile']['name']=='hybrid'
    assert second['attribution']['original_input_formal_winner_fee']=='3.00'
    assert second['attribution']['corrected_optimized_fee']=='3.00'
    first=run_application(prepared['case_file'],study_file,baseline_file)
    assert first['profile_rank']==1 and first['selected_profile']['name']=='hybrid'
    assert Path(first['run_output']).name=='application-run'
    assert first['private_graph_hash']==second['private_graph_hash']==prepared['private_graph_hash']
    assert first['corrected_input_hash']==second['corrected_input_hash']==content_hash(private_case['new'])
    assert first['result_file']!=second['result_file']
    assert first['source_hashes_unchanged'] and second['source_hashes_unchanged']
    for report in [first,second]:
        assert Path(report['run_output']).stat().st_mode&0o777==0o700
        assert Path(report['result_file']).stat().st_mode&0o777==0o600
        result=json.loads(Path(report['result_file']).read_text())
        meta=result['algorithm']['correction_application']
        assert meta['formal_winning_profile']['name']=='hybrid'
        assert meta['application_selected_profile']==report['selected_profile']


@pytest.mark.parametrize('tamper',['rank_order','entrypoint','options','best_fee','input','duplicate','runs'])
def test_ranked_profile_rejects_unregistered_or_inconsistent_summary(private_case,tmp_path,monkeypatch,tamper):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    summary=second_ranked_profile(study_file,baseline_file)
    second=summary['ranking_by_case']['main225'][1]
    if tamper=='rank_order':summary['ranking_by_case']['main225'].reverse()
    if tamper=='entrypoint':second['best']['entrypoint']='untrusted.module:run'
    if tamper=='options':second['best']['options']={'common_unit_price':0}
    if tamper=='best_fee':second['best_fee']='0.01'
    if tamper=='input':second['best']['input_hash']='different-input'
    if tamper=='duplicate':second['profile_name']=summary['ranking_by_case']['main225'][0]['profile_name']
    if tamper=='runs':summary['runs'].pop()
    # Exercise the registration gate too, independent of the optional run-row cross-check.
    if tamper in {'entrypoint','options','input'}:summary.pop('runs')
    with pytest.raises(ApplicationGuardError):winner_profile(summary,content_hash(private_case['old']),2)


@pytest.mark.parametrize('rank',[0,11,True,-1])
def test_profile_rank_is_only_one_to_ten(private_case,tmp_path,monkeypatch,rank):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    summary=json.loads(study_file.read_text())
    with pytest.raises(ApplicationGuardError):winner_profile(summary,content_hash(private_case['old']),rank)


def test_missing_second_rank_rejected_and_legacy_default_is_compatible(private_case,tmp_path,monkeypatch):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    summary=json.loads(study_file.read_text())
    with pytest.raises(ApplicationGuardError):winner_profile(summary,content_hash(private_case['old']),2)
    with pytest.raises(ApplicationGuardError):winner_profile(summary,content_hash(private_case['old']),10)
    summary.pop('ranking_by_case');summary.pop('runs')
    assert winner_profile(summary,content_hash(private_case['old']))[1]['name']=='hybrid'
    with pytest.raises(ApplicationGuardError):winner_profile(summary,content_hash(private_case['old']),1)


def test_third_ranked_registered_profile_can_run_and_missing_third_is_rejected(private_case,tmp_path,monkeypatch):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    summary=second_ranked_profile(study_file,baseline_file)
    with pytest.raises(ApplicationGuardError,match='no requested ranked profile'):
        winner_profile(summary,content_hash(private_case['old']),3)
    third=deepcopy(summary['runs'][1])
    third.update(profile_name='gls',entrypoint='app.route_ortools:optimize_ortools',
                 options={'strategy':'GUIDED_LOCAL_SEARCH'})
    summary['runs'].append(third)
    summary['ranking_by_case']['main225']=profile_ranking(summary['runs'],'main225')
    private_json(study_file,summary)
    report=run_application(prepared['case_file'],study_file,baseline_file,profile_rank=3,run_output='rank3-screen')
    assert report['selected_profile']['name']=='gls' and report['profile_rank']==3
    assert report['selected_profile']['options']=={'strategy':'GUIDED_LOCAL_SEARCH'}
    assert report['winning_profile']['name']=='hybrid'
    assert report['attribution']['original_input_formal_winner_fee']=='3.00'


@pytest.mark.parametrize('destination',['existing','symlink','outside','nested','database','graph','formal'])
def test_run_output_cannot_overwrite_or_escape_private_case(private_case,tmp_path,monkeypatch,destination):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    root=Path(prepared['case_file']).parent
    if destination=='existing':
        target=root/'existing';target.mkdir(mode=0o700);(target/'keep').write_text('unchanged')
    elif destination=='symlink':
        target=root/'link';target.symlink_to(tmp_path/'not-created',target_is_directory=True)
    elif destination=='outside':target=tmp_path/'outside'
    elif destination=='nested':target=root/'nested'/'run'
    elif destination=='database':
        target=root/'main.db';prepared['paths']['database']=str(target)
    elif destination=='graph':target=Path(prepared['private_graph'])
    else:
        root=study_file.parent/'application';root.mkdir(mode=0o700)
        prepared['case_file']=str(root/'case.json');target=root/'run'
    with pytest.raises(ApplicationGuardError):
        application_run_output(prepared['case_file'],study_file,baseline_file,prepared,target)
    if destination=='existing':assert (target/'keep').read_text()=='unchanged'
    elif destination=='symlink':assert target.is_symlink() and not (tmp_path/'not-created').exists()
    elif destination not in {'graph'}:assert not target.exists()


def test_application_run_requires_private_parent_and_never_reuses_output(private_case,tmp_path,monkeypatch):
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    root=Path(prepared['case_file']).parent;root.chmod(0o755)
    with pytest.raises(ApplicationGuardError):run_application(prepared['case_file'],study_file,baseline_file)
    assert not (root/'application-run').exists()
    root.chmod(0o700)
    report=run_application(prepared['case_file'],study_file,baseline_file,run_output='first')
    before=Path(report['result_file']).read_bytes()
    with pytest.raises(ApplicationGuardError):run_application(prepared['case_file'],study_file,baseline_file,run_output='first')
    assert Path(report['result_file']).read_bytes()==before


def test_changed_source_gate_during_application_is_not_append_eligible(private_case,tmp_path,monkeypatch):
    import scripts.prepare_corrected_application as module
    prepared=prepare_application(private_case['inspection'],tmp_path/'prepared')
    study_file,baseline_file=application_study(private_case,prepared,monkeypatch)
    actual=module.verify_source_ready;calls=[]
    def changed_gate(path):
        value=actual(path);calls.append(1)
        if len(calls)>1:value['source_hashes']['app/config.py']='different-after-run'
        return value
    monkeypatch.setattr(module,'verify_source_ready',changed_gate)
    with pytest.raises(ApplicationGuardError,match='Frozen sources changed'):
        run_application(prepared['case_file'],study_file,baseline_file)
    assert not (Path(prepared['case_file']).parent/'application-run'/'corrected-result.json').exists()
