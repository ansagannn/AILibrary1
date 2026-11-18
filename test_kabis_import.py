import hashlib
import json
import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from app import kabis_import as ki
from app import main as appmain

ID1='01234567-89AB-CDEF-0123-456789ABCDEF'
ID2='11234567-89AB-CDEF-0123-456789ABCDEF'

def record(guid=ID1,title='Әлемдегі тау-кен ісі',extra=None):
    fields=[('001','{guid {'+guid+'}}'),('200','^A'+title),('101','^Aказ'),('210','^CБаспасы^D2022'),('700','^AӘуезов М.'),('610','^Aтау-кен')]
    fields += extra or []
    directory=b''; values=b''
    for tag,value in fields:
        data=value.encode('ptcp154')+b'\x1e'
        directory += (tag+f'{len(data):04d}{len(values):05d}').encode('ascii')
        values += data
    base=24+len(directory)+1
    length=base+len(values)+1
    leader=bytearray(b'00000nam0 2200000 i 450 ')
    leader[:5]=f'{length:05d}'.encode();leader[12:17]=f'{base:05d}'.encode()
    return bytes(leader)+directory+b'\x1e'+values+b'\x1d'

@pytest.fixture
def env(tmp_path,monkeypatch):
    db=tmp_path/'qor.db';monkeypatch.setattr(appmain,'DB',db);appmain.init()
    with sqlite3.connect(db) as c: c.execute('DELETE FROM items')
    return tmp_path,db

def call(p, db=None, *flags):
    args=[str(p/'export.mrc'),'--report',str(p/f'report{len(list(p.glob("report*")))}.json')]
    if db: args+=['--db',str(db)]
    return ki.main(args+list(flags))

def test_strict_decoding_and_mapping(tmp_path):
    raw=record();(tmp_path/'export.mrc').write_bytes(raw)
    valid,r=ki.inspect(tmp_path/'export.mrc')
    assert r['records']==1 and r['rejected']==0
    assert valid[0][0]==ID1.lower() and valid[0][3]==raw
    assert valid[0][4]['title']=='Әлемдегі тау-кен ісі'
    assert valid[0][4]['authors']=='Әуезов М.'
    assert valid[0][4]['language']=='Kazakh' and valid[0][4]['year']=='2022'

def test_dry_run_and_commit_backup_idempotence_and_source_api(env):
    p,db=env;(p/'export.mrc').write_bytes(record())
    assert call(p,db)==0
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0
    assert call(p,db,'--apply','--backup',str(p/'backup.db'))==0
    with sqlite3.connect(p/'backup.db') as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0
    with sqlite3.connect(db) as c:
        item=c.execute('SELECT id,title FROM items').fetchone()
        sid, original, sha, raw=c.execute('SELECT source_id, source_id_raw, raw_sha256, raw_record FROM kabis_sources').fetchone()
        assert original=='{guid {'+ID1+'}}' and sid==ID1.lower() and raw==record()
        assert hashlib.sha256(raw).hexdigest()==sha
    with TestClient(appmain.app) as c:
        source=c.get(f'/api/items/{item[0]}/source').json()
        assert source['source_id']==sid and source['source_id_raw']==original
        assert c.get('/api/items/999/source').status_code==404
    assert call(p,db,'--apply','--backup',str(p/'backup2.db'))==0
    assert json.loads((p/'report2.json').read_text())['comparison']['unchanged']==1
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==1

def test_rejects_require_opt_in(env):
    p,db=env;(p/'export.mrc').write_bytes(record()+record(ID2,''))
    assert call(p,db,'--apply','--backup',str(p/'before.db'))==2
    assert not (p/'before.db').exists()
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0
    assert call(p,db,'--apply','--allow-rejects','--backup',str(p/'before.db'))==0
    report=json.loads((p/'report1.json').read_text())
    assert report['status']=='committed' and report['inserted']==1 and report['rejected']==1

