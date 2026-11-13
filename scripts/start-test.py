"""Serve only the registered test snapshot and its private data copy."""
import argparse
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.runtime_isolation import validate_test_runtime


def test_settings(url=None):
    # BaseSettings reads process environment before .env. Drop inherited values
    # before importing settings so a terminal cannot point this runner at main.
    for name in list(os.environ):
        if name.upper().startswith('SUKA_'):
            del os.environ[name]
    from app.config import Settings

    options = dict(environment='test', public_origin='http://127.0.0.1:8002',
                   allowed_hosts=['127.0.0.1', 'localhost'], secure_cookie=False,
                   cookie_namespace='test_local', map_auto_geocode_enabled=False)
    if url:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com', parts.netloc)
                or parts.path not in {'', '/'} or parts.query or parts.fragment):
            raise ValueError('测试外网入口必须是完整的 Cloudflare HTTPS 地址。')
        options.update(environment='production', public_origin='https://' + parts.netloc,
                       allowed_hosts=[parts.netloc], secure_cookie=True,
                       cookie_namespace='test_external')
    settings = Settings(_env_file=ROOT / '.env', **options)
    validate_test_runtime(ROOT, settings)
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url')
    parser.add_argument('--port', type=int)
    parser.add_argument('--check', action='store_true', help='仅校验隔离配置，不启动服务')
    args = parser.parse_args()
    expected_port = 8771 if args.url else 8002
    if args.port is not None and args.port != expected_port:
        parser.error('本机测试使用 8002，外网测试源服务使用 8771。')
    settings = test_settings(args.url)
    if args.check:
        print('测试运行隔离配置通过。', flush=True)
        return
    from app.database import make_engine, migrate
    from app.main import create_app
    import uvicorn

    engine = make_engine(settings)
    try:
        migrate(engine)
    finally:
        engine.dispose()
    print('测试版 · 独立数据：' + settings.public_origin, flush=True)
    uvicorn.run(create_app(settings), host='127.0.0.1', port=expected_port,
                access_log=False, proxy_headers=bool(args.url),
                forwarded_allow_ips='127.0.0.1')


if __name__ == '__main__':
    main()
