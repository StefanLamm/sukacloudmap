"""Read-only, typed per-primary-key verification for code-only releases."""
from contextlib import closing
import argparse,hashlib,json,os,sqlite3,sys,time
from pathlib import Path


def quoted(value):return '"'+value.replace('"','""')+'"'
def typed(value):return ['bytes',value.hex()] if isinstance(value,bytes) else [type(value).__name__,value]
def encoded(values):return json.dumps([typed(v) for v in values],ensure_ascii=False,separators=(',',':'),allow_nan=False)


def inventory(path):
    path=Path(path).resolve()
    if not path.is_file():raise ValueError('Missing database')
    tables={}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            schema=db.execute('PRAGMA table_info('+quoted(name)+')').fetchall()
            columns=[v[1] for v in schema];pk=[v[1] for v in sorted(schema,key=lambda v:v[5]) if v[5]]
            if not pk:raise ValueError('A business table has no primary key: '+name)
            positions=[columns.index(v) for v in pk];rows={}
            for row in db.execute('SELECT * FROM '+quoted(name)):
                key=encoded([row[p] for p in positions])
                if key in rows:raise ValueError('Duplicate primary key')
                rows[key]=hashlib.sha256(encoded(row).encode()).hexdigest()
            tables[name]={'columns':columns,'primary_key':pk,'rows':rows,'count':len(rows)}
        if db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Foreign-key check failed')
    return tables


def difference(before,after):
    if set(before)!=set(after):raise ValueError('Database table set changed')
    changes={}
    for name,old in before.items():
        new=after[name]
        if old['columns']!=new['columns'] or old['primary_key']!=new['primary_key']:raise ValueError('Business table columns changed')
        a,b=old['rows'],new['rows']
        changes[name]={'added':len(b.keys()-a.keys()),'deleted':len(a.keys()-b.keys()),
                       'changed':sum(a[k]!=b[k] for k in a.keys()&b.keys()),'before':old['count'],'after':new['count']}
    return changes


def write_private(path,value):
    with Path(path).open('x') as stream:
        os.fchmod(stream.fileno(),0o600);json.dump(value,stream,ensure_ascii=False,indent=2)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['capture','compare']);p.add_argument('--sources',required=True,type=Path)
    p.add_argument('--backup',required=True,type=Path);p.add_argument('--output',required=True)
    args=p.parse_args()
    if sys.flags.optimize:raise ValueError('Verification requires non-optimized Python')
    folder=args.backup.resolve()
    if not folder.is_dir() or folder.stat().st_mode&0o077:raise ValueError('Private backup folder required')
    leaf=Path(args.output)
    if leaf.is_absolute() or len(leaf.parts)!=1 or leaf.suffix!='.json':raise ValueError('Output must be a single JSON filename')
    sources={name:str(Path(value).resolve()) for name,value in json.loads(args.sources.read_text()).items()}
    if set(sources)!={'main','trial','history','driving'} or len(set(sources.values()))!=4:raise ValueError('Exactly four separate source databases required')
    binding=hashlib.sha256(args.sources.read_bytes()).hexdigest()
    if args.mode=='capture':
        databases={name:inventory(folder/(name+'.db')) for name in sources}
        report={'status':'CAPTURED','sources':sources,'sources_file_sha256':binding,'captured_at':int(time.time()),'databases':databases}
    else:
        before=json.loads((folder/'prior-rows.json').read_text())
        if before['sources']!=sources or before['sources_file_sha256']!=binding:raise ValueError('Source bindings changed')
        changes={name:difference(before['databases'][name],inventory(path)) for name,path in sources.items()}
        valid=all(not any(row[k] for k in ('added','deleted','changed')) for tables in changes.values() for row in tables.values())
        report={'status':'PASS' if valid else 'FAILED','all_business_rows_identical':valid,'checked_at':int(time.time()),'databases':changes}
    write_private(folder/leaf,report)
    print(json.dumps({'status':report['status'],'tables':sum(len(v) for v in report['databases'].values())}))
    if report['status']=='FAILED':raise ValueError('Business rows changed; keep current data and investigate without rollback')

if __name__=='__main__':main()
