"""Exercise context acquisition through the worker without a phone or server."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adb import Cancelled
from engine import RescueEngine


class ContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.engine = RescueEngine(self.base, self.base / "missing-adb")
        # Any unexpected phone operation fails instead of reaching a real ADB.
        self.engine.adb = object()
        self.options = {
            "shared": False, "apks": False, "helper": False, "legacy": False,
            "privateApps": False, "existingRoot": False, "archiveChecks": False,
            "deviceContext": True,
        }
        self.info = {"serial": "SYNTHETIC", "model": "Synthetic phone", "android": "11",
                     "sdk": 30, "roots": [], "properties": {"ro.serialno": "SYNTHETIC"}}
        self.inspect = patch.object(self.engine, "inspect", return_value=self.info)
        self.inspect.start()

    def tearDown(self):
        for event in self.engine._cancel.values():
            event.set()
        for thread in self.engine._threads.values():
            thread.join(5)
        self.inspect.stop()
        self.temp.cleanup()

    def captured_result(self, root, status="complete", issues=0, outputs=None, write_report=True):
        """Use real saved bytes and the capture module's public result schema."""
        root = Path(root)
        folder = root / "device-context" / "synthetic-run"
        folder.mkdir(parents=True)
        if outputs is None:
            outputs = {"usage-current.txt": b"synthetic usage snapshot\n",
                       "setting-auto-time.txt": b"1\n"}
        records = []
        for name, content in outputs.items():
            path = folder / name
            path.write_bytes(content)
            records.append({"source": "device-context:synthetic-run/" + name,
                            "localPath": path.relative_to(root).as_posix(), "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(), "status": "copied",
                            "category": "device-context", "acquiredAt": "2026-01-01T00:00:01Z",
                            "hashScope": "saved-PC-bytes-only"})
        report = folder.parent / "report.html"
        if write_report:
            report.write_text("<!doctype html><title>Synthetic context report</title>", encoding="utf-8")
        copied_bytes = sum(record["size"] for record in records)
        return {"schemaVersion": 1, "status": status, "checkedAt": "2026-01-01T00:00:00Z",
                "finishedAt": "2026-01-01T00:00:01Z", "folder": str(folder), "reportPath": str(report),
                "limitations": ["Synthetic PC snapshot; no phone history is implied."],
                "counts": {"commandsPlanned": len(records) + issues,
                           "commandsAttempted": len(records) + issues,
                           "commandsSucceeded": len(records), "commandsNotAttempted": 0,
                           "filesCopied": len(records), "bytesCopied": copied_bytes,
                           "bytesSaved": copied_bytes, "issues": issues},
                "records": records}

    def wait_for_job(self, job):
        thread = self.engine._threads[job["id"]]
        thread.join(5)
        self.assertFalse(thread.is_alive(), "Synthetic context worker did not finish")
        return self.engine.get_job(job["id"])

    def manifest(self, job):
        path = Path(job["destination"]) / "manifest.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_context_only_worker_registers_captures_and_compact_summary(self):
        captured = {}

        def capture(root, adb, serial, cancel=None, progress=None):
            self.assertIs(adb, self.engine.adb)
            self.assertEqual(serial, "SYNTHETIC")
            self.assertIsInstance(cancel, threading.Event)
            self.assertFalse(cancel.is_set())
            progress("device-context", "Capturing synthetic output")
            captured.update(self.captured_result(root))
            return captured

        with patch("device_context.capture_context", side_effect=capture) as operation:
            started = self.engine.start("SYNTHETIC", str(self.base / "archives"), self.options)
            job = self.wait_for_job(started)

        operation.assert_called_once()
        self.assertTrue(job["options"]["deviceContext"])
        self.assertFalse(any(job["options"][key] for key in self.options if key != "deviceContext"))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["errors"], [])
        self.assertEqual(self.manifest(job), captured["records"])
        self.assertEqual((job["filesCopied"], job["filesTotal"]), (2, 2))
        self.assertEqual(job["bytesCopied"], captured["counts"]["bytesCopied"])
        expected = {key: value for key, value in captured.items() if key != "records"}
        self.assertEqual(job["deviceContext"], expected)
        root = Path(job["destination"])
        for path in (root / "report.json", self.engine.state_dir / (job["id"] + ".json")):
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["deviceContext"], expected)
            self.assertNotIn("records", persisted["deviceContext"])
        self.assertIn('href="device-context/report.html"', (root / "report.html").read_text(encoding="utf-8"))
        receipt = self.engine.verify(job["id"])
        self.assertTrue(receipt["ok"])
        self.assertEqual((receipt["verified"], receipt["total"]), (2, 2))
        self.assertEqual(receipt["issues"], [])

    def test_cancelled_capture_registers_completed_files_before_cancel_status(self):
        entered = threading.Event()
        captured = {}
        registered_at_cancel = []
        original_update = self.engine._update

        def capture(root, adb, serial, cancel=None, progress=None):
            captured.update(self.captured_result(root, status="cancelled", issues=1,
                                               outputs={"usage-current.txt": b"completed snapshot\n"}))
            (Path(captured["folder"]) / "recent-logs.partial").write_bytes(b"unfinished output")
            entered.set()
            if not cancel.wait(5):
                raise AssertionError("Cancellation was not delivered to the context capture")
            exc = Cancelled("Synthetic context cancellation")
            exc.result = captured
            raise exc

        def observe_update(job, **changes):
            if changes.get("status") == "cancelled":
                registered_at_cancel.append({"records": self.manifest(job),
                                             "filesCopied": job["filesCopied"],
                                             "filesTotal": job["filesTotal"],
                                             "bytesCopied": job["bytesCopied"],
                                             "summary": job.get("deviceContext")})
            return original_update(job, **changes)

        with patch("device_context.capture_context", side_effect=capture), \
                patch.object(self.engine, "_update", side_effect=observe_update):
            started = self.engine.start("SYNTHETIC", str(self.base / "archives"), self.options)
            self.assertTrue(entered.wait(3), "Context capture did not start")
            self.engine.cancel(started["id"])
            job = self.wait_for_job(started)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["errors"], [])
        self.assertEqual(len(registered_at_cancel), 1)
        saved = registered_at_cancel[0]
        self.assertEqual(saved["records"], captured["records"])
        self.assertEqual((saved["filesCopied"], saved["filesTotal"]), (1, 1))
        self.assertEqual(saved["bytesCopied"], captured["counts"]["bytesCopied"])
        self.assertEqual(saved["summary"]["status"], "cancelled")
        self.assertNotIn("records", saved["summary"])
        self.assertEqual(self.manifest(job), captured["records"])
        self.assertEqual((Path(captured["folder"]) / "recent-logs.partial").read_bytes(), b"unfinished output")
        self.assertTrue(self.engine.verify(job["id"])["ok"])

    def test_partial_context_marks_transfer_partial_without_transfer_errors(self):
        def capture(root, *args, **kwargs):
            return self.captured_result(root, status="partial", issues=1)

        with patch("device_context.capture_context", side_effect=capture):
            job = self.wait_for_job(self.engine.start("SYNTHETIC", str(self.base / "archives"), self.options))

        self.assertEqual(job["errors"], [])
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["deviceContext"]["status"], "partial")
        self.assertEqual(job["deviceContext"]["counts"]["issues"], 1)
        coverage = {item["category"]: item for item in job["coverage"]}
        self.assertEqual(coverage["Device activity and settings"]["status"], "partial")
        self.assertEqual(job["filesCopied"], 2)
        self.assertEqual(len(self.manifest(job)), 2)

    def test_capture_error_preserves_completed_records_when_receipt_is_unavailable(self):
        captured = {}
        old_report = "<!doctype html><title>Preserved report from an earlier run</title>"

        def capture(root, *args, **kwargs):
            stable_report = Path(root) / "device-context" / "report.html"
            stable_report.parent.mkdir()
            stable_report.write_text(old_report, encoding="utf-8")
            captured.update(self.captured_result(root, status="failed", issues=1, write_report=False))
            captured.update(receiptSaved=False, reportPath=None)
            exc = OSError("Synthetic context receipt write failed")
            exc.result = captured
            raise exc

        with patch("device_context.capture_context", side_effect=capture):
            job = self.wait_for_job(self.engine.start("SYNTHETIC", str(self.base / "archives"), self.options))

        # A failed acquisition with some saved files retains the engine's partial status.
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["phase"], "failed")
        self.assertEqual(job["errors"], ["Synthetic context receipt write failed"])
        self.assertEqual(self.manifest(job), captured["records"])
        self.assertEqual((job["filesCopied"], job["filesTotal"]), (2, 2))
        self.assertEqual(job["bytesCopied"], captured["counts"]["bytesCopied"])
        expected = {key: value for key, value in captured.items() if key != "records"}
        self.assertEqual(job["deviceContext"], expected)
        self.assertEqual(job["deviceContext"]["status"], "failed")
        self.assertFalse(job["deviceContext"]["receiptSaved"])
        self.assertIsNone(job["deviceContext"]["reportPath"])
        self.assertNotIn("records", job["deviceContext"])
        root = Path(job["destination"])
        self.assertEqual((root / "device-context" / "report.html").read_text(encoding="utf-8"), old_report)
        for path in (root / "report.json", self.engine.state_dir / (job["id"] + ".json")):
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["deviceContext"], expected)
        self.assertNotIn('href="device-context/report.html"', (root / "report.html").read_text(encoding="utf-8"))
        verification = self.engine.verify(job["id"])
        self.assertTrue(verification["ok"])
        self.assertEqual((verification["verified"], verification["total"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
