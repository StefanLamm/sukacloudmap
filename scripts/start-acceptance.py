"""Open the preserved acceptance data on loopback with external routing disabled."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from app.config import ROOT,Settings
from app.main import create_app

parser=argparse.ArgumentParser(description='本轮完整历史回放与真实三店轨迹验收')
parser.add_argument('--mode',choices=['history','driving'],default='history')
args=parser.parse_args()
folder=ROOT/'data/retest-20261112'
path=folder/('full-replay.db' if args.mode=='history' else 'real-route-qa.db')
if not path.is_file():parser.exit(1,'验收归档不存在，请先完成本轮测试。\n')
port=8765 if args.mode=='history' else 8766
maps=Settings()
s=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(path),
           public_origin=f'http://127.0.0.1:{port}',allowed_hosts=['127.0.0.1','localhost'],
           cookie_namespace='acceptance_'+args.mode,planning_road_source='TEST_GRID' if args.mode=='history' else 'LIVE',
           planning_route_mode='DRIVING',truck_live_calls_enabled=False,driving_live_calls_enabled=False,
           map_auto_geocode_enabled=False,uat_delivery_date='2026-08-06' if args.mode=='history' else None,
           map_provider=maps.map_provider,amap_web_service_key=maps.amap_web_service_key,
           amap_js_key=maps.amap_js_key,amap_security_js_code=maps.amap_security_js_code,
           amap_rate_limit_path=maps.amap_rate_limit_path,amap_web_qps=maps.amap_web_qps,amap_js_qps=maps.amap_js_qps)
print(f'隔离验收：http://127.0.0.1:{port}；使用原有账号，真实算路已关闭。',flush=True)
if args.mode=='driving':print('在物流线路规划中选择2026-10-31，查看真实三店驾车参考轨迹。',flush=True)
uvicorn.run(create_app(s),host='127.0.0.1',port=port,access_log=False)
