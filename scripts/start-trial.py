"""Serve the isolated trial database; never binds to a public interface."""
import json
from pathlib import Path
import sys
if (Path(__file__).resolve().parents[1] / 'data/runtime-isolation.json').exists():
    raise SystemExit('已启用稳定/测试版本隔离，请使用“启动测试版.command”（8002）。')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import ROOT,Settings
from app.main import create_app
from app.database import make_engine, migrate
import uvicorn
pointer=json.loads((ROOT/'data/trial-current.json').read_text())
path=Path(pointer['database']).resolve()
assert path.parent.parent==ROOT/'data' and path.parent.name.startswith('trial-') and path.is_file()
maps=Settings()
s=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(path),public_origin='http://127.0.0.1:8001',allowed_hosts=['127.0.0.1','localhost'],
 planning_road_source='TEST_GRID',truck_live_calls_enabled=False,cookie_namespace='uat',uat_delivery_date=pointer['default_delivery_date'],
 map_provider=maps.map_provider,amap_js_key=maps.amap_js_key,amap_security_js_code=maps.amap_security_js_code,amap_web_service_key=maps.amap_web_service_key,
 amap_rate_limit_path=maps.amap_rate_limit_path,amap_web_qps=maps.amap_web_qps,amap_js_qps=maps.amap_js_qps)
engine=make_engine(s)
migrate(engine)
engine.dispose()
uvicorn.run(create_app(s),host='127.0.0.1',port=8001,access_log=False)
