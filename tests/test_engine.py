"""Offline integration tests: a real TCP ADB/sync peer plus a fake phone tree."""
import hashlib
import base64
import errno
import io
import json
from pathlib import Path
import shlex
import socket
import stat
import struct
import sys
import tempfile
import tarfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adb import Adb, AdbError, Cancelled, Sync
from engine import BoundedOutput, RescueEngine, local_remote_path, safe_join, safe_name


class Phone:
    def __init__(self):
        self.files = {
            "/shared/DCIM/photo.jpg": b"JPEG fixture" * 9000,
            "/shared/.hidden": b"hidden data",
            "/shared/CON": b"reserved name",
            "/shared/A.txt": b"upper",
            "/shared/a.txt": b"lower",
            "/shared/new\nline.txt": b"newline filename",
            "/shared/question?.txt": b"question",
            "/shared/trailing. ": b"trailing",
            "/shared/unicode-\u2603.txt": b"unicode name",
            "/shared/invalid-\udcff.bin": b"raw bytes filename",
            "/apk/base.apk": b"base installer",
            "/apk/split_config.apk": b"split installer",
        }
        self.dirs = {"/shared", "/shared/DCIM", "/shared/empty", "/storage", "/apk"}
        self.denied = set()
        self.symlinks = set()
        self.mtime = 1700000000
        self.delay = 0
        self.reads = []
        self.fault = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(50)
        self.listener.settimeout(.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def read(sock, size):
        data = b""
        while len(data) < size:
            part = sock.recv(size - len(data))
            if not part:
                raise EOFError()
            data += part
        return data

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.listener.close()

    def _serve(self):
        while not self.stop.is_set():
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    def _client(self, sock):
        try:
            with sock:
                sock.settimeout(3)
                for expected in ("host:transport:TEST", "sync:"):
                    size = int(self.read(sock, 4), 16)
                    request = self.read(sock, size).decode()
                    if request != expected:
                        message = b"unauthorized"
                        sock.sendall(b"FAIL" + f"{len(message):04x}".encode() + message)
                        return
                    sock.sendall(b"OKAY")
                op = self.read(sock, 4)
                length, = struct.unpack("<I", self.read(sock, 4))
                path = self.read(sock, length).decode("utf-8", "surrogateescape")
                if path.startswith("/sdcard/"):
                    path = "/shared/" + path[len("/sdcard/"):]
                if self.fault == "malformed":
                    sock.sendall(b"BADD" + struct.pack("<I", 0))
                    return
                if op == b"LIST":
                    if path not in self.denied:
                        nodes = {p: stat.S_IFDIR | 0o755 for p in self.dirs}
                        nodes.update({p: stat.S_IFREG | 0o644 for p in self.files})
                        nodes.update({p: stat.S_IFLNK | 0o777 for p in self.symlinks})
                        for child, mode in nodes.items():
                            if str(Path(child).parent).replace("\\", "/") != path:
                                continue
                            name = child.rsplit("/", 1)[-1].encode("utf-8", "surrogateescape")
                            sock.sendall(b"DENT" + struct.pack("<IIII", mode, len(self.files.get(child, b"")), self.mtime, len(name)) + name)
                    sock.sendall(b"DONE" + bytes(16))
                elif op == b"STAT":
                    mode = stat.S_IFREG | 0o644 if path in self.files else stat.S_IFDIR | 0o755 if path in self.dirs else 0
                    if path in self.denied:
                        mode = 0
                    sock.sendall(b"STAT" + struct.pack("<III", mode, len(self.files.get(path, b"")), self.mtime))
                elif op == b"RECV":
                    if path not in self.files or path in self.denied:
                        error = b"Permission denied"
                        sock.sendall(b"FAIL" + struct.pack("<I", len(error)) + error)
                        return
                    self.reads.append(path)
                    data = self.files[path]
                    for offset in range(0, len(data), 4096):
                        part = data[offset:offset + 4096]
                        sock.sendall(b"DATA" + struct.pack("<I", len(part)) + part)
                        if self.delay:
                            time.sleep(self.delay)
                        if self.fault == "disconnect":
                            return
                    sock.sendall(b"DONE" + bytes(4))
        except (EOFError, OSError):
            pass


class FakeAdb:
    def __init__(self, phone):
        self.phone = phone
        self.fallback_fault = None
        self.shell_reads = []
        self.hash_calls = {}

    def devices(self):
        return [{"serial": "TEST", "state": "device", "model": "Fixture phone"}]

    def sync(self, serial, cancel=None):
        return Sync(serial, port=self.phone.port, cancel=cancel, idle_timeout=2)

    def shell_args(self, serial, args, **kwargs):
        if args[:3] == ["stat", "-c", "%s %Y %f"]:
            return f"{len(self.phone.files[args[3]])} {self.phone.mtime} 81a4"
        if args[:1] == ["sha256sum"]:
            path = args[1]
            self.hash_calls[path] = self.hash_calls.get(path, 0) + 1
            if self.fallback_fault == "changed" and self.hash_calls[path] >= 2:
                self.phone.files[path] = b"!" + self.phone.files[path][1:]
            return hashlib.sha256(self.phone.files[path]).hexdigest() + "  " + path
        if args == ["getprop"]:
            return "[ro.product.model]: [Fixture phone]\n[ro.build.version.release]: [4.4.4]\n[ro.build.version.sdk]: [19]\n[ro.serialno]: [TEST]"
        if args == ["pm", "path", "org.androidrescue.helper"]:
            return ""
        if args == ["pm", "list", "packages", "-f"]:
            return "package:/apk/base.apk=org.fixture"
        if args == ["pm", "path", "org.fixture"]:
            return "package:/apk/base.apk\npackage:/apk/split_config.apk"
        if args == ["pm", "list", "packages"]:
            return "package:org.fixture"
        if args[:1] == ["run-as"]:
            return "run-as: package not debuggable"
        raise AssertionError(args)

    def shell(self, serial, command, **kwargs):
        if command.startswith("cd "):
            parts = shlex.split(command)
            path = parts[1]
            if path == "/sdcard":
                return "/shared"
            if path.startswith("/sdcard/"):
                path = "/shared/" + path[len("/sdcard/"):]
            if path in self.phone.dirs and path not in self.phone.denied:
                return path
            return ""
        if command.startswith("if [ -d "):
            path = shlex.split(command)[3]
            return "RESCUE_READABLE" if path in self.phone.dirs and path not in self.phone.denied else ""
        raise AssertionError(command)

    def command(self, serial, args):
        assert args[0] == "exec-out"
        command = shlex.split(args[1])
        assert command[0] == "cat"
        path = command[1]
        self.shell_reads.append(path)
        content = self.phone.files[path]
        if self.fallback_fault == "truncated":
            content = content[:-1]
        elif self.fallback_fault == "corrupt":
            content = b"!" + content[1:]
        elif self.fallback_fault == "oversized":
            content += b"excess data"
        program = "import sys,base64,time;data=base64.b64decode('" + base64.b64encode(content).decode() + "');"
        if self.fallback_fault == "slow":
            program += "sys.stdout.buffer.write(data[:1]);sys.stdout.buffer.flush();time.sleep(10);sys.stdout.buffer.write(data[1:])"
        elif self.fallback_fault == "stderr":
            program += "sys.stderr.write('warning' * 200000);sys.stdout.buffer.write(data)"
        else:
            program += "sys.stdout.buffer.write(data)"
        return [sys.executable, "-c", program]

    @staticmethod
    def process_options():
        return {}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.phone = Phone()

    def tearDown(self):
        self.phone.close()

    def test_sync_preserves_arbitrary_names_and_hidden_files(self):
        with Sync("TEST", port=self.phone.port) as sync:
            entries = sync.list("/shared")
        self.assertIn("new\nline.txt", [e.name for e in entries])
        self.assertIn("invalid-\udcff.bin", [e.name for e in entries])
        self.assertIn(".hidden", [e.name for e in entries])

    def test_receive_multiple_chunks_and_zero_length(self):
        for path, content in [("/shared/DCIM/photo.jpg", self.phone.files["/shared/DCIM/photo.jpg"]), ("/shared/zero", b"")]:
            self.phone.files[path] = content
            output = io.BytesIO()
            progress = []
            with Sync("TEST", port=self.phone.port) as sync:
                total = sync.receive(path, output, progress.append)
            self.assertEqual(output.getvalue(), content)
            self.assertEqual(total, len(content))
            self.assertEqual(sum(progress), len(content))

    def test_permission_fail_and_disconnect_are_not_success(self):
        self.phone.denied.add("/shared/.hidden")
        with self.assertRaisesRegex(AdbError, "Permission denied"):
            with Sync("TEST", port=self.phone.port) as sync:
                sync.receive("/shared/.hidden", io.BytesIO())
        self.phone.fault = "disconnect"
        with self.assertRaisesRegex(AdbError, "closed"):
            with Sync("TEST", port=self.phone.port) as sync:
                sync.receive("/shared/DCIM/photo.jpg", io.BytesIO())

    def test_cancel_during_large_receive(self):
        self.phone.delay = .02
        event = threading.Event()
        with self.assertRaises(Cancelled):
            with Sync("TEST", port=self.phone.port, cancel=event) as sync:
                sync.receive("/shared/DCIM/photo.jpg", io.BytesIO(), lambda _: event.set())

    def test_reject_malformed_and_unauthorized(self):
        self.phone.fault = "malformed"
        with self.assertRaises(AdbError):
            with Sync("TEST", port=self.phone.port) as sync:
                sync.receive("/shared/.hidden", io.BytesIO())
        with self.assertRaisesRegex(AdbError, "unauthorized"):
            with Sync("OTHER", port=self.phone.port):
                pass

    def test_metadata_subprocess_is_cancellable_before_long_timeout(self):
        adb = Adb(Path(sys.executable))
        adb.command = lambda serial, args: [sys.executable, "-c", "import time;time.sleep(20)"]
        event = threading.Event()
        timer = threading.Timer(.15, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                adb.shell_args("TEST", ["sha256sum", "/apk/base.apk"], timeout=120, cancel=event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.phone = Phone()
        self.engine = RescueEngine(self.root / "app", self.root / "adb")
        self.engine.adb = FakeAdb(self.phone)

    def tearDown(self):
        for event in self.engine._cancel.values():
            event.set()
        for thread in self.engine._threads.values():
            thread.join(5)
        self.phone.close()
        self.temp.cleanup()

    def wait(self, job):
        self.engine._threads[job["id"]].join(20)
        self.assertFalse(self.engine._threads[job["id"]].is_alive())
        return self.engine.get_job(job["id"])

    def start(self, **opts):
        return self.engine.start("TEST", str(self.root / "copies"), {"shared": True, "apks": True, "helper": False, **opts})

    def test_end_to_end_files_splits_manifest_hashes_report(self):
        job = self.wait(self.start())
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["filesCopied"], len(self.phone.files))
        root = Path(job["destination"])
        records = self.engine._manifest(root)
        self.assertEqual(set(records), set(self.phone.files))
        for source, record in records.items():
            self.assertEqual((root / record["localPath"]).read_bytes(), self.phone.files[source])
            self.assertEqual(record["sha256"], hashlib.sha256(self.phone.files[source]).hexdigest())
        self.assertTrue((root / "report.html").is_file())
        self.assertTrue((root / "device.json").is_file())
        result = self.engine.verify(job["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["verified"], len(self.phone.files))

    def test_empty_permission_denied_and_symlinks_become_explicit_gaps(self):
        self.phone.dirs.add("/shared/blocked")
        self.phone.denied.add("/shared/blocked")
        self.phone.symlinks.add("/shared/cycle")
        job = self.wait(self.start())
        self.assertEqual(job["status"], "partial")
        self.assertTrue(any("permission denied" in e for e in job["errors"]), job)
        self.assertTrue(any("symbolic link" in e for e in job["errors"]), job)

    def test_resume_rechecks_without_recopied_or_overwritten_originals(self):
        job = self.wait(self.start())
        first_reads = len(self.phone.reads)
        job = self.wait(self.engine.resume(job["id"]))
        self.assertEqual(len(self.phone.reads), first_reads)
        self.assertEqual(job["filesCopied"], len(self.phone.files))
        old_records = self.engine._manifest(Path(job["destination"]))
        old_path = Path(job["destination"]) / old_records["/shared/A.txt"]["localPath"]
        self.phone.files["/shared/A.txt"] = b"changed new contents"
        job = self.wait(self.engine.resume(job["id"]))
        self.assertEqual(old_path.read_bytes(), b"upper")
        new_records = self.engine._manifest(Path(job["destination"]))
        self.assertNotEqual(new_records["/shared/A.txt"]["localPath"], old_records["/shared/A.txt"]["localPath"])
        self.assertTrue(self.engine.verify(job["id"])["ok"])

    def test_tampering_detected_and_resume_preserves_tampered_file(self):
        job = self.wait(self.start())
        record = self.engine._manifest(Path(job["destination"]))["/shared/A.txt"]
        path = Path(job["destination"]) / record["localPath"]
        path.write_bytes(b"tampered")
        self.assertFalse(self.engine.verify(job["id"])["ok"])
        self.wait(self.engine.resume(job["id"]))
        self.assertEqual(path.read_bytes(), b"tampered")
        # Earlier acquisitions remain represented; damaged evidence stays flagged.
        self.assertFalse(self.engine.verify(job["id"])["ok"])

    def test_cancel_and_resume_keeps_partials_and_completes(self):
        self.phone.delay = .05
        job = self.start()
        deadline = time.monotonic() + 5
        while self.engine.get_job(job["id"])["phase"] != "copying" and time.monotonic() < deadline:
            time.sleep(.01)
        self.engine.cancel(job["id"])
        stopped = self.wait(job)
        self.assertEqual(stopped["status"], "cancelled", stopped)
        self.phone.delay = 0
        resumed = self.wait(self.engine.resume(job["id"]))
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertTrue(self.engine.verify(job["id"])["ok"])

    def test_disconnection_creates_partial_not_verified_copy(self):
        self.phone.fault = "disconnect"
        job = self.wait(self.start(apks=False))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["filesCopied"], 0)
        self.assertEqual(len(self.phone.reads), 1, "Disconnected jobs must stop immediately")
        self.assertTrue(list(Path(job["destination"]).rglob("*.partial-*")))

    def test_missing_helper_and_private_permissions_remain_visible(self):
        job = self.wait(self.start(helper=True, privateApps=True))
        statuses = {row["category"]: row["status"] for row in job["coverage"]}
        self.assertEqual(statuses["Contacts, messages and call history"], "blocked")
        self.assertEqual(statuses["Private app data"], "blocked")
        self.assertEqual(job["status"], "partial")

    def test_selected_private_app_method_blocked_means_partial(self):
        job = self.wait(self.start(privateApps=True))
        self.assertEqual(job["status"], "partial")

    def test_resume_does_not_reuse_an_old_verification_receipt(self):
        job = self.wait(self.start())
        receipt = self.engine.verify(job["id"])
        resumed = self.wait(self.engine.resume(job["id"]))
        self.assertNotIn("verification", resumed)
        self.assertEqual(resumed["previousVerification"], receipt)

    def test_state_reload_marks_interruption_and_retains_jobs(self):
        job = self.wait(self.start())
        path = self.engine.state_dir / (job["id"] + ".json")
        data = json.loads(path.read_text())
        data["status"] = "running"
        path.write_text(json.dumps(data))
        reloaded = RescueEngine(self.root / "app", self.root / "adb")
        self.assertEqual(reloaded.history()[0]["status"], "partial")

    def test_unsafe_manifest_path_cannot_escape_verification(self):
        job = self.wait(self.start())
        root = Path(job["destination"])
        self.engine._append(root, {"source": "bad", "status": "copied", "localPath": "../outside", "size": 1, "sha256": "0"})
        result = self.engine.verify(job["id"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("Unsafe" in issue["error"] for issue in result["issues"]))

    def test_torn_manifest_tail_preserved_without_swallowing_new_record(self):
        root = self.root / "manifest-fixture"
        root.mkdir()
        (root / "manifest.jsonl").write_text('{"source":"interrupted', encoding="utf-8")
        self.engine._append(root, {"source": "/valid", "status": "copied", "localPath": "file"})
        records = self.engine._manifest(root)
        self.assertIn("/valid", records)
        self.assertTrue((root / "manifest.jsonl").read_text().startswith('{"source":"interrupted\n'))

    def test_verification_receipt_is_persisted_in_job_and_report(self):
        job = self.wait(self.start())
        receipt = self.engine.verify(job["id"])
        self.assertEqual(self.engine.get_job(job["id"])["verification"], receipt)
        self.assertIn("Latest PC verification", Path(job["reportPath"]).read_text())

    def helper_fixture(self, asset=True, newer=False):
        base = "/shared/AndroidRescue/exports"
        run = "20260102T030405Z-1234abcd"
        prefix = base + "/" + run
        self.phone.dirs.update({"/shared/AndroidRescue", base, prefix, prefix + "/vcards"})
        binary = b"BEGIN:VCARD\nEND:VCARD\n"
        if asset:
            self.phone.files[prefix + "/vcards/contact-1.vcf"] = binary
        row = {"values": {"_id": 1}, "export": {"vcard": {"file": "vcards/contact-1.vcf", "bytes": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}}}
        rows = (json.dumps(row) + "\n").encode()
        self.phone.files[prefix + "/contacts.jsonl"] = rows
        report = {"runFinished": True, "status": "complete", "complete": True, "exportedRows": 1, "exportedBinaryFiles": 1,
                  "categories": [{"category": "contacts", "file": "contacts.jsonl", "exportedRows": 1, "status": "complete", "bytes": len(rows), "sha256": hashlib.sha256(rows).hexdigest()}], "errors": []}
        self.phone.files[prefix + "/report.json"] = json.dumps(report).encode()
        if newer:
            self.phone.dirs.add(base + "/20260102T040405Z-aaaaaaaa")

    def test_helper_validates_report_rows_and_binary_assets(self):
        self.helper_fixture()
        info = self.engine.inspect("TEST")
        self.assertEqual(info["helperExport"]["state"], "ready", info)
        job = self.wait(self.start(helper=True))
        coverage = {c["category"]: c for c in job["coverage"]}
        self.assertEqual(coverage["Phone export: contacts"]["status"], "copied", job)
        self.assertEqual(coverage["Contacts, messages and call history"]["status"], "copied", job)
        self.assertEqual(job["status"], "completed", job)

    def test_helper_missing_asset_cannot_claim_complete(self):
        self.helper_fixture(asset=False)
        job = self.wait(self.start(helper=True))
        coverage = {c["category"]: c for c in job["coverage"]}
        self.assertEqual(coverage["Phone export: contacts"]["status"], "partial", job)
        self.assertEqual(job["status"], "partial", job)
        self.assertTrue(any("attachment references" in e for e in job["errors"]))

    def test_newest_interrupted_helper_run_is_not_hidden_by_old_complete_run(self):
        self.helper_fixture(newer=True)
        self.assertEqual(self.engine.inspect("TEST")["helperExport"]["state"], "incomplete")
        job = self.wait(self.start(helper=True))
        category = next(c for c in job["coverage"] if c["category"] == "Contacts, messages and call history")
        self.assertEqual(category["status"], "partial")
        self.assertIn("no valid completion marker", category["detail"])

    def test_helper_export_finishing_after_shared_stage_is_copied_once_at_end(self):
        self.helper_fixture()
        prefix = "/shared/AndroidRescue/exports/20260102T030405Z-1234abcd/"
        report_path = prefix + "report.json"
        completed_report = self.phone.files.pop(report_path)
        original_inventory = self.engine._apk_inventory
        def finish_export(job, cancel, records):
            self.assertFalse(any(path.startswith(prefix) for path in self.phone.reads),
                             "Shared stage must defer all helper export files")
            self.assertIn("/shared/DCIM/photo.jpg", self.phone.reads)
            self.phone.files[report_path] = completed_report
            return original_inventory(job, cancel, records)
        with mock.patch.object(self.engine, "_apk_inventory", side_effect=finish_export):
            job = self.wait(self.start(helper=True))
        self.assertEqual(job["status"], "completed", job)
        expected_paths = [path for path in self.phone.files if path.startswith(prefix)]
        for path in expected_paths:
            self.assertEqual(self.phone.reads.count(path), 1, path)
        records = self.engine._manifest(Path(job["destination"]))
        self.assertTrue(all(records[path]["category"] == "helper-exports" for path in expected_paths))
        category = next(c for c in job["coverage"] if c["category"] == "Contacts, messages and call history")
        self.assertEqual(category["status"], "copied", category)

    def test_large_helper_report_is_accepted_and_address_coverage_is_grouped(self):
        self.helper_fixture()
        prefix = "/shared/AndroidRescue/exports/20260102T030405Z-1234abcd/"
        report = json.loads(self.phone.files[prefix + "report.json"])
        source = report["categories"][0]
        addresses = [{**source, "category": "mms_addresses_" + str(i), "sourceUri": "fixture:" + "x" * 5500} for i in range(1000)]
        # One deliberately absent category proves grouping does not skip validation.
        addresses[-1]["file"] = "missing-address-file.jsonl"
        report["categories"].extend(addresses)
        self.phone.files[prefix + "report.json"] = json.dumps(report).encode()
        self.assertGreater(len(self.phone.files[prefix + "report.json"]), 5 * 1024 * 1024)
        self.assertEqual(self.engine.inspect("TEST")["helperExport"]["state"], "ready")
        job = self.wait(self.start(helper=True))
        coverage = {row["category"]: row for row in job["coverage"]}
        self.assertIn("Phone export: MMS addresses", coverage)
        self.assertIn("999 / 1,000 files verified", coverage["Phone export: MMS addresses"]["detail"])
        self.assertEqual(coverage["Phone export: MMS addresses"]["status"], "partial")
        self.assertLess(len(job["coverage"]), 10)
        summary = json.loads((Path(job["destination"]) / "helper-reports.json").read_text())
        self.assertIn("localPath", summary["reports"][summary["latestRun"]])
        self.assertLess((Path(job["destination"]) / "helper-reports.json").stat().st_size, 4000)

    def test_helper_report_still_has_a_hard_metadata_bound(self):
        self.helper_fixture()
        with mock.patch("engine.MAX_HELPER_REPORT_BYTES", 16):
            state = self.engine.inspect("TEST")["helperExport"]
        self.assertEqual(state["state"], "partial")
        self.assertIn("metadata limit", state["detail"])
        output = io.BytesIO()
        bounded = BoundedOutput(output, 8)
        bounded.write(b"12345678")
        with self.assertRaises(AdbError):
            bounded.write(b"9")
        self.assertEqual(output.getvalue(), b"12345678")

    def test_missing_remote_helper_asset_does_not_trust_corrupt_old_pc_copy(self):
        self.helper_fixture()
        job = self.wait(self.start(helper=True))
        prefix = "/shared/AndroidRescue/exports/20260102T030405Z-1234abcd/"
        source = prefix + "vcards/contact-1.vcf"
        record = self.engine._manifest(Path(job["destination"]))[source]
        path = Path(job["destination"]) / record["localPath"]
        path.write_bytes(b"!" + path.read_bytes()[1:])
        del self.phone.files[source]
        resumed = self.wait(self.engine.resume(job["id"]))
        category = next(c for c in resumed["coverage"] if c["category"] == "Phone export: contacts")
        self.assertEqual(category["status"], "partial", resumed)

    def test_missing_remote_and_local_helper_category_is_reported_partial(self):
        self.helper_fixture()
        job = self.wait(self.start(helper=True))
        source = "/shared/AndroidRescue/exports/20260102T030405Z-1234abcd/contacts.jsonl"
        record = self.engine._manifest(Path(job["destination"]))[source]
        (Path(job["destination"]) / record["localPath"]).unlink()
        del self.phone.files[source]
        resumed = self.wait(self.engine.resume(job["id"]))
        category = next(c for c in resumed["coverage"] if c["category"] == "Phone export: contacts")
        self.assertEqual(category["status"], "partial", resumed)
        self.assertEqual(resumed["phase"], "finished")

    def test_apk_shell_fallback_reads_denied_sync_with_independent_hashes(self):
        self.phone.denied.add("/apk/base.apk")
        job = self.wait(self.start())
        self.assertEqual(job["status"], "completed", job)
        records = self.engine._manifest(Path(job["destination"]))
        record = records["/apk/base.apk"]
        self.assertEqual(record["acquisitionMethod"], "adb-exec-out-cat")
        self.assertEqual(record["sha256"], record["sourceSha256Before"])
        self.assertEqual(record["sha256"], record["sourceSha256After"])
        self.assertEqual(self.engine.adb.shell_reads, ["/apk/base.apk"])
        resumed = self.wait(self.engine.resume(job["id"]))
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertEqual(self.engine.adb.shell_reads, ["/apk/base.apk"], "Verified fallback files should not be copied again")

    def test_apk_shell_fallback_rejects_truncation_corruption_and_source_changes(self):
        self.phone.denied.add("/apk/base.apk")
        for fault, message in (("truncated", "wrong size"), ("corrupt", "SHA-256 mismatch"), ("oversized", "exceeds its measured size"), ("changed", "changed on the phone")):
            with self.subTest(fault=fault):
                self.engine.adb.fallback_fault = fault
                self.engine.adb.hash_calls.clear()
                job = self.wait(self.start(shared=False))
                self.assertEqual(job["status"], "partial", job)
                records = self.engine._manifest(Path(job["destination"]))
                self.assertNotIn("/apk/base.apk", records)
                self.assertTrue(any(message in error for error in job["errors"]), job)
                self.assertTrue(list(Path(job["destination"]).rglob("*.partial-*")))
                if fault == "oversized":
                    self.assertTrue(all(p.stat().st_size <= len(self.phone.files["/apk/base.apk"]) for p in Path(job["destination"]).rglob("*.partial-*")))

    def test_apk_shell_fallback_cancels_and_bounded_stderr_cannot_deadlock(self):
        self.phone.denied.add("/apk/base.apk")
        self.engine.adb.fallback_fault = "slow"
        job = self.start(shared=False)
        deadline = time.monotonic() + 5
        while not self.engine.adb.shell_reads and time.monotonic() < deadline:
            time.sleep(.01)
        self.engine.cancel(job["id"])
        self.assertEqual(self.wait(job)["status"], "cancelled")
        self.engine.adb.fallback_fault = "stderr"
        failed = self.wait(self.start(shared=False))
        self.assertEqual(failed["status"], "partial", failed)
        self.assertLess(len(str(failed["errors"])), 3000)

    def test_apk_fallback_disk_full_is_fatal_and_preserves_partial(self):
        self.phone.denied.add("/apk/base.apk")
        original_open = Path.open
        class FullDisk:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, data):
                raise OSError(errno.ENOSPC, "Disk full during APK fallback")
        def disk_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return FullDisk(stream) if mode == "xb" and ".partial-" in path.name else stream
        with mock.patch.object(Path, "open", new=disk_open):
            job = self.wait(self.start(shared=False))
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["filesCopied"], 0)
        self.assertEqual(self.engine.adb.shell_reads, ["/apk/base.apk"])

    def test_report_failure_changes_success_to_partial(self):
        with mock.patch.object(self.engine, "_write_report", side_effect=OSError("disk full")):
            job = self.wait(self.start())
        self.assertEqual(job["status"], "partial")
        self.assertIn("report could not", job["message"])

    def test_public_status_stays_finalizing_until_reports_exist(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine._write_report
        def delayed(job):
            entered.set()
            release.wait(5)
            original(job)
        with mock.patch.object(self.engine, "_write_report", side_effect=delayed):
            job = self.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.engine.get_job(job["id"])["status"], "finalizing")
            self.assertEqual(self.engine.history()[0]["status"], "finalizing")
            with self.assertRaises(ValueError):
                self.engine.resume(job["id"])
            release.set()
            result = self.wait(job)
        self.assertEqual(result["status"], "completed")

    def test_disk_full_stops_without_retrying_every_file(self):
        with mock.patch.object(self.engine, "_copy", side_effect=OSError(errno.ENOSPC, "Disk full")) as copying:
            job = self.wait(self.start())
        self.assertEqual(copying.call_count, 1)
        self.assertEqual(job["status"], "failed")

    def test_long_destination_rejected_before_creation(self):
        destination = self.root / ("a" * 91)
        with self.assertRaisesRegex(ValueError, "shorter"):
            self.engine.start("TEST", str(destination))
        self.assertFalse(destination.exists())

    def test_raw_archive_is_structure_checked_without_extracting(self):
        job = self.wait(self.start())
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo("../../untrusted.txt")
            entry.size = 7
            archive.addfile(entry, io.BytesIO(b"fixture"))
        encoded = base64.b64encode(buffer.getvalue()).decode()
        program = "import sys,base64;sys.stdout.buffer.write(base64.b64decode('" + encoded + "'))"
        self.engine.adb.command = lambda serial, args: [sys.executable, "-c", program]
        self.engine.adb.process_options = lambda: {}
        result = self.engine._stream_archive(job, threading.Event(), ["exec-out"], "fixture", "private-apps", "fixture-private")
        self.assertEqual(Path(result).read_bytes(), buffer.getvalue())
        self.assertFalse((self.root / "untrusted.txt").exists())
        broken = base64.b64encode(buffer.getvalue()[:512]).decode()
        program = "import sys,base64;sys.stdout.buffer.write(base64.b64decode('" + broken + "'))"
        with self.assertRaisesRegex(AdbError, "failed or empty"):
            self.engine._stream_archive(job, threading.Event(), ["exec-out"], "broken", "private-apps", "broken")


class PathTests(unittest.TestCase):
    def test_windows_collisions_reserved_names_and_controls_are_safe(self):
        names = ["CON", "NUL.txt", "A", "a", "a.", "a ", "new\nline", "question?", "question*", "\u00e9", "e\u0301", "..name", "invalid-\udcff"]
        mapped = [safe_name(n) for n in names]
        self.assertEqual(len(mapped), len(set(n.casefold() for n in mapped)))
        for name in mapped:
            self.assertFalse(any(c in name for c in '<>:"/\\|?*\n'))
            self.assertFalse(name.endswith((".", " ")))

    def test_traversal_and_absolute_injection_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for source in ("/sdcard/../secret", "/sdcard/./a", "relative", "/sdcard//a"):
                with self.assertRaises(ValueError):
                    local_remote_path(root, "shared", source)
            for target in ("../outside", str(root.parent / "absolute")):
                with self.assertRaises(ValueError):
                    safe_join(root, target)

    def test_long_path_leaves_room_for_revisions_and_partial_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ("a" * 45) / ("b" * 37)
            remote = "/shared/" + "/".join(["a" * 80] * 8) + "/photo.jpeg"
            target = local_remote_path(root, "shared-storage", remote)
            self.assertLess(len(str(target)) + len("_revision_0123456789.partial-01234567"), 260)

    def test_long_dotted_basename_cannot_invent_oversized_fallback_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ("r" * (131 - len(str(Path(temp))) - 1))
            self.assertEqual(len(str(root)), 131)
            for basename in ("prefix.with.dots." + "x" * 140,
                             "base." + "x" * 140 + ".jpeg"):
                with self.subTest(basename=basename):
                    remote = "/apps/org.example/data/" + basename
                    target = local_remote_path(root, "shared-storage", remote)
                    self.assertIn("_long_paths", target.parts)
                    self.assertLessEqual(len(target.suffix), 12)
                    self.assertLess(len(str(target)) + len("_revision_0123456789.partial-01234567"), 260)
            self.assertEqual(local_remote_path(root, "shared-storage", "/apps/p/" + "x" * 140 + ".jpeg").suffix, ".jpeg")


if __name__ == "__main__":
    unittest.main()
