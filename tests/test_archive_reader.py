"""Synthetic reader fixtures; no real acquisition or personal records."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import archive_reader as reader
from adb import Cancelled


RUN = '20260102T030405Z-1234abcd'
PREFIX = '/storage/emulated/0/AndroidRescue/exports/' + RUN + '/'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')


def build_fixture(root):
    """Reusable, clearly synthetic UI/API fixture; returns original-path records."""
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    records = {}
    def file(source, content):
        local = 'originals/copy-%04d.bin' % len(records)
        path = root / local; path.parent.mkdir(exist_ok=True); path.write_bytes(content)
        record = {'source': source, 'localPath': local, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
                  'status': 'copied', 'category': 'helper-exports'}
        records[source] = record
        return {'file': source.removeprefix(PREFIX), 'bytes': len(content), 'sha256': record['sha256']}
    def rows(name, values):
        return file(PREFIX + name + '.jsonl', ''.join(json.dumps(value) + '\n' for value in values).encode())
    def row(values, extra=None): return {'values': values, **({'export': extra} if extra else {})}
    photo = file(PREFIX + 'mms-attachments/part-12.bin', PNG)
    rows('contacts', [row({'_id': 1, 'display_name': 'Synthetic Alice'})])
    rows('contact_data', [row({'contact_id': 1, 'mimetype': 'vnd.android.cursor.item/phone_v2', 'data1': '+1 (555) 010-0001', 'data4': '+15550100001'})])
    rows('message_addresses', [row({'_id': 7, 'address': '+15550100001'})])
    rows('message_threads', [row({'_id': 9, 'recipient_ids': '7'})])
    rows('sms', [row({'_id': 1, 'thread_id': 9, 'date': 1700000000123, 'type': 1,
                      'address': '+15550100001', 'body': 'Synthetic hello <script> & 100%_ marker'}),
                 row({'_id': 3, 'thread_id': 10, 'date': 1700000002000, 'type': 2,
                      'address': '5550100002', 'body': 'Synthetic separate conversation'})])
    rows('mms', [row({'_id': 2, 'thread_id': 9, 'date': 1700000001, 'msg_box': 2, 'sub': 'Synthetic subject'},
                     {'addresses': 'mms-addresses/message-2.jsonl'})])
    rows('mms_parts', [row({'_id': 11, 'mid': 2, 'seq': 2, 'ct': 'text/plain', 'text': 'Second synthetic part'}),
                       row({'_id': 10, 'mid': 2, 'seq': 1, 'ct': 'text/plain', 'text': 'First synthetic part'}),
                       row({'_id': 12, 'mid': 2, 'seq': 3, 'ct': 'image/png', 'fn': 'synthetic.png'},
                           {'attachment': photo, 'attachmentRequestedFile': photo['file']})])
    rows('mms-addresses/message-2', [row({'msg_id': 2, 'address': '+15550100001', 'type': 151}),
                                     row({'msg_id': 2, 'address': 'insert-address-token', 'type': 137})])
    file(PREFIX + 'report.json', json.dumps({'runFinished': True, 'status': 'complete'}).encode())
    file('/storage/emulated/0/DCIM/synthetic-photo.png', PNG)
    file('/storage/emulated/0/Movies/synthetic-video.mp4', b'\x00\x00\x00\x18ftypmp42SYNTHETIC')
    file('/storage/emulated/0/Music/synthetic-audio.mp3', b'ID3SYNTHETIC')
    (root / 'manifest.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records.values()), encoding='utf-8')
    return records


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.records = build_fixture(self.root)

    def tearDown(self): self.temp.cleanup()

    def rewrite(self, suffix, content):
        source = PREFIX + suffix
        record = self.records[source]
        (self.root / record['localPath']).write_bytes(content)
        record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        (self.root / 'manifest.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in self.records.values()), encoding='utf-8')

    def test_sms_mms_threads_dates_parts_contact_roles_and_bin_media(self):
        result = reader.build_index(self.root)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['counts']['messages'], 3)
        self.assertEqual(result['counts']['threads'], 2)
        self.assertEqual(result['counts']['photos'], 2)
        self.assertEqual(result['counts']['videos'], 1)
        thread = reader.list_threads(self.root, q='Synthetic Alice')['items'][0]
        self.assertEqual(thread['messageCount'], 2)
        messages = reader.list_messages(self.root, thread=thread['id'])['items']
        self.assertEqual([row['kind'] for row in messages], ['sms', 'mms'])
        self.assertEqual(messages[0]['dateMs'], 1700000000123)
        self.assertEqual(messages[1]['dateMs'], 1700000001000)
        self.assertEqual(messages[1]['dateRaw'], '1700000001')
        self.assertEqual(messages[1]['body'], 'First synthetic part\nSecond synthetic part')
        self.assertEqual(messages[1]['participants'], [{'address': '+15550100001', 'name': 'Synthetic Alice', 'role': 'to'}])
        attachment = messages[1]['attachments'][0]
        self.assertEqual(attachment['mime'], 'image/png')
        self.assertEqual(attachment['kind'], 'photo')
        looked = reader.lookup_media(self.root, attachment['id'])
        self.assertEqual(looked['path'].read_bytes(), PNG)
        self.assertEqual(len(looked['statIdentity']), 5)

    def test_unknown_dates_retained_without_guessing(self):
        self.rewrite('sms.jsonl', (json.dumps({'values': {'_id': 1, 'thread_id': 9, 'date': 'not-a-date', 'body': 'Synthetic', 'type': 1}}) + '\n').encode())
        result = reader.build_index(self.root)
        self.assertEqual(result['state'], 'partial')
        message = reader.list_messages(self.root, q='Synthetic')['items'][-1]
        self.assertIsNone(message['dateIso']); self.assertEqual(message['dateRaw'], 'not-a-date')

    def test_unverified_json_export_is_excluded(self):
        record = self.records[PREFIX + 'sms.jsonl']
        path = self.root / record['localPath']; content = path.read_bytes(); path.write_bytes(content.replace(b'Synthetic hello', b'CORRUPTED hello'))
        result = reader.build_index(self.root)
        self.assertEqual(result['counts']['sms'], 0)
        self.assertEqual(result['counts']['mms'], 1)
        self.assertTrue(any(issue['code'] == 'invalid-export' for issue in result['issues']))

    def test_missing_or_mismatched_attachment_does_not_get_media_id(self):
        record = self.records[PREFIX + 'mms-attachments/part-12.bin']
        (self.root / record['localPath']).unlink()
        result = reader.build_index(self.root)
        self.assertEqual(result['counts']['missingAttachments'], 1)
        mms = [row for row in reader.list_messages(self.root)['items'] if row['kind'] == 'mms'][0]
        self.assertFalse(mms['attachments'][0]['available'])
        self.assertIsNone(mms['attachments'][0]['id'])

    def test_media_hash_checked_on_open_and_cache_invalidated_by_change(self):
        reader.build_index(self.root)
        media = reader.list_media(self.root, kind='video')['items'][0]
        good = reader.lookup_media(self.root, media['id'])
        path = good['path']; data = path.read_bytes(); path.write_bytes(b'X' + data[1:])
        with self.assertRaises(reader.ReaderError): reader.lookup_media(self.root, media['id'])

    def test_media_cache_rechecks_changed_bytes_even_when_mtime_restored(self):
        reader.build_index(self.root)
        media = reader.list_media(self.root, kind='video')['items'][0]
        good = reader.lookup_media(self.root, media['id'])
        path = good['path']; original = path.stat()
        data = path.read_bytes(); path.write_bytes(b'X' + data[1:])
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(path.stat().st_size, original.st_size)
        self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
        with self.assertRaises(reader.ReaderError): reader.lookup_media(self.root, media['id'])

    def test_ambiguous_contact_names_remain_unknown(self):
        self.rewrite('contact_data.jsonl', ''.join(json.dumps({'values': {'contact_id': i, 'mimetype': 'vnd.android.cursor.item/phone_v2',
                     'data1': '+15550100001', 'display_name': name}}) + '\n' for i, name in [(1, 'Synthetic Alice'), (2, 'Synthetic Bob')]).encode())
        reader.build_index(self.root)
        thread = reader.list_threads(self.root, q='+15550100001')['items'][0]
        self.assertIsNone(thread['participants'][0]['name'])

    def test_unsafe_manifest_paths_are_excluded_without_opening_external_file(self):
        with (self.root / 'manifest.jsonl').open('a', encoding='utf-8') as stream:
            for path in ['../outside.mp4', 'C:/outside.mp4', '/outside.mp4', 'file:stream.mp4']:
                stream.write(json.dumps({'source': '/sdcard/hostile.mp4', 'localPath': path, 'size': 1, 'sha256': '0' * 64, 'status': 'copied'}) + '\n')
        result = reader.build_index(self.root)
        self.assertEqual(next(issue['count'] for issue in result['issues'] if issue['code'] == 'manifest-row'), 4)
        self.assertEqual(result['counts']['videos'], 1)

    def test_parameterized_search_literal_wildcards_and_pagination(self):
        reader.build_index(self.root)
        self.assertEqual(reader.list_threads(self.root, q="' OR 1=1 --")['total'], 0)
        self.assertEqual(reader.list_messages(self.root, q='100%_')['total'], 1)
        self.assertEqual(reader.list_media(self.root, q="' UNION SELECT 1 --")['total'], 0)
        page = reader.list_messages(self.root, limit=1)
        self.assertTrue(page['hasMore']); self.assertEqual(page['total'], 3)
        self.assertEqual(reader.list_messages(self.root, limit=10000)['limit'], 200)
        with self.assertRaises(reader.ReaderError): reader.list_messages(self.root, thread='../bad')
        with self.assertRaises(reader.ReaderError): reader.list_threads(self.root, q='x' * 201)
        with self.assertRaises(reader.ReaderError): reader.list_media(self.root, offset=-1)

    def test_latest_generation_preserves_prior_index_and_cancellation_does_not_publish(self):
        first = reader.build_index(self.root)
        cancel = threading.Event()
        def progress(phase, message):
            if phase == 'messages': cancel.set()
        with self.assertRaises(Cancelled): reader.build_index(self.root, cancel=cancel, progress=progress)
        self.assertEqual(reader.status(self.root)['generation'], first['generation'])
        second = reader.build_index(self.root)
        self.assertNotEqual(second['generation'], first['generation'])
        self.assertTrue(Path(first['receiptPath']).is_file())

    def test_only_latest_helper_run_supplies_messages(self):
        old = dict(self.records[PREFIX + 'sms.jsonl'])
        old['source'] = old['source'].replace(RUN, '20250102T030405Z-1234abcd')
        with (self.root / 'manifest.jsonl').open('a', encoding='utf-8') as stream: stream.write(json.dumps(old) + '\n')
        result = reader.build_index(self.root)
        self.assertEqual(result['helperRun'], RUN)
        self.assertEqual(result['counts']['sms'], 2)

    def test_missing_index_is_explicit(self):
        self.assertEqual(reader.status(self.root)['state'], 'missing')
        with self.assertRaises(reader.ReaderNotReady): reader.list_threads(self.root)

    def test_manifest_change_marks_stale_and_blocks_queries_until_rebuild(self):
        reader.build_index(self.root)
        with (self.root / 'manifest.jsonl').open('a', encoding='utf-8') as stream: stream.write('\n')
        self.assertEqual(reader.status(self.root)['state'], 'stale')
        with self.assertRaises(reader.ReaderStale): reader.list_threads(self.root)

    def test_inline_mms_html_is_preserved_as_literal_text(self):
        body = '<b>Synthetic literal</b><script>not executed</script>'
        self.rewrite('mms_parts.jsonl', (json.dumps({'values': {'_id': 10, 'mid': 2, 'seq': 1, 'ct': 'text/html', 'text': body}}) + '\n').encode())
        reader.build_index(self.root)
        mms = [row for row in reader.list_messages(self.root)['items'] if row['kind'] == 'mms'][0]
        self.assertEqual(mms['body'], body)

    def test_download_extension_from_known_mime_preserves_original_metadata(self):
        self.assertEqual(reader._download_name('part-12.bin', 'video/mp4'), 'part-12.mp4')
        self.assertEqual(reader._download_name('part-12', 'image/jpeg'), 'part-12.jpg')
        self.assertEqual(reader._download_name('original.mov', 'video/mp4'), 'original.mov')
        self.assertEqual(reader._download_name('part.bin', 'application/octet-stream'), 'part.bin')
        reader.build_index(self.root)
        row = reader.list_media(self.root, kind='photo')['items'][0]
        self.assertIn('source', row)
        self.assertIn('downloadName', row)


if __name__ == '__main__': unittest.main()
