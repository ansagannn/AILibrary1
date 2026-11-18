"""Hybrid PostgreSQL index for QOR. SQLite remains the source of truth."""
import hashlib
import json
import os
import re
import threading
import uuid
from functools import lru_cache

MODEL='intfloat/multilingual-e5-small'
REVISION='614241f622f53c4eeff9890bdc4f31cfecc418b3'
DIMENSION=384
FIELDS=('title','title_ru','title_kk','authors','publisher','year','kind','language','keywords','description','url','isbn','udc','bbk_candidate','availability','location')

class SearchUnavailable(Exception): pass

def source_id(c):
    c.execute('CREATE TABLE IF NOT EXISTS qor_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    c.execute("INSERT OR IGNORE INTO qor_meta(key,value) VALUES('catalog_id',?)",(str(uuid.uuid4()),))
    return c.execute("SELECT value FROM qor_meta WHERE key='catalog_id'").fetchone()[0]

def digest(row):
    values=[str(row[k] or '') for k in FIELDS]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def document(row):
    # Explicit labels make bibliographic meaning clearer; no claimed summaries.
    keys=('title','title_ru','title_kk','authors','publisher','keywords','description','udc','bbk_candidate')
    return '\n'.join(f'{k}: {row[k]}' for k in keys if row[k])[:10000]

def vec(v):
    if len(v)!=DIMENSION: raise ValueError('embedding dimension mismatch')
    import math
    if any(not math.isfinite(float(x)) for x in v):raise ValueError('non-finite embedding')
    return '['+','.join(format(float(x),'.9g') for x in v)+']'

@lru_cache(maxsize=1)
def model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL, revision=REVISION, device='cpu')

model_lock=threading.Lock()
def embed(texts, kind):
    if kind not in ('query','passage'): raise ValueError('invalid embedding kind')
    with model_lock:
        return model().encode([kind+': '+t for t in texts],batch_size=16,normalize_embeddings=True,show_progress_bar=False)

def pg_connect():
    dsn=os.getenv('QOR_PG_DSN','').strip()
    if not dsn:raise SearchUnavailable('PostgreSQL-поиск не настроен: задайте QOR_PG_DSN')
    try:
        import psycopg
        return psycopg.connect(dsn,connect_timeout=5)
    except Exception as e:raise SearchUnavailable('Не удалось подключиться к PostgreSQL') from e

def ensure_schema(pg):
    with pg.cursor() as c:
        c.execute('CREATE EXTENSION IF NOT EXISTS vector')
        c.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        c.execute('''CREATE TABLE IF NOT EXISTS qor_search_docs (
            catalog_id uuid NOT NULL, item_id bigint NOT NULL, doc_hash text NOT NULL,
            model_revision text NOT NULL, search_text text NOT NULL, title text NOT NULL,
            kind text NOT NULL, language text NOT NULL, year text NOT NULL,
            embedding vector(384) NOT NULL,
            lex tsvector GENERATED ALWAYS AS (to_tsvector('simple',search_text)) STORED,
            PRIMARY KEY(catalog_id,item_id))''')
        c.execute('CREATE INDEX IF NOT EXISTS qor_search_lex_idx ON qor_search_docs USING gin (lex)')
        c.execute('CREATE INDEX IF NOT EXISTS qor_search_title_trgm_idx ON qor_search_docs USING gin (title gin_trgm_ops)')
        c.execute('CREATE INDEX IF NOT EXISTS qor_search_vector_idx ON qor_search_docs USING hnsw (embedding vector_cosine_ops)')
    pg.commit()

def sync(sqlite, pg, limit=None, batch_size=16):
    """Update changed records in bounded batches; never delete from source DB."""
    catalog=source_id(sqlite); sqlite.commit()
    with pg.cursor() as c:
        c.execute('SELECT item_id,doc_hash,model_revision FROM qor_search_docs WHERE catalog_id=%s',(catalog,))
        existing={r[0]:(r[1],r[2]) for r in c.fetchall()}
    rows=sqlite.execute('SELECT * FROM items ORDER BY id').fetchall()
    if limit is not None: rows=rows[:limit]
    changed=[r for r in rows if existing.get(r['id'])!=(digest(r),REVISION)]
    for offset in range(0,len(changed),batch_size):
        batch=changed[offset:offset+batch_size]
        vectors=embed([document(r) for r in batch],'passage')
        if len(vectors)!=len(batch): raise ValueError('incorrect embedding batch size')
        with pg.cursor() as c:
            c.executemany('''INSERT INTO qor_search_docs(catalog_id,item_id,doc_hash,model_revision,search_text,title,kind,language,year,embedding)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                   ON CONFLICT(catalog_id,item_id) DO UPDATE SET doc_hash=excluded.doc_hash,model_revision=excluded.model_revision,
                   search_text=excluded.search_text,title=excluded.title,kind=excluded.kind,language=excluded.language,
                   year=excluded.year,embedding=excluded.embedding''',
                 [(catalog,r['id'],digest(r),REVISION,document(r),r['title'],r['kind'],r['language'],r['year'],vec(v)) for r,v in zip(batch,vectors)])
        pg.commit()
        print(f'Индексировано: {min(offset+len(batch),len(changed))}/{len(changed)}',flush=True)
    # No deletion: stale and removed items are filtered against SQLite at query time.
    return {'catalog_id':catalog,'scanned':len(rows),'updated':len(changed)}

