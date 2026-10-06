"""Archive checks must not race copying/verification or alter copy receipts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adb import Cancelled
from engine import RescueEngine


class ArchiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = RescueEngine(self.base, self.base / "missing-adb")
        self.root = self.base / "recovery"
        self.root.mkdir()
        (self.root / "original.txt").write_bytes(b"original")
        record = {"status": "copied", "source": "/sdcard/original.txt", "localPath": "original.txt",
                  "size": 8, "sha256": hashlib.sha256(b"original").hexdigest()}
        (self.root / "manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.jid = "a" * 32
        self.job = {"id": self.jid, "serial": "TEST", "status": "partial", "phase": "finished",
                    "message": "Copy done", "destination": str(self.root), "filesCopied": 1, "filesTotal": 1,
                    "bytesCopied": 8, "errors": [], "coverage": [], "startedAt": "2026-01-01T00:00:00Z",
                    "finishedAt": "2026-01-01T01:00:00Z", "reportPath": str(self.root / "report.html"),
                    "options": {"shared": True}, "verification": {"ok": True, "verified": 1, "issues": []}}
        self.engine._jobs[self.jid] = self.job

    def tearDown(self):
        for event in self.engine._cancel.values():
            event.set()
        for thread in self.engine._threads.values():
            thread.join(3)
        self.temp.cleanup()

    def test_analysis_excludes_verify_resume_and_other_analysis(self):
        entered = threading.Event()
        release = threading.Event()
        def operation(job, cancel):
            entered.set()
            release.wait(3)
            job["analysisStatus"] = "complete"
        with patch.object(self.engine, "_archive_checks", operation):
            self.engine.analyze(self.jid)
            self.assertTrue(entered.wait(2))
            self.assertTrue(self.engine.get_job(self.jid)["analysisOnly"])
            for action in (self.engine.analyze, self.engine.verify, self.engine.resume):
                with self.assertRaises(ValueError):
                    action(self.jid)
            release.set()
            self.engine._threads[self.jid].join(3)
        result = self.engine.get_job(self.jid)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["finishedAt"], "2026-01-01T01:00:00Z")
        self.assertEqual(result["filesCopied"], 1)
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual((self.root / "original.txt").read_bytes(), b"original")

    def test_cancellation_restores_transfer_status_and_keeps_original(self):
        entered = threading.Event()
        def operation(job, cancel):
            entered.set()
            cancel.wait(3)
            raise Cancelled("Stopped")
        with patch.object(self.engine, "_archive_checks", operation):
            self.engine.analyze(self.jid)
            self.assertTrue(entered.wait(2))
            self.engine.cancel(self.jid)
            self.engine._threads[self.jid].join(3)
        result = self.engine.get_job(self.jid)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["analysisStatus"], "cancelled")
        self.assertFalse(result["analysisOnly"])
        self.assertEqual((self.root / "original.txt").read_bytes(), b"original")

    def test_parser_failure_does_not_skip_other_check(self):
        good = {"status": "complete", "counts": {"features": 2, "gpsMedia": 1}}
        def fail(*args, **kwargs):
            raise ValueError("Invalid source format")
        modules = {"deleted_items": types.SimpleNamespace(check_archive=fail),
                   "location_data": types.SimpleNamespace(check_archive=lambda *a, **kw: good)}
        with patch.dict(sys.modules, modules), patch.object(self.engine, "devices", return_value=[]):
            self.engine._archive_checks(self.job, threading.Event())
        self.assertEqual(self.job["phoneTrash"]["status"], "unavailable")
        self.assertEqual(self.job["deletedItems"]["status"], "partial")
        self.assertEqual(self.job["locationData"]["counts"]["features"], 2)
        self.assertEqual(self.job["analysisStatus"], "partial")

    def test_verification_is_publicly_busy_and_blocks_analysis(self):
        self.engine._verifying.add(self.jid)
        self.assertEqual(self.engine.get_job(self.jid)["status"], "verifying")
        with self.assertRaises(ValueError):
            self.engine.analyze(self.jid)

    def test_report_failure_cannot_leave_analysis_success(self):
        def complete(job, cancel):
            job["analysisStatus"] = "complete"
        with patch.object(self.engine, "_archive_checks", complete), patch.object(self.engine, "_write_report", side_effect=OSError("disk full")):
            self.engine.analyze(self.jid)
            self.engine._threads[self.jid].join(3)
        result = self.engine.get_job(self.jid)
        self.assertEqual(result["analysisStatus"], "partial")
        self.assertIn("could not be saved", result["message"])
        self.assertEqual(result["status"], "partial")

    def test_interrupted_analysis_restores_copy_status_on_restart(self):
        self.job.update(status="running", statusBeforeAnalysis="completed", analysisOnly=True)
        self.engine._save(self.job, True)
        reopened = RescueEngine(self.base, self.base / "missing-adb")
        result = reopened.get_job(self.jid)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["analysisStatus"], "interrupted")
        self.assertFalse(result["analysisOnly"])


if __name__ == "__main__":
    unittest.main()
