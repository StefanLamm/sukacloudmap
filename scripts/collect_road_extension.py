"""Explicit, bounded real-road extension for a confirmed warehouse correction."""
import argparse,gzip,json,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.road_service import DrivingRoad
from app.road_graph import SCHEMA
from app.planning_inputs import content_hash,encode
from scripts.collect_algorithm_roads import private_json


def main(args):
    jobs_path=Path(args.jobs);description=json.loads(jobs_path.read_text());jobs=description['jobs']
    assert len(jobs)==description['new_provider_keys']
    path=Path(args.output);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);path.touch(mode=0o600,exist_ok=True);path.chmod(0o600)
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL');db.execute(SCHEMA)
    db.executescript('CREATE TABLE IF NOT EXISTS edges(from_id TEXT,to_id TEXT,provider_key TEXT,PRIMARY KEY(from_id,to_id));CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);')
    db.executemany('INSERT OR REPLACE INTO edges VALUES (?,?,?)',[(j['from_id'],j['to_id'],j['key']) for j in jobs]);db.commit()
    existing={row[0] for row in db.execute('select provider_key from metrics')};calls=0;errors=[]
    if args.live:
        settings=Settings()
        if not settings.driving_live_calls_enabled or not settings.amap_web_service_key:raise ValueError('Live driving is disabled')
        for job in jobs:
            if job['key'] in existing:continue
            assert content_hash(DrivingRoad.reference_request(job['origin'],job['destination']))==job['key']
            road=DrivingRoad(settings,2)
            try:
                item=road.segment(job['vehicle'],job['origin'],job['destination'],job['departure'])
                provider=road.reference_item(DrivingRoad.reference_request(job['origin'],job['destination']),item)
                assert DrivingRoad.valid_reference(provider)
                db.execute('INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?)',(job['key'],job['from_id'],job['to_id'],provider['meters'],provider['seconds'],provider['received_at'],provider['evidence_hash'],gzip.compress(encode(provider).encode(),compresslevel=3)))
                db.commit();existing.add(job['key'])
            except Exception as exc:
                errors.append(getattr(exc,'code',type(exc).__name__))
                break
            finally:calls+=road.calls;road.close()
    complete=len(existing)==len(jobs)
    summary={'scope':'CONFIRMED_WAREHOUSE_CORRECTION_ROADS_ONLY','input_hash':description['corrected_input_hash'],
        'status':'COMPLETED' if complete else 'INCOMPLETE' if args.live else 'PREPARED_NO_NETWORK','expected_edges':len(jobs),'known_edges':len(existing),
        'http_attempts':calls,'errors':errors,'source_business_database_written':False,'collected_at':int(time.time())}
    db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in summary.items()]);db.commit();db.close()
    private_json(path.with_suffix('.json'),summary);print(json.dumps(summary,ensure_ascii=False))
    if args.live and not complete:raise SystemExit(1)


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--jobs',required=True);p.add_argument('--output',required=True);p.add_argument('--live',action='store_true');main(p.parse_args())
