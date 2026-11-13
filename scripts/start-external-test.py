"""Start a temporary HTTPS tunnel and a strict production-mode origin.

Port 8770 uses the frozen stable runtime; 8771 uses the registered isolated
test runtime. Only loopback listeners are used. Ctrl+C stops both children.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
FOLDER=ROOT/'data/external-test'
# Published edge IPs, checked against Cloudflare's official firewall reference.
# https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/
EXPECTED_EDGES={
    'region1.v2.argotunnel.com':{f'198.41.192.{suffix}' for suffix in (167,67,57,107,27,7,227,47,37,77)},
    'region2.v2.argotunnel.com':{f'198.41.200.{suffix}' for suffix in (13,193,33,233,53,63,113,73,43,23)},
}


def configured_edge_discovery(explicit=None):
    path=FOLDER/'settings.json'
    if explicit is None and path.exists():
        try:explicit=json.loads(path.read_text())['edge_discovery']
        except (OSError,ValueError,KeyError,TypeError):
            raise RuntimeError('外网入口项目配置无效，请检查data/external-test/settings.json。') from None
    mode=explicit if explicit is not None else 'system'
    if not isinstance(mode,str) or mode not in {'system','doh'}:raise RuntimeError('外网入口边缘解析配置必须为system或doh。')
    return mode


def save_edge_discovery(mode):
    if mode not in {'system','doh'}:raise ValueError('无效的边缘解析方式。')
    path=FOLDER/'settings.json';temporary=FOLDER/'settings.json.tmp'
    with open(temporary,'w',opener=lambda name,flags:os.open(name,flags,0o600)) as output:
        os.fchmod(output.fileno(),0o600)
        output.write(json.dumps({'edge_discovery':mode},ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)


def process_identity(pid):
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart=','-o','command='],
                          capture_output=True,text=True,check=False)
    return result.stdout.strip() if result.returncode==0 else None


def verified_edge_addresses():
    """Resolve only Cloudflare's official edges over authenticated HTTPS.

    This explicit recovery mode leaves host DNS and proxy settings unchanged.
    cloudflared's dns-resolver-addrs configures origin DNS, not edge discovery.
    Its official --edge override is used only when this mode is requested.
    """
    edges=[]
    with httpx.Client(timeout=8,trust_env=False) as client:
        for region in EXPECTED_EDGES:
            try:
                response=client.get('https://cloudflare-dns.com/dns-query',params={'name':region,'type':'A'},
                                    headers={'accept':'application/dns-json'})
                response.raise_for_status();payload=response.json()
            except (httpx.HTTPError,ValueError):
                raise RuntimeError('Cloudflare官方HTTPS解析失败；TLS验证保持开启，请检查本机网络后重试。') from None
            if not isinstance(payload,dict) or payload.get('Status')!=0:
                raise RuntimeError('Cloudflare官方边缘地址解析失败。')
            addresses=[]
            for answer in payload.get('Answer',[]):
                if answer.get('type')!=1 or str(answer.get('name','')).rstrip('.').lower()!=region:continue
                try:address=ipaddress.ip_address(answer['data'])
                except (KeyError,ValueError):raise RuntimeError('Cloudflare边缘解析返回无效地址。') from None
                if address.version!=4 or not address.is_global or address.is_multicast:
                    raise RuntimeError('Cloudflare边缘解析返回非公网地址，未建立隧道。')
                if str(address) not in EXPECTED_EDGES[region]:
                    raise RuntimeError('解析地址不在Cloudflare官方预期边缘列表中，请先核对官方文档。')
                if str(address) not in addresses:addresses.append(str(address))
            if not addresses:raise RuntimeError('Cloudflare官方边缘解析未返回公网IPv4地址。')
            edges.extend(address+':7844' for address in addresses[:2])
    return edges


def stop_existing():
    path=FOLDER/'current.json'
    if not path.is_file():
        print('当前没有外网测试入口。');return
    state=json.loads(path.read_text())
    if state.get('status') not in {'RUNNING','STARTING'}:
        print('外网测试入口已停止。');return
    pid=state.get('launcher_pid')
    if not isinstance(pid,int) or pid<=1 or pid==os.getpid():raise SystemExit('进程记录无效，未停止其他程序。')
    command=process_identity(pid) or ''
    owned_script=re.search(r'(?<!\S)'+re.escape(str(ROOT/'scripts/start-external-test.py'))+r'(?=\s|$)',command)
    expected=state.get('launcher_identity')
    if not owned_script or '--stop' in command or (expected is not None and expected!=command):
        raise SystemExit('原测试进程已退出，未停止其他程序。')
    os.kill(pid,signal.SIGTERM)
    print('已请求关闭临时HTTPS入口，原网址随后失效。')


def ensure_runtime_launch(port=8770):
    if (ROOT/'data/runtime-isolation.json').exists():
        raise RuntimeError('请通过 scripts/manage-services.py 启动已隔离的 external 或 test-external。')
    if (ROOT/'.git').exists():
        branch=subprocess.run(['git','branch','--show-current'],cwd=ROOT,capture_output=True,text=True)
        if branch.returncode==0 and branch.stdout.strip()=='test':
            raise RuntimeError('test 工作区不能直接公开业务服务；请先准备隔离目录并通过 manage-services.py 启动。')
    if ROOT.parent.name=='runtimes' and ROOT.parent.parent.name=='data':
        from scripts.runtime_isolation import runtime_roots
        roots=runtime_roots(ROOT.parents[2])
        key='test_root' if port==8771 else 'stable_root'
        if roots is None or roots.get(key)!=ROOT:
            raise RuntimeError('外网端口与已登记的稳定/测试运行目录不匹配。')


def main(edge_discovery=None,port=8770):
    if port not in {8770,8771}:raise ValueError('外网源服务端口只能是 8770 或 8771。')
    ensure_runtime_launch(port)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',port))==0:
            raise RuntimeError('外网源服务端口已占用，未建立隧道。')
    if port==8771:
        # Check the private database binding before opening any public route.
        checked=subprocess.run([sys.executable,str(ROOT/'scripts/start-test.py'),'--check'],cwd=ROOT,
            env={k:v for k,v in os.environ.items() if not k.upper().startswith(('SUKA_','TUNNEL_'))},
            capture_output=True,text=True)
        if checked.returncode:raise RuntimeError('测试运行隔离配置校验失败，未建立隧道。')
    FOLDER.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(FOLDER/'launcher.lock').open('w');os.chmod(lock.name,0o600)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('外网测试入口已经运行，请使用现有地址。')
    edge_discovery=configured_edge_discovery(edge_discovery)
    binary=ROOT/'.cache/cloudflared'
    if not binary.is_file():raise SystemExit('缺少项目内cloudflared，请先按部署说明安装。')
    if not (ROOT/'frontend/dist/index.html').is_file():raise SystemExit('请先构建前端。')
    edges=verified_edge_addresses() if edge_discovery=='doh' else []
    empty_config=FOLDER/'empty.yml';empty_config.write_text('{}\n');empty_config.chmod(0o600)
    logs=[];children=[];address=[];ready=threading.Event();edge_ready=threading.Event();stop=threading.Event()
    for name in ['tunnel.log','origin.log']:
        path=FOLDER/name;path.touch(mode=0o600,exist_ok=True);path.chmod(0o600);logs.append(path.open('w'))
    def shutdown(_signum=None,_frame=None):stop.set()
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    launcher_identity=process_identity(os.getpid())
    if not launcher_identity:raise RuntimeError('无法核实外网启动器进程身份，未建立隧道。')
    state={'status':'STARTING','launcher_pid':os.getpid(),'launcher_identity':launcher_identity,
           'port':port,'edge_discovery':edge_discovery}
    if edges:state['edge_addresses']=edges
    def save():
        path=FOLDER/'current.json';path.touch(mode=0o600,exist_ok=True);path.chmod(0o600)
        path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n')
    save()
    try:
        env={k:v for k,v in os.environ.items() if not k.upper().startswith(('SUKA_','TUNNEL_'))}
        edge_args=[argument for endpoint in edges for argument in ('--edge',endpoint)]
        tunnel=subprocess.Popen([str(binary),'tunnel','--config',str(empty_config),'--no-autoupdate',
            '--url',f'http://127.0.0.1:{port}','--protocol','http2','--edge-ip-version','4',
            '--metrics','127.0.0.1:0','--management-diagnostics=false',*edge_args],cwd=ROOT,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env)
        children.append(tunnel);state['tunnel_pid']=tunnel.pid;save()
        def read_logs():
            assert tunnel.stdout is not None
            for line in tunnel.stdout:
                logs[0].write(line);logs[0].flush()
                if 'Registered tunnel connection' in line:edge_ready.set()
                match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
                if match and not address:address.append(match[0]);ready.set()
        threading.Thread(target=read_logs,daemon=True).start()
        deadline=time.monotonic()+60
        while not ready.wait(.5):
            if stop.is_set():return
            if tunnel.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('临时隧道建立失败，诊断保存在data/external-test/tunnel.log')
        url=address[0];host=url.removeprefix('https://')
        origin='start-test.py' if port==8771 else 'start-external-origin.py'
        server=subprocess.Popen([sys.executable,str(ROOT/'scripts'/origin),'--url',url,'--port',str(port)],
                                cwd=ROOT,stdout=logs[1],stderr=subprocess.STDOUT,env=env)
        children.append(server);state.update(url=url,origin_pid=server.pid);save()
        with httpx.Client(timeout=3,trust_env=False) as client:
            deadline=time.monotonic()+30
            while not stop.is_set():
                try:
                    response=client.get(f'http://127.0.0.1:{port}/healthz',headers={'host':host})
                    if response.status_code==200:break
                except httpx.HTTPError:pass
                if server.poll() is not None or time.monotonic()>deadline:raise RuntimeError('外网测试应用未启动，请查看origin.log')
                stop.wait(.5)
        if stop.is_set():return
        deadline=time.monotonic()+60
        while not edge_ready.wait(.5):
            if stop.is_set():return
            if tunnel.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('隧道地址已创建，但未连接Cloudflare边缘，请查看tunnel.log。')
        state['status']='RUNNING';save();save_edge_discovery(edge_discovery)
        print('临时HTTPS测试入口：'+url,flush=True)
        print('另一台电脑打开上述地址，使用现有账号密码登录。当前电脑需保持开机；Ctrl+C关闭入口。',flush=True)
        while not stop.wait(1):
            if any(child.poll() is not None for child in children):raise RuntimeError('外网测试进程已退出，请重新启动。')
    finally:
        # Close the public route before stopping its local origin.
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
        state['status']='STOPPED';save()
        for log in logs:log.close()
        lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop',action='store_true')
    parser.add_argument('--port',type=int,choices=[8770,8771],default=8770)
    parser.add_argument('--edge-discovery',choices=['system','doh'],default=None,
                        help='默认沿用项目已保存配置；doh仅为本隧道使用Cloudflare官方HTTPS解析')
    args=parser.parse_args()
    if args.stop:stop_existing()
    else:main(args.edge_discovery,args.port)
