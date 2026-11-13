"""A handed-off backup must contain committed WAL pages without relying on GC."""
from contextlib import closing
import gc
import hashlib
import sqlite3

from scripts.backup_sqlite import backup


def test_backup_is_closed_standalone_and_contains_wal_only_commit(tmp_path):
    source=tmp_path/'source.db';destination=tmp_path/'backup.db'
    with closing(sqlite3.connect(source)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.executescript('CREATE TABLE alembic_version(version_num TEXT);'
                            "INSERT INTO alembic_version VALUES ('0006_group_workflows');"
                            'CREATE TABLE items(id INTEGER PRIMARY KEY,value TEXT);')
        writer.execute('INSERT INTO items VALUES (?,?)',(1,'committed in WAL'))
        writer.commit()
        before=source.read_bytes();wal_before=(tmp_path/'source.db-wal').read_bytes()
        gc_enabled=gc.isenabled();gc.disable()
        try:
            proof=backup(source,destination)
            assert not any((tmp_path/('backup.db'+suffix)).exists() for suffix in ('-wal','-shm'))
            assert source.read_bytes()==before and (tmp_path/'source.db-wal').read_bytes()==wal_before
            assert proof['sha256']==hashlib.sha256(destination.read_bytes()).hexdigest()
            assert destination.stat().st_mode&0o777==0o600
            with closing(sqlite3.connect(destination.as_uri()+'?mode=ro&immutable=1',uri=True)) as restored:
                assert restored.execute('SELECT value FROM items WHERE id=1').fetchone()==('committed in WAL',)
                assert restored.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
            writer.execute('UPDATE items SET value=? WHERE id=1',('later source update',));writer.commit()
            with closing(sqlite3.connect(destination.as_uri()+'?mode=ro&immutable=1',uri=True)) as restored:
                assert restored.execute('SELECT value FROM items WHERE id=1').fetchone()==('committed in WAL',)
        finally:
            if gc_enabled:gc.enable()
