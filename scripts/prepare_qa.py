from pathlib import Path
import json,sqlite3,sys,tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import ROOT,Settings
from app.database import make_engine,session_factory
from app.models import Account
from app.security import hash_password
source=Path(json.loads((ROOT/'data/trial-current.json').read_text())['database'])
folder=Path(tempfile.mkdtemp(prefix='suka-hardening-qa-'));path=folder/'qa.db'
from scripts.backup_sqlite import backup
backup(source,path)
s=Settings(_env_file=None,environment='test',database_url='sqlite:///'+str(path),planning_road_source='TEST_GRID',truck_live_calls_enabled=False,amap_web_service_key=None,cookie_namespace='qa')
e=make_engine(s)
with session_factory(e)() as db:
 for i in range(13):
  name='hardening' if i==0 else f'stress{i:02}'
  db.add(Account(username=name,display_name='隔离测试管理员' if i==0 else f'压力测试{i:02}',password_hash=hash_password('Qa-Only-Hardening-2026!'),is_admin=i==0))
 db.commit()
e.dispose()
pointer={'database':str(path),'folder':str(folder),'port':8765,'default_delivery_date':'2026-08-06'}
Path('/tmp/suka-hardening-qa.json').write_text(json.dumps(pointer));print(str(path))
