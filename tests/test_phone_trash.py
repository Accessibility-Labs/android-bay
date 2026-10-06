import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import phone_trash as pt
from adb import AdbError, Cancelled


class FakeAdb:
    def devices(self):
        return [{"serial": "fixture-phone", "state": "device"}]


class StreamAdb(FakeAdb):
    def __init__(self, program):
        self.program = program

    def command(self, serial, arguments):
        return [sys.executable, "-c", self.program]

    def process_options(self):
        return {}


class PhoneTrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adb = FakeAdb()
        self.payload = b"\x00binary fixture\xff\n"
        self.row = {"id": "42", "uri": pt.BASE_URI + "/42", "isTrashed": 1,
                    "dateExpires": 1900000000, "size": len(self.payload), "dateModified": 1700000000}

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, payload=None):
        local = self.root / "shared" / "café 雪.bin"
        local.parent.mkdir()
        local.write_bytes(self.payload if payload is None else payload)
        record = {"source": "/storage/emulated/0/Pictures/.trashed-1900000000-café 雪.jpg", "status": "copied",
                  "localPath": str(local.relative_to(self.root)), "size": len(self.payload),
                  "sha256": hashlib.sha256(self.payload).hexdigest(), "acquiredAt": "fixture-time"}
        (self.root / "manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        return record, local

    def execute(self, query=None, path=None, stream=None, cancel=None, progress=None):
        query = query or (lambda *args, **kwargs: [dict(self.row)])
        with patch.object(pt, "_query", side_effect=query), patch.object(pt, "_source_path", return_value=path), patch.object(pt, "_run_stream", side_effect=stream) as streaming:
            result = pt.acquire_trash(self.root, self.adb, "fixture-phone", cancel=cancel, progress=progress)
        return result, streaming

    def report(self, result):
        return json.loads((Path(result["folder"]) / "report.json").read_text(encoding="utf-8"))

    def test_verified_pc_copy_does_not_read_phone_media(self):
        record, original = self.manifest()
        result, streaming = self.execute(path=record["source"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"], {"rows": 1, "filesCopied": 1, "bytesCopied": len(self.payload), "issues": 0})
        streaming.assert_not_called()
        item = self.report(result)["items"][0]
        self.assertEqual(item["method"], "verified-existing-PC-copy")
        self.assertEqual(Path(item["file"]).name, "media-42.jpg")
        self.assertEqual((self.root / item["file"]).read_bytes(), self.payload)
        self.assertEqual(original.read_bytes(), self.payload)
        self.assertTrue(Path(result["reportPath"]).is_file())
        self.assertEqual(Path(result["reportPath"]), self.root / "deleted-items" / "phone-trash" / "report.html")
        self.assertNotIn("items", result)
        self.assertNotIn("issues", result)
        self.assertNotIn("source", result)

    def test_phone_fallback_has_numeric_content_uri_and_exact_bound(self):
        commands = []
        def stream(adb, serial, args, output, limit, cancel, **kwargs):
            commands.append(args)
            self.assertEqual(limit, len(self.payload))
            output.write(self.payload)
            return len(self.payload), hashlib.sha256(self.payload).hexdigest()
        result, streaming = self.execute(stream=stream)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(commands, [["exec-out", "content read --uri content://media/external/file/42"]])
        self.assertEqual((self.root / self.report(result)["items"][0]["file"]).read_bytes(), self.payload)

    def test_tampered_pc_copy_not_reused(self):
        record, original = self.manifest(payload=b"x" * len(self.payload))
        def stream(adb, serial, args, output, limit, cancel, **kwargs):
            output.write(self.payload)
            return len(self.payload), hashlib.sha256(self.payload).hexdigest()
        result, streaming = self.execute(path=record["source"], stream=stream)
        self.assertEqual(result["status"], "complete")
        streaming.assert_called_once()
        self.assertEqual(original.read_bytes(), b"x" * len(self.payload))

    def test_source_changes_preserve_partial_and_do_not_claim_copy(self):
        record, original = self.manifest()
        calls = 0
        def query(*args):
            nonlocal calls
            calls += 1
            changed = dict(self.row)
            if calls == 3:
                changed["isTrashed"] = 0
            return [changed]
        result, _ = self.execute(query=query, path=record["source"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["filesCopied"], 0)
        self.assertTrue((self.root / self.report(result)["items"][0]["partialFile"]).exists())
        self.assertEqual(original.read_bytes(), self.payload)

    def test_repeated_runs_never_replace_each_other(self):
        record, _ = self.manifest()
        first, _ = self.execute(path=record["source"])
        first_html = (Path(first["folder"]) / "report.html").read_bytes()
        second, _ = self.execute(path=record["source"])
        self.assertNotEqual(first["folder"], second["folder"])
        self.assertEqual((self.root / self.report(first)["items"][0]["file"]).read_bytes(), self.payload)
        self.assertEqual((Path(first["folder"]) / "report.html").read_bytes(), first_html)
        stable = Path(second["reportPath"]).read_text(encoding="utf-8")
        self.assertIn(Path(second["folder"]).name + "/report.json", stable)
        self.assertIn(Path(second["folder"]).name + "/media-42.jpg", stable)
        self.assertNotIn(Path(first["folder"]).name, stable)

    def test_permission_denied_is_unavailable_not_zero_success(self):
        result, _ = self.execute(query=lambda *args: (_ for _ in ()).throw(AdbError("permission denied")))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["counts"]["issues"], 1)

    def test_zero_visible_rows_are_documented(self):
        result, _ = self.execute(query=lambda *args: [])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["rows"], 0)
        self.assertTrue(any("does not prove" in x for x in result["limitations"]))

    def test_cancelled_run_preserves_report_without_reads(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(pt, "_query") as query:
            with self.assertRaises(Cancelled):
                self.execute(cancel=cancel)
            query.assert_not_called()
        reports = list((self.root / "deleted-items" / "phone-trash").glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        result = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(Path(result["reportPath"]).is_file())

    def test_progress_callback_receives_stage_and_safe_message(self):
        record, _ = self.manifest()
        messages = []
        self.execute(path=record["source"], progress=lambda stage, message: messages.append((stage, message)))
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(stage == "phone-trash" for stage, _ in messages))
        self.assertTrue(all(record["source"] not in message for _, message in messages))

    def test_html_escapes_provider_errors(self):
        unsafe = '<script>alert("private")</script> & details'
        result, _ = self.execute(query=lambda *args: (_ for _ in ()).throw(AdbError(unsafe)))
        html = Path(result["reportPath"]).read_text(encoding="utf-8")
        self.assertNotIn(unsafe, html)
        self.assertIn('&lt;script&gt;alert(&quot;private&quot;)&lt;/script&gt; &amp; details', html)
        self.assertEqual(self.report(result)["issues"][0]["error"], unsafe)

    def test_cancel_during_copy_preserves_partial_and_raises_after_receipt(self):
        record, _ = self.manifest()
        cancel = threading.Event()
        def copy(source, output, expected, event):
            output.write(b"partial")
            event.set()
            pt._check(event)
        with patch.object(pt, "_copy_pc", side_effect=copy):
            with self.assertRaises(Cancelled):
                self.execute(path=record["source"], cancel=cancel)
        report_path = next((self.root / "deleted-items" / "phone-trash").glob("*/report.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["counts"]["filesCopied"], 0)
        self.assertEqual((self.root / report["items"][0]["partialFile"]).read_bytes(), b"partial")
        self.assertTrue(Path(report["reportPath"]).is_file())

    def test_disk_full_stops_before_next_item_and_propagates(self):
        record, _ = self.manifest()
        second = dict(self.row, id="43", uri=pt.BASE_URI + "/43")
        queried = []
        def query(adb, serial, cancel, item_id=None):
            queried.append(item_id)
            return [self.row, second] if item_id is None else [self.row]
        def copy(source, output, expected, cancel):
            output.write(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")
        with patch.object(pt, "_copy_pc", side_effect=copy):
            with self.assertRaises(OSError) as error:
                self.execute(query=query, path=record["source"])
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(queried, [None, "42"])
        report_path = next((self.root / "deleted-items" / "phone-trash").glob("*/report.json"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"], {"rows": 2, "filesCopied": 0, "bytesCopied": 0, "issues": 1})
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual((self.root / report["items"][0]["partialFile"]).read_bytes(), b"partial")

    def test_windows_disk_full_error_is_fatal(self):
        error = OSError("Disk full")
        error.winerror = 112
        self.assertTrue(pt._out_of_space(error))

    def test_only_simple_original_extensions_are_used(self):
        self.assertEqual(pt._extension("/private/a name.HEIC"), ".heic")
        self.assertEqual(pt._extension("/private/a.1234567890"), ".1234567890")
        for original in (None, "/a.jpg ", "/a.abcdefghijkl", "/a.雪", "/a.jpg/unnamed", "/a.exe:stream"):
            self.assertEqual(pt._extension(original), ".bin")

    def test_unknown_size_refuses_unbounded_capture(self):
        self.row["size"] = None
        result, streaming = self.execute()
        self.assertEqual(result["status"], "partial")
        streaming.assert_not_called()

    def test_query_parser_preserves_extra_colon_and_rejects_untrashed(self):
        line = "Row: 0 _id=42, is_trashed=1, date_expires=1900000000, _size=17, date_modified=1700000000\n"
        with patch.object(pt, "_capture", return_value=line) as capture:
            rows = pt._query(self.adb, "fixture-phone", None)
            self.assertEqual(rows[0]["size"], 17)
            self.assertEqual(capture.call_args.args[2][-1], r"android\:query-arg-match-trashed:i:3")
        with patch.object(pt, "_capture", return_value=line.replace("is_trashed=1", "is_trashed=0")):
            with self.assertRaises(AdbError):
                pt._query(self.adb, "fixture-phone", None)
        with self.assertRaises(ValueError):
            pt._query(self.adb, "fixture-phone", None, "42; rm")

    def test_source_path_newline_and_unicode_are_not_stripped(self):
        value = "/storage/emulated/0/café 雪\n.jpg "
        with patch.object(pt, "_capture", return_value="Row: 0 _data=" + value + "\n"):
            self.assertEqual(pt._source_path(self.adb, "fixture-phone", self.row, None), value)

    def test_stream_bound_enforced_before_writing_excess(self):
        output = io.BytesIO()
        adb = StreamAdb("import sys;sys.stdout.buffer.write(b'a'*200000);sys.stdout.flush()")
        with self.assertRaises(AdbError):
            pt._run_stream(adb, "fixture", [], output, 70000, None)
        self.assertLessEqual(len(output.getvalue()), 70000)

    def test_stream_is_binary_exact(self):
        output = io.BytesIO()
        payload = bytes(range(256)) * 300
        adb = StreamAdb("import sys;sys.stdout.buffer.write(bytes(range(256))*300)")
        size, checksum = pt._run_stream(adb, "fixture", [], output, len(payload), None)
        self.assertEqual(output.getvalue(), payload)
        self.assertEqual(size, len(payload))
        self.assertEqual(checksum, hashlib.sha256(payload).hexdigest())

    def test_stream_cancellation_is_prompt(self):
        cancel = threading.Event()
        timer = threading.Timer(.15, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                pt._run_stream(StreamAdb("import time;time.sleep(30)"), "fixture", [], io.BytesIO(), 1024, cancel)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_manifest_path_cannot_escape_archive(self):
        record = {"size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest(), "localPath": "../outside"}
        self.assertIsNone(pt._pc_source(self.root, record, len(self.payload), None))


if __name__ == "__main__":
    unittest.main()
