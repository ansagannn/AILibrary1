from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Depends, Request
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app import auth
from collections import defaultdict, deque
from functools import lru_cache
import time
from pathlib import Path
from contextlib import contextmanager
import sqlite3, csv, io, os, re, json, difflib, uuid, gzip, shutil, tempfile

ROOT=Path(__file__).resolve().parent.parent
DB=Path(os.getenv('QOR_DB_PATH', ROOT/'data/qor.db'))
DB.parent.mkdir(parents=True,exist_ok=True)

def seed_default_db(destination, seed):
 """Install a compressed initial catalogue once without replacing an existing DB."""
 if destination.exists() or not seed.is_file(): return False
 destination.parent.mkdir(parents=True,exist_ok=True)
 fd, temp_name=tempfile.mkstemp(prefix='.qor-seed-', suffix='.tmp', dir=destination.parent)
 try:
  with os.fdopen(fd,'wb') as output, gzip.open(seed,'rb') as source:
   shutil.copyfileobj(source,output)
  try:
   # Same-directory hard link atomically avoids overwriting a concurrently created DB.
   os.link(temp_name,destination)
   return True
  except FileExistsError: return False
 finally:
  Path(temp_name).unlink(missing_ok=True)

# Only the unconfigured default database is seeded. Explicit QOR_DB_PATH values
# are never overwritten or implicitly populated.
if 'QOR_DB_PATH' not in os.environ:
 seed_default_db(DB, ROOT/'data'/'catalog-seed.sqlite.gz')
app=FastAPI(title='QOR — AI Librarian', version='1.0.0', description='Bilingual university library discovery MVP')

SAMPLE=[
('Introduction to Artificial Intelligence','Введение в искусственный интеллект','Жасанды интеллектке кіріспе','Stuart Russell, Peter Norvig','Pearson','2021','book','English','Artificial intelligence; machine learning; agents','Foundational textbook covering intelligent agents, search, reasoning and machine learning.'),
('Deep Learning','Глубокое обучение','Терең оқыту','Ian Goodfellow, Yoshua Bengio, Aaron Courville','MIT Press','2016','book','English','нейронные сети; deep learning; машиналық оқыту','Comprehensive reference on neural networks, optimization and deep learning.'),
('Database System Concepts','Основы систем баз данных','Деректер қоры жүйелерінің негіздері','Abraham Silberschatz, Henry Korth, S. Sudarshan','McGraw-Hill','2019','book','English','database; SQL; деректер қоры; мәліметтер базасы','Database architecture, relational model, SQL, transactions and storage.'),
('Ақпараттық-коммуникациялық технологиялар','Информационно-коммуникационные технологии','Ақпараттық-коммуникациялық технологиялар','Ұжымдық авторлар','Оқу құралы','2022','textbook','Kazakh','АКТ; ақпараттық технология; цифрлық сауаттылық','Ақпараттық жүйелер, компьютерлік желілер және цифрлық технологиялар туралы оқу құралы.'),
('Қазақстан тарихы','История Казахстана','Қазақстан тарихы','Ұжымдық авторлар','Университет баспасы','2020','textbook','Kazakh','тарих; Қазақстан; тарихнама','Қазақстан тарихының негізгі кезеңдері мен тарихи деректеріне шолу.'),
('Mining Engineering Handbook','Справочник горного инженера','Тау-кен инженері анықтамалығы','Howard L. Hartman','SME','1992','reference','English','mining; тау-кен ісі; шахта; кен өндіру','Reference covering mining methods, mine planning, ventilation and operations.'),
('Occupational Health and Safety','Охрана труда и безопасность','Еңбекті қорғау және қауіпсіздік','Charles D. Reese','CRC Press','2017','book','English','safety; еңбек қауіпсіздігі; охрана труда','Principles of workplace safety, hazard identification and risk control.'),
('Renewable Energy Systems','Системы возобновляемой энергетики','Жаңартылатын энергия жүйелері','М. Калдес','Academic Press','2020','book','English','энергетика; renewable energy; күн энергиясы; жел энергиясы','Overview of solar, wind and other renewable energy technologies.'),
('Қазақ тілінің грамматикасы','Грамматика казахского языка','Қазақ тілінің грамматикасы','С. Исаев','Мектеп','2018','textbook','Kazakh','қазақ тілі; грамматика; морфология; синтаксис','Қазақ тілінің морфологиялық және синтаксистік құрылымы.'),
('Research Methods for the Social Sciences','Методы исследования в социальных науках','Әлеуметтік ғылымдардағы зерттеу әдістері','John Creswell','SAGE','2018','book','English','research; methodology; зерттеу әдістемесі','Research design, qualitative and quantitative methods, and academic writing.')]

