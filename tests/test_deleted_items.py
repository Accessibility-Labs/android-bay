import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adb import Cancelled
import deleted_items as deleted


class DeletedItemsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'archive'
        self.root.mkdir()
        self.records = []

    def add(self, source, data=b'original data', local=None):
        local = local or f'copied/file-{len(self.records)}.bin'
        path = self.root / local
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        record = dict(source=source, localPath=local, size=len(data), sha256=hashlib.sha256(data).hexdigest(), status='copied', acquiredAt='2026-10-05T00:00:00+00:00')
        self.records.append(record)
        self.manifest()
        return record

    def manifest(self):
        (self.root / 'manifest.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.records), encoding='utf-8')

    def messages(self, filename, rows):
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode('utf-8')
        return self.add('/storage/emulated/0/AndroidRescue/exports/run1/' + filename, raw)

    def index(self):
        return [json.loads(line) for line in (self.root / 'deleted-items/index.jsonl').read_text().splitlines()]

    def test_exact_path_markers_not_generic_deleted_names(self):
        for source in ('/sdcard/.Trash/photo.jpg', '/storage/a/.Trashes/501/photo.jpg', '/sdcard/.RecycleBin/x', '/sdcard/recycle_bin/x', '/sdcard/.Trash-1000/files/x', '/sdcard/DCIM/.trashed-1700000000-photo.jpg', '/sdcard/DCIM/.TRASHED-1-X.jpg'):
            with self.subTest(source=source):
                self.assertIsNotNone(deleted.trash_reason(source))
        for source in ('/sdcard/deleted-photo.jpg', '/sdcard/not-deleted/x', '/sdcard/DCIM/.pending-1700000000-photo.jpg', '/sdcard/.trashed-not-a-time-file.jpg', '/sdcard/.trashed-123-', '/sdcard/.Trash', '/sdcard/../.Trash/x'):
            with self.subTest(source=source):
                self.assertIsNone(deleted.trash_reason(source))

    def test_verified_copy_is_idempotent_and_original_preserved(self):
        record = self.add('/sdcard/.Trash/photo.jpg', b'photo bytes')
        original = self.root / record['localPath']
        before = original.stat().st_mtime_ns
        first = deleted.check_archive(self.root)
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(first['counts']['fileCopiesSaved'], 1)
        row = self.index()[0]
        candidate = self.root / row['candidatePath']
        candidate_time = candidate.stat().st_mtime_ns
        self.assertEqual(candidate.read_bytes(), original.read_bytes())
        self.records.append(dict(record))
        self.manifest()
        second = deleted.check_archive(self.root)
        self.assertEqual(second['counts']['fileCandidates'], 1)
        self.assertEqual(candidate.stat().st_mtime_ns, candidate_time)
        self.assertEqual(original.stat().st_mtime_ns, before)
        self.assertEqual(len(list((self.root / 'deleted-items/files').rglob('*.jpg'))), 1)
        self.assertIn('permanently deleted', Path(second['reportPath']).read_text())

    def test_bad_original_hash_is_reported_without_copying(self):
        record = self.add('/sdcard/.Trash/x.jpg', b'original')
        (self.root / record['localPath']).write_bytes(b'modified')
        result = deleted.check_archive(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['fileCopiesSaved'], 0)
        self.assertEqual(result['counts']['issues'], 1)
        self.assertEqual((self.root / record['localPath']).read_bytes(), b'modified')

    def test_existing_modified_candidate_is_never_overwritten(self):
        self.add('/sdcard/.Trash/x.jpg', b'original')
        deleted.check_archive(self.root)
        candidate = self.root / self.index()[0]['candidatePath']
        candidate.write_bytes(b'user changed derived copy')
        result = deleted.check_archive(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(candidate.read_bytes(), b'user changed derived copy')
        self.assertEqual(result['counts']['fileCopiesSaved'], 0)

    def test_only_explicit_truthy_message_flags_preserve_full_raw_rows(self):
        rows = [
            {'values': {'_id': 9007199254740993, 'body': 'hello Ω\nnext', 'deleted': 1, 'type': 1}, 'export': {'all': ['preserved']}},
            {'values': {'_id': 2, 'is_trashed': 'true'}},
            {'values': {'_id': 3, 'deleted': 0, 'type': 6, 'read': 0, 'deletable': True}},
            {'values': {'_id': 4, 'archived': 1, 'msg_box': 4, 'seen': 0}},
            {'values': {'_id': 5, 'deleted': 2}},
            {'values': {'_id': 6, 'deleted': {'$error': 'unreadable'}}},
        ]
        self.messages('sms.jsonl', rows)
        self.messages('mms.jsonl', [{'values': {'_id': 7, 'is_deleted': True}, 'export': {'addresses': 'mms-addresses/message-7.jsonl'}}])
        self.messages('mms_parts.jsonl', [{'values': {'deleted': 1, 'text': 'not a message record'}}])
        self.add('/sdcard/Downloads/sms.jsonl', b'{"values":{"deleted":1}}\n')
        result = deleted.check_archive(self.root)
        self.assertEqual(result['counts']['messageFilesExamined'], 2)
        self.assertEqual(result['counts']['messageRowsExamined'], 7)
        self.assertEqual(result['counts']['messageCandidates'], 3)
        messages = [json.loads(line) for line in Path(result['messagesPath']).read_text().splitlines()]
        self.assertEqual(messages[0]['row'], rows[0])
        self.assertEqual(json.loads(messages[0]['rawJson']), rows[0])
        self.assertTrue(messages[0]['rawJson'].endswith('\n'))
        self.assertEqual(messages[0]['line'], 1)
        self.assertEqual(messages[2]['kind'], 'mms')
        self.assertEqual(messages[2]['row']['export']['addresses'], 'mms-addresses/message-7.jsonl')
        saved = Path(result['messagesPath']).read_bytes()
        again = deleted.check_archive(self.root)
        self.assertEqual(Path(again['messagesPath']).read_bytes(), saved)
        self.assertEqual(again['counts']['messageCandidates'], 3)
        self.assertFalse((self.root / 'deleted-items/previous').exists())

    def test_unsafe_manifest_paths_are_not_read(self):
        external = self.root.parent / 'outside.txt'
        external.write_bytes(b'secret')
        for local in ('../outside.txt', str(external), 'C:\\outside.txt', 'file.txt:ads'):
            self.records.append(dict(source='/sdcard/.Trash/x', localPath=local, size=6, sha256=hashlib.sha256(b'secret').hexdigest(), status='copied'))
        self.manifest()
        result = deleted.check_archive(self.root)
        self.assertEqual(result['counts']['fileCopiesSaved'], 0)
        self.assertEqual(result['counts']['issues'], 4)
        self.assertEqual(external.read_bytes(), b'secret')

    def test_source_and_output_symlinks_are_refused(self):
        record = self.add('/sdcard/.Trash/photo.jpg')
        original = self.root / record['localPath']
        outside = self.root.parent / 'outside'
        outside.write_bytes(original.read_bytes())
        original.unlink()
        try:
            original.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Host does not permit test symlinks')
        result = deleted.check_archive(self.root)
        self.assertEqual(result['counts']['fileCopiesSaved'], 0)
        self.assertEqual(result['counts']['issues'], 1)
        (self.root / 'deleted-items/report.json').unlink()
        (self.root / 'deleted-items/report.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            deleted.check_archive(self.root)
        self.assertEqual(outside.read_bytes(), b'original data')

    def test_cancellation_preserves_originals_and_previous_reports(self):
        self.add('/sdcard/.Trash/x.jpg', b'original')
        old = deleted.check_archive(self.root)
        before = Path(old['reportPath']).read_bytes()
        event = threading.Event()
        def progress(*args):
            event.set()
        with self.assertRaises(Cancelled):
            deleted.check_archive(self.root, cancel=event, progress=progress)
        self.assertEqual(Path(old['reportPath']).read_bytes(), before)
        self.assertEqual((self.root / self.records[0]['localPath']).read_bytes(), b'original')
        self.assertFalse(list((self.root / 'deleted-items').glob('.tmp-*')))

    def test_message_row_bound_and_malformed_rows_are_reported(self):
        self.messages('sms.jsonl', [{'values': {'deleted': 1, 'body': 'x' * 100}}])
        with mock.patch.object(deleted, 'MAX_MESSAGE_LINE', 50):
            result = deleted.check_archive(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['messageCandidates'], 0)
        self.assertEqual(Path(result['messagesPath']).read_text(), '')
        self.assertIn('bounded parsing limit', self.index()[0]['reason'])

    def test_message_change_after_first_verification_withholds_rows(self):
        record = self.messages('sms.jsonl', [{'values': {'deleted': 1, 'body': 'old'}}])
        verify = deleted._verified
        def changed(*args):
            result = verify(*args)
            (self.root / record['localPath']).write_bytes(b'{"values":{"deleted":1,"body":"new"}}\n')
            return result
        with mock.patch.object(deleted, '_verified', side_effect=changed):
            result = deleted.check_archive(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['messageCandidates'], 0)
        self.assertEqual(Path(result['messagesPath']).read_text(), '')

    def test_changed_findings_preserve_prior_message_rows_and_other_children(self):
        record = self.messages('sms.jsonl', [{'values': {'deleted': 1, 'body': 'preserve me'}}])
        first = deleted.check_archive(self.root)
        saved = Path(first['messagesPath']).read_bytes()
        sibling = self.root / 'deleted-items/phone-trash/independent.txt'
        sibling.parent.mkdir()
        sibling.write_bytes(b'other module output')
        (self.root / record['localPath']).write_bytes(b'broken')
        second = deleted.check_archive(self.root)
        self.assertEqual(second['status'], 'partial')
        self.assertEqual((Path(second['previousFindingsFolder']) / 'messages.jsonl').read_bytes(), saved)
        self.assertEqual(sibling.read_bytes(), b'other module output')
        self.assertEqual(Path(second['messagesPath']).read_text(), '')
        third = deleted.check_archive(self.root)
        self.assertEqual(len(list((self.root / 'deleted-items/previous').iterdir())), 1)
        self.assertEqual(third['counts']['messageCandidates'], 0)

    def test_malformed_manifest_rows_are_visible_issues(self):
        self.records.append({'status': 'copied', 'source': None, 'localPath': 'missing.bin'})
        self.manifest()
        with (self.root / 'manifest.jsonl').open('a', encoding='utf-8') as output:
            output.write('{broken json}\n')
        result = deleted.check_archive(self.root)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts']['issues'], 2)
        self.assertEqual(result['counts']['fileCopiesSaved'], 0)

    def test_empty_archive_and_html_escaping(self):
        self.manifest()
        result = deleted.check_archive(self.root)
        self.assertEqual(result['counts']['fileCandidates'], 0)
        self.assertIn('Zero findings do not prove', Path(result['reportPath']).read_text())
        self.add('/sdcard/.Trash/<script>alert(1)</script>.jpg')
        result = deleted.check_archive(self.root)
        report = Path(result['reportPath']).read_text()
        self.assertNotIn('<script>alert(1)</script>', report)
        self.assertIn('&lt;script&gt;', report)


if __name__ == '__main__':
    unittest.main()
