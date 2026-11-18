"""Fail-closed paths for the two private runtime snapshots.

The workspace owns service records; each runtime owns its code, assets, local
configuration and data. Only the AMap rate limiter is deliberately shared.
"""
import json
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def runtime_roots(workspace):
    workspace = Path(workspace).resolve()
    pointer = workspace / 'data/runtime-isolation.json'
    if not pointer.exists():
        return None
    try:
        payload = json.loads(pointer.read_text())
        if not isinstance(payload, dict):
            raise ValueError
        roots = {}
        for name in ('stable_root', 'test_root'):
            value = payload.get(name)
            if value is None and name == 'test_root':
                continue  # Stable can be frozen before a test build is prepared.
            if not isinstance(value, str):
                raise ValueError
            original = Path(value)
            resolved = original.resolve()
            if (not original.is_absolute() or original != resolved
                    or resolved.parent != workspace / 'data/runtimes'
                    or not resolved.is_dir()):
                raise ValueError
            roots[name] = resolved
        if roots.get('test_root') == roots['stable_root']:
            raise ValueError
        return roots
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('运行隔离配置无效，未回退到工作区业务库。') from None


def service_root(workspace, name):
    workspace = Path(workspace).resolve()
    if name not in {'main', 'external', 'test', 'test-external'}:
        return workspace
    roots = runtime_roots(workspace)
    if roots is None:
        if name in {'test', 'test-external'}:
            raise RuntimeError('尚未准备独立测试运行目录。')
        return workspace  # Existing installations remain operable before setup.
    key = 'test_root' if name in {'test', 'test-external'} else 'stable_root'
    if key not in roots:
        raise RuntimeError('尚未准备独立测试运行目录。')
    return roots[key]


def clean_environment():
    """Runtime .env files, rather than a caller's SUKA_* values, are authoritative."""
    return {name: value for name, value in os.environ.items()
            if not name.upper().startswith(('SUKA_', 'TUNNEL_'))}


def qa_runtime_records(workspace):
    """Optional third-runtime records never replace stable/test service roots."""
    workspace = Path(workspace).resolve()
    roots = runtime_roots(workspace)
    if roots is None:
        raise RuntimeError('QA 验收必须先有稳定/测试隔离登记。')
    try:
        payload = json.loads((workspace / 'data/runtime-isolation.json').read_text())
        records = payload.get('qa_runtimes', {})
        if not isinstance(records, dict):
            raise ValueError
        found = {}
        for name, record in records.items():
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
                raise ValueError
            if not isinstance(record, dict):
                raise ValueError
            raw = Path(record['root'])
            root = raw.resolve()
            if (not raw.is_absolute() or raw != root or root.parent != workspace / 'data/runtimes'
                    or not root.is_dir() or root in roots.values() or root in found
                    or record.get('public_origin') != 'http://127.0.0.1:8003'
                    or record.get('cookie_namespace') != 'test_delivery_qa'
                    or not re.fullmatch(r'[0-9a-f]{64}', record.get('source_manifest_sha256', ''))):
                raise ValueError
            manifest = root / 'source-build-manifest.json'
            if (not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_nlink != 1
                    or hashlib.sha256(manifest.read_bytes()).hexdigest() != record['source_manifest_sha256']):
                raise ValueError
            found[root] = record
        return found
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('QA 验收登记无效，未回退到稳定版或测试版业务库。') from None


def validate_test_runtime(root, settings):
    root = Path(root).resolve()
    if root.parent.name != 'runtimes' or root.parent.parent.name != 'data':
        raise RuntimeError('测试服务必须从独立运行目录启动。')
    workspace = root.parents[2]
    roots = runtime_roots(workspace)
    qa = False
    if roots is not None and roots.get('test_root') != root:
        qa = root in qa_runtime_records(workspace)
    if roots is None or (roots.get('test_root') != root and not qa):
        raise RuntimeError('当前目录不是已登记的独立测试版本。')
    config = root / '.env'
    if not config.is_file() or config.is_symlink():
        raise RuntimeError('测试版缺少独立配置文件。')
    try:
        url = make_url(settings.database_url.get_secret_value())
    except ArgumentError:
        raise RuntimeError('测试数据库地址配置无效。') from None
    if url.drivername != 'sqlite' or not url.database:
        raise RuntimeError('测试版仅允许专属 SQLite 副本。')
    paths = [Path(url.database), Path(settings.amap_reference_graph_path)]
    for path in paths:
        resolved = path.resolve()
        if (not path.is_absolute() or path != resolved
                or not resolved.is_relative_to(root / 'data')
                or resolved == root / 'data'
                or (resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1))):
            raise RuntimeError('测试数据库和道路缓存必须位于测试版专属目录，且不能共享文件。')
    if paths[0] == paths[1] or not paths[0].is_file():
        raise RuntimeError('测试数据库副本不存在或与道路缓存路径重复。')
    if not qa and settings.cookie_namespace not in {'test_local', 'test_external'}:
        raise RuntimeError('测试版必须使用独立登录 Cookie。')
    if qa:
        if (settings.cookie_namespace != 'test_delivery_qa' or settings.environment != 'test'
                or settings.secure_cookie or settings.public_origin != 'http://127.0.0.1:8003'
                or set(settings.allowed_hosts) != {'127.0.0.1', 'localhost'}):
            raise RuntimeError('QA 验收版必须使用已登记的本机 8003 入口和 test 环境。')
    elif settings.cookie_namespace == 'test_local':
        if (settings.environment != 'test' or settings.secure_cookie
                or settings.public_origin != 'http://127.0.0.1:8002'
                or set(settings.allowed_hosts) != {'127.0.0.1', 'localhost'}):
            raise RuntimeError('本机测试版必须固定使用独立 8002 入口。')
    else:
        origin = urlsplit(settings.public_origin)
        if (settings.environment != 'production' or not settings.secure_cookie
                or origin.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com', origin.netloc)
                or origin.path or origin.query or origin.fragment
                or settings.allowed_hosts != [origin.netloc]):
            raise RuntimeError('外网测试版必须使用严格 HTTPS 来源和安全 Cookie。')
    if settings.map_auto_geocode_enabled:
        raise RuntimeError('测试版默认必须关闭后台自动地址关联。')
    return paths[0]
