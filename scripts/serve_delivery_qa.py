"""Prepare/check/serve a third, explicit loopback QA snapshot on port 8003.

Preparation never stops a service or opens a listener. It copies only the given
isolated test source tree and its built frontend, reads the registered test data
through SQLite backup, preserves sessions/accounts/history, and appends one QA
record without changing stable_root/test_root. No secrets are printed.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ORIGIN = 'http://127.0.0.1:8003'
MUTABLE_COPY_TABLES = {'planning_jobs', 'location_tasks'}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def private_json(path, value, *, exclusive=True):
    path = Path(path)
    if exclusive:
        with path.open('x', encoding='utf-8') as stream:
            path.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write('\n')
    else:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write('\n')
        temporary.replace(path)


@contextmanager
def no_inherited_settings():
    removed = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith(('SUKA_', 'TUNNEL_'))}
    try:
        yield
    finally:
        os.environ.update(removed)


def settings_from_file(path, **changes):
    from app.config import Settings
    with no_inherited_settings():
        current = Settings(_env_file=path)
        values = {key: getattr(current, key) for key in type(current).model_fields}
        return Settings(_env_file=None, **{**values, **changes})


def validate_test_experiments(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('QA 实验开关必须为 JSON 对象。')
    from app.route_experiments import DeepExperiments
    try:
        DeepExperiments.parse(value)
    except (TypeError, ValueError):
        raise ValueError('QA 实验开关名称、类型或策略无效。') from None
    return dict(value)


def parse_experiments_json(value):
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = json.loads(Path(value).read_text())
    return validate_test_experiments(parsed)


def sqlite_path(settings):
    from sqlalchemy.engine import make_url
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername != 'sqlite' or not url.database:
        raise RuntimeError('QA 仅复制已登记测试版的独立 SQLite 数据。')
    raw = Path(url.database)
    if not raw.is_absolute() or raw != raw.resolve():
        raise RuntimeError('QA 源数据库必须为绝对路径且不能使用链接。')
    return raw


def protected_table_hashes(path):
    """Hash every original table except explicitly cancelled background queues."""
    result = {}
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in sorted(set(names) - MUTABLE_COPY_TABLES):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + quoted + ')')]
            encoded = []
            for row in db.execute('SELECT * FROM ' + quoted):
                values = [{'bytes_hex': value.hex()} if isinstance(value, bytes) else value for value in row]
                encoded.append(json.dumps(values, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
            payload = json.dumps({'columns': columns, 'rows': sorted(encoded)}, ensure_ascii=False, separators=(',', ':')).encode()
            result[name] = {'rows': len(encoded), 'sha256': hashlib.sha256(payload).hexdigest()}
        db.rollback()
    return result


def backup_readonly(source, destination):
    source, destination = Path(source), Path(destination)
    if (not source.is_file() or source.is_symlink() or destination.exists()
            or destination.is_symlink() or destination.resolve() == source.resolve()):
        raise RuntimeError('QA 备份需要普通源文件和全新独立目标，拒绝覆盖。')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        dst.commit()
        if (dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0
                or dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete'
                or dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                or dst.execute('PRAGMA foreign_key_check').fetchall()):
            raise RuntimeError('QA SQLite 独立备份完整性检查失败。')
    if destination.stat().st_nlink != 1:
        raise RuntimeError('QA 备份不能与原库共享硬链接。')
    return {'source': str(source), 'copy': str(destination), 'sha256': sha(destination), 'source_connection': 'mode=ro', 'integrity': 'ok'}


def cancel_copied_queues(path):
    """Only the new copy is changed; sessions and saved business plans survive."""
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'planning_jobs' in tables:
            db.execute("UPDATE planning_jobs SET status='CANCELLED', cancel_requested=1, lease_until=NULL, "
                       "finished_at=?, error_code='QA_COPY', error_message='隔离验收副本未继续执行来源任务。' "
                       "WHERE status IN ('QUEUED','RUNNING')", (int(time.time()),))
        if 'location_tasks' in tables:
            db.execute('DELETE FROM location_tasks')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('QA 取消副本后台任务后完整性检查失败。')


def source_files(source):
    source = Path(source)
    branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=source, text=True).strip()
    if branch != 'test':
        raise RuntimeError('QA 只能快照明确指定的独立 test 源码。')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    raw = subprocess.check_output(['git', 'ls-files', '-c', '-o', '--exclude-standard', '-z'], cwd=source).decode()
    names = set(filter(None, raw.split('\0')))
    names.update(str(p.relative_to(source)) for p in (source / 'frontend/dist').rglob('*') if p.is_file())
    selected = []
    for name in sorted(names):
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts
                or name.startswith(('data/', '.git/', '.venv/', '.cache/', 'frontend/node_modules/'))
                or name == 'build-source-manifest.json'
                or (relative.name.startswith('.env') and name != '.env.example')):
            continue
        path = source / relative
        if not path.is_file():
            continue
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError('QA 源码和构建产物不能通过链接追随其他目录。')
        selected.append(name)
    if 'app/routing_engine.py' not in selected or 'frontend/dist/index.html' not in selected:
        raise RuntimeError('QA 源码缺少配送引擎或独立构建前端。')
    return selected, commit


def copy_source(source, target):
    names, commit = source_files(source)
    files = {}
    attestation = None
    for name in names:
        path, destination = source / name, target / name
        before = sha(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        if sha(destination) != before or sha(path) != before:
            raise RuntimeError('QA 快照期间源码或构建产物发生变化，请使用新目录重试。')
        files[name] = before
    if (source / 'app/source_provenance.py').is_file():
        # This app-only attestation is distinct from the full code/frontend
        # snapshot manifest below. It never borrows a parent repository HEAD.
        from app.source_provenance import MANIFEST_NAME, write_build_manifest
        attestation = write_build_manifest(source, target / MANIFEST_NAME)
        copied_app = {name: checksum for name, checksum in files.items()
                      if name.startswith('app/') and name.endswith('.py') and '__pycache__' not in Path(name).parts}
        if attestation['app_file_hashes'] != copied_app:
            raise RuntimeError('QA 快照期间应用源码变化，源码归属清单与副本不符。')
        files[MANIFEST_NAME] = sha(target / MANIFEST_NAME)
    manifest = {'schema_version': 1, 'source_root': str(source), 'source_git_commit': commit,
                'source_branch': 'test', 'created_at': int(time.time()), 'files': files,
                'app_source_hash': (attestation or {}).get('app_source_hash'),
                'app_commit': (attestation or {}).get('commit'),
                'app_commit_verified': bool((attestation or {}).get('source_matches_commit')),
                'git_head_is_only_an_origin_label_unless_app_commit_verified': True,
                'frontend_build_files': [name for name in files if name.startswith('frontend/dist/')],
                'code_and_build_are_copied_not_linked': True}
    private_json(target / 'source-build-manifest.json', manifest)
    return manifest


def verify_snapshot(root):
    root = Path(root)
    manifest_path = root / 'source-build-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get('files', {})
    if 'frontend/dist/index.html' not in files or 'app/routing_engine.py' not in files:
        raise RuntimeError('QA 源码/前端构建清单不完整。')
    for name, checksum in files.items():
        raw = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or raw.resolve() != raw
                or not raw.is_file() or raw.is_symlink() or raw.stat().st_nlink != 1 or sha(raw) != checksum):
            raise RuntimeError('QA 源码或前端构建与已登记清单不符。')
    return manifest


def write_config(target, source_settings, experiments=None):
    from app.config import Settings
    from pydantic import SecretStr
    values = {key: getattr(source_settings, key) for key in type(source_settings).model_fields}
    values.update(environment='test', database_url='sqlite:///' + str(target / 'data/application.db'),
                  public_origin=ORIGIN, allowed_hosts=['127.0.0.1', 'localhost'], secure_cookie=False,
                  cookie_namespace='test_delivery_qa', map_auto_geocode_enabled=False,
                  amap_reference_graph_path=target / 'data/reference-graph.db',
                  amap_reference_graph_test_enabled=True, truck_job_max_calls=None, driving_job_max_calls=None,
                  planning_deep_experiments=experiments,
                  runtime_label='配送算法验收 · 8003 独立副本')
    with no_inherited_settings():
        validated = Settings(_env_file=None, **values)
    values = {key: getattr(validated, key) for key in type(validated).model_fields}
    lines = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, Path):
            value = str(value.resolve())
        lines.append('SUKA_' + key.upper() + '=' + json.dumps(value, ensure_ascii=False))
    with (target / '.env').open('x', encoding='utf-8') as stream:
        (target / '.env').chmod(0o600)
        stream.write('\n'.join(lines) + '\n')


def register_qa(workspace, target, name):
    from scripts.runtime_isolation import qa_runtime_records, runtime_roots
    folder = workspace / 'data/managed-services'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'manager.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        roots = runtime_roots(workspace)
        qa_runtime_records(workspace)  # Refuse any malformed prior QA registration.
        pointer = workspace / 'data/runtime-isolation.json'
        payload = json.loads(pointer.read_text())
        records = payload.setdefault('qa_runtimes', {})
        if name in records or any(record['root'] == str(target) for record in records.values()):
            raise RuntimeError('QA 运行登记已存在，拒绝覆盖。')
        if target in roots.values() or target.parent != workspace / 'data/runtimes':
            raise RuntimeError('QA 运行目录必须是全新独立运行目录。')
        original_roots = {key: payload.get(key) for key in ('stable_root', 'test_root')}
        records[name] = {'root': str(target), 'public_origin': ORIGIN, 'cookie_namespace': 'test_delivery_qa',
                         'source_manifest_sha256': sha(target / 'source-build-manifest.json'),
                         'created_at': int(time.time())}
        private_json(pointer, payload, exclusive=False)
        changed = json.loads(pointer.read_text())
        if any(changed.get(key) != value for key, value in original_roots.items()):
            raise RuntimeError('稳定/测试服务登记意外改变。')
        qa_runtime_records(workspace)


def prepare(workspace, source, name, experiments=None):
    from scripts.runtime_isolation import runtime_roots, validate_test_runtime
    workspace, source = Path(workspace).resolve(), Path(source).resolve()
    experiments = validate_test_experiments(experiments)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise RuntimeError('QA 名称仅允许小写字母、数字、短横线及下划线。')
    roots = runtime_roots(workspace)
    if not roots or 'test_root' not in roots:
        raise RuntimeError('缺少已登记独立测试版。')
    if source == workspace or source in roots.values() or not source.is_relative_to(workspace / 'data'):
        raise RuntimeError('QA 必须显式使用 data 下独立源码，不能追随工作区或既有运行服务。')
    source_settings = settings_from_file(roots['test_root'] / '.env')
    source_db = validate_test_runtime(roots['test_root'], source_settings)
    target = workspace / 'data/runtimes' / name
    target.mkdir(mode=0o700)  # Never reuse/refresh a previously served snapshot.
    (target / 'data').mkdir(mode=0o700)
    manifest = copy_source(source, target)
    before = protected_table_hashes(source_db)
    database = backup_readonly(source_db, target / 'data/application.db')
    graph = backup_readonly(Path(source_settings.amap_reference_graph_path), target / 'data/reference-graph.db')
    copied = protected_table_hashes(target / 'data/application.db')
    cancel_copied_queues(target / 'data/application.db')
    after_copy = protected_table_hashes(target / 'data/application.db')
    source_after = protected_table_hashes(source_db)
    if before != source_after or before != copied or before != after_copy:
        raise RuntimeError('复制期间来源数据变化或副本账号/历史资料未保持一致，请用新目录重试。')
    write_config(target, source_settings, experiments)
    verify_snapshot(target)
    proof = {'schema_version': 1, 'created_at': int(time.time()), 'source_test_root': str(roots['test_root']),
             'source_database': str(source_db), 'source_config_sha256': sha(roots['test_root'] / '.env'),
             'stable_root_untouched': str(roots['stable_root']), 'test_root_untouched': str(roots['test_root']),
             'source_read_mode': 'SQLite mode=ro backup and hash only', 'protected_table_hashes': before,
             'source_before_equals_after': before == source_after, 'copy_protected_equals_source': before == after_copy,
             'preserved_sessions': 'Original login_sessions hashes preserved; independent test_delivery_qa cookie requires normal login, no session injection or reuse',
             'copy_only_queue_changes': sorted(MUTABLE_COPY_TABLES), 'application_backup': database, 'graph_backup': graph,
             'application_copy_sha256_after_queue_cancel': sha(target / 'data/application.db'),
             'source_git_commit': manifest['source_git_commit'], 'source_build_manifest_sha256': sha(target / 'source-build-manifest.json'),
             'app_source_hash': manifest.get('app_source_hash'), 'app_commit': manifest.get('app_commit'),
             'app_commit_verified': manifest.get('app_commit_verified', False),
             'test_experiments_override': experiments,
             'no_listener_started': True, 'no_service_stopped': True}
    private_json(target / 'data/preservation-proof.json', proof)
    register_qa(workspace, target, name)
    settings = settings_from_file(target / '.env')
    validate_test_runtime(target, settings)
    return {'status': 'PREPARED_NOT_STARTED', 'qa_root': str(target), 'origin': ORIGIN,
            'source_git_commit': manifest['source_git_commit'], 'source_build_manifest_sha256': sha(target / 'source-build-manifest.json'),
            'app_source_hash': manifest.get('app_source_hash'), 'app_commit': manifest.get('app_commit'),
            'app_commit_verified': manifest.get('app_commit_verified', False),
            'test_experiments_override': experiments,
            'preservation_proof': str(target / 'data/preservation-proof.json')}


def check(root):
    from scripts.runtime_isolation import validate_test_runtime
    root = Path(root).resolve()
    settings = settings_from_file(root / '.env')
    validate_test_runtime(root, settings)
    manifest = verify_snapshot(root)
    return settings, {'status': 'QA_GUARDS_VALID', 'qa_root': str(root), 'origin': ORIGIN,
                      'source_git_commit': manifest['source_git_commit'],
                      'app_source_hash': manifest.get('app_source_hash'), 'app_commit': manifest.get('app_commit'),
                      'app_commit_verified': manifest.get('app_commit_verified', False),
                      'test_experiments_override': settings.planning_deep_experiments,
                      'source_build_manifest_sha256': sha(root / 'source-build-manifest.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--workspace', required=True)
    prep.add_argument('--source', required=True)
    prep.add_argument('--name', required=True)
    prep.add_argument('--experiments-json', help='Test-only JSON object, or path to a JSON file; omitted uses production defaults')
    for name in ('check', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--runtime', required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        experiments = None
        if args.experiments_json is not None:
            experiments = parse_experiments_json(args.experiments_json)
        result = prepare(args.workspace, args.source, args.name, experiments)
    else:
        # Background helpers must also see this snapshot's .env, even if a
        # terminal exported settings for a different service before launch.
        for key in list(os.environ):
            if key.upper().startswith(('SUKA_', 'TUNNEL_')):
                del os.environ[key]
        root = Path(args.runtime).resolve()
        if ROOT != root:
            raise RuntimeError('校验/启动必须执行 QA 快照内自己的脚本，不能引用动态源码。')
        settings, result = check(root)
        if args.command == 'run':
            from app.database import make_engine, migrate
            from app.main import create_app
            import uvicorn
            engine = make_engine(settings)
            try:
                migrate(engine)  # Only the explicitly validated private QA copy.
            finally:
                engine.dispose()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            uvicorn.run(create_app(settings), host='127.0.0.1', port=8003, access_log=False,
                        proxy_headers=False, forwarded_allow_ips='127.0.0.1')
            return 0
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        # Our expected errors contain no credentials, connection URLs or rows.
        raise SystemExit(str(exc)) from None
