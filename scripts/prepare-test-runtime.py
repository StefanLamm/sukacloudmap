"""Prepare a stopped test snapshot without changing stable code or business data."""
import hashlib
import fcntl
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from app.config import Settings
from scripts.backup_sqlite import backup
from scripts.runtime_isolation import runtime_roots


def private_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def clone_test_database(source, target):
    """One-time online copy. Never reset an existing test database."""
    source, target = Path(source).resolve(), Path(target)
    if target.exists() or target.is_symlink():
        raise RuntimeError('测试数据库已存在，拒绝覆盖。')
    # Do not publish a raw copy containing stable sessions if sanitization fails.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.test-copy-') as staging:
        temporary = Path(staging) / 'application.db'
        proof = backup(source, temporary)
        with sqlite3.connect(temporary) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('DELETE FROM login_sessions')
            db.execute('DELETE FROM login_throttles')
            db.execute("UPDATE planning_jobs SET status='CANCELLED', cancel_requested=1, "
                       "lease_until=NULL, finished_at=?, error_code='TEST_COPY', "
                       "error_message='初始化独立测试副本，未继续执行原任务。' "
                       "WHERE status IN ('QUEUED','RUNNING')", (int(time.time()),))
            db.execute('DELETE FROM location_tasks')
            db.commit()
            if (db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                    or db.execute('PRAGMA foreign_key_check').fetchall()):
                raise RuntimeError('测试副本完整性检查失败。')
        # Same-filesystem link provides exclusive installation without overwrite.
        os.link(temporary, target)
        temporary.unlink()
    proof.update(backup=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    return proof


def file_settings(config):
    """Only the frozen local configuration is a source, not inherited SUKA_*."""
    inherited = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith('SUKA_')}
    try:
        return Settings(_env_file=config)
    finally:
        os.environ.update(inherited)


def initialize_reference_graph(source, target):
    """Seed prior measured-road learning once, into an independent SQLite file."""
    source, target = Path(source).resolve(), Path(target)
    if source == target.resolve():
        raise RuntimeError('稳定版与测试版道路参考图不能共用文件。')
    if target.is_symlink() or target.resolve() != target:
        raise RuntimeError('测试道路参考图不能使用链接。')
    if target.exists():
        if not target.is_file() or target.stat().st_nlink != 1:
            raise RuntimeError('测试道路参考图必须是独立文件。')
        return False
    if not source.is_file():
        return False
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.road-copy-') as staging:
        temporary = Path(staging) / 'reference-graph.db'
        temporary.touch(mode=0o600)
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.commit()
            if dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise RuntimeError('道路参考图备份检查点未完成。')
            if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                raise RuntimeError('道路参考图备份仍依赖 WAL。')
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('道路参考图副本完整性检查失败。')
            # The runtime writes eight values in this order. A partial legacy
            # schema must not appear usable and then fail on the first update.
            columns = [row[1] for row in dst.execute('PRAGMA table_info(metrics)')]
            if columns != ['provider_key', 'from_id', 'to_id', 'meters', 'seconds',
                           'received_at', 'evidence_hash', 'content']:
                raise RuntimeError('道路参考图结构不兼容，未创建测试副本。')
        os.link(temporary, target)
        temporary.unlink()
    return True


