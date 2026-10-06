"""Loopback HTTP integration checks using synthetic archives only."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from server import LocalServer, _stat_identity


MEDIA_ID = "a" * 24


class Engine:
    def __init__(self, root):
        self.job = {"id": "fixture", "destination": str(root), "status": "complete"}
        self.calls = []

    def get_job(self, jid):
        if jid != "fixture":
            raise KeyError(jid)
        return dict(self.job)

    def history(self):
        return [dict(self.job)]

    def devices(self):
        return [{"serial": "fixture-phone", "state": "device"}]

    def start(self, *args):
        self.calls.append("start")
        self.job["status"] = "running"
        return dict(self.job)

    def resume(self, jid):
        self.calls.append("resume")
        return dict(self.job)

    analyze = resume
    verify = resume


class Reader:
    def __init__(self, root):
        self.root = root
        self.file = root / "synthetic.jpg"
        self.file.write_bytes(b"\xff\xd8\xff" + bytes(range(256)) * 8192)
        self.digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.state = "ready"
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def status(self, root):
        self.assert_root(root)
        return {"state": self.state, "counts": {"messages": 1, "media": 1}}

    def assert_root(self, root):
        assert root == self.root

    def build_index(self, root):
        self.assert_root(root)
        self.calls.append("build")
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("synthetic build deadline")
        self.state = "ready"
        return self.status(root)

    def list_threads(self, root, **kwargs):
        self.assert_root(root)
        self.calls.append(kwargs)
        return {"items": [{"id": "t1", "title": "Synthetic conversation"}], "total": 4,
                "offset": kwargs["offset"], "limit": kwargs["limit"]}

    list_messages = list_threads
    list_media = list_threads

    def lookup_media(self, root, mid):
        self.assert_root(root)
        if mid != MEDIA_ID:
            raise KeyError(mid)
        # Model the index layer's required source hash check.
        with self.file.open("rb") as source:
            if hashlib.sha256(source.read()).hexdigest() != self.digest:
                raise ValueError("PRIVATE_MARKER hash mismatch")
            identity = _stat_identity(os.fstat(source.fileno()))
        return {"path": self.file, "size": identity[2], "sha256": self.digest,
                "statIdentity": identity, "contentType": "image/jpeg",
                "downloadName": 'photo\r\nInject:evil-ä.jpg'}


class ReaderServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "archive-æ"
        self.root.mkdir()
        (self.base / "web").mkdir()
        for name in ("reader.html", "reader.js", "reader.css"):
            (self.base / "web" / name).write_text("synthetic-reader", encoding="utf-8")
        self.engine = Engine(self.root)
        self.reader = Reader(self.root)
        self.server = LocalServer(self.engine, base=self.base)
        self.server.reader = self.reader
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.reader.release.set()
        if self.server.reader_thread:
            self.server.reader_thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, path, data=None, headers=None, method=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = dict(headers or {})
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            if token:
                headers.setdefault("X-CSRF-Token", self.server.csrf)
        connection.request(method or ("GET" if data is None else "POST"), path,
                           None if data is None else json.dumps(data), headers)
        response = connection.getresponse()
        answer = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return answer

    @property
    def api(self):
        return "/api/jobs/fixture/reader/"

    @property
    def media_url(self):
        return self.api + "media/" + MEDIA_ID

    def test_fixed_assets_capability_and_demo(self):
        for path in ("/reader?job=fixture", "/reader.html?job=fixture", "/reader.js", "/reader.css"):
            status, headers, body = self.request(path)
            self.assertEqual((status, body), (200, b"synthetic-reader"))
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(json.loads(self.request("/api/config")[2])["readerAvailable"])
        self.server.demo = True
        self.assertFalse(json.loads(self.request("/api/config")[2])["readerAvailable"])
        self.assertEqual(self.request(self.api + "status")[0], 409)
        for path in ("/reader/../../server.py", "/reader-data", "/reader.js/extra"):
            self.assertEqual(self.request(path)[0], 404)

    def test_private_routes_enforce_origin_host_and_csrf(self):
        for path in (self.api + "status", self.api + "threads", self.media_url):
            for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(self.request(path, headers=headers)[0], 403)
        self.assertEqual(self.request(self.api + "build", {}, token=False)[0], 403)
        self.assertEqual(self.request(self.api + "build", {}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertNotIn("build", self.reader.calls)

    def test_metadata_lists_pagination_and_query_validation(self):
        self.assertEqual(json.loads(self.request(self.api + "status")[2])["state"], "ready")
        for suffix in ("threads?q=hello&offset=2&limit=1", "messages?thread=t1&offset=2&limit=1", "media?kind=video&offset=2&limit=1"):
            status, _, raw = self.request(self.api + suffix)
            self.assertEqual(status, 200)
            result = json.loads(raw)
            self.assertEqual(result["offset"], 2)
            self.assertTrue(result["hasMore"])
        for suffix in ("threads?limit=0", "media?offset=-1", "messages?limit=201", "media?kind=html",
                       "media?path=secret", "threads?q=a&q=b", "threads?q=%00", "messages?thread=../../secret",
                       "threads?limit=9999999999999999999999", "status?q=secret"):
            self.assertEqual(self.request(self.api + suffix)[0], 400, suffix)
        self.assertEqual(self.request("/api/jobs/unknown/reader/status")[0], 404)

    def test_build_async_and_busy_admission(self):
        self.reader.state = "missing"
        self.reader.release.clear()
        status, _, raw = self.request(self.api + "build", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["state"], "building")
        self.assertTrue(self.reader.entered.wait(1))
        self.assertEqual(json.loads(self.request(self.api + "status")[2])["state"], "building")
        self.assertEqual(self.request(self.api + "messages")[0], 409)
        for action in ("resume", "analyze", "verify"):
            self.assertEqual(self.request("/api/jobs/fixture/" + action, {})[0], 409)
        self.assertEqual(self.request("/api/shutdown", {})[0], 409)
        self.assertEqual(self.request("/api/jobs", {"serial": "fixture-phone", "destination": str(self.base / "new")})[0], 409)
        self.assertEqual(self.engine.calls, [])
        self.reader.release.set()
        self.server.reader_thread.join(2)
        self.assertEqual(json.loads(self.request(self.api + "status")[2])["state"], "ready")

    def test_existing_index_no_rebuild_and_active_acquisition_rejected(self):
        self.assertEqual(self.request(self.api + "build", {})[0], 200)
        self.assertEqual(self.reader.calls, [])
        for state in ("running", "finalizing", "verifying"):
            self.engine.job["status"] = state
            self.assertEqual(self.request(self.api + "build", {"rebuild": True})[0], 409)
        self.engine.job["status"] = "complete"
        for body in ({"rebuild": "yes"}, {"path": "elsewhere"}):
            self.assertEqual(self.request(self.api + "build", body)[0], 400)
        self.assertEqual(self.request(self.api + "build", {"rebuild": True})[0], 200)
        self.server.reader_thread.join(2)
        self.assertEqual(self.reader.calls, ["build"])

    def test_admission_prevents_start_build_race(self):
        admitted, release = threading.Event(), threading.Event()
        original = self.engine.start
        result = []

        def delayed(*args):
            admitted.set()
            release.wait(3)
            return original(*args)

        with mock.patch.object(self.engine, "start", side_effect=delayed):
            first = threading.Thread(target=lambda: result.append(self.request("/api/jobs", {"serial": "fixture-phone", "destination": str(self.base / "new")})))
            second = threading.Thread(target=lambda: result.append(self.request(self.api + "build", {"rebuild": True})))
            first.start()
            self.assertTrue(admitted.wait(1))
            second.start()
            release.set()
            first.join(3)
            second.join(3)
        self.assertEqual(sorted(r[0] for r in result), [200, 409])
        self.assertNotIn("build", self.reader.calls)

    def test_failed_build_and_lookup_errors_do_not_leak_paths_or_content(self):
        with mock.patch.object(self.reader, "build_index", side_effect=ValueError("PRIVATE_MARKER")):
            self.request(self.api + "build", {"rebuild": True})
            self.server.reader_thread.join(2)
        raw = self.request(self.api + "status")[2]
        self.assertEqual(json.loads(raw)["state"], "failed")
        self.assertNotIn(b"PRIVATE_MARKER", raw)
        # A failed rebuild can be retried by the ordinary first-run button even
        # when an older published index still exists.
        self.assertEqual(self.request(self.api + "build", {})[0], 200)
        self.server.reader_thread.join(2)
        self.assertEqual(json.loads(self.request(self.api + "status")[2])["state"], "ready")
        with mock.patch.object(self.reader, "lookup_media", side_effect=ValueError("PRIVATE_MARKER")):
            status, _, raw = self.request(self.media_url)
        self.assertEqual(status, 409)
        self.assertNotIn(b"PRIVATE_MARKER", raw)

    def test_full_stream_head_and_single_ranges(self):
        expected = self.reader.file.read_bytes()
        status, headers, body = self.request(self.media_url)
        self.assertEqual((status, body), (200, expected))
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertNotIn("\r", headers["Content-Disposition"])
        self.assertEqual(headers["X-Archive-Verification"], "sha256-verified-local-file")
        for value, first, last in (("bytes=0-3", 0, 3), ("bytes=4-", 4, len(expected)-1), ("bytes=-5", len(expected)-5, len(expected)-1), ("bytes=3-999999999999", 3, len(expected)-1)):
            status, headers, body = self.request(self.media_url, headers={"Range": value})
            self.assertEqual((status, body), (206, expected[first:last+1]))
            self.assertEqual(headers["Content-Range"], f"bytes {first}-{last}/{len(expected)}")
        status, headers, body = self.request(self.media_url, method="HEAD")
        self.assertEqual((status, body, int(headers["Content-Length"])), (200, b"", len(expected)))
        status, headers, body = self.request(self.media_url, method="HEAD", headers={"Range": "bytes=1-3"})
        self.assertEqual((status, body, int(headers["Content-Length"])), (206, b"", 3))

    def test_invalid_ranges_are_416_with_total_size(self):
        for value in ("bytes=0-1,3-4", "bytes=2-1", "bytes=-0", "bytes=", "items=1-2", "bytes=9999999999-", "bytes=" + "9" * 100 + "-", "bytes=--1"):
            status, headers, body = self.request(self.media_url, headers={"Range": value})
            self.assertEqual((status, body), (416, b""), value)
            self.assertEqual(headers["Content-Range"], "bytes */" + str(self.reader.file.stat().st_size))

    def test_untrusted_active_types_download_and_safe_types_sniffed(self):
        cases = [(b"<html>test</html>", "application/octet-stream"), (b"<svg onload='x'/>", "application/octet-stream"),
                 (b"%PDF-1.7", "application/octet-stream"), (b"\0\0\0\x20ftypisom" + b"\0"*20, "video/mp4"),
                 (b"RIFF\0\0\0\0WAVE", "audio/wav")]
        for data, mime in cases:
            self.reader.file.write_bytes(data)
            self.reader.digest = hashlib.sha256(data).hexdigest()
            status, headers, body = self.request(self.media_url)
            self.assertEqual((status, body, headers["Content-Type"]), (200, data, mime))
            self.assertTrue(headers["Content-Disposition"].startswith("attachment" if mime == "application/octet-stream" else "inline"))
        self.assertTrue(self.request(self.media_url + "?download=1")[1]["Content-Disposition"].startswith("attachment"))
        self.assertEqual(self.request(self.media_url + "?download=2")[0], 400)

    def test_changed_hash_or_opened_identity_denied(self):
        self.reader.file.write_bytes(b"changed after index")
        self.assertEqual(self.request(self.media_url)[0], 409)
        self.reader.digest = hashlib.sha256(self.reader.file.read_bytes()).hexdigest()
        record = self.reader.lookup_media(self.root, MEDIA_ID)
        record["statIdentity"][1] += 1
        with mock.patch.object(self.reader, "lookup_media", return_value=record):
            self.assertEqual(self.request(self.media_url)[0], 409)
        record.pop("statIdentity")
        with mock.patch.object(self.reader, "lookup_media", return_value=record):
            self.assertEqual(self.request(self.media_url)[0], 409)

    def test_lookup_cannot_serve_outside_or_parent_traversal(self):
        record = self.reader.lookup_media(self.root, MEDIA_ID)
        for path in (self.base / "outside.bin", self.root / ".." / "outside.bin"):
            record["path"] = path
            with mock.patch.object(self.reader, "lookup_media", return_value=record):
                self.assertEqual(self.request(self.media_url)[0], 409)
        for path in (self.api + "media/../secret", self.api + "media/%2e%2e", self.api + "media/" + "a"*25):
            self.assertEqual(self.request(path)[0], 404)

    def test_reparse_attribute_is_rejected(self):
        original = Path.lstat

        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.root:
                return type("Reparse", (), {"st_mode": info.st_mode, "st_file_attributes": 0x400})()
            return info

        with mock.patch.object(Path, "lstat", reparse):
            self.assertEqual(self.request(self.api + "status")[0], 409)
            self.assertEqual(self.request(self.media_url)[0], 409)

    def test_actual_index_module_build_list_stream_and_tamper(self):
        import archive_reader
        from tests.test_archive_reader import build_fixture, PNG
        build_fixture(self.root)
        self.server.reader = archive_reader
        self.assertEqual(self.request(self.api + "build", {})[0], 200)
        self.server.reader_thread.join(5)
        status = json.loads(self.request(self.api + "status")[2])
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["counts"]["messages"], 3)
        threads = json.loads(self.request(self.api + "threads?q=Synthetic%20Alice")[2])
        self.assertEqual(threads["total"], 1)
        messages = json.loads(self.request(self.api + "messages?thread=" + threads["items"][0]["id"])[2])
        self.assertEqual(messages["total"], 2)
        attachment = messages["items"][1]["attachments"][0]
        path = self.api + "media/" + attachment["id"]
        code, headers, body = self.request(path)
        self.assertEqual((code, headers["Content-Type"], body), (200, "image/png", PNG))
        # Cached lookup still checks source identity and rehashes changed files.
        resolved = archive_reader.lookup_media(self.root, attachment["id"])["path"]
        before = resolved.stat()
        resolved.write_bytes(b"x" * len(PNG))
        os.utime(resolved, ns=(before.st_atime_ns, before.st_mtime_ns))
        code, _, body = self.request(path)
        self.assertEqual(code, 409)
        self.assertNotIn(b"x" * len(PNG), body)


if __name__ == "__main__":
    unittest.main()