@contextmanager
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
 c.create_function('qor_casefold', 1, lambda value: (value or '').casefold(), deterministic=True)
 try:
  with c:
   yield c
 finally:
  c.close()

def init():
 with conn() as c:
  is_new = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone() is None
  c.execute('''CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,title_ru TEXT DEFAULT '',title_kk TEXT DEFAULT '',authors TEXT DEFAULT '',publisher TEXT DEFAULT '',year TEXT DEFAULT '',kind TEXT DEFAULT 'book',language TEXT DEFAULT '',keywords TEXT DEFAULT '',description TEXT DEFAULT '',url TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
  # Additive migration; never overwrite pre-existing catalog values.
  auth.init(c)
  c.execute('CREATE TABLE IF NOT EXISTS item_pdfs (item_id INTEGER PRIMARY KEY REFERENCES items(id), storage_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
  columns={r[1] for r in c.execute('PRAGMA table_info(items)')}
  for name in ('isbn','udc','bbk_candidate','availability','location'):
   if name not in columns: c.execute(f"ALTER TABLE items ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
  if is_new:
   c.executemany('INSERT INTO items(title,title_ru,title_kk,authors,publisher,year,kind,language,keywords,description) VALUES(?,?,?,?,?,?,?,?,?,?)',SAMPLE)
init()

class SearchIn(BaseModel):
 query:str
 language:str='auto'  # response language, not the material language
 material_language:str=''
 kind:str=''
 year:str=''
 limit:int=20

security=HTTPBearer(auto_error=False)
# Best-effort single-process throttle; production needs shared rate limiting.
login_attempts=defaultdict(deque)
class LoginIn(BaseModel):
 username:str
 password:str

def require_staff(credentials:HTTPAuthorizationCredentials=Depends(security)):
 with conn() as c:
  user=auth.lookup(c, credentials.credentials if credentials else '')
 if not user: raise HTTPException(401,'Необходима авторизация')
 return user

def require_admin(user=Depends(require_staff)):
 if user['role']!='admin': raise HTTPException(403,'Нужны права администратора')
 return user

@app.post('/api/login')
def login(data:LoginIn, request:Request):
 if len(data.username)>100 or len(data.password)>256: raise HTTPException(400,'Неверные параметры')
 key=request.client.host if request.client else 'unknown'; now=time.monotonic(); attempts=login_attempts[key]
 while attempts and attempts[0]<now-60: attempts.popleft()
 if len(attempts)>=10: raise HTTPException(429,'Слишком много попыток, повторите через минуту')
 attempts.append(now)
 with conn() as c:
  result=auth.login(c,data.username,data.password)
  auth.audit(c,data.username[:100] or '(empty)','login_success' if result else 'login_failed')
 if not result: raise HTTPException(401,'Неверные учётные данные')
 token,user=result
 return {'token':token,'user':user,'expires_in':28800}

@app.post('/api/logout')
def logout(user=Depends(require_staff), credentials:HTTPAuthorizationCredentials=Depends(security)):
 with conn() as c:
  auth.logout(c,credentials.credentials);auth.audit(c,user['username'],'logout')
 return {'status':'ok'}

@app.get('/api/me')
def me(user=Depends(require_staff)): return user

@app.get('/api/audit')
def read_audit(user=Depends(require_admin)):
 with conn() as c: rows=c.execute('SELECT id,created_at,actor,action,target,details FROM qor_audit ORDER BY id DESC LIMIT 100').fetchall()
 return {'results':[dict(r) for r in rows]}

def norm(s): return re.sub(r'[^\w\s-]',' ',(s or '').lower(),flags=re.UNICODE).strip()
def score_item(q,row,similarity=None):
 terms=norm(q).split(); fields=[row['title'],row['title_ru'],row['title_kk'],row['authors'],row['keywords'],row['description'],row['publisher']]
 text=' '.join(norm(x) for x in fields); score=0
 for t in terms:
  if t in text: score+=3
  else:
   compare = similarity or (lambda a,b: difflib.SequenceMatcher(None,a,b).ratio())
   best=max((compare(t,w) for w in set(text.split())),default=0)
   if best>.78: score+=best
 for f,w in [('title',5),('title_ru',4),('title_kk',4),('keywords',3)]:
  if norm(q) and norm(q) in norm(row[f]): score+=w
 return score

@app.get('/api/health')
def health():
 with conn() as c: n=c.execute('SELECT COUNT(*) FROM items').fetchone()[0]
 configured=(os.getenv('QOR_LLM_BACKEND','api').lower()=='local' or all(os.getenv(k) for k in ('QOR_LLM_BASE_URL','QOR_LLM_API_KEY','QOR_LLM_MODEL')))
 return {'status':'ok','service':'QOR AI Librarian','catalog_items':n,'llm_enabled':configured,'llm_status':'configured_not_verified' if configured else 'not_configured','hybrid_search_configured':bool(os.getenv('QOR_PG_DSN'))}

@app.get('/api/stats')
def stats():
 with conn() as c:
  total=c.execute('SELECT COUNT(*) FROM items').fetchone()[0]
  langs=[dict(r) for r in c.execute('SELECT language,COUNT(*) count FROM items GROUP BY language ORDER BY count DESC')]
  kinds=[dict(r) for r in c.execute('SELECT kind,COUNT(*) count FROM items GROUP BY kind ORDER BY count DESC')]
 return {'total':total,'languages':langs,'types':kinds}

def filters_sql(kind='', year='', material_language='', catalog_query=''):
 clauses, params = [], []
 if kind:
  clauses.append('kind = ?'); params.append(kind.strip())
 if year:
  year = year.strip()
  if not re.fullmatch(r'\d{4}', year):
   raise HTTPException(400, 'Год должен состоять из 4 цифр')
  clauses.append('year = ?'); params.append(year)
 if material_language and material_language != 'auto':
  clauses.append('language = ?'); params.append(material_language.strip())
 if catalog_query:
  # Escape SQL LIKE wildcards: input is literal, not a pattern.
  q = catalog_query.strip().casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
  columns = ['title', 'title_ru', 'title_kk', 'authors', 'publisher', 'keywords', 'description']
  clauses.append('(' + ' OR '.join(f"qor_casefold({col}) LIKE ? ESCAPE '\\'" for col in columns) + ')')
  params.extend(['%' + q + '%'] * len(columns))
 return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params

def add_pdf_flags(c, items):
 ids=[x['id'] for x in items]
 if ids:
  linked={r[0] for r in c.execute('SELECT item_id FROM item_pdfs WHERE item_id IN ('+','.join('?' for _ in ids)+')',ids)}
  for item in items: item['pdf_available']=item['id'] in linked
 return items

@app.get('/api/items')
def list_items(limit:int=Query(50, ge=1, le=100), offset:int=Query(0, ge=0),
               kind:str='', year:str='', material_language:str='', q:str=''):
 where, params = filters_sql(kind, year, material_language, q)
 with conn() as c:
  total = c.execute('SELECT COUNT(*) FROM items' + where, params).fetchone()[0]
  rows = c.execute('SELECT * FROM items' + where + ' ORDER BY id LIMIT ? OFFSET ?', params + [limit, offset]).fetchall()
  results=add_pdf_flags(c,[dict(row) for row in rows])
 return {'total': total, 'limit': limit, 'offset': offset, 'has_more': offset + len(rows) < total,
         'results': results}

@app.get('/api/items/{item_id}')
def get_item(item_id:int):
 with conn() as c:
  row=c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
  if row is None: raise HTTPException(404, 'Запись не найдена')
  return add_pdf_flags(c,[dict(row)])[0]

@app.get('/api/items/{item_id}/source')
def item_source(item_id:int):
 with conn() as c:
  if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kabis_sources'").fetchone():
   raise HTTPException(404, 'Источник КАБИС не найден')
  row=c.execute('SELECT source_id,source_id_raw,raw_sha256,imported_at FROM kabis_sources WHERE item_id=?', (item_id,)).fetchone()
  if row is None: raise HTTPException(404, 'Источник КАБИС не найден')
  return {'source_system':'kabis', **dict(row)}

@app.post('/api/search')
def search(data:SearchIn):
 q=data.query.strip()
 if len(q)<2: raise HTTPException(400,'Введите минимум 2 символа / Кемінде 2 таңба енгізіңіз')
 if len(q)>256: raise HTTPException(400,'Запрос слишком длинный: максимум 256 символов')
 where, params = filters_sql(data.kind, data.year, data.material_language)
 with conn() as c: rows=c.execute('SELECT * FROM items' + where, params).fetchall()
 found=[]
 @lru_cache(maxsize=100000)
 def similarity(a,b):
  # The longest possible match is bounded by the shorter word.
  if 2*min(len(a),len(b))/(len(a)+len(b)) <= .78: return 0.0
  return difflib.SequenceMatcher(None,a,b).ratio()
 for r in rows:
  d=dict(r); s=score_item(q,d,similarity)
  if s>0: d['score']=round(s,2); found.append(d)
 found.sort(key=lambda x:(-x['score'], -(int(x['year']) if str(x['year']).isdigit() else 0)))
 with conn() as c: results=add_pdf_flags(c,found[:max(1,min(data.limit,100))])
 return {'query':q,'count':len(found),'results':results, 'answer':answer_text(q,len(found),data.language)}

def answer_text(q,count,lang):
 if not count:
  return 'Сұрауыңыз бойынша каталогтан материал табылмады. Басқа кілт сөзбен іздеп көріңіз.' if lang=='kk' else 'По запросу ничего не найдено. Попробуйте другие ключевые слова или более общую формулировку.'
 if lang=='kk': return f'«{q}» тақырыбы бойынша каталогтан {count} сәйкес материал табылды. Алдымен атауы мен кілт сөздері сұрауыңызға жақын материалдарды қараңыз.'
 return f'По теме «{q}» найдено записей: {count}. Сначала показаны наиболее релевантные материалы каталога.'

class AssistantIn(BaseModel):
 question:str
 language:str='ru'

@app.post('/api/search/hybrid')
def hybrid_search(data:SearchIn):
 from app.search_pg import pg_connect, retrieve, SearchUnavailable
 q=data.query.strip()
 if not 2<=len(q)<=256: raise HTTPException(400,'Запрос должен содержать от 2 до 256 символов')
 if data.year and not re.fullmatch(r'\d{4}',data.year): raise HTTPException(400,'Год должен состоять из 4 цифр')
 try:
  with conn() as c, pg_connect() as pg:
   result=retrieve(c,pg,q,data.kind,data.material_language,data.year,data.limit)
 except Exception as e:
  # Avoid exposing DSNs, credentials, or internal database errors.
  raise HTTPException(503,'Гибридный поиск недоступен: проверьте PostgreSQL, pgvector и индекс') from e
 with conn() as c: add_pdf_flags(c,result['results'])
 result.update({'query':q,'count':len(result['results']),'count_type':'returned_candidates','answer':'Результаты гибридного поиска; индекс может быть неполным или устаревшим.'})
 return result

@app.post('/api/assistant')
def assistant(data:AssistantIn):
 from app.librarian import select_sources,make_reply,ModelUnavailable,ModelInvalid
 q=data.question.strip()
 if not 2<=len(q)<=256: raise HTTPException(400,'Запрос должен содержать от 2 до 256 символов')
 if data.language not in ('ru','kk'): raise HTTPException(400,'Доступны языки ru и kk')
 from app.search_pg import pg_connect,retrieve
 try:
  with conn() as c, pg_connect() as pg: result=retrieve(c,pg,q,limit=20)
 except Exception as e: raise HTTPException(503,'Поиск недоступен: проверьте PostgreSQL и индекс') from e
 if not result['results']:
  reply=make_reply([],True,data.language)
 else:
  try:
   selected,clarify=select_sources(q,result['results'],data.language)
   # Re-check after the model call: a librarian may have edited a record meanwhile.
   from app.search_pg import digest
   with conn() as c:
    verified=[]
    for source in selected:
     current=c.execute('SELECT * FROM items WHERE id=?',(source['id'],)).fetchone()
     if current is not None and digest(current)==digest(source): verified.append(dict(current))
   reply=make_reply(verified,clarify,data.language)
  except (ModelUnavailable,ModelInvalid) as e: raise HTTPException(503,str(e)) from e
 reply['index']={'indexed':result['indexed'],'catalog_total':result['catalog_total'],'stale_candidates_skipped':result['stale_candidates_skipped']}
 return reply

@app.post('/api/items')
def add_item(item:dict, user=Depends(require_staff)):
 if not isinstance(item.get('title'),str) or not item['title'].strip(): raise HTTPException(400,'title is required')
 fields=['title','title_ru','title_kk','authors','publisher','year','kind','language','keywords','description','url','isbn','udc','bbk_candidate','availability','location']
 if any(k not in fields for k in item) or any(not isinstance(v,str) or len(v)>10000 for v in item.values()): raise HTTPException(400,'Недопустимые значения полей')
 if item.get('availability','') not in ('','available','unavailable','on_request'): raise HTTPException(400,'Недопустимый статус наличия')
 vals=[item.get(k,'') for k in fields]
 with conn() as c:
  cur=c.execute('INSERT INTO items('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',vals)
  auth.audit(c,user['username'],'create_item',cur.lastrowid)
  return {'id':cur.lastrowid,'status':'created'}

@app.put('/api/items/{item_id}')
def update_item(item_id:int, item:dict, user=Depends(require_staff)):
 allowed=['title','title_ru','title_kk','authors','publisher','year','kind','language','keywords','description','url','isbn','udc','bbk_candidate','availability','location']
 if not item or any(k not in allowed for k in item): raise HTTPException(400,'Недопустимые поля')
 if any(not isinstance(v,str) or len(v)>10000 for v in item.values()): raise HTTPException(400,'Недопустимое значение поля')
 if 'title' in item and not item['title'].strip(): raise HTTPException(400,'Название обязательно')
 if 'availability' in item and item['availability'] not in ('','available','unavailable','on_request'): raise HTTPException(400,'Недопустимый статус наличия')
 with conn() as c:
  if not c.execute('SELECT 1 FROM items WHERE id=?',(item_id,)).fetchone(): raise HTTPException(404,'Запись не найдена')
  c.execute('UPDATE items SET '+','.join(k+'=?' for k in item)+' WHERE id=?', list(item.values())+[item_id])
  auth.audit(c,user['username'],'update_item',item_id,','.join(sorted(item)))
 return {'status':'updated','id':item_id}

CSV_FIELDS=['title','title_ru','title_kk','authors','publisher','year','kind','language','keywords','description','url','isbn','udc','bbk_candidate','availability','location']
MAX_CSV_BYTES=10 * 1024 * 1024

@app.post('/api/import')
async def import_csv(file:UploadFile=File(...), user=Depends(require_staff)):
 if not file.filename or not file.filename.lower().endswith('.csv'): raise HTTPException(400,'Upload a CSV file')
 raw=await file.read(MAX_CSV_BYTES+1)
 if len(raw)>MAX_CSV_BYTES: raise HTTPException(413,'CSV file exceeds 10 MB')
 try:
  text=raw.decode('utf-8-sig')
 except UnicodeDecodeError:
  raise HTTPException(400,'CSV должен быть в кодировке UTF-8')
 try:
  reader=csv.DictReader(io.StringIO(text, newline=''), strict=True)
  if not reader.fieldnames or 'title' not in reader.fieldnames:
   raise HTTPException(400,'CSV должен содержать столбец title')
  if len(set(reader.fieldnames)) != len(reader.fieldnames):
   raise HTTPException(400,'CSV содержит повторяющиеся заголовки')
  parsed=[]; errors=[]
  for row in reader:
   line=reader.line_num
   if None in row: errors.append(f'Строка {line}: лишние столбцы'); continue
   title=(row.get('title') or '').strip()
   if not title: errors.append(f'Строка {line}: не указано название'); continue
   vals=[(row.get(k) or '').strip() for k in CSV_FIELDS]
   if not vals[6]: vals[6]='book'
   if vals[14] not in ('','available','unavailable','on_request'):
    errors.append(f'Строка {line}: недопустимый статус наличия'); continue
   parsed.append(vals)
  if errors: raise HTTPException(400, {'message':'Импорт отменён: исправьте ошибки. Данные не изменены.', 'errors':errors[:50], 'error_count':len(errors)})
 except csv.Error as exc:
  raise HTTPException(400, f'Ошибка CSV: {exc}')
 with conn() as c:
  c.executemany('INSERT INTO items('+','.join(CSV_FIELDS)+') VALUES('+','.join('?' for _ in CSV_FIELDS)+')',parsed)
  auth.audit(c,user['username'],'import_csv','',f'count={len(parsed)}')
 return {'imported':len(parsed)}

@app.get('/api/export')
def export_csv(user=Depends(require_staff)):
 with conn() as c:
  rows=c.execute('SELECT '+','.join(CSV_FIELDS)+' FROM items ORDER BY id').fetchall()
  auth.audit(c,user['username'],'export_csv')
 out=io.StringIO(newline=''); w=csv.writer(out); w.writerow(CSV_FIELDS); w.writerows([tuple(r) for r in rows])
 return Response(content=out.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                 headers={'Content-Disposition':'attachment; filename="qor_catalog.csv"'})

# Local PDFs are served publicly in the prototype. Only attach files the library is
# authorized to make available to all visitors; this is not an access-control system.
PDF_DIR=Path(os.getenv('QOR_PDF_DIR',ROOT/'data'/'pdfs'))
MAX_PDF_BYTES=30*1024*1024

@app.get('/api/items/{item_id}/pdf')
def read_pdf(item_id:int):
 with conn() as c: row=c.execute('SELECT storage_name FROM item_pdfs WHERE item_id=?',(item_id,)).fetchone()
 if row is None or not re.fullmatch(r'[0-9a-f]{32}\.pdf',row['storage_name']): raise HTTPException(404,'PDF не прикреплён')
 path=PDF_DIR/row['storage_name']
 if not path.is_file(): raise HTTPException(404,'PDF не найден в хранилище')
 return FileResponse(path,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="qor-{item_id}.pdf"','X-Content-Type-Options':'nosniff'})

@app.post('/api/items/{item_id}/pdf')
async def upload_pdf(item_id:int, file:UploadFile=File(...), user=Depends(require_staff)):
 if not file.filename or not file.filename.lower().endswith('.pdf'): raise HTTPException(400,'Выберите PDF-файл')
 with conn() as c:
  if not c.execute('SELECT 1 FROM items WHERE id=?',(item_id,)).fetchone(): raise HTTPException(404,'Запись не найдена')
 PDF_DIR.mkdir(parents=True,exist_ok=True)
 name=uuid.uuid4().hex+'.pdf'; path=PDF_DIR/name; size=0
 try:
  with path.open('xb') as out:
   while True:
    chunk=await file.read(1024*1024)
    if not chunk: break
    size+=len(chunk)
    if size>MAX_PDF_BYTES: raise HTTPException(413,'PDF больше 30 МиБ')
    out.write(chunk)
  with path.open('rb') as check:
   if check.read(5)!=b'%PDF-': raise HTTPException(400,'Файл не похож на PDF')
  with conn() as c:
   old=c.execute('SELECT storage_name FROM item_pdfs WHERE item_id=?',(item_id,)).fetchone()
   c.execute('INSERT INTO item_pdfs(item_id,storage_name) VALUES(?,?) ON CONFLICT(item_id) DO UPDATE SET storage_name=excluded.storage_name, created_at=CURRENT_TIMESTAMP',(item_id,name))
   auth.audit(c,user['username'],'upload_pdf',item_id,f'bytes={size}')
 except Exception:
  path.unlink(missing_ok=True)
  raise
 # Keep old file for backup/recovery; remove only by an explicit maintenance process.
 return {'status':'uploaded','item_id':item_id,'size':size}

app.mount('/',StaticFiles(directory=ROOT/'app'/'static',html=True),name='static')