# A small transparent domain glossary, not a general translation system.
GLOSSARY=[
    (re.compile(r'тау[ -]?кен|горн\w*|mining',re.I), ['тау кен','тау-кен ісі','горное дело','mining']),
    (re.compile(r'электротех\w*|electrical engineering',re.I), ['электротехника','электр техникасы','electrical engineering']),
    (re.compile(r'жасанды интеллект|искусственн\w* интеллект\w*|artificial intelligence',re.I), ['жасанды интеллект','искусственный интеллект','artificial intelligence']),
    (re.compile(r'охран\w* труда|еңбек\w* қорға\w*|occupational safety',re.I), ['охрана труда','еңбекті қорғау','occupational safety']),
]
def expand_query(query):
    matched=[]
    for pattern,terms in GLOSSARY:
        if pattern.search(query):matched.extend(terms)
    terms=list(dict.fromkeys(matched))
    lexical=query + ''.join(' OR "'+term+'"' for term in terms)
    semantic=query + (' ' + '; '.join(terms) if terms else '')
    return lexical,semantic,terms

def retrieve(sqlite, pg, query, kind='',language='',year='',limit=20):
    """RRF of indexed lexical, typo-tolerant title, and cross-lingual semantic lists."""
    catalog=source_id(sqlite)
    lexical_query,semantic_query,expansions=expand_query(query)
    vector=vec(embed([semantic_query],'query')[0]); filters=['catalog_id=%s','model_revision=%s'];base=[catalog,REVISION]
    for field,value in [('kind',kind),('language',language if language!='auto' else ''),('year',year)]:
        if value: filters.append(field+'=%s');base.append(value)
    where=' AND '.join(filters); ranks={}
    def add(rows,label):
        for rank,(item_id,doc_hash) in enumerate(rows,1):
            entry=ranks.setdefault(item_id,{'rrf':0.0,'signals':[],'hash':doc_hash})
            entry['rrf']+=1/(60+rank);entry['signals'].append(label)
    with pg.cursor() as c:
        c.execute('SELECT count(*) FROM qor_search_docs WHERE catalog_id=%s AND model_revision=%s',(catalog,REVISION)); indexed=c.fetchone()[0]
        c.execute(f"SELECT item_id,doc_hash FROM qor_search_docs WHERE {where} AND lex @@ websearch_to_tsquery('simple',%s) ORDER BY ts_rank_cd(lex,websearch_to_tsquery('simple',%s)) DESC,item_id LIMIT 100",base+[lexical_query,lexical_query]);add(c.fetchall(),'lexical')
        c.execute(f"SELECT item_id,doc_hash FROM qor_search_docs WHERE {where} AND title %% %s ORDER BY similarity(title,%s) DESC,item_id LIMIT 100",base+[query,query]);add(c.fetchall(),'fuzzy')
        # Exact scan prevents approximate HNSW pre-filtering from dropping records
        # belonging to a filtered language or a small catalog.
        c.execute('SET LOCAL enable_indexscan = off')
        threshold=float(os.getenv('QOR_MIN_SIMILARITY','0.80'))
        if not 0 <= threshold <= 1: raise ValueError('QOR_MIN_SIMILARITY must be between 0 and 1')
        c.execute(f"SELECT item_id,doc_hash FROM qor_search_docs WHERE {where} AND (1 - (embedding <=> %s::vector)) >= %s ORDER BY embedding <=> %s::vector LIMIT 100",base+[vector,threshold,vector]);add(c.fetchall(),'semantic')
    ordered=sorted(ranks.items(),key=lambda x:(-x[1]['rrf'],x[0])); results=[]; stale=0
    for item_id,meta in ordered:
        r=sqlite.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone()
        if r is None or digest(r)!=meta['hash']:
            stale+=1;continue
        item=dict(r);item['search_signals']=meta['signals'];item['score']=round(meta['rrf'],6);results.append(item)
        if len(results)>=max(1,min(limit,100)):break
    total=sqlite.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    return {'results':results,'candidates':len(ranks),'indexed':indexed,'catalog_total':total,'stale_candidates_skipped':stale,'query_expansion':expansions}