def test_duplicate_source_guid_and_broken_data_block_import(env):
    p,db=env;(p/'export.mrc').write_bytes(record()+record(title='Другое'))
    assert call(p,db,'--apply','--backup',str(p/'before.db'))==1
    (p/'export.mrc').write_bytes(record()[:-4])
    assert call(p,db,'--apply','--backup',str(p/'before.db'))==1
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0

def test_existing_changed_record_never_overwritten(env):
    p,db=env;(p/'export.mrc').write_bytes(record())
    assert call(p,db,'--apply','--backup',str(p/'b1.db'))==0
    (p/'export.mrc').write_bytes(record(title='Изменённая запись'))
    assert call(p,db,'--apply','--backup',str(p/'b2.db'))==0
    report=json.loads((p/'report1.json').read_text())
    assert report['comparison']['changed_skipped']==1 and report['inserted']==0
    with sqlite3.connect(db) as c: assert c.execute('SELECT title FROM items').fetchone()[0]=='Әлемдегі тау-кен ісі'

def test_exception_rolls_back_entire_transaction(env):
    p,db=env;(p/'export.mrc').write_bytes(record()+record(ID2,'Ошибка'))
    with sqlite3.connect(db) as c:
        c.execute("CREATE TRIGGER fail_item BEFORE INSERT ON items WHEN NEW.title='Ошибка' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    assert call(p,db,'--apply','--backup',str(p/'b.db'))==1
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='kabis_sources'").fetchone()[0]==0
    assert json.loads((p/'report0.json').read_text())['status']=='prepared_not_committed'

def test_backup_and_report_existing_never_overwritten(env):
    p,db=env;(p/'export.mrc').write_bytes(record());(p/'backup.db').write_bytes(b'old')
    assert call(p,db,'--apply','--backup',str(p/'backup.db'))==1
    assert (p/'backup.db').read_bytes()==b'old'
    (p/'report-fixed.json').write_text('old')
    assert ki.main([str(p/'export.mrc'),'--report',str(p/'report-fixed.json')])==1
    assert (p/'report-fixed.json').read_text()=='old'
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0

def test_rejected_fields_are_available_for_manual_review(tmp_path):
    raw=record(title='',extra=[('461','^CСвязанное название')])
    f=tmp_path/'export.mrc';f.write_bytes(raw)
    valid,report=ki.inspect(f)
    assert not valid and report['rejected']==1
    assert report['rejected_records'][0]['review_fields']['461']==['^CСвязанное название']

def test_report_failure_after_commit_is_not_reported_as_rollback(env, monkeypatch, capsys):
    p,db=env;(p/'export.mrc').write_bytes(record())
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(ki,'finalize_report',fail)
    assert call(p,db,'--apply','--backup',str(p/'b.db'))==0
    assert 'данные импортированы' in capsys.readouterr().err
    with sqlite3.connect(db) as c: assert c.execute('SELECT COUNT(*) FROM items').fetchone()[0]==1
    assert json.loads((p/'report0.json').read_text())['status']=='prepared_not_committed'

def test_backfill_never_overwrites_existing_metadata(env):
    from app import kabis_backfill as backfill
    p,db=env
    original=record(extra=[('010','^A978-1-234'),('675','^A37.01'),('621','^A74.2')])
    (p/'export.mrc').write_bytes(original)
    assert call(p,db,'--apply','--backup',str(p/'initial.db'))==0
    with sqlite3.connect(db) as c: c.execute("UPDATE items SET isbn='',udc='Ручное значение',bbk_candidate=''")
    assert backfill.main(['--db',str(db),'--report',str(p/'backfill-dry.json')])==0
    with sqlite3.connect(db) as c: assert c.execute('SELECT isbn FROM items').fetchone()[0]==''
    assert backfill.main(['--db',str(db),'--report',str(p/'backfill.json'),'--apply','--backup',str(p/'bb.db')])==0
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT isbn,udc,bbk_candidate FROM items').fetchone()==('978-1-234','Ручное значение','74.2')
