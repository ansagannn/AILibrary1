import json,os
import pytest
from app import librarian, search_pg

def candidate(i,title):
 return {'id':i,'title':title,'authors':'Known author','year':'2020','kind':'book','language':'Kazakh','availability':'','keywords':'тау-кен','description':''}

class Response:
 def __init__(self,obj): self.obj=obj
 def raise_for_status(self): pass
 def json(self): return {'choices':[{'message':{'content':json.dumps(self.obj)}}]}

def test_model_cannot_return_fabricated_id_or_bibliographic_prose(monkeypatch):
 import httpx
 for name,val in [('QOR_LLM_BASE_URL','https://example.com/v1'),('QOR_LLM_API_KEY','testing'),('QOR_LLM_MODEL','example')]:monkeypatch.setenv(name,val)
 monkeypatch.setattr(httpx,'post',lambda *a,**kw:Response({'selected_ids':[999,1,1,'2',True], 'clarify':False, 'answer':'FAKE BOOK 999999'}))
 selected,clarify=librarian.select_sources('тау-кен',[candidate(1,'Реальная книга'),candidate(2,'Вторая')])
 assert [x['id'] for x in selected]==[1]
 reply=librarian.make_reply(selected,clarify,'ru')
 assert reply['sources'][0]['title']=='Реальная книга' and 'FAKE' not in str(reply)
 assert reply['sources'][0]['url']=='/item.html?id=1'

def test_invalid_model_response_and_missing_key(monkeypatch):
 import httpx
 monkeypatch.delenv('QOR_LLM_API_KEY',raising=False)
 with pytest.raises(librarian.ModelUnavailable): librarian.select_sources('q',[candidate(1,'a')])
 for name,val in [('QOR_LLM_BASE_URL','https://example.com/v1'),('QOR_LLM_API_KEY','testing'),('QOR_LLM_MODEL','example')]:monkeypatch.setenv(name,val)
 monkeypatch.setattr(httpx,'post',lambda *a,**kw:Response({'selected_ids':[1],'clarify':'false'}))
 with pytest.raises(librarian.ModelInvalid):librarian.select_sources('q',[candidate(1,'a')])

def test_vector_shape_and_finite():
 assert search_pg.vec([1.0]*384).startswith('[')
 with pytest.raises(ValueError):search_pg.vec([1.0])
 with pytest.raises(ValueError):search_pg.vec([float('nan')]*384)

@pytest.mark.skipif(not os.getenv('QOR_TEST_PG_DSN'),reason='requires disposable PostgreSQL with pgvector and pg_trgm')
def test_real_postgres_index_stale_update_and_isolation(tmp_path,monkeypatch):
 import sqlite3
 from app import main
 monkeypatch.setattr(main,'DB',tmp_path/'test.db'); main.init()
 monkeypatch.setenv('QOR_PG_DSN',os.environ['QOR_TEST_PG_DSN'])
 def fake_embed(texts,kind):
  return [[1.0]+[0.0]*383 for _ in texts]
 monkeypatch.setattr(search_pg,'embed',fake_embed)
 catalog=None
 with main.conn() as sq, search_pg.pg_connect() as pg:
  search_pg.ensure_schema(pg)
  result=search_pg.sync(sq,pg,batch_size=8); catalog=result['catalog_id']
  assert result['updated']==10
  result=search_pg.retrieve(sq,pg,'жасанды интеллект',limit=5)
  assert result['indexed']==10 and len(result['results'])==5
  assert search_pg.sync(sq,pg,batch_size=8)['updated']==0
  original=sq.execute('SELECT title FROM items WHERE id=1').fetchone()[0]
  sq.execute('UPDATE items SET title=? WHERE id=1',('Изменённое название',));sq.commit()
  result=search_pg.retrieve(sq,pg,'жасанды интеллект',limit=20)
  assert all(x['id']!=1 for x in result['results'])
  assert search_pg.sync(sq,pg,batch_size=8)['updated']==1
  result=search_pg.retrieve(sq,pg,'жасанды интеллект',limit=20)
  assert next(x for x in result['results'] if x['id']==1)['title']=='Изменённое название'
  with pg.cursor() as c:c.execute('DELETE FROM qor_search_docs WHERE catalog_id=%s',(catalog,))
  pg.commit()

def test_model_cannot_select_an_unsubmitted_candidate(monkeypatch):
 import httpx
 monkeypatch.setenv('QOR_LLM_BACKEND','api')
 for name,val in [('QOR_LLM_BASE_URL','https://example.com/v1'),('QOR_LLM_API_KEY','testing'),('QOR_LLM_MODEL','example')]:monkeypatch.setenv(name,val)
 monkeypatch.setattr(httpx,'post',lambda *a,**kw:Response({'selected_ids':[7,1],'clarify':False}))
 selected,_=librarian.select_sources('q',[candidate(i,str(i)) for i in range(1,8)])
 assert [x['id'] for x in selected]==[1]

def test_assistant_drops_record_edited_during_llm(tmp_path,monkeypatch):
 from contextlib import nullcontext
 from fastapi.testclient import TestClient
 from app import main
 monkeypatch.setattr(main,'DB',tmp_path/'db.sqlite');main.init()
 with main.conn() as c: record=dict(c.execute('SELECT * FROM items WHERE id=1').fetchone())
 monkeypatch.setattr(search_pg,'pg_connect',lambda:nullcontext(None))
 monkeypatch.setattr(search_pg,'retrieve',lambda *a,**kw:{'results':[record], 'indexed':10,'catalog_total':10,'stale_candidates_skipped':0})
 def select(question,candidates,language):
  with main.conn() as c:c.execute("UPDATE items SET title='Edited later' WHERE id=1")
  return [record],False
 monkeypatch.setattr(librarian,'select_sources',select)
 with TestClient(main.app) as c:
  d=c.post('/api/assistant',json={'question':'искусственный интеллект'}).json()
 assert d['sources']==[] and 'не удалось' in d['answer'].lower()

def test_glossary_terms_are_explicit():
 lex,sem,terms=search_pg.expand_query('книги по горному делу')
 assert 'тау-кен ісі' in terms and 'тау-кен ісі' in lex and 'mining' in sem
 assert search_pg.expand_query('химия')[2]==[]
