"""Fill missing ISBN/UDC/621 values from preserved KABIS bytes; dry-run by default."""
import argparse, hashlib, io, sqlite3, sys
from pathlib import Path
from app.kabis_import import connect_existing, parse_file, mapped_item, backup_database, write_report, finalize_report

COLUMNS=('isbn','udc','bbk_candidate')
def plan(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kabis_sources'").fetchone():
        raise ValueError('Импортированных источников КАБИС нет')
    changes=[]
    for item_id,sha,raw,isbn,udc,bbk in c.execute('''SELECT k.item_id,k.raw_sha256,k.raw_record,i.isbn,i.udc,i.bbk_candidate
                FROM kabis_sources k JOIN items i ON i.id=k.item_id'''):
        if hashlib.sha256(raw).hexdigest()!=sha: raise ValueError(f'Не совпадает хеш исходной записи item_id={item_id}')
        _,_,fields=next(parse_file(io.BytesIO(raw)))
        mapped=mapped_item(fields)
        if not mapped: raise ValueError(f'В исходной записи не найдено название item_id={item_id}')
        current=(isbn,udc,bbk)
        updates={k:mapped[k] for k,v in zip(COLUMNS,current) if not v and mapped[k]}
        if updates:changes.append((item_id,updates))
    return changes

def main(argv=None):
    p=argparse.ArgumentParser(description='Заполнить только пустые метаданные из сохранённых записей КАБИС')
    p.add_argument('--db',required=True);p.add_argument('--report',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--backup');a=p.parse_args(argv)
    if a.apply and not a.backup:p.error('--apply требует --backup')
    paths=[Path(x).resolve() for x in (a.db,a.report,a.backup) if x]
    if len(set(paths))!=len(paths):p.error('Пути базы, отчёта и копии должны различаться')
    try:
        c=connect_existing(a.db)
        try:
            changes=plan(c)
            report={'mode':'apply' if a.apply else 'dry-run','status':'prepared_not_committed',
                    'records_to_update':len(changes),'fields':{k:sum(k in x[1] for x in changes) for k in COLUMNS}}
            write_report(a.report,report)
            if not a.apply:
                print(f'Проверка: к заполнению {len(changes)} карточек, отчёт {a.report}');return 0
            backup_database(c,a.backup)
            try:
                c.execute('BEGIN IMMEDIATE')
                # Recompute under the write lock: retain any fields changed since planning.
                changes=plan(c)
                for item_id,updates in changes:
                    c.execute('UPDATE items SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',list(updates.values())+[item_id])
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qor_audit'").fetchone():
                    c.execute("INSERT INTO qor_audit(actor,action,target,details) VALUES('local-cli','backfill_kabis','',?)",(f'count={len(changes)}',))
                c.commit()
            except BaseException:
                c.rollback();raise
            report.update({'status':'committed','records_updated':len(changes),
                           'fields':{k:sum(k in x[1] for x in changes) for k in COLUMNS}})
            try:finalize_report(a.report,report)
            except OSError as e:print(f'Данные обновлены, но отчёт не записан: {e}',file=sys.stderr)
            print(f'Обновлено карточек: {len(changes)}; копия: {a.backup}');return 0
        finally:c.close()
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f'Ошибка: {e}. Изменения не применены либо транзакция отменена.',file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
