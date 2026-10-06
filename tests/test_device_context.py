"""Real local subprocess fixtures only. Never invokes ADB or a phone."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adb import Cancelled
import device_context as dc


class FixtureAdb:
    def __init__(self, program="import sys; sys.stdout.buffer.write(b'fixture\\n')", select=None):
        self.program = program
        self.select = select
        self.calls = []

    def command(self, serial, args):
        assert serial == "fixture-phone"
        assert args[0] == "shell" and len(args) == 2
        command = tuple(shlex.split(args[1]))
        self.calls.append(command)
        program = self.select(command) if self.select else self.program
        return [sys.executable, "-u", "-c", program]

    @staticmethod
    def process_options():
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class DeviceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "café 雪 archive"
        self.root.mkdir()
        self.original = self.root / "existing-original.bin"
        self.original.write_bytes(b"unchanged original")

    def tearDown(self):
        self.assertEqual(self.original.read_bytes(), b"unchanged original")
        self.temp.cleanup()

    def capture(self, program=None, spec=None, cancel=None, progress=None, select=None):
        adb = FixtureAdb(select=select) if program is None else FixtureAdb(program, select)
        commands = (spec,) if spec is not None else (dc.COMMANDS[0],)
        with patch.object(dc, "COMMANDS", commands):
            result = dc.capture_context(self.root, adb, "fixture-phone", cancel, progress)
        return result, adb

    def report(self, result=None):
        path = Path(result["folder"]) / "report.json" if result else next((self.root / "device-context").glob("*/report.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_saved(self, result, item, name="stdout"):
        saved = item[name]
        actual = (self.root / saved["localPath"]).read_bytes()
        self.assertEqual(len(actual), saved["size"])
        self.assertEqual(hashlib.sha256(actual).hexdigest(), saved["sha256"])
        self.assertIn(Path(result["folder"]), (self.root / saved["localPath"]).parents)
        return actual

    def test_success_metadata_manifest_hash_and_callback(self):
        calls = []
        result, adb = self.capture(progress=lambda *args: calls.append(args))
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["receiptSaved"])
        self.assertEqual(adb.calls, [("dumpsys", "notification", "--noredact")])
        self.assertEqual(calls, [("device-context", "Capturing notifications")])
        report = self.report(result)
        item = report["commands"][0]
        self.assertEqual(self.assert_saved(result, item), b"fixture\n")
        self.assertEqual(item["exitCode"], 0)
        self.assertFalse(item["truncated"])
        self.assertFalse(item["cancelled"])
        self.assertFalse(item["timedOut"])
        self.assertTrue(item["startedAt"] and item["finishedAt"])
        self.assertEqual(item["hashScope"], "saved-PC-bytes-only")
        record = result["records"][0]
        self.assertEqual(record["status"], "copied")
        self.assertEqual(record["category"], "device-context")
        self.assertEqual(record["source"], "device-context:" + Path(result["folder"]).name + "/notifications")
        self.assertEqual(record["localPath"], item["stdout"]["localPath"])
        self.assertEqual(result["counts"]["filesCopied"], 1)
        self.assertEqual(result["counts"]["commandsSucceeded"], 1)
        self.assertEqual(result["counts"]["bytesCopied"], len(b"fixture\n"))
        self.assertTrue((Path(result["folder"]) / "notifications.receipt.json").is_file())
        self.assertTrue((Path(result["folder"]) / "plan.json").is_file())

    def test_hostile_private_output_stays_only_in_raw_files(self):
        payload = '<script>SECRET-private-person & ../escape</script>\n'.encode()
        result, _ = self.capture("import sys; sys.stdout.buffer.write(" + repr(payload) + ")")
        report = self.report(result)
        self.assertEqual(self.assert_saved(result, report["commands"][0]), payload)
        for path in (Path(result["reportPath"]), Path(result["folder"]) / "report.html", Path(result["folder"]) / "report.json"):
            self.assertNotIn("SECRET-private-person", path.read_text(encoding="utf-8"))
        self.assertNotIn("SECRET-private-person", json.dumps(result))
        self.assertNotIn("commands", result)
        self.assertIn("default-src 'none'", Path(result["reportPath"]).read_text(encoding="utf-8"))

    def test_stdout_cap_preserves_partial_and_never_claims_copy(self):
        spec = dc.Command("notifications", dc.COMMANDS[0].args, 1000, 5)
        result, _ = self.capture("import os; os.write(1,b'x'*1000000)", spec=spec)
        item = self.report(result)["commands"][0]
        self.assertEqual(item["status"], "truncated")
        self.assertTrue(item["truncated"])
        self.assertEqual(item["reason"], "stdout-byte-limit")
        self.assertEqual(len(self.assert_saved(result, item)), 1000)
        self.assertIn(".partial-", item["stdout"]["localPath"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["status"], "unavailable")

    def test_large_stderr_is_bounded_without_deadlock(self):
        started = time.monotonic()
        with patch.object(dc, "MAX_STDERR_BYTES", 5000):
            result, _ = self.capture("import os; os.write(2,b'e'*1000000); os.write(1,b'ok')")
        self.assertLess(time.monotonic() - started, 5)
        item = self.report(result)["commands"][0]
        self.assertEqual(item["status"], "truncated")
        self.assertEqual(item["reason"], "stderr-byte-limit")
        self.assertEqual(len(self.assert_saved(result, item, "stderr")), 5000)
        self.assertEqual(result["records"], [])

    def test_simultaneous_pipes_respect_total_budget(self):
        spec = dc.Command("notifications", dc.COMMANDS[0].args, 10000, 5)
        program = "import os,threading; t=threading.Thread(target=lambda:os.write(2,b'e'*10000));t.start();os.write(1,b'o'*10000);t.join()"
        with patch.object(dc, "MAX_TOTAL_BYTES", 7000):
            result, _ = self.capture(program, spec=spec)
        item = self.report(result)["commands"][0]
        self.assertEqual(item["status"], "truncated")
        self.assertEqual(item["reason"], "total-byte-limit")
        self.assertEqual(sum(len(self.assert_saved(result, item, stream)) for stream in ("stdout", "stderr")), 7000)
        self.assertEqual(result["counts"]["bytesSaved"], 7000)

    def test_total_exhaustion_skips_later_commands(self):
        adb = FixtureAdb("import os; os.write(1,b'x'*1000)")
        with patch.object(dc, "MAX_TOTAL_BYTES", 100), patch.object(dc, "COMMANDS", dc.COMMANDS[:3]):
            result = dc.capture_context(self.root, adb, "fixture-phone")
        self.assertEqual(len(adb.calls), 1)
        self.assertEqual([item["status"] for item in self.report(result)["commands"]], ["truncated", "skipped", "skipped"])
        self.assertEqual(result["counts"]["issues"], 3)
        self.assertEqual(result["counts"]["commandsNotAttempted"], 2)

    def test_deadline_kills_silent_child_and_preserves_partial(self):
        spec = dc.Command("notifications", dc.COMMANDS[0].args, 1000, .25)
        started = time.monotonic()
        result, _ = self.capture("import os,time;os.write(1,b'begin');time.sleep(30)", spec=spec)
        self.assertLess(time.monotonic() - started, 4)
        item = self.report(result)["commands"][0]
        self.assertEqual(item["status"], "timed-out")
        self.assertTrue(item["timedOut"])
        self.assertEqual(self.assert_saved(result, item), b"begin")
        self.assertEqual(result["records"], [])

    def test_cancel_saves_receipt_then_raises_and_stops_following_commands(self):
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        adb = FixtureAdb("import os,time;os.write(1,b'begin');time.sleep(30)")
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                dc.capture_context(self.root, adb, "fixture-phone", cancel=cancel)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 4)
        report = self.report()
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(len(adb.calls), 1)
        self.assertTrue(report["commands"][0]["cancelled"])
        self.assertEqual(report["records"], [])
        self.assertTrue(Path(report["reportPath"]).is_file())

    def test_preexisting_cancel_still_saves_run_receipt(self):
        cancel = threading.Event()
        cancel.set()
        adb = FixtureAdb()
        with self.assertRaises(Cancelled) as error:
            dc.capture_context(self.root, adb, "fixture-phone", cancel=cancel)
        report = self.report()
        self.assertEqual(adb.calls, [])
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["counts"]["commandsNotAttempted"], len(dc.COMMANDS))
        self.assertEqual(error.exception.result["status"], "cancelled")
        self.assertEqual(error.exception.result["records"], [])

    def test_cancel_result_preserves_earlier_completed_manifest_records(self):
        cancel = threading.Event()
        timer = None
        def select(args):
            nonlocal timer
            if args == dc.COMMANDS[0].args:
                return "print('completed first fixture')"
            timer = threading.Timer(.25, cancel.set)
            timer.start()
            return "import time; time.sleep(30)"
        adb = FixtureAdb(select=select)
        try:
            with self.assertRaises(Cancelled) as error:
                dc.capture_context(self.root, adb, "fixture-phone", cancel=cancel)
        finally:
            if timer:
                timer.cancel()
        result = error.exception.result
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["counts"]["commandsSucceeded"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(adb.calls), 2)
        self.assertEqual(result["records"], self.report(result)["records"])
        self.assertTrue(Path(result["reportPath"]).is_file())

    def test_publish_failure_does_not_claim_successful_manifest_copy(self):
        adb = FixtureAdb()
        with patch.object(dc, "_publish", side_effect=OSError(errno.EACCES, "fixture cannot publish")):
            with self.assertRaises(OSError):
                dc.capture_context(self.root, adb, "fixture-phone")
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["commands"][0]["status"], "failed")
        self.assertEqual(report["commands"][0]["reason"], "output-publish-error")
        self.assertEqual(report["records"], [])
        self.assertEqual(len(adb.calls), 1)

    def test_final_receipt_failure_exposes_completed_records_and_not_stale_report(self):
        previous, _ = self.capture()
        old_html = Path(previous["reportPath"]).read_bytes()
        with patch.object(dc, "_receipt", side_effect=OSError(errno.ENOSPC, "fixture full disk")):
            with self.assertRaises(OSError) as error:
                self.capture()
        result = error.exception.result
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["receiptSaved"])
        self.assertEqual(result["receiptErrorType"], "OSError")
        self.assertIsNone(result["reportPath"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(Path(previous["reportPath"]).read_bytes(), old_html)
        self.assertNotEqual(previous["folder"], result["folder"])

    def test_receipt_failure_does_not_replace_original_cancel_exception(self):
        cancel = threading.Event()
        timer = None
        def select(args):
            nonlocal timer
            if args == dc.COMMANDS[0].args:
                return "print('completed first fixture')"
            timer = threading.Timer(.25, cancel.set)
            timer.start()
            return "import time; time.sleep(30)"
        adb = FixtureAdb(select=select)
        try:
            with patch.object(dc, "_receipt", side_effect=OSError(errno.ENOSPC, "fixture disk full")):
                with self.assertRaises(Cancelled) as error:
                    dc.capture_context(self.root, adb, "fixture-phone", cancel=cancel)
        finally:
            if timer:
                timer.cancel()
        result = error.exception.result
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["receiptSaved"])
        self.assertIsNone(result["reportPath"])
        self.assertEqual(result["receiptErrorType"], "OSError")

    def test_command_receipt_failure_does_not_mask_cancel(self):
        cancel = threading.Event()
        original = dc._write_new
        def write(path, content):
            if path.name.endswith(".receipt.json"):
                raise OSError(errno.ENOSPC, "fixture receipt failure")
            original(path, content)
        def progress(*args):
            cancel.set()
        with patch.object(dc, "_write_new", side_effect=write):
            with self.assertRaises(Cancelled) as error:
                self.capture(cancel=cancel, progress=progress)
        self.assertTrue(error.exception.result["receiptSaved"])
        self.assertEqual(error.exception.result["status"], "cancelled")
        self.assertEqual(self.report()["commands"][0]["receiptErrorType"], "OSError")

    def test_permissions_and_missing_service_are_honest_gaps_even_exit_zero(self):
        for payload, expected in ((b"Permission Denial: private diagnostic\n", "denied"),
                                  (b"java.lang.SecurityException: not allowed\n", "denied"),
                                  (b"Can't find service: usagestats\n", "unsupported"),
                                  (b"Error: Unknown option: --show-versioncode\n", "unsupported")):
            with self.subTest(expected=expected):
                result, _ = self.capture("import sys; sys.stdout.buffer.write(" + repr(payload) + ")")
                report = self.report(result)
                self.assertEqual(report["commands"][0]["status"], expected)
                self.assertEqual(result["records"], [])
                self.assertNotIn("private diagnostic", json.dumps(result))

    def test_nonzero_exit_preserves_failure_and_stderr(self):
        result, _ = self.capture("import sys; sys.stderr.write('secret failure'); sys.exit(9)")
        item = self.report(result)["commands"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["exitCode"], 9)
        self.assertEqual(self.assert_saved(result, item, "stderr"), b"secret failure")
        self.assertNotIn("secret failure", Path(result["reportPath"]).read_text())

    def test_benign_stderr_is_preserved_and_hashed(self):
        result, _ = self.capture("import sys; sys.stdout.write('ok'); sys.stderr.write('warning fixture')")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["records"]), 2)
        self.assertTrue(result["records"][1]["source"].endswith("/notifications.stderr"))
        for record in result["records"]:
            self.assertEqual(hashlib.sha256((self.root / record["localPath"]).read_bytes()).hexdigest(), record["sha256"])

    def test_package_fallback_only_for_unsupported_options(self):
        def select(args):
            return "print('Error: Unknown option: --show-versioncode')" if "--show-versioncode" in args else "print('package:org.example installer=null')"
        for spec in (dc.COMMANDS[4], dc.COMMANDS[5]):
            with self.subTest(key=spec.key):
                result, adb = self.capture(spec=spec, select=select)
                self.assertEqual(adb.calls, [spec.args, spec.fallback])
                report = self.report(result)
                self.assertEqual([item["status"] for item in report["commands"]], ["unsupported", "complete"])
                self.assertTrue(report["commands"][1]["reducedMetadata"])
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["counts"]["filesCopied"], 1)
                self.assertTrue(result["records"][0]["source"].endswith("-compat"))

    def test_package_denial_does_not_fallback(self):
        result, adb = self.capture("print('Permission Denial: fixture')", spec=dc.COMMANDS[4])
        self.assertEqual(len(adb.calls), 1)
        self.assertEqual(result["status"], "unavailable")

    def test_location_and_user_snapshots_are_private_bounded_read_only_captures(self):
        specs = {spec.key: spec for spec in dc.COMMANDS}
        fixtures = (
            (specs["location-state"], ("dumpsys", "location"), 4 * dc.MIB,
             b"Location Manager State:\nlast location=Location[gps 12.345678,98.765432]\n"),
            (specs["users"], ("pm", "list", "users"), dc.MIB,
             b"Users:\n UserInfo{10:PRIVATE-FIXTURE-PROFILE:30} running\n"),
        )
        for spec, args, limit, payload in fixtures:
            with self.subTest(key=spec.key):
                result, adb = self.capture("import sys; sys.stdout.buffer.write(" + repr(payload) + ")", spec=spec)
                self.assertEqual(adb.calls, [args])
                self.assertEqual(spec.limit, limit)
                self.assertEqual(spec.timeout, 30)
                self.assertIsNone(spec.fallback)
                item = self.report(result)["commands"][0]
                self.assertEqual(self.assert_saved(result, item), payload)
                self.assertEqual(result["counts"]["commandsSucceeded"], 1)
                self.assertEqual(result["records"][0]["localPath"], item["stdout"]["localPath"])
                public_metadata = json.dumps(result) + Path(result["reportPath"]).read_text(encoding="utf-8")
                self.assertNotIn("12.345678", public_metadata)
                self.assertNotIn("PRIVATE-FIXTURE-PROFILE", public_metadata)

    def test_location_and_user_denials_do_not_try_to_enable_or_unlock(self):
        for spec in (item for item in dc.COMMANDS if item.key in ("location-state", "users")):
            with self.subTest(key=spec.key):
                result, adb = self.capture("print('Permission Denial: fixture')", spec=spec)
                self.assertEqual(adb.calls, [spec.args])
                self.assertEqual(result["records"], [])
                self.assertEqual(self.report(result)["commands"][0]["status"], "denied")

    def test_runs_never_replace_original_outputs_only_stable_html(self):
        first, _ = self.capture("print('first private value')")
        saved = {path: path.read_bytes() for path in Path(first["folder"]).iterdir()}
        second, _ = self.capture("print('second private value')")
        self.assertNotEqual(first["folder"], second["folder"])
        self.assertEqual(first["reportPath"], second["reportPath"])
        stable = Path(second["reportPath"]).read_text(encoding="utf-8")
        self.assertIn(Path(second["folder"]).name, stable)
        self.assertNotIn(Path(first["folder"]).name, stable)
        for path, content in saved.items():
            self.assertEqual(path.read_bytes(), content)

    def test_fsync_is_used_for_saved_outputs_and_receipts(self):
        actual = os.fsync
        with patch.object(dc.os, "fsync", wraps=actual) as sync:
            result, _ = self.capture()
        self.assertGreaterEqual(sync.call_count, 7)
        self.assertEqual(result["status"], "complete")

    def test_disk_full_stops_next_command_and_propagates(self):
        actual = dc._new_file
        class NoSpace:
            def __init__(self, stream):
                self.stream = stream
            def write(self, value):
                raise OSError(errno.ENOSPC, "fixture disk full")
            def __getattr__(self, name):
                return getattr(self.stream, name)
        def open_file(path):
            stream = actual(path)
            return NoSpace(stream) if ".stdout.partial-" in path.name else stream
        adb = FixtureAdb()
        with patch.object(dc, "_new_file", side_effect=open_file):
            with self.assertRaises(OSError) as error:
                dc.capture_context(self.root, adb, "fixture-phone")
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(len(adb.calls), 1)
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report()["records"], [])
        self.assertEqual(error.exception.result["status"], "failed")
        self.assertTrue(error.exception.result["receiptSaved"])

    def test_short_pc_write_receipt_hashes_the_actual_partial(self):
        actual = dc._new_file
        class ShortWrite:
            def __init__(self, stream):
                self.stream = stream
            def write(self, value):
                return self.stream.write(value[:3])
            def __getattr__(self, name):
                return getattr(self.stream, name)
        def open_file(path):
            stream = actual(path)
            return ShortWrite(stream) if ".stdout.partial-" in path.name else stream
        adb = FixtureAdb()
        with patch.object(dc, "_new_file", side_effect=open_file), self.assertRaises(OSError):
            dc.capture_context(self.root, adb, "fixture-phone")
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.assert_saved(report, report["commands"][0]), b"fix")
        self.assertEqual(report["counts"]["bytesSaved"], 3)
        self.assertEqual(report["records"], [])
        self.assertEqual(len(adb.calls), 1)

    def test_saved_output_tampering_is_not_registered_as_complete(self):
        original = dc._saved_metadata
        def tampered(path, root, limit):
            if ".stdout.partial-" in path.name:
                path.write_bytes(b"changed")
            return original(path, root, limit)
        adb = FixtureAdb()
        with patch.object(dc, "_saved_metadata", side_effect=tampered), self.assertRaises(OSError):
            dc.capture_context(self.root, adb, "fixture-phone")
        report = self.report()
        self.assertEqual(report["commands"][0]["reason"], "saved-bytes-mismatch")
        self.assertEqual(report["records"], [])
        self.assertEqual(len(adb.calls), 1)

    def test_symlink_output_root_rejected_before_process(self):
        link = Path(self.temp.name) / "archive-link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit creating symlinks")
        adb = FixtureAdb()
        with self.assertRaises(ValueError):
            dc.capture_context(link, adb, "fixture-phone")
        self.assertEqual(adb.calls, [])

    def test_windows_reparse_flag_rejected(self):
        from types import SimpleNamespace
        original = Path.lstat
        def read(path, *args, **kwargs):
            if path == self.root:
                return SimpleNamespace(st_mode=stat_mode, st_file_attributes=1024)
            return original(path, *args, **kwargs)
        stat_mode = self.root.stat().st_mode
        adb = FixtureAdb()
        with patch.object(Path, "lstat", read), self.assertRaises(ValueError):
            dc.capture_context(self.root, adb, "fixture-phone")
        self.assertEqual(adb.calls, [])

    def test_stable_report_symlink_cannot_overwrite_external_file(self):
        base = self.root / "device-context"
        base.mkdir()
        external = Path(self.temp.name) / "do-not-change.html"
        external.write_text("original")
        try:
            (base / "report.html").symlink_to(external)
        except OSError:
            self.skipTest("Host does not permit creating symlinks")
        with self.assertRaises(ValueError):
            self.capture()
        self.assertEqual(external.read_text(), "original")

    def test_fixed_allowlist_has_no_state_mutations_and_uses_host_deadlines(self):
        actual = [spec.args for spec in dc.COMMANDS]
        self.assertEqual(actual[:4], [("dumpsys", "notification", "--noredact"), ("dumpsys", "usagestats"),
                                     ("dumpsys", "usagestats", "file"), ("dumpsys", "account", "--checkin")])
        for spec in dc.COMMANDS:
            self.assertNotIn("-t", spec.args if spec.args[0] == "dumpsys" else ())
            self.assertLessEqual(spec.limit, 64 * dc.MIB)
            self.assertLessEqual(spec.timeout, 120)
            if spec.args[0] == "settings":
                self.assertEqual(spec.args[1], "get")
            if spec.args[:2] == ("dumpsys", "usagestats"):
                self.assertNotIn("--checkin", spec.args)
                self.assertNotIn("flush", spec.args)
        all_words = {word for spec in dc.COMMANDS for word in spec.args}
        self.assertTrue(all_words.isdisjoint({"put", "delete", "insert", "update", "clear", "reset", "enable", "disable", "start-scan", "root", "install", "restore", "-c", "--gnssmetrics", "set-location-enabled", "switch-user", "start-user", "unlock-user"}))
        self.assertIn(("dumpsys", "location"), actual)
        self.assertIn(("pm", "list", "users"), actual)
        self.assertIn(("cmd", "wifi", "list-networks"), actual)
        self.assertIn(("cmd", "wifi", "status"), actual)
        self.assertIn(("logcat", "-b", "main", "-b", "system", "-b", "events", "-b", "crash", "-d", "-t", "10000", "-v", "epoch"), actual)
        self.assertEqual(dc.MAX_TOTAL_BYTES, 128 * dc.MIB)


if __name__ == "__main__":
    unittest.main()
