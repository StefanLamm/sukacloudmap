"""Start test services in independent OS sessions, with private logs and PID records.

No system daemon, login auto-start, router or firewall change is installed.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.runtime_isolation import clean_environment, runtime_roots, service_root

FOLDER = ROOT / 'data/managed-services'
PYTHON = str(ROOT / '.venv/bin/python')
SERVICES = {
    'main': (8000, ['/bin/bash', str(ROOT / 'scripts/start.sh')]),
    'trial': (8001, [PYTHON, str(ROOT / 'scripts/start-trial.py')]),
    'history': (8765, [PYTHON, str(ROOT / 'scripts/start-acceptance.py'), '--mode', 'history']),
    'driving': (8766, [PYTHON, str(ROOT / 'scripts/start-acceptance.py'), '--mode', 'driving']),
    'external': (8770, [PYTHON, str(ROOT / 'scripts/start-external-test.py')]),
    'test': (8002, [PYTHON, str(ROOT / 'scripts/start-test.py')]),
    'test-external': (8771, [PYTHON, str(ROOT / 'scripts/start-external-test.py'), '--port', '8771']),
}


def service_spec(name):
    root = service_root(ROOT, name)
    port, command = SERVICES[name]
    if root == ROOT:
        return port, command, root
    python = str(root / '.venv/bin/python')
    if name == 'main':
        command = ['/bin/bash', str(root / 'scripts/start.sh')]
    elif name == 'test':
        command = [python, str(root / 'scripts/start-test.py')]
    else:
        command = [python, str(root / 'scripts/start-external-test.py')]
        if name == 'test-external':
            command += ['--port', str(port)]
    return port, command, root


def all_services(action):
    roots = runtime_roots(ROOT)
    if roots is None:
        return [name for name in SERVICES if name not in {'test', 'test-external'}]
    if action == 'start':
        # Public tunnels are opt-in; restoring local environments never opens one.
        return ['main'] + (['test'] if 'test_root' in roots else [])
    return [name for name in SERVICES
            if name not in {'test', 'test-external'} or 'test_root' in roots]


def identity(pid):
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return None
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True)
    if result.returncode or str(ROOT) not in result.stdout:
        return None
    return result.stdout.strip()


def owned_pid(name):
    try:
        state = json.loads((FOLDER / (name + '.json')).read_text())
        pid = state['pid']
        current = identity(pid)
        if current and current == state['identity'] and os.getpgid(pid) == pid:
            return pid
    except (OSError, ValueError, KeyError):
        pass
    return None


def ready_url(name):
    port, _, root = service_spec(name)
    if root != ROOT:
        pid = owned_pid(name)
        if not pid or str(root) + '/' not in (identity(pid) or ''):
            return None
    headers = {}
    url = f'http://127.0.0.1:{port}'
    if name in {'external', 'test-external'}:
        try:
            state = json.loads((root / 'data/external-test/current.json').read_text())
            if state.get('status') != 'RUNNING' or state.get('port') != port:
                return None
            url = state['url']
            parts = urlsplit(url)
            if (parts.scheme != 'https' or not parts.hostname
                    or not parts.hostname.endswith('.trycloudflare.com')
                    or parts.username or parts.password or parts.port or parts.path or parts.query or parts.fragment):
                return None
            headers['host'] = parts.netloc
        except (OSError, ValueError, KeyError):
            return None
    try:
        response = httpx.get(f'http://127.0.0.1:{port}/healthz', headers=headers,
                             timeout=2, trust_env=False)
        if response.status_code != 200 or response.json().get('status') != 'ok':
            return None
        if name in {'external', 'test-external'}:
            # A healthy loopback origin does not prove the tunnel is connected.
            response = httpx.get(url + '/healthz', timeout=3, trust_env=False)
            if response.status_code != 200 or response.json().get('status') != 'ok':
                return None
        return url
    except (httpx.HTTPError, ValueError):
        pass
    return None


def save_process(name, pid):
    current = identity(pid)
    if current:
        path = FOLDER / (name + '.json')
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps({'pid': pid, 'identity': current}, ensure_ascii=False, indent=2) + '\n')


def start(name):
    if name in {'trial', 'history', 'driving'} and runtime_roots(ROOT) is not None:
        raise RuntimeError('已启用版本隔离，旧验收入口停止启动；请使用 test 独立测试版。')
    port, command, root = service_spec(name)
    if root != ROOT:
        pid = owned_pid(name)
        if pid and str(root) + '/' not in (identity(pid) or ''):
            raise RuntimeError(f'{name} 仍由旧目录运行，请先停止该服务再启动隔离版。')
    if url := ready_url(name):
        print(f'{name} 已运行：{url}', flush=True)
        return
    process = None
    if not owned_pid(name):
        # Never start over an unrelated listener or an old unmanaged origin.
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'{name} 端口已占用，请先检查原进程。')
        path = FOLDER / (name + '.log')
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with path.open('a') as log:
            process = subprocess.Popen(command, cwd=root,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
                env={**clean_environment(), 'PYTHONUNBUFFERED': '1'})
        save_process(name, process.pid)
    print(f'正在恢复 {name}…', flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f'{name} 启动退出，请查看 data/managed-services/{name}.log')
        if process is not None:
            # Only our still-live Popen may refresh identity after start.sh execs
            # uvicorn. Never adopt an arbitrary listener or reused PID.
            save_process(name, process.pid)
        if url := ready_url(name):
            if process is not None:
                # start.sh execs uvicorn; record its final command identity.
                save_process(name, process.pid)
            print(f'{name} 已恢复：{url}', flush=True)
            return
        time.sleep(.5)
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
    raise RuntimeError(f'{name} 未能就绪，请查看 data/managed-services/{name}.log')


def stop(name):
    if pid := owned_pid(name):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 25
        while owned_pid(name) and time.monotonic() < deadline:
            time.sleep(.2)
        if owned_pid(name):
            raise RuntimeError(f'{name} 尚未完成停止，请稍后重试。')
        (FOLDER / (name + '.json')).unlink(missing_ok=True)
        print(f'{name} 后台服务已停止。', flush=True)
    elif name in {'external', 'test-external'}:
        # Compatibility: the original launcher independently verifies PID identity.
        _, command, root = service_spec(name)
        result = subprocess.run(command + ['--stop'], cwd=root, env=clean_environment())
        if result.returncode:
            raise RuntimeError('原外网测试进程未运行或无法停止。')
    else:
        print(f'{name} 没有本项目托管的后台服务。', flush=True)


def main():
    parser = argparse.ArgumentParser(description='启动、停止或查看独立于聊天应用的测试服务')
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('services', nargs='+', choices=[*SERVICES, 'all'])
    args = parser.parse_args()
    names = all_services(args.action) if 'all' in args.services else list(dict.fromkeys(args.services))
    if args.action == 'stop':
        names = ([name for name in names if name in {'external', 'test-external'}]
                 + [name for name in names if name not in {'external', 'test-external'}])
    FOLDER.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = FOLDER / 'manager.lock'
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in names:
            if args.action == 'start':
                start(name)
            elif args.action == 'stop':
                stop(name)
            else:
                print(f'{name}：{ready_url(name) or "未就绪"}；独立后台：{bool(owned_pid(name))}')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
