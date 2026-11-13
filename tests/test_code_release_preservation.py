import sqlite3
from scripts.verify_unchanged_databases import difference,inventory


def test_typed_primary_key_inventory_preserves_blob_null_and_composite_key(tmp_path):
    source=tmp_path/'source.db';backup=tmp_path/'backup.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE sample(a TEXT,b INTEGER,payload BLOB,value TEXT,PRIMARY KEY(a,b))')
        db.executemany('INSERT INTO sample VALUES(?,?,?,?)',[('x',1,b'\x00\xff',None),('x',2,b'', '')]);db.commit()
        with sqlite3.connect(backup) as copy:db.backup(copy)
    before=inventory(backup);after=inventory(source)
    assert before==after and before['sample']['count']==2
    assert not any(difference(before,after)['sample'][k] for k in ('added','changed','deleted'))
    with sqlite3.connect(source) as db:db.execute('UPDATE sample SET value=? WHERE b=1',('',));db.commit()
    assert difference(before,inventory(source))['sample']['changed']==1
    with sqlite3.connect(source) as db:db.execute('DELETE FROM sample WHERE b=2');db.execute('INSERT INTO sample VALUES(?,?,?,?)',('y',3,b'',None));db.commit()
    changes=difference(before,inventory(source))['sample']
    assert (changes['added'],changes['deleted'],changes['changed'])==(1,1,1)
