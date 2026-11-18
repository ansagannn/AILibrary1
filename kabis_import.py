"""Import this observed KABIS ISO-2709-like export safely, not a generic MARC reader.

Dry-run by default. Raw bytes are preserved for every imported record. This
module never creates the application database and never performs updates.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone

ENCODING = 'ptcp154'
MAX_RECORD_BYTES = 99999
GUID = re.compile(r'^\{guid\s+\{([0-9a-fA-F-]{36})\}\}$', re.IGNORECASE)
SUBFIELD = re.compile(r'\^([A-Za-z0-9])')
LANGUAGES = {'каз': 'Kazakh', 'каз.': 'Kazakh', 'рус': 'Russian', 'рус.': 'Russian', 'eng': 'English'}
KINDS = {'учебник': 'textbook', 'учебное пособие': 'textbook', 'учебно-методическое пособие': 'textbook'}
FIELDS = ('title','title_ru','title_kk','authors','publisher','year','kind','language','keywords','description','url','isbn','udc','bbk_candidate','availability','location')

class InvalidExport(ValueError):
    pass

def parse_file(path):
    """Read exact leader/directory framing and return source records."""
    offset = 0
    with (path if hasattr(path, 'read') else open(path, 'rb')) as stream:
        while True:
            leader = stream.read(24)
            if not leader:
                return
            if len(leader) != 24 or not leader[:5].isdigit() or not leader[12:17].isdigit():
                raise InvalidExport(f'Некорректный лидер записи в байте {offset}')
            length, base = int(leader[:5]), int(leader[12:17])
            if not 25 <= length <= MAX_RECORD_BYTES or not 25 <= base < length:
                raise InvalidExport(f'Некорректная длина записи/каталога в байте {offset}')
            raw = leader + stream.read(length - 24)
            if len(raw) != length or raw[-1:] != b'\x1d' or raw[base-1:base] != b'\x1e':
                raise InvalidExport(f'Обрезанная запись или нет разделителя в байте {offset}')
            directory = raw[24:base-1]
            if len(directory) % 12:
                raise InvalidExport(f'Некорректный каталог полей в байте {offset}')
            fields = {}; ranges = []
            for pos in range(0, len(directory), 12):
                entry = directory[pos:pos+12]
                if not entry[:3].isdigit() or not entry[3:7].isdigit() or not entry[7:12].isdigit():
                    raise InvalidExport(f'Некорректный элемент каталога в байте {offset}')
                size, start = int(entry[3:7]), int(entry[7:12])
                if size < 1 or base + start + size > length - 1:
                    raise InvalidExport(f'Поле за пределами записи в байте {offset}')
                value = raw[base+start:base+start+size]
                if value[-1:] != b'\x1e':
                    raise InvalidExport(f'Нет разделителя поля в байте {offset}')
                try:
                    text = value[:-1].decode(ENCODING, errors='strict')
                except UnicodeDecodeError as e:
                    raise InvalidExport(f'Ошибка кодировки в байте {offset}: {e}') from e
                fields.setdefault(entry[:3].decode('ascii'), []).append(text)
                ranges.append((start, start+size))
            ranges.sort()
            if any(a[1] > b[0] for a,b in zip(ranges,ranges[1:])):
                raise InvalidExport(f'Перекрывающиеся поля в байте {offset}')
            yield offset, raw, fields
            offset += length

def subfields(fields, tag, code):
    values = []
    for value in fields.get(tag, []):
        matches = list(SUBFIELD.finditer(value))
        for i, match in enumerate(matches):
            if match.group(1).upper() == code:
                end = matches[i+1].start() if i+1 < len(matches) else len(value)
                text = value[match.end():end].strip()
                if text: values.append(text)
    return values

def normalized_id(fields):
    vals = fields.get('001', [])
    if len(vals) != 1:
        return None, None
    original = vals[0].strip()
    m = GUID.fullmatch(original)
    if not m: return None, original
    try: return str(uuid.UUID(m.group(1))), original
    except ValueError: return None, original

def mapped_item(fields):
    title = subfields(fields, '200', 'A')
    if not title: return None
    author = subfields(fields, '700', 'A') + subfields(fields, '710', 'A')
    publisher = subfields(fields, '210', 'C')
    dates = subfields(fields, '210', 'D')
    year = dates[0].strip() if dates and re.fullmatch(r'\d{4}', dates[0].strip()) else ''
    lang = subfields(fields, '101', 'A')
    language = LANGUAGES.get(lang[0].strip().casefold(), '') if len(lang) == 1 else ''
    kinds = subfields(fields, '900', 'C')
    kind = KINDS.get(kinds[0].casefold(), 'material') if len(kinds) == 1 else 'material'
    keywords = list(dict.fromkeys(subfields(fields,'606','A') + subfields(fields,'610','A')))
    urls = subfields(fields,'856','U')
    url = urls[0] if urls and re.match(r'^https?://\S+$', urls[0], flags=re.IGNORECASE) else ''
    return {'title':' '.join(title), 'title_ru':'', 'title_kk':'',
            'authors':'; '.join(author), 'publisher':'; '.join(publisher), 'year':year,
            'kind':kind, 'language':language, 'keywords':'; '.join(keywords), 'description':'', 'url':url,
            'isbn':'; '.join(subfields(fields,'010','A')), 'udc':'; '.join(subfields(fields,'675','A')),
            'bbk_candidate':'; '.join(subfields(fields,'621','A')), 'availability':'', 'location':''}

def inspect(path):
    valid = []; rejected = []; seen = set(); counts = Counter(); languages = Counter(); kinds = Counter()
    digest = hashlib.sha256()
    for offset, raw, fields in parse_file(path):
        digest.update(raw); counts['records'] += 1
        sid, original = normalized_id(fields)
        if not sid:
            rejected.append({'offset':offset,'source_id':original,'reason':'Отсутствует или некорректен GUID поля 001'})
            continue
        if sid in seen: raise InvalidExport(f'Повторный GUID {sid}; импорт целиком отменён')
        seen.add(sid)
        item = mapped_item(fields)
        if item is None:
            rejected.append({'offset':offset,'source_id':original,'reason':'Нет названия в подполе 200^A',
                             'review_fields':{tag:fields[tag] for tag in ('200','461','463','700','210') if tag in fields}})
            continue
        lang = subfields(fields,'101','A'); languages['; '.join(lang) if lang else '(не указан)'] += 1
        kinds[item['kind']] += 1
        for name in ('authors','publisher','year','language','url'):
            if not item[name]: counts['empty_'+name] += 1
        valid.append((sid, original, hashlib.sha256(raw).hexdigest(), raw, item))
    if not counts['records']: raise InvalidExport('Файл выгрузки пуст')
    return valid, {'input_sha256':digest.hexdigest(), 'encoding':ENCODING, 'records':counts['records'],
                   'valid':len(valid),'rejected':len(rejected), 'rejected_records':rejected,
                   'empty_fields':{k.removeprefix('empty_'):v for k,v in counts.items() if k.startswith('empty_')},
                   'source_languages':dict(languages.most_common()), 'mapped_types':dict(kinds)}

def connect_existing(path):
    p=Path(path).resolve()
    if not p.is_file(): raise ValueError('Файл базы данных не найден; создать его должен QOR')
    c=sqlite3.connect(p.as_uri()+'?mode=rw', uri=True, timeout=10)
    c.execute('PRAGMA foreign_keys=ON')
    try:
        columns = {r[1] for r in c.execute('PRAGMA table_info(items)')}
        if not set(FIELDS).issubset(columns): raise ValueError('Схема items несовместима с этой версией QOR')
        return c
    except BaseException:
        c.close(); raise

def existing_sources(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kabis_sources'").fetchone(): return {}
    return {r[0]:r[1] for r in c.execute('SELECT source_id, raw_sha256 FROM kabis_sources')}

def comparison(valid, existing):
    count = Counter()
    for sid, _, digest, _, _ in valid:
        count['new' if sid not in existing else ('unchanged' if existing[sid]==digest else 'changed_skipped')] += 1
    return {x:count[x] for x in ('new','unchanged','changed_skipped')}

def write_report(path, report):
    target=Path(path).resolve()
    if target.exists(): raise ValueError('Файл отчёта уже существует; укажите другое имя')
    if not target.parent.is_dir(): raise ValueError('Папка отчёта не существует')
    # Exclusive creation: never overwrite a previous audit report.
    with open(target, 'x', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')

def finalize_report(path, report):
    target = Path(path).resolve()
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as f:
            temp_name = f.name
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name: Path(temp_name).unlink(missing_ok=True)

def backup_database(source, path):
    target=Path(path).resolve()
    if target.exists(): raise ValueError('Файл резервной копии уже существует')
    if not target.parent.is_dir(): raise ValueError('Папка резервной копии не существует')
    # Exclusive create avoids silently replacing an existing file.
    with open(target, 'xb'): pass
    try:
        dest=sqlite3.connect(target)
        try: source.backup(dest)
        finally: dest.close()
    except BaseException:
        target.unlink(missing_ok=True); raise

def commit(c, valid):
    inserted = 0
    try:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''CREATE TABLE IF NOT EXISTS kabis_sources (
            source_id TEXT PRIMARY KEY, source_id_raw TEXT NOT NULL,
            item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
            raw_sha256 TEXT NOT NULL, raw_record BLOB NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        existing=existing_sources(c)
        comparisons=comparison(valid,existing)
        for sid, original, sha, raw, item in valid:
            if sid in existing: continue
            cur=c.execute('INSERT INTO items('+','.join(FIELDS)+') VALUES ('+','.join('?' for _ in FIELDS)+')', [item[f] for f in FIELDS])
            c.execute('INSERT INTO kabis_sources(source_id,source_id_raw,item_id,raw_sha256,raw_record) VALUES (?,?,?,?,?)', (sid, original, cur.lastrowid, sha, raw))
            inserted+=1
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qor_audit'").fetchone():
            c.execute("INSERT INTO qor_audit(actor,action,target,details) VALUES('local-cli','import_kabis','',?)", (f'count={inserted}',))
        c.commit()
    except BaseException:
        c.rollback(); raise
    return inserted, comparisons

def main(argv=None):
    p=argparse.ArgumentParser(description='Анализ или безопасный импорт выгрузки КАБИС в QOR. По умолчанию только анализ.')
    p.add_argument('input', help='Путь к распакованной выгрузке (не ZIP)')
    p.add_argument('--report', required=True, help='Новый JSON-отчёт; существующий файл не перезаписывается')
    p.add_argument('--db', help='Существующая SQLite база QOR')
    p.add_argument('--apply', action='store_true', help='Записать новые корректные записи в базу')
    p.add_argument('--allow-rejects', action='store_true', help='Разрешить импорт корректных записей при наличии отклонённых')
    p.add_argument('--backup', help='Путь к новой резервной копии SQLite (обязательно при --apply)')
    args=p.parse_args(argv)
    if args.apply and (not args.db or not args.backup): p.error('--apply требует --db и --backup')
    paths=[Path(x).resolve() for x in (args.input,args.report,args.db,args.backup) if x]
    if len(set(paths))!=len(paths): p.error('Пути входного файла, отчёта, базы и резервной копии должны различаться')
    try:
        valid, report = inspect(args.input)
        report.update({'generated_at_utc':datetime.now(timezone.utc).isoformat(), 'mode':'dry-run',
                       'note':'Исходные записи и идентификаторы хранятся в kabis_sources; изменённые записи не обновляются автоматически.'})
        c=connect_existing(args.db) if args.db else None
        try:
            if c is not None: report['comparison'] = comparison(valid, existing_sources(c))
            if not args.apply:
                write_report(args.report, report)
                print(f"Анализ: {report['records']} записей, пригодны {report['valid']}, отклонены {report['rejected']}. Отчёт: {args.report}")
                return 0
            if report['rejected'] and not args.allow_rejects:
                report['status']='blocked_rejected_records'; write_report(args.report,report)
                print('Импорт отменён: имеются отклонённые записи. Изучите отчёт; для импорта остальных нужен --allow-rejects.', file=sys.stderr)
                return 2
            # The report is written before any DB change. Stop the application before applying.
            report.update({'mode':'apply', 'status':'prepared_not_committed'})
            write_report(args.report,report)
            backup_database(c,args.backup)
            inserted, actual_comparison=commit(c,valid)
            report.update({'status':'committed', 'inserted':inserted, 'comparison':actual_comparison})
            try:
                finalize_report(args.report, report)
            except OSError as e:
                print(f'ВНИМАНИЕ: данные импортированы, но не удалось обновить отчёт: {e}', file=sys.stderr)
            print(f"Импорт завершён: добавлено {inserted}, без изменений {actual_comparison['unchanged']}, изменены в источнике и пропущены {actual_comparison['changed_skipped']}, отклонено {report['rejected']}. Резервная копия: {args.backup}; отчёт: {args.report}")
            return 0
        finally:
            if c is not None: c.close()
    except (OSError, sqlite3.Error, ValueError) as e:
        print(f'Ошибка: {e}. Изменения не применены либо транзакция отменена.', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
