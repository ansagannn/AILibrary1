import csv
import io
import sqlite3
import pytest
from fastapi.testclient import TestClient
from app import main, auth

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB', tmp_path / 'test.db')
    main.init()
    main.login_attempts.clear()
    with main.conn() as db: auth.create_user(db,'testadmin','correct horse battery staple','admin')
    with TestClient(main.app) as c:
        token=c.post('/api/login',json={'username':'testadmin','password':'correct horse battery staple'}).json()['token']
        c.headers['Authorization']='Bearer '+token
        yield c

def test_health_and_seed(client, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    body = client.get('/api/health').json()
    assert body['catalog_items'] == 10
    assert body['llm_enabled'] is False
    main.init()
    assert client.get('/api/stats').json()['total'] == 10

def test_empty_catalog_not_reseeded(client):
    with sqlite3.connect(main.DB) as c: c.execute('DELETE FROM items')
    main.init()
    assert client.get('/api/items').json()['total'] == 0
    exported=client.get('/api/export').content.decode('utf-8-sig')
    assert next(csv.reader(io.StringIO(exported))) == main.CSV_FIELDS

def test_catalog_pagination(client):
    with sqlite3.connect(main.DB) as c:
        c.executemany('INSERT INTO items(title) VALUES (?)', [(f'Книга {i}',) for i in range(103)])
    rows=[]
    for offset in (0, 50, 100):
        result=client.get('/api/items', params={'limit':50,'offset':offset}).json()
        assert result['total'] == 113
        rows.extend(result['results'])
    assert len(rows) == len({x['id'] for x in rows}) == 113
    assert not result['has_more']
    assert client.get('/api/items?limit=101').status_code == 422

def test_catalog_search_unicode_and_escaped_wildcards(client):
    r=client.post('/api/items',json={'title':'ӘҒҚҢӨҰҮҺІ','language':'Kazakh','kind':'book','year':'2024'})
    assert r.status_code == 200
    d=client.get('/api/items',params={'q':'әғқңөұүһі'}).json()
    assert len(d['results']) == 1 and d['results'][0]['title'] == 'ӘҒҚҢӨҰҮҺІ'
    assert client.get('/api/items',params={'q':'%'}).json()['total'] == 0
    assert client.get('/api/items',params={'q':"' OR 1=1 --"}).json()['total'] == 0

def test_filters_for_list_and_search(client):
    params={'material_language':'Kazakh','kind':'textbook','year':'2022'}
    d=client.get('/api/items',params=params).json()
    assert d['total'] == 1 and d['results'][0]['title'] == 'Ақпараттық-коммуникациялық технологиялар'
    d=client.post('/api/search',json={'query':'ақпараттық','language':'kk',**params}).json()
    assert d['count'] == 1 and d['results'][0]['language'] == 'Kazakh'
    assert '1' in d['answer']
    d=client.post('/api/search',json={'query':'интеллект','material_language':'Kazakh'}).json()
    assert all(r['language']=='Kazakh' for r in d['results'])
    assert client.get('/api/items?year=22').status_code == 400
    assert client.post('/api/search',json={'query':'ab','year':'22'}).status_code == 400
    assert client.post('/api/search',json={'query':'а'}).status_code == 400

def test_csv_roundtrip_kazakh(client):
    text='title,title_kk,authors,kind,language,year\n"Ә ғ қ ң ө ұ ү һ і","Қазақ тілі","Т. Әуезов",textbook,Kazakh,2023\n'
    r=client.post('/api/import',files={'file':('catalog.csv',text.encode('utf-8'),'text/csv')})
    assert r.status_code == 200 and r.json()['imported']==1
    rows=list(csv.DictReader(io.StringIO(client.get('/api/export').content.decode('utf-8-sig'))))
    row=next(x for x in rows if x['title']=='Ә ғ қ ң ө ұ ү һ і')
    assert row['authors']=='Т. Әуезов' and row['title_kk']=='Қазақ тілі'
    assert client.get('/api/items',params={'q':'ә ғ қ ң ө ұ ү һ і'}).json()['total']==1

def test_import_validation_is_atomic(client):
    before=client.get('/api/stats').json()['total']
    raw='title,authors\nValid,Author\n,No title\n'.encode()
    r=client.post('/api/import',files={'file':('bad.csv',raw,'text/csv')})
    assert r.status_code == 400 and r.json()['detail']['error_count']==1
    assert client.get('/api/stats').json()['total']==before
    for name, content in [('bad.csv',b'author\nA'),('bad.csv',b'title,title\nX,Y'),('bad.csv',b'title\n\xff'),('bad.txt',b'title\nA')]:
        assert client.post('/api/import',files={'file':(name,content)}).status_code == 400
    assert client.get('/api/stats').json()['total']==before

def test_data_persists_after_reinitialization(client):
    assert client.post('/api/items',json={'title':'Тұрақты дерек','year':'2026'}).status_code==200
    main.init()
    assert client.get('/api/items',params={'q':'тұрақты'}).json()['total']==1


def test_security_roles_audit_and_edit(client):
    assert client.get('/api/audit').status_code==200
    r=client.put('/api/items/1',json={'isbn':'978-1-234','availability':'on_request'})
    assert r.status_code==200 and client.get('/api/items/1').json()['isbn']=='978-1-234'
    assert client.put('/api/items/1',json={'availability':'invented'}).status_code==400
    assert client.put('/api/items/1',json={'id':'5'}).status_code==400
    assert any(x['action']=='update_item' for x in client.get('/api/audit').json()['results'])
    with main.conn() as c: auth.create_user(c,'staffuser','a strong password 1234','librarian')
    staff=client.post('/api/login',json={'username':'staffuser','password':'a strong password 1234'}).json()['token']
    assert client.get('/api/audit',headers={'Authorization':'Bearer '+staff}).status_code==403
    assert client.post('/api/items',headers={'Authorization':'Bearer '+staff},json={'title':'New'}).status_code==200
    assert client.post('/api/logout',headers={'Authorization':'Bearer '+staff}).status_code==200
    assert client.post('/api/items',headers={'Authorization':'Bearer '+staff},json={'title':'New'}).status_code==401
    assert client.post('/api/import',headers={'Authorization':''},files={'file':('a.csv',b'title\na')}).status_code in (401,403)
    assert client.get('/api/items').status_code==200

def test_metadata_import_export(client):
    data='title,isbn,udc,bbk_candidate,availability,location\nКнига,978-1,37.01,74.2,on_request,Зал\n'
    assert client.post('/api/import',files={'file':('new.csv',data.encode())}).status_code==200
    row=client.get('/api/items',params={'q':'Книга'}).json()['results'][0]
    assert (row['isbn'],row['udc'],row['bbk_candidate'],row['availability'],row['location'])==('978-1','37.01','74.2','on_request','Зал')

def test_long_search_and_metadata_validation(client):
    assert client.post('/api/search',json={'query':'a'*257}).status_code==400
    assert client.post('/api/items',json={'title':'test','availability':'invented'}).status_code==400
    bad=b'title,availability\nExample,invented\n'
    before=client.get('/api/stats').json()['total']
    assert client.post('/api/import',files={'file':('a.csv',bad)}).status_code==400
    assert client.get('/api/stats').json()['total']==before

def test_pdf_attachment_access_validation_and_replacement(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'PDF_DIR', tmp_path / 'pdfs')
    assert client.get('/api/items/1').json()['pdf_available'] is False
    assert client.get('/api/items/1/pdf').status_code == 404
    assert client.post('/api/items/1/pdf',headers={'Authorization':''},files={'file':('a.pdf',b'%PDF-1.4\nfirst')}).status_code in (401,403)
    assert client.post('/api/items/999999/pdf',files={'file':('a.pdf',b'%PDF-1.4\nfirst')}).status_code == 404
    assert client.post('/api/items/1/pdf',files={'file':('a.pdf',b'not a PDF')}).status_code == 400
    assert client.post('/api/items/1/pdf',files={'file':('a.txt',b'%PDF-1.4\nfirst')}).status_code == 400
    assert client.post('/api/items/1/pdf',files={'file':('a.pdf',b'%PDF-1.4\nfirst')}).status_code == 200
    assert client.get('/api/items/1').json()['pdf_available'] is True
    assert next(x for x in client.get('/api/items').json()['results'] if x['id']==1)['pdf_available'] is True
    r=client.get('/api/items/1/pdf',headers={'Authorization':''})
    assert r.status_code==200 and r.content==b'%PDF-1.4\nfirst' and r.headers['content-type']=='application/pdf'
    assert client.post('/api/items/1/pdf',files={'file':('bad.pdf',b'wrong')}).status_code == 400
    assert client.get('/api/items/1/pdf').content==b'%PDF-1.4\nfirst'
    assert client.post('/api/items/1/pdf',files={'file':('new.pdf',b'%PDF-1.7\nsecond')}).status_code == 200
    assert client.get('/api/items/1/pdf').content==b'%PDF-1.7\nsecond'
    assert any(x['action']=='upload_pdf' for x in client.get('/api/audit').json()['results'])


def test_seed_installs_once_and_never_overwrites(tmp_path):
    import gzip
    seed_db=tmp_path/'seed.sqlite'; dest=tmp_path/'data'/'qor.db'; seed=tmp_path/'seed.sqlite.gz'
    with sqlite3.connect(seed_db) as c:
        c.execute('CREATE TABLE example (value TEXT)')
        c.execute("INSERT INTO example VALUES ('catalog')")
    with open(seed_db,'rb') as source, gzip.open(seed,'wb') as target: target.write(source.read())
    assert main.seed_default_db(dest,seed) is True
    with sqlite3.connect(dest) as c:
        assert c.execute('SELECT value FROM example').fetchone()[0]=='catalog'
        c.execute("UPDATE example SET value='edited'")
    assert main.seed_default_db(dest,seed) is False
    with sqlite3.connect(dest) as c: assert c.execute('SELECT value FROM example').fetchone()[0]=='edited'
    bad=tmp_path/'corrupt.gz'; bad.write_bytes(b'not gzip');bad_dest=tmp_path/'bad.db'
    with pytest.raises((OSError, EOFError)): main.seed_default_db(bad_dest,bad)
    assert not bad_dest.exists()
