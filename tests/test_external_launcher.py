"""Network-free recovery checks; no origin, tunnel or account is started."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


_spec=importlib.util.spec_from_file_location('external_launcher',Path(__file__).resolve().parents[1]/'scripts/start-external-test.py')
launcher=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)
_manager_spec=importlib.util.spec_from_file_location('external_service_manager',Path(__file__).resolve().parents[1]/'scripts/manage-services.py')
manager=importlib.util.module_from_spec(_manager_spec)
_manager_spec.loader.exec_module(manager)


def install_dns(monkeypatch,handler):
    original=launcher.httpx.Client
    def client(**kwargs):
        assert kwargs=={'timeout':8,'trust_env':False}
        return original(transport=httpx.MockTransport(handler),**kwargs)
    monkeypatch.setattr(launcher.httpx,'Client',client)


def answer(name,addresses):
    return {'Status':0,'Answer':[{'name':name,'type':1,'data':ip} for ip in addresses]}


def test_https_edge_resolution_uses_official_regions_and_keeps_redundancy(monkeypatch):
    calls=[]
    def handler(request):
        assert request.url.host=='cloudflare-dns.com' and request.url.scheme=='https'
        assert request.headers['accept']=='application/dns-json'
        name=request.url.params['name'];calls.append(name)
        addresses=sorted(launcher.EXPECTED_EDGES[name])
        return httpx.Response(200,json=answer(name,[addresses[0],addresses[0],*addresses[1:]]))
    install_dns(monkeypatch,handler)
    endpoints=launcher.verified_edge_addresses()
    assert calls==list(launcher.EXPECTED_EDGES)
    assert len(endpoints)==len(set(endpoints))==4
    for name,offset in zip(calls,(0,2)):
        assert endpoints[offset:offset+2]==[ip+':7844' for ip in sorted(launcher.EXPECTED_EDGES[name])[:2]]


@pytest.mark.parametrize('address',['198.18.1.129','127.0.0.1','192.168.1.1','198.51.100.1','8.8.8.8','198.41.200.13','::1','invalid'])
def test_https_edge_resolution_rejects_fake_private_unexpected_and_wrong_region_addresses(monkeypatch,address):
    install_dns(monkeypatch,lambda request:httpx.Response(200,json=answer(request.url.params['name'],[address])))
    with pytest.raises(RuntimeError,match='地址|列表'):launcher.verified_edge_addresses()


@pytest.mark.parametrize('payload',[{'Status':3},{'Status':0,'Answer':[]},[]])
def test_failed_or_empty_dns_does_not_start_with_unverified_endpoints(monkeypatch,payload):
    install_dns(monkeypatch,lambda request:httpx.Response(200,json=payload))
    with pytest.raises(RuntimeError,match='解析'):launcher.verified_edge_addresses()


def test_https_dns_failure_keeps_tls_enabled_and_explains_failure(monkeypatch):
    def handler(request):raise httpx.ConnectError('simulated TLS failure',request=request)
    install_dns(monkeypatch,handler)
    with pytest.raises(RuntimeError,match='TLS验证保持开启'):launcher.verified_edge_addresses()


@pytest.mark.parametrize('command,owned',[
    ('python /other/project/scripts/start-external-test.py',False),
    ('python '+str(launcher.ROOT/'scripts/start-external-test.py')+'.other',False),
    ('python '+str(launcher.ROOT/'scripts/start-external-test.py')+' --stop',False),
    ('python '+str(launcher.ROOT/'scripts/start-external-test.py')+' --edge-discovery doh',True),
])
def test_stop_checks_full_project_launcher_identity_before_signalling(tmp_path,monkeypatch,command,owned):
    (tmp_path/'current.json').write_text(json.dumps({'status':'RUNNING','launcher_pid':12345}))
    monkeypatch.setattr(launcher,'FOLDER',tmp_path)
    monkeypatch.setattr(launcher.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=command,returncode=0))
    signalled=[];monkeypatch.setattr(launcher.os,'kill',lambda *args:signalled.append(args))
    if owned:
        launcher.stop_existing();assert signalled==[(12345,launcher.signal.SIGTERM)]
    else:
        with pytest.raises(SystemExit,match='未停止其他程序'):launcher.stop_existing()
        assert signalled==[]


def test_saved_doh_mode_survives_plain_launcher_and_managed_restart(tmp_path,monkeypatch):
    monkeypatch.setattr(launcher,'FOLDER',tmp_path)
    assert launcher.configured_edge_discovery()=='system'
    launcher.save_edge_discovery('doh')
    assert launcher.configured_edge_discovery()=='doh'
    assert launcher.configured_edge_discovery('system')=='system'
    assert launcher.configured_edge_discovery()=='doh'
    assert (tmp_path/'settings.json').stat().st_mode&0o777==0o600


@pytest.mark.parametrize('content',['{}','[]','{"edge_discovery":"invalid"}','{"edge_discovery":[]}','not JSON'])
def test_invalid_saved_mode_does_not_silently_fall_back_to_system_dns(tmp_path,monkeypatch,content):
    monkeypatch.setattr(launcher,'FOLDER',tmp_path)
    (tmp_path/'settings.json').write_text(content)
    with pytest.raises(RuntimeError,match='配置'):launcher.configured_edge_discovery()


def test_reused_pid_with_same_script_but_different_start_identity_is_not_stopped(tmp_path,monkeypatch):
    command='Fri Nov 13 05:06:00 2026 python '+str(launcher.ROOT/'scripts/start-external-test.py')
    (tmp_path/'current.json').write_text(json.dumps({'status':'RUNNING','launcher_pid':12345,
        'launcher_identity':'Fri Nov 13 04:00:00 2026 python '+str(launcher.ROOT/'scripts/start-external-test.py')}))
    monkeypatch.setattr(launcher,'FOLDER',tmp_path)
    monkeypatch.setattr(launcher,'process_identity',lambda pid:command)
    signalled=[];monkeypatch.setattr(launcher.os,'kill',lambda *args:signalled.append(args))
    with pytest.raises(SystemExit,match='未停止其他程序'):launcher.stop_existing()
    assert signalled==[]


@pytest.mark.parametrize('current,group,owned',[('old-start',12345,True),('new-start',12345,False),('old-start',99999,False)])
def test_manager_ownership_requires_same_start_identity_and_independent_session(tmp_path,monkeypatch,current,group,owned):
    (tmp_path/'external.json').write_text(json.dumps({'pid':12345,'identity':'old-start'}))
    monkeypatch.setattr(manager,'FOLDER',tmp_path)
    monkeypatch.setattr(manager,'identity',lambda pid:current)
    monkeypatch.setattr(manager.os,'getpgid',lambda pid:group)
    assert manager.owned_pid('external')==(12345 if owned else None)


def test_managed_stop_keeps_legacy_launcher_stop_compatibility(monkeypatch):
    monkeypatch.setattr(manager,'owned_pid',lambda name:None)
    calls=[]
    def run(command,**kwargs):calls.append(command);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(manager.subprocess,'run',run)
    manager.stop('external')
    assert calls==[manager.SERVICES['external'][1]+['--stop']]
