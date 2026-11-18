"""Input-only unseen-road preparation and isolated mock-provider collection."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import httpx
from pydantic import SecretStr
import pytest

from app.config import Settings
from app.planning_inputs import content_hash
from scripts.benchmark_algorithms import graph_digest,private_json,read_only,write_graph
import scripts.freeze_unseen_roads as module
from test_algorithm_benchmark import measured_edges,problem
from test_critical_algorithm_roads import response


class NoWait:
    def acquire(self,*args,**kwargs):pass
    def defer(self,*args,**kwargs):pass


def frozen(tmp_path,*,tasks=3):
    root=tmp_path/'data';root.mkdir();body=problem()
    original=deepcopy(body['tasks'][0])
    body['tasks']=[{**original,'id':f'T{i}','store_id':f'T{i}','longitude':str(117+i*.001)} for i in range(1,tasks+1)]
    on=root/'with-roads.db';off=root/'without-roads.db'
    write_graph(on,body,measured_edges(body,[('W','T1')]))
    module.copy_db(on,off)
    with sqlite3.connect(off) as db:db.execute('DELETE FROM hints');db.commit()
    cases=[]
    for regime,graph in [('with-hints',on),('without-hints',off)]:
        case_dir=root/regime;case_dir.mkdir();private_json(case_dir/'input.json',body)
        # The artifact is intentionally not parseable: preparation/execution
        # must never open it to select roads or inherit result metadata.
        (case_dir/'baseline.json').write_text('FORBIDDEN NON-JSON SOLVER OUTCOME')
        with read_only(graph) as db:digest=graph_digest(db)
        case={'body_file':'input.json','baseline_file':'baseline.json','road_database':str(graph),'input_hash':content_hash(body),'graph_hash':digest}
        private_json(case_dir/'case.json',case)
        cases.append({'day':body['delivery_date'],'regime':regime,'case_file':str(case_dir/'case.json'),'input_hash':case['input_hash'],'graph_hash':digest})
    index=root/'frozen-cases.json';private_json(index,{'inputs_frozen_before_any_algorithm_result':True,'cases':cases})
    return root,index,body,on,off


def settings(tmp_path):
    return Settings(_env_file=None,environment='test',planning_route_mode='DRIVING',driving_live_calls_enabled=True,
        amap_web_service_key=SecretStr('MOCK_ONLY'),amap_rate_limit_path=tmp_path/'pacing.db',
        amap_reference_graph_path=tmp_path/'never-touch-original-reference.db')


def test_input_only_import_preserves_originals_and_hint_regimes(tmp_path):
    root,index,body,on,off=frozen(tmp_path)
    extra=root/'additional-validated-roads.db';write_graph(extra,body,measured_edges(body,[('T1','T2')]))
    originals={path:module.sha(path) for path in root.rglob('*') if path.is_file()}
    manifest=module.prepare(index,tmp_path/'prepared',data_root=root)
    assert manifest['unique_provider_keys']==9 and manifest['imported_provider_keys']==2
    assert manifest['live_missing_provider_keys']==7 and manifest['total_call_limit'] is None
    assert not manifest['solver_results_read'] and not manifest['baseline_files_read']
    offline=module.execute(manifest,tmp_path/'offline',wait_seconds=30)
    assert offline['actual_http_attempts']==0 and offline['uncollected_provider_keys']==7
    assert all(module.sha(path)==value for path,value in originals.items())
    requests=[]
    def handler(request):requests.append(request);return response(request)
    cfg=settings(tmp_path)
    result=module.execute(manifest,tmp_path/'live',wait_seconds=30,live=True,settings=cfg,
        transport=httpx.MockTransport(handler),limiter=NoWait())
    assert result['status']=='COMPLETED' and result['actual_http_attempts']==len(requests)==7
    assert result['road_stats']['total_call_policy']=='PRODUCTION_UNLIMITED' and result['road_stats']['call_limit'] is None
    assert len({case['graph_hash'] for case in result['cases']})==1
    for entry in result['cases']:
        case=module.load(entry['case_file'])
        with read_only(case['road_database']) as db,read_only(entry['source_graph']) as original:
            assert module.hints(db)==module.hints(original)
            for row in original.execute('SELECT * FROM metrics'):
                assert tuple(db.execute('SELECT * FROM metrics WHERE provider_key=?',(row['provider_key'],)).fetchone())==tuple(row)
        assert content_hash(module.load(Path(entry['case_file']).parent/'input.json'))==entry['input_hash']
    assert all(module.sha(path)==value for path,value in originals.items())
    assert not cfg.amap_reference_graph_path.exists()


def test_neighbours_use_complete_quote_signature_same_warehouse_and_both_directions():
    body=problem();template=body['tasks'][0]
    body['tasks']=[{**template,'id':f'T{i}','store_id':f'T{i}','longitude':str(117+i*.001),'city':str(i%2)} for i in range(12)]
    body['quotes']['ALIAS']={'mode':'KM','per_km':'1.00'}
    body['quotes']['DIFFERENT']={'mode':'KM','per_km':'2'}
    body['tasks'][0]['quote_id']='ALIAS'
    body['tasks'].append({**template,'id':'OTHER_QUOTE','quote_id':'DIFFERENT'})
    body['warehouses'].append({**body['warehouses'][0],'id':'X'})
    body['vehicles'].append({**body['vehicles'][0],'id':'VX','warehouse_id':'X'})
    body['tasks'].append({**template,'id':'OTHER_WAREHOUSE','warehouse_id':'X'})
    jobs,rejected=module.requested_jobs(body);pairs={(row['from_id'],row['to_id']) for row in jobs}
    assert not rejected
    assert ('W','OTHER_QUOTE') in pairs and ('X','OTHER_WAREHOUSE') in pairs
    assert ('W','OTHER_WAREHOUSE') not in pairs
    assert not any(a.startswith('T') and b in {'OTHER_QUOTE','OTHER_WAREHOUSE'} for a,b in pairs)
    assert ('T0','T1') in pairs and ('T1','T0') in pairs  # Same quote, differing city/quote ID.
    for a,b in pairs:
        if a.startswith('T'):assert (b,a) in pairs
    assert not any(b in {'W','X'} for _,b in pairs)


def test_collection_has_no_old_96_call_ceiling(tmp_path):
    root,index,_,_,_=frozen(tmp_path,tasks=20)
    manifest=module.prepare(index,tmp_path/'prepared',data_root=root);requests=[]
    def handler(request):requests.append(request);return response(request)
    result=module.execute(manifest,tmp_path/'live',wait_seconds=30,live=True,settings=settings(tmp_path),
        transport=httpx.MockTransport(handler),limiter=NoWait())
    assert len(requests)>96 and result['status']=='COMPLETED'
    assert result['actual_http_attempts']==manifest['live_missing_provider_keys']==len(requests)


def test_index_hash_schema_revision_writes_only_new_metadata(tmp_path):
    root,index,_,_,_=frozen(tmp_path)
    prepared=module.prepare(index,tmp_path/'prepared',data_root=root)
    result=module.execute(prepared,tmp_path/'offline',wait_seconds=30)
    # Reproduce the original v1 metadata field, without modifying any graph.
    legacy=deepcopy(result)
    for row in legacy['cases']:
        row['hint_hash']=row.pop('hint_rows_hash')
    old=tmp_path/'legacy-index.json';private_json(old,legacy)
    originals={p:module.sha(p) for p in root.rglob('*') if p.is_file()}
    original_index_sha=module.sha(old)
    target=tmp_path/'revised-index.json';proof=module.upgrade_index(old,target)
    revised=module.load(target)
    for entry in revised['cases']:
        case=module.load(entry['case_file'])
        actual=module.hint_identity(case['road_database'])
        assert entry['hint_hash']==actual['hint_hash']
        assert entry['hint_rows_hash']==actual['hint_rows_hash']
    assert proof['metadata_only'] and proof['solver_runs']==proof['network_calls']==0
    assert proof['source_index_sha256']==original_index_sha==module.sha(old)
    assert all(module.sha(path)==value for path,value in originals.items())


@pytest.mark.parametrize('reason',['CANCELLED','WALL_BUDGET'])
def test_collection_stops_new_http_and_preserves_distinct_stop_reason(tmp_path,monkeypatch,reason):
    root,index,_,_,_=frozen(tmp_path)
    manifest=module.prepare(index,tmp_path/'prepared',data_root=root)
    clock=[100.];cancel=[False];requests=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    def handler(request):
        requests.append(request)
        if reason=='CANCELLED':cancel[0]=True
        else:clock[0]+=2
        return response(request)
    result=module.execute(manifest,tmp_path/'live',wait_seconds=1,live=True,settings=settings(tmp_path),
        transport=httpx.MockTransport(handler),limiter=NoWait(),cancelled=lambda:cancel[0])
    assert result['status']=='INCOMPLETE' and result['stop_reason']==reason
    assert result['actual_http_attempts']==len(requests)==1
    assert result['measured_provider_keys']==0
