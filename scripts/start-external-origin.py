"""Serve the existing business app only through one temporary HTTPS hostname."""
import argparse
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings


def external_settings(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com', parts.netloc) or parts.path not in {'','/'} or parts.query or parts.fragment:
        raise ValueError('临时入口必须是完整的Cloudflare HTTPS测试地址')
    return Settings(environment='production', public_origin='https://' + parts.netloc,
                    allowed_hosts=[parts.netloc], secure_cookie=True, cookie_namespace='external_test')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--port',type=int,default=8770)
    args=parser.parse_args();settings=external_settings(args.url)
    from app.main import create_app
    import uvicorn
    uvicorn.run(create_app(settings),host='127.0.0.1',port=args.port,access_log=False,
                proxy_headers=True,forwarded_allow_ips='127.0.0.1')


if __name__=='__main__':main()
