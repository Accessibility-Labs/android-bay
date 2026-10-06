"""Private, read-only archive browsing backed by a disposable SQLite index."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
import time
import uuid

from adb import Cancelled

MAX_LINE = 8 * 1024 * 1024
MAX_MANIFEST_LINE = 1024 * 1024
MAX_ROWS = 2_000_000
HELPER = re.compile(r'/AndroidRescue/exports/(\d{8}T\d{6}Z-[0-9a-f]{8})/(.+)$')
HEX_ID = re.compile(r'[0-9a-f]{24}')
MIME = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
        '.webp': 'image/webp', '.bmp': 'image/bmp', '.heic': 'image/heic', '.heif': 'image/heif',
        '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.3gp': 'video/3gpp', '.webm': 'video/webm',
        '.mkv': 'video/x-matroska', '.mov': 'video/quicktime', '.avi': 'video/x-msvideo',
        '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.wav': 'audio/wav',
        '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.amr': 'audio/amr', '.flac': 'audio/flac'}
ISSUES = {
    'manifest-row': 'Manifest rows with invalid fields or unsafe paths were excluded.',
    'missing-file': 'Referenced files are missing, changed in size, or unsafe to open.',
    'invalid-export': 'An export file failed integrity or format checks; its parsed rows were excluded.',
    'invalid-row': 'Export rows with unsupported or missing fields were excluded.',
    'unknown-date': 'Some message dates could not be normalized; raw values are retained.',
    'missing-attachment': 'Some MMS attachments are missing or do not match their recorded references.',
    'duplicate-row': 'Repeated provider IDs were excluded from this conversation view.',
    'no-helper': 'No recognizable helper export was found; conversation coverage is unavailable.',
    'missing-category': 'One or more conversation/contact export categories are unavailable.',
    'incomplete-helper': 'The selected helper run lacks a verified completed export marker.',
}
LIMITATIONS = [
    'This is a view of retained archive copies, not whole-phone or deleted-data completeness.',
    'Only the latest recognizable helper run supplies conversations; original exports remain preserved.',
    'Parsed JSONL files are SHA-256 checked. Media metadata is indexed by manifest/path/size; each media file is SHA-256 checked before its first stream in this process.',
    'Media counts count saved file copies; identical bytes may appear more than once.',
    'Contact names require an exact normalized address match; ambiguous matches remain unnamed.',
    'SMS dates use Android milliseconds and MMS dates use Android seconds; unsupported dates remain raw.',
    'Browser playback depends on the original codec. Unsupported media remains available to download.',
]
_hash_cache = {}
_cache_lock = threading.Lock()


class ReaderError(ValueError):
    pass


class ReaderNotReady(ReaderError):
    pass


class ReaderStale(ReaderNotReady):
    pass


def _now(): return datetime.now(timezone.utc).isoformat()


def _check(cancel):
    if cancel is not None and cancel.is_set(): raise Cancelled('Archive indexing cancelled')


def _id(value): return hashlib.sha256(value.encode('utf-8', 'surrogatepass')).hexdigest()[:24]


def _text(value):
    if value is None: return ''
    return value if isinstance(value, str) else str(value) if isinstance(value, (int, float)) else ''


def _safe(root, relative=None):
    root = Path(os.path.abspath(root))
    if relative is None:
        target = root
    else:
        if not isinstance(relative, str) or not relative or '\x00' in relative:
            raise ReaderError('Unsafe archive path')
        relative = relative.replace('\\', '/')
        parts = relative.split('/')
        if relative.startswith('/') or any(p in ('', '.', '..') or ':' in p for p in parts):
            raise ReaderError('Unsafe archive path')
        target = root.joinpath(*parts)
    for path in [target, *target.parents]:
        if path.exists() or path.is_symlink():
            value = path.lstat()
            if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 1024:
                raise ReaderError('Archive links/reparse points are not allowed')
    return target


def _identity(value): return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _file(root, record):
    path = _safe(root, record['local_path'])
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != record['size']:
        raise ReaderError('File does not match the manifest size')
    return path, info


@contextmanager
def _verified(root, record, cancel=None):
    path, info = _file(root, record)
    with path.open('rb') as stream:
        opened = _identity(os.fstat(stream.fileno()))
        # Windows path-stat ctime can mean creation time while fstat reports
        # change time. Compare like-for-like identities throughout the read.
        if opened[:4] != _identity(info)[:4]: raise ReaderError('File changed before opening')
        digest = hashlib.sha256()
        while chunk := stream.read(1024 ** 2):
            _check(cancel); digest.update(chunk)
        if digest.hexdigest() != record['sha256']: raise ReaderError('File hash mismatch')
        if _identity(os.fstat(stream.fileno())) != opened: raise ReaderError('File changed during verification')
        stream.seek(0)
        yield stream
        if _identity(os.fstat(stream.fileno())) != opened or _identity(path.stat()) != _identity(info): raise ReaderError('File changed during reading')


def _rows(root, record, cancel=None):
    with _verified(root, record, cancel) as stream:
        digest = hashlib.sha256()
        number = 0
        while raw := stream.readline(MAX_LINE + 1):
            _check(cancel); number += 1
            if len(raw) > MAX_LINE or number > MAX_ROWS: raise ReaderError('Export exceeds parsing limits')
            digest.update(raw)
            if not raw.strip(): continue
            row = json.loads(raw)
            if not isinstance(row, dict) or not isinstance(row.get('values'), dict): raise ReaderError('Unsupported export row')
            yield number, row['values'], row.get('export') if isinstance(row.get('export'), dict) else {}
        if digest.hexdigest() != record['sha256']: raise ReaderError('File changed during parsing')


def _date(value, kind):
    raw = _text(value)
    if not re.fullmatch(r'-?\d{1,17}', raw): return raw, None, None
    try:
        milliseconds = int(raw) * (1000 if kind == 'mms' else 1)
        date = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        if not 1900 <= date.year <= 3000: return raw, None, None
        return raw, milliseconds, date.isoformat()
    except (ValueError, OverflowError, OSError): return raw, None, None


def _address(value):
    value = _text(value).strip()
    if not value or value == 'insert-address-token': return ''
    if '@' in value: return value.casefold()
    if re.fullmatch(r'[+0-9 ()\-.]+', value): return re.sub(r'[ ()\-.]', '', value)
    return value


def _mime(value, source=''):
    mime = _text(value).split(';', 1)[0].strip().lower()
    if not re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', mime): mime = MIME.get(PurePosixPath(source).suffix.lower(), 'application/octet-stream')
    if mime in ('text/html', 'image/svg+xml', 'application/xhtml+xml'): mime = 'application/octet-stream'
    kind = 'photo' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'audio' if mime.startswith('audio/') else 'other'
    return mime, kind


def _json_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2); stream.flush(); os.fsync(stream.fileno())


SCHEMA = '''
CREATE TABLE files(id TEXT PRIMARY KEY,source TEXT,local_path TEXT,size INTEGER,sha256 TEXT,seq INTEGER,helper_run TEXT,helper_rel TEXT);
CREATE INDEX files_helper ON files(helper_run,helper_rel,seq);
CREATE TABLE media(id TEXT PRIMARY KEY,kind TEXT,mime TEXT,name TEXT);
CREATE TABLE contacts(id TEXT PRIMARY KEY,name TEXT);
CREATE TABLE names(normalized TEXT,address TEXT,name TEXT);
CREATE INDEX names_match ON names(normalized);
CREATE TABLE canonical(id TEXT PRIMARY KEY,address TEXT);
CREATE TABLE thread_meta(id TEXT PRIMARY KEY,recipients TEXT);
CREATE TABLE messages(id TEXT PRIMARY KEY,thread_id TEXT,android_thread TEXT,android_id TEXT,kind TEXT,box TEXT,direction TEXT,date_raw TEXT,date_ms INTEGER,date_iso TEXT,body TEXT,subject TEXT,source TEXT,row_number INTEGER);
CREATE INDEX message_thread ON messages(thread_id,date_ms,id);
CREATE TABLE participants(message_id TEXT,address TEXT,normalized TEXT,role TEXT);
CREATE INDEX participant_message ON participants(message_id);
CREATE TABLE parts(id TEXT PRIMARY KEY,message_id TEXT,sequence INTEGER,text TEXT,mime TEXT,name TEXT,media_id TEXT,available INTEGER,source TEXT);
CREATE INDEX part_message ON parts(message_id,sequence,id);
CREATE TABLE threads(id TEXT PRIMARY KEY,android_id TEXT,title TEXT,message_count INTEGER,last_ms INTEGER,last_iso TEXT,snippet TEXT,participants TEXT);
'''


def build_index(root, cancel=None, progress=None):
    root = _safe(root)
    base = _safe(root, 'viewer-data'); base.mkdir(exist_ok=True)
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    folder = _safe(base, generation); folder.mkdir()
    result = {'state': 'building', 'status': 'running', 'generation': generation, 'startedAt': _now(),
              'receiptPath': str(folder / 'receipt.json'), 'counts': {}, 'issues': [], 'limitations': LIMITATIONS}
    issues = Counter(); validated = set(); connection = None
    def tell(phase, message):
        if progress: progress(phase, message)
    def issue(code): issues[code] += 1
    def parse(record, callback):
        connection.execute('SAVEPOINT export_file')
        try:
            for number, values, extra in _rows(root, record, cancel): callback(number, values, extra)
            connection.execute('RELEASE export_file'); validated.add(record['id'])
        except (ReaderError, OSError, ValueError, TypeError, OverflowError):
            connection.execute('ROLLBACK TO export_file'); connection.execute('RELEASE export_file'); issue('invalid-export')
    try:
        _json_new(folder / 'plan.json', result)
        connection = sqlite3.connect(folder / 'index.sqlite')
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA trusted_schema=OFF'); connection.execute('PRAGMA journal_mode=DELETE')
        connection.executescript(SCHEMA)
        tell('manifest', 'Reading saved-file references')
        manifest_count = 0
        manifest_path = _safe(root, 'manifest.jsonl')
        before = _identity(manifest_path.stat()); manifest_hash = hashlib.sha256()
        with manifest_path.open('rb') as stream:
            while raw := stream.readline(MAX_MANIFEST_LINE + 1):
                _check(cancel)
                if len(raw) > MAX_MANIFEST_LINE: raise ReaderError('Manifest line exceeds limit')
                manifest_hash.update(raw); manifest_count += 1
                if manifest_count > MAX_ROWS: raise ReaderError('Manifest record limit exceeded')
                try:
                    record = json.loads(raw)
                    if record.get('status') != 'copied': continue
                    source, local, size, sha = record['source'], record['localPath'], record['size'], record['sha256']
                    if not isinstance(source, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0 or not re.fullmatch('[0-9a-fA-F]{64}', sha): raise ValueError()
                    _safe(root, local)
                    matched = HELPER.search(source)
                    run, relative = matched.groups() if matched else (None, None)
                    connection.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?,?,?,?)',
                                       (_id(source + '\0' + local + '\0' + sha.lower()), source, local, size, sha.lower(), manifest_count, run, relative))
                except (ValueError, KeyError, TypeError, AttributeError, OSError): issue('manifest-row')
        if _identity(manifest_path.stat()) != before: raise ReaderError('Manifest changed while indexing')
        result['manifestSha256'] = manifest_hash.hexdigest()
        result['manifestStatIdentity'] = before
        latest = connection.execute('SELECT MAX(helper_run) FROM files').fetchone()[0]
        result['helperRun'] = latest
        selected = {}
        if latest:
            for row in connection.execute('SELECT * FROM files WHERE helper_run=? ORDER BY seq', (latest,)): selected[row['helper_rel']] = dict(row)
        else: issue('no-helper')
        marker = selected.get('report.json')
        try:
            if marker is None or marker['size'] > 128 * 1024 ** 2: raise ReaderError('No completed helper marker')
            with _verified(root, marker, cancel) as stream:
                report = json.load(stream)
            if report.get('runFinished') is not True or report.get('status') not in ('complete', 'partial'): raise ReaderError('Incomplete marker')
            result['helperExportStatus'] = report['status']; validated.add(marker['id'])
            if report['status'] != 'complete': issue('incomplete-helper')
        except (ReaderError, OSError, ValueError, AttributeError):
            if latest: issue('incomplete-helper')
        def category(name, callback, required=False):
            record = selected.get(name + '.jsonl')
            if record: parse(record, callback)
            elif required: issue('missing-category')
        def media(record, declared='', name=''):
            try: _file(root, record)
            except (OSError, ReaderError): issue('missing-file'); return None
            mime, kind = _mime(declared, record['source'])
            connection.execute('INSERT OR REPLACE INTO media VALUES(?,?,?,?)',
                               (record['id'], kind, mime, name or PurePosixPath(record['source']).name))
            return record['id']
        tell('contacts', 'Reading verified contact and conversation references')
        def contact(number, v, extra):
            key = _text(v.get('_id')); name = _text(v.get('display_name') or v.get('display_name_alt'))
            if key: connection.execute('INSERT OR REPLACE INTO contacts VALUES(?,?)', (key, name))
            reference = extra.get('photo')
            if isinstance(reference, dict): attachment(reference, '', 'Contact photo')
        def contact_data(number, v, extra):
            if v.get('mimetype') not in ('vnd.android.cursor.item/phone_v2', 'vnd.android.cursor.item/email_v2'): return
            address = _text(v.get('data1')); name = _text(v.get('display_name'))
            if not name:
                found = connection.execute('SELECT name FROM contacts WHERE id=?', (_text(v.get('contact_id')),)).fetchone()
                name = found[0] if found else ''
            for raw_address in {address, _text(v.get('data4')) if v.get('mimetype') == 'vnd.android.cursor.item/phone_v2' else ''}:
                if _address(raw_address) and name:
                    connection.execute('INSERT INTO names VALUES(?,?,?)', (_address(raw_address), address, name))
        def attachment(reference, mime, name):
            relative = reference.get('file')
            if not isinstance(relative, str) or any(p in ('', '.', '..') for p in relative.split('/')) or '\\' in relative or ':' in relative:
                issue('missing-attachment'); return None
            record = selected.get(relative)
            if record is None or reference.get('sha256') != record['sha256'] or reference.get('bytes') != record['size']:
                issue('missing-attachment'); return None
            found = media(record, mime, name)
            if found is None: issue('missing-attachment')
            return found
        category('contacts', contact, True); category('contact_data', contact_data, True)
        category('message_addresses', lambda n,v,e: connection.execute('INSERT OR REPLACE INTO canonical VALUES(?,?)', (_text(v.get('_id')), _text(v.get('address')))))
        category('message_threads', lambda n,v,e: connection.execute('INSERT OR REPLACE INTO thread_meta VALUES(?,?)', (_text(v.get('_id')), _text(v.get('recipient_ids')))))
        tell('messages', 'Reading verified SMS and MMS exports')
        for kind in ('sms', 'mms'):
            record = selected.get(kind + '.jsonl')
            if record is None: issue('missing-category'); continue
            def message(number, v, extra, kind=kind, record=record):
                key = _text(v.get('_id'))
                if not key: issue('invalid-row'); return
                android_thread = _text(v.get('thread_id'))
                message_id = _id(str(latest) + '|' + kind + '|' + key)
                thread_id = _id(str(latest) + '|thread|' + android_thread) if android_thread else _id(message_id + '|unknown-thread')
                box = _text(v.get('type' if kind == 'sms' else 'msg_box'))
                direction = {'1': 'incoming', '2': 'outgoing', '3': 'draft', '4': 'outgoing', '5': 'failed', '6': 'queued'}.get(box, 'unknown')
                raw_date, date_ms, date_iso = _date(v.get('date'), kind)
                if date_ms is None: issue('unknown-date')
                cursor = connection.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (message_id, thread_id, android_thread, key, kind, box, direction, raw_date, date_ms, date_iso,
                     _text(v.get('body')) if kind == 'sms' else '', _text(v.get('subject' if kind == 'sms' else 'sub')), record['source'], number))
                if cursor.rowcount == 0: issue('duplicate-row'); return
                address = _text(v.get('address'))
                if kind == 'sms' and _address(address):
                    connection.execute('INSERT INTO participants VALUES(?,?,?,?)',
                        (message_id, address, _address(address), 'sender' if direction == 'incoming' else 'recipient' if direction == 'outgoing' else 'unknown'))
            parse(record, message)
        tell('attachments', 'Joining MMS text parts, attachment references, and address roles')
        def part(number, v, extra):
            key, parent = _text(v.get('_id')), _text(v.get('mid'))
            if not key or not parent: issue('invalid-row'); return
            message_id = _id(str(latest) + '|mms|' + parent)
            if not connection.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone(): return
            mime, _ = _mime(v.get('ct'))
            name = _text(v.get('fn') or v.get('name') or v.get('cl'))
            text = _text(v.get('text')) if _text(v.get('ct')).lower().startswith('text/') else ''
            reference = extra.get('attachment')
            media_id = attachment(reference, mime, name) if isinstance(reference, dict) else None
            if not media_id and extra.get('attachmentRequestedFile') and not isinstance(reference, dict): issue('missing-attachment')
            try: sequence = int(v.get('seq', number))
            except (ValueError, TypeError): sequence = number
            reference_source = extra.get('attachmentRequestedFile') or (reference.get('file') if isinstance(reference, dict) else '')
            connection.execute('INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?,?,?,?,?)',
                (_id(str(latest) + '|part|' + key), message_id, sequence, text, mime, name, media_id, int(media_id is not None), _text(reference_source)))
        category('mms_parts', part, True)
        for count, (relative, record) in enumerate(selected.items(), 1):
            if not relative.startswith('mms-addresses/') or not relative.endswith('.jsonl'): continue
            if count % 500 == 0: tell('addresses', 'Joining verified MMS address files')
            def address_row(number, v, extra):
                parent, address = _text(v.get('msg_id')), _text(v.get('address'))
                if not parent or not _address(address): return
                message_id = _id(str(latest) + '|mms|' + parent)
                if not connection.execute('SELECT 1 FROM messages WHERE id=?', (message_id,)).fetchone(): return
                role = {'137': 'from', '151': 'to', '130': 'cc', '129': 'bcc'}.get(_text(v.get('type')), 'unknown')
                connection.execute('INSERT INTO participants VALUES(?,?,?,?)', (message_id, address, _address(address), role))
            parse(record, address_row)
        for row in connection.execute("SELECT id FROM messages WHERE kind='mms'").fetchall():
            body = '\n'.join(p[0] for p in connection.execute('SELECT text FROM parts WHERE message_id=? AND text<>\'\' ORDER BY sequence,id', (row[0],)))
            connection.execute('UPDATE messages SET body=? WHERE id=?', (body, row[0]))
        tell('media', 'Indexing saved photo, video, and audio references')
        for row in connection.execute('SELECT * FROM files').fetchall():
            _check(cancel)
            if PurePosixPath(row['source']).suffix.lower() in MIME and not connection.execute('SELECT 1 FROM media WHERE id=?', (row['id'],)).fetchone(): media(row)
        tell('threads', 'Preparing searchable conversations')
        for row in connection.execute('SELECT thread_id,android_thread,COUNT(*) AS count FROM messages GROUP BY thread_id').fetchall():
            _check(cancel)
            participants = {}
            for value in connection.execute('SELECT DISTINCT p.address,p.normalized FROM participants p JOIN messages m ON m.id=p.message_id WHERE m.thread_id=?', (row['thread_id'],)):
                participants[value['normalized']] = {'address': value['address'], 'name': _name(connection, value['normalized'])}
            meta = connection.execute('SELECT recipients FROM thread_meta WHERE id=?', (row['android_thread'],)).fetchone()
            if meta:
                for recipient in meta[0].split():
                    found = connection.execute('SELECT address FROM canonical WHERE id=?', (recipient,)).fetchone()
                    if found and _address(found[0]): participants[_address(found[0])] = {'address': found[0], 'name': _name(connection, _address(found[0]))}
            people = list(participants.values())
            title = ', '.join(person['name'] or person['address'] for person in people) or 'Unknown conversation'
            last = connection.execute('SELECT date_ms,date_iso,body,subject FROM messages WHERE thread_id=? ORDER BY date_ms IS NULL,date_ms DESC,id DESC LIMIT 1', (row['thread_id'],)).fetchone()
            connection.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                (row['thread_id'], row['android_thread'], title, row['count'], last['date_ms'], last['date_iso'], (last['body'] or last['subject'])[:200], json.dumps(people, ensure_ascii=True)))
        counts = {'manifestRecords': manifest_count, 'validatedFiles': len(validated),
                  'missingAttachments': issues['missing-attachment'], 'issues': sum(issues.values())}
        for key in ('messages', 'threads', 'contacts', 'media'): counts[key] = connection.execute('SELECT COUNT(*) FROM ' + key).fetchone()[0]
        for key in ('sms', 'mms'): counts[key] = connection.execute('SELECT COUNT(*) FROM messages WHERE kind=?', (key,)).fetchone()[0]
        for key, kind in [('photos', 'photo'), ('videos', 'video'), ('audio', 'audio'), ('otherMedia', 'other')]:
            counts[key] = connection.execute('SELECT COUNT(*) FROM media WHERE kind=?', (kind,)).fetchone()[0]
        connection.commit(); connection.close(); connection = None
        if _identity(manifest_path.stat()) != before: raise ReaderError('Manifest changed during indexing')
        result.update(state='partial' if issues else 'ready', status='partial' if issues else 'complete', finishedAt=_now(), counts=counts,
                      issues=[{'code': code, 'count': count, 'detail': ISSUES.get(code, code)} for code, count in sorted(issues.items()) if count])
        _json_new(folder / 'receipt.json', result)
        pointer = _safe(base, 'current-' + uuid.uuid4().hex + '.tmp')
        _json_new(pointer, {'generation': generation})
        os.replace(pointer, _safe(base, 'current.json'))
        tell('complete', 'Archive reader index is ready')
        return result
    except BaseException as error:
        if connection is not None: connection.close()
        result.update(state='failed', status='cancelled' if isinstance(error, Cancelled) else 'failed', finishedAt=_now(), errorType=type(error).__name__)
        if not (folder / 'receipt.json').exists():
            try: _json_new(folder / 'receipt.json', result)
            except OSError: pass
        error.reader_result = result
        raise


def _name(connection, normalized):
    names = connection.execute('SELECT DISTINCT name FROM names WHERE normalized=? AND name<>\'\' LIMIT 2', (normalized,)).fetchall()
    return names[0][0] if len(names) == 1 else None


def _stale(root, receipt):
    try: return receipt.get('manifestStatIdentity') != _identity(_safe(root, 'manifest.jsonl').stat())
    except OSError: return True


def _generation(root, check_fresh=True):
    root = _safe(root)
    pointer = _safe(root, 'viewer-data/current.json')
    if not pointer.is_file(): raise ReaderNotReady('Archive reader index is not built')
    if pointer.stat().st_size > 4096: raise ReaderError('Invalid reader pointer')
    generation = json.loads(pointer.read_text(encoding='utf-8')).get('generation')
    if not isinstance(generation, str) or not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}', generation): raise ReaderError('Invalid reader generation')
    folder = _safe(root, 'viewer-data/' + generation)
    if check_fresh:
        receipt = _safe(folder, 'receipt.json')
        if receipt.stat().st_size > 1024 ** 2: raise ReaderError('Reader receipt exceeds limit')
        if _stale(root, json.loads(receipt.read_text(encoding='utf-8'))): raise ReaderStale('Archive changed; rebuild the reader index')
    return folder


def status(root):
    try: folder = _generation(root, check_fresh=False)
    except ReaderNotReady: return {'state': 'missing', 'counts': {}, 'issues': [], 'limitations': LIMITATIONS}
    receipt = _safe(folder, 'receipt.json')
    if receipt.stat().st_size > 1024 ** 2: raise ReaderError('Reader receipt exceeds limit')
    result = json.loads(receipt.read_text(encoding='utf-8'))
    if _stale(root, result): result.update(state='stale', message='Archive changed; rebuild the reader index')
    return result


overview = status


@contextmanager
def _connect(root):
    path = _safe(_generation(root), 'index.sqlite')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA trusted_schema=OFF'); connection.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 15
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try: yield connection
    finally: connection.close()


def _page(q, offset, limit):
    q = _text(q).strip()
    if len(q) > 200: raise ReaderError('Search exceeds 200 characters')
    try: offset, limit = int(offset), int(limit)
    except (ValueError, TypeError): raise ReaderError('Invalid pagination')
    if offset < 0 or offset > MAX_ROWS or limit < 1: raise ReaderError('Invalid pagination')
    return q, offset, min(limit, 200)


def _like(value): return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _paged(items, total, offset, limit): return {'items': items, 'total': total, 'offset': offset, 'limit': limit, 'hasMore': offset + len(items) < total}


def list_threads(root, q='', offset=0, limit=50):
    q, offset, limit = _page(q, offset, limit)
    where, args = '', []
    if q:
        where = " WHERE title LIKE ? ESCAPE '\\' OR participants LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM messages m WHERE m.thread_id=threads.id AND (m.body LIKE ? ESCAPE '\\' OR m.subject LIKE ? ESCAPE '\\'))"
        args = [_like(q)] * 4
    with _connect(root) as connection:
        total = connection.execute('SELECT COUNT(*) FROM threads' + where, args).fetchone()[0]
        items = []
        for row in connection.execute('SELECT * FROM threads' + where + ' ORDER BY last_ms IS NULL,last_ms DESC,id LIMIT ? OFFSET ?', args + [limit, offset]):
            items.append({'id': row['id'], 'androidThreadId': row['android_id'], 'title': row['title'], 'participants': json.loads(row['participants']),
                          'messageCount': row['message_count'], 'count': row['message_count'], 'lastDateMs': row['last_ms'], 'lastDateIso': row['last_iso'], 'lastTimestamp': row['last_iso'], 'snippet': row['snippet']})
        return _paged(items, total, offset, limit)


def _media_row(row):
    return {'id': row['id'], 'kind': row['kind'], 'contentType': row['mime'], 'mime': row['mime'], 'name': row['name'],
            'downloadName': _download_name(row['name'], row['mime']),
            'size': row['size'], 'source': row['source'], 'sha256': row['sha256'], 'date': None}


def _download_name(name, mime):
    name = name.replace('\\', '/').rsplit('/', 1)[-1] or 'attachment'
    suffix = PurePosixPath(name).suffix.lower()
    extension = {value: key for key, value in MIME.items()}.get(mime)
    extension = {'image/jpeg': '.jpg', 'video/mp4': '.mp4', 'audio/ogg': '.ogg'}.get(mime, extension)
    if extension and suffix in ('', '.bin'):
        return (name[:-4] if suffix == '.bin' else name) + extension
    return name


def list_messages(root, thread='', q='', offset=0, limit=100):
    q, offset, limit = _page(q, offset, limit)
    if thread and not HEX_ID.fullmatch(thread): raise ReaderError('Invalid thread ID')
    clauses, args = [], []
    if thread: clauses.append('thread_id=?'); args.append(thread)
    if q: clauses.append("(body LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')"); args.extend([_like(q)] * 2)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with _connect(root) as connection:
        total = connection.execute('SELECT COUNT(*) FROM messages' + where, args).fetchone()[0]
        items = []
        for row in connection.execute('SELECT * FROM messages' + where + ' ORDER BY date_ms IS NULL,date_ms,id LIMIT ? OFFSET ?', args + [limit, offset]):
            people = [{'address': p['address'], 'name': _name(connection, p['normalized']), 'role': p['role']}
                      for p in connection.execute('SELECT DISTINCT address,normalized,role FROM participants WHERE message_id=?', (row['id'],))]
            attachments = []
            for part in connection.execute('SELECT * FROM parts WHERE message_id=? ORDER BY sequence,id', (row['id'],)):
                if part['media_id']:
                    media = connection.execute('SELECT m.*,f.size,f.source,f.sha256 FROM media m JOIN files f ON f.id=m.id WHERE m.id=?', (part['media_id'],)).fetchone()
                    if media: attachments.append({**_media_row(media), 'available': True, 'sequence': part['sequence']})
                elif part['source']:
                    attachments.append({'id': None, 'available': False, 'name': part['name'] or 'Unavailable attachment',
                                        'contentType': part['mime'], 'mime': part['mime'], 'kind': _mime(part['mime'])[1], 'size': None, 'sequence': part['sequence']})
            items.append({'id': row['id'], 'threadId': row['thread_id'], 'kind': row['kind'], 'androidId': row['android_id'], 'box': row['box'],
                          'direction': row['direction'], 'dateRaw': row['date_raw'], 'dateMs': row['date_ms'], 'dateIso': row['date_iso'], 'timestamp': row['date_iso'],
                          'body': row['body'], 'text': row['body'], 'subject': row['subject'], 'participants': people, 'attachments': attachments,
                          'source': row['source'], 'sourceRow': row['row_number']})
        return _paged(items, total, offset, limit)


def list_media(root, q='', kind='', offset=0, limit=60):
    q, offset, limit = _page(q, offset, limit)
    if kind and kind not in ('photo', 'video', 'audio', 'other'): raise ReaderError('Invalid media kind')
    clauses, args = [], []
    if kind: clauses.append('m.kind=?'); args.append(kind)
    if q: clauses.append("(m.name LIKE ? ESCAPE '\\' OR f.source LIKE ? ESCAPE '\\')"); args.extend([_like(q)] * 2)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with _connect(root) as connection:
        total = connection.execute('SELECT COUNT(*) FROM media m JOIN files f ON f.id=m.id' + where, args).fetchone()[0]
        rows = connection.execute('SELECT m.*,f.size,f.source,f.sha256 FROM media m JOIN files f ON f.id=m.id' + where + ' ORDER BY m.name,m.id LIMIT ? OFFSET ?', args + [limit, offset])
        return _paged([_media_row(row) for row in rows], total, offset, limit)


def lookup_media(root, media_id):
    if not isinstance(media_id, str) or not HEX_ID.fullmatch(media_id): raise ReaderError('Invalid media ID')
    with _connect(root) as connection:
        row = connection.execute('SELECT m.*,f.local_path,f.size,f.source,f.sha256 FROM media m JOIN files f ON f.id=m.id WHERE m.id=?', (media_id,)).fetchone()
        if row is None: raise ReaderError('Media file not found')
        record = dict(row)
    path, info = _file(root, record); identity = _identity(info)
    cache_key = (str(path), record['sha256'], *identity)
    with _cache_lock: cached_identity = _hash_cache.get(cache_key)
    verified = False
    if cached_identity is not None:
        with path.open('rb') as stream:
            opened = _identity(os.fstat(stream.fileno()))
            if opened[:4] != identity[:4] or _identity(path.stat()) != identity: raise ReaderError('Media changed before opening')
            # The cached path stat can keep its creation time and restored mtime
            # on Windows. A changed handle ctime must trigger fresh hashing.
            verified = opened == cached_identity
    if not verified:
        with _verified(root, record) as stream: opened = _identity(os.fstat(stream.fileno()))
        with _cache_lock:
            if len(_hash_cache) > 4096: _hash_cache.clear()
            _hash_cache[cache_key] = opened
    return {**_media_row(record), 'path': path, 'localPath': record['local_path'], 'statIdentity': opened}
