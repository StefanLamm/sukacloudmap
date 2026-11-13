"""Online SQLite backup with integrity verification. Existing backup paths are refused."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from sqlalchemy.engine import make_url


def backup(source, destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if not source.is_file():raise ValueError('源数据库不存在')
    if destination.exists() or source==destination:raise ValueError('备份目标已存在，请使用新文件名')
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation and private permissions before any database bytes are copied.
    descriptor=__import__('os').open(destination,__import__('os').O_CREAT|__import__('os').O_EXCL|__import__('os').O_WRONLY,0o600)
    __import__('os').close(descriptor)
    # SQLite's transaction context does not close connections. Explicitly close
    # and consolidate the backup before handing off its filename and checksum.
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        dst.commit()
        if dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0:raise ValueError('备份WAL合并未完成')
        if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()!='delete':raise ValueError('备份未转换为独立数据库')
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or dst.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('备份完整性校验失败')
        version=dst.execute('SELECT version_num FROM alembic_version').fetchone()[0]
    if any(Path(str(destination)+suffix).exists() for suffix in ('-wal','-shm')):raise ValueError('备份仍依赖WAL辅助文件')
    with destination.open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'source':str(source),'backup':str(destination),'schema':version,'sha256':checksum,'integrity':'ok'}


def main():
    p=argparse.ArgumentParser();p.add_argument('destination');p.add_argument('--source');args=p.parse_args()
    url=make_url(Settings().database_url.get_secret_value())
    if not args.source and url.get_backend_name()!='sqlite':raise ValueError('此工具仅处理SQLite，PostgreSQL请使用pg_dump')
    print(json.dumps(backup(args.source or url.database,args.destination),ensure_ascii=False))


if __name__=='__main__':main()
