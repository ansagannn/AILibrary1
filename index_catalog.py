"""Build or incrementally refresh the optional PostgreSQL index."""
import argparse,sys
from app.main import conn
from app.search_pg import pg_connect,ensure_schema,sync

def main():
 p=argparse.ArgumentParser();p.add_argument('--limit',type=int,help='Ограничить число записей для теста; индекс будет неполным');p.add_argument('--batch-size',type=int,default=16);a=p.parse_args()
 if (a.limit is not None and a.limit<1) or not 1<=a.batch_size<=128:p.error('Недопустимый размер')
 try:
  with conn() as sq, pg_connect() as pg:
   ensure_schema(pg); print(sync(sq,pg,a.limit,a.batch_size))
  return 0
 except Exception as e:print(f'Ошибка индексации: {e}',file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
