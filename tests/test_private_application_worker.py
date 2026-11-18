"""No sockets/servers are started by these private-acceptance helper regressions."""
import json
from types import SimpleNamespace

import httpx
import pytest

import scripts.verify_application_worker as module


def test_default_cli_never_reads_sources_or_starts_private_work(monkeypatch,capsys):
    def forbidden(*args,**kwargs):pytest.fail('Explicit execute-private is required')
    monkeypatch.setattr(module,'verify_worker',forbidden)
    module.main(['--case','not-read.json','--study-summary','not-read-study.json','--application-report','not-read-report.json'])
    output=json.loads(capsys.readouterr().out)
    assert output['status']=='NOT_EXECUTED' and output['requires']=='--execute-private'


def test_private_settings_and_spawn_payload_keep_real_profile_and_local_database_without_keys(tmp_path):
    settings=module.private_settings(tmp_path/'copy.db',tmp_path/'copy-graph.db',tmp_path,'GUIDED_LOCAL_SEARCH',.75,12345)
    payload=module.server_payload(settings)
    assert payload['database_url']=='sqlite:///'+str(tmp_path/'copy.db')
    assert payload['planning_optimizer_profile']=='GUIDED_LOCAL_SEARCH' and payload['planning_common_penalty_multiplier']==.75
    assert payload['environment']=='development'  # The frozen adapter deliberately skips graph autoload in test mode.
    assert payload['public_origin']=='http://127.0.0.1:12345' and payload['allowed_hosts']==['127.0.0.1']
    assert not payload['driving_live_calls_enabled'] and not payload['truck_live_calls_enabled']
    assert payload['driving_job_max_calls'] is None and payload['truck_job_max_calls'] is None
    assert payload['amap_web_service_key']==module.PRIVATE_KEY_PLACEHOLDER=='private-no-network-placeholder'
    assert all(payload[key] is None for key in ['amap_js_key','amap_security_js_code'])
    assert payload['amap_reference_graph_path']==str(tmp_path/'copy-graph.db')
    assert payload['map_auto_geocode_enabled'] is False and payload['cookie_namespace'].startswith('worker_')


def test_http_client_setup_failure_still_stops_owned_child_and_listener(tmp_path,monkeypatch):
    class Child:
        alive=False;joined=False
        def start(self):self.alive=True
        def is_alive(self):return self.alive
        def terminate(self):self.alive=False
        def join(self,**kwargs):self.joined=True
        def kill(self):self.alive=False
    child=Child();listener=SimpleNamespace(close=lambda:closed.append(True));closed=[]
    def process(**kwargs):
        assert 'never-print-this' not in repr(kwargs['args'][0])
        return child
    monkeypatch.setattr(module.multiprocessing,'get_context',lambda method:SimpleNamespace(Process=process))
    def failed(*args,**kwargs):raise RuntimeError('injected client initialization failure')
    monkeypatch.setattr(httpx,'Client',failed)
    settings=module.private_settings(tmp_path/'copy.db',tmp_path/'copy-graph.db',tmp_path,'HYBRID',1,12345)
    with pytest.raises(RuntimeError,match='client initialization'):
        module.run_loopback(settings,listener,'private-user','never-print-this',{'id':'S','group_id':'G'},
            {'id':'P','version':1},300,0,9999999999)
    assert not child.alive and child.joined and closed


@pytest.mark.parametrize('change',[None,'wrong_profile','no_roads','empty_candidates','empty_graph','no_native_matrix','wrong_strategy'])
def test_actual_profile_requires_loaded_measured_graph_and_real_candidate_search(change):
    result={'algorithm':{'mode':'ORTOOLS_MEASURED_HYBRID','strategy':'GUIDED_LOCAL_SEARCH',
        'surrogate_common_penalty_multiplier':'0.75','stop_reason':'TIME_BUDGET','candidate_count':12,
        'distinct_route_count':5,'road_stats':{'reference_graph_edges':17},
        'routing_diagnostics':{'measured_edges':17,'native_slices':1}}}
    if change=='wrong_profile':result['algorithm']['mode']='ADAPTIVE_LARGE_NEIGHBORHOOD'
    elif change=='no_roads':result['algorithm']['stop_reason']='NO_AVAILABLE_ROAD_EVIDENCE'
    elif change=='empty_candidates':result['algorithm']['candidate_count']=0
    elif change=='empty_graph':result['algorithm']['road_stats']['reference_graph_edges']=0
    elif change=='no_native_matrix':result['algorithm']['routing_diagnostics']['measured_edges']=0
    elif change=='wrong_strategy':result['algorithm']['strategy']='TABU_SEARCH'
    if change is None:
        assert module.verify_search_activity(result,'GUIDED_LOCAL_SEARCH',.75,17)['profile_mode_verified'] is True
    else:
        with pytest.raises(module.ApplicationGuardError):module.verify_search_activity(result,'GUIDED_LOCAL_SEARCH',.75,17)