def write_test_config(target, stable_config):
    # Stable remains the initialization source, even if an old test .env is
    # present but its database has not yet been created. Existing test choices
    # (keys, request limits and optimizer profile) survive ordinary refreshes.
    stable_settings = file_settings(stable_config)
    source = make_url(stable_settings.database_url.get_secret_value())
    if source.get_backend_name() != 'sqlite' or not source.database or not Path(source.database).is_absolute():
        raise RuntimeError('稳定版必须配置绝对路径 SQLite 数据库才能复制。')
    config = target / '.env'
    if config.exists() and (not config.is_file() or config.is_symlink()):
        raise RuntimeError('测试版配置必须是独立的普通文件。')
    settings = file_settings(config) if config.is_file() else stable_settings
    values = {key: getattr(settings, key) for key in type(settings).model_fields}
    values.update(environment='test', database_url='sqlite:///' + str(target / 'data/application.db'),
                  public_origin='http://127.0.0.1:8002', allowed_hosts=['127.0.0.1', 'localhost'],
                  secure_cookie=False, cookie_namespace='test_local',
                  amap_reference_graph_path=target / 'data/reference-graph.db',
                  amap_reference_graph_test_enabled=True,
                  map_auto_geocode_enabled=False,
                  runtime_label='测试版 · 独立数据，修改不影响稳定版')
    lines = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, Path):
            value = str(value.resolve())
        # dotenv quoted strings protect spaces; JSON arrays remain valid settings.
        lines.append('SUKA_' + key.upper() + '=' + json.dumps(value, ensure_ascii=False))
    with tempfile.NamedTemporaryFile(mode='w', dir=target, prefix='.env.', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write('\n'.join(lines) + '\n')
    temporary.replace(config)
    return Path(source.database)


def check_existing_runtime(target):
    """Run before copying source/config so an old lock cannot be overwritten first."""
    for relative in ('data', 'data/reference-graph.db', '.env', '.venv', 'frontend', 'frontend/dist', 'requirements.lock', 'source-manifest.json'):
        path = target / relative
        if path.resolve() != path or path.is_symlink():
            raise RuntimeError('测试运行目录不能通过链接共用稳定版文件。')
    database = target / 'data/application.db'
    if database.exists() and (not database.is_file() or database.stat().st_nlink != 1):
        raise RuntimeError('测试数据库不能共用硬链接文件。')
    graph = target / 'data/reference-graph.db'
    if graph.exists() and (not graph.is_file() or graph.stat().st_nlink != 1):
        raise RuntimeError('测试道路参考图不能共用硬链接文件。')
    if (target / '.venv').exists():
        lock = target / 'requirements.lock'
        if not lock.is_file() or lock.read_bytes() != (ROOT / 'requirements.lock').read_bytes():
            raise RuntimeError('依赖锁已变化，请准备新的独立Python依赖后重试。')


def _prepare():
    if subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip() != 'test':
        raise RuntimeError('仅允许在 test 分支准备测试版本。')
    roots = runtime_roots(ROOT)
    if roots is None:
        raise RuntimeError('请先冻结并登记稳定版独立运行目录。')
    stable = roots['stable_root']
    target = roots.get('test_root', ROOT / 'data/runtimes/test-working')
    if target == stable or target.is_symlink():
        raise RuntimeError('测试目录不能与稳定目录相同或使用链接。')
    check_existing_runtime(target)
    # A stopped listener alone is insufficient: a tunnel may still be starting.
    for name in ('test', 'test-external'):
        record = ROOT / 'data/managed-services' / (name + '.json')
        if record.exists():
            pid = json.loads(record.read_text())['pid']
            process = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
            if process.returncode == 0:
                raise RuntimeError('请先用 manage-services.py stop test test-external 停止测试服务。')
            if process.returncode != 1 or process.stderr.strip() or process.stdout.strip():
                raise RuntimeError('无法核实测试进程已停止，请检查进程查询权限后重试。')
    # Refuse unmanaged listeners as well; preparation must never rewrite live assets.
    import socket
    for port in (8002, 8771):
        try:
            with socket.socket() as probe:
                result = probe.connect_ex(('127.0.0.1', port))
        except OSError:
            raise RuntimeError('无法检查测试端口，请检查本机网络访问权限后重试。') from None
        if result == 0:
            raise RuntimeError('测试端口仍被占用，请检查后再准备。')
        if result != errno.ECONNREFUSED:
            raise RuntimeError('无法确认测试端口空闲，请检查本机网络访问权限后重试。')
    if not (ROOT / 'frontend/dist/index.html').is_file():
        raise RuntimeError('请先测试并构建当前 test 前端。')
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    (target / 'data').mkdir(mode=0o700, exist_ok=True)
    source = write_test_config(target, stable / '.env')
    database = target / 'data/application.db'
    initialized = not database.exists()
    if initialized:
        clone_test_database(source, database)
    graph_initialized = initialize_reference_graph(
        file_settings(stable / '.env').amap_reference_graph_path,
        target / 'data/reference-graph.db')
    files = subprocess.check_output(['git', 'ls-files', '-c', '-o', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    hashes = {}
    for name in filter(None, files):
        path = ROOT / name
        if not path.is_file():
            continue
        if path.is_symlink() or name.startswith(('data/', '.env', '.venv/', '.cache/')):
            if name == '.env.example':
                continue
            raise RuntimeError('源文件清单包含私有或链接文件，请检查 Git 跟踪状态。')
        destination = target / name
        if destination.resolve() != destination or destination.is_symlink():
            raise RuntimeError('快照代码不能使用链接。')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    previous = target / 'source-manifest.json'
    if previous.exists():
        for name in json.loads(previous.read_text()).get('files', {}):
            path = target / name
            if name not in hashes and path.resolve().is_relative_to(target) and not name.startswith(('data/', '.env', '.venv/')):
                path.unlink(missing_ok=True)
    if not (target / '.venv').exists():
        shutil.copytree(ROOT / '.venv', target / '.venv', symlinks=True)
    dist = target / 'frontend/dist'
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(ROOT / 'frontend/dist', dist)
    (target / '.cache').mkdir(exist_ok=True)
    shutil.copy2(ROOT / '.cache/cloudflared', target / '.cache/cloudflared')
    external = target / 'data/external-test'
    external.mkdir(mode=0o700, exist_ok=True)
    shutil.copy2(stable / 'data/external-test/settings.json', external / 'settings.json')
    private_json(previous, {'branch': 'test', 'base_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(), 'files': hashes})
    private_json(ROOT / 'data/runtime-isolation.json', {'stable_root': str(stable), 'test_root': str(target)})
    print(json.dumps({'status': 'PREPARED', 'test_root': str(target), 'database_initialized': initialized,
                      'reference_graph_initialized': graph_initialized, 'source_files': len(hashes)}, ensure_ascii=False))


def prepare():
    # Use the service manager's lock: start/stop cannot race a snapshot update.
    folder = ROOT / 'data/managed-services'
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (folder / 'manager.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _prepare()


if __name__ == '__main__':
    try:
        prepare()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
