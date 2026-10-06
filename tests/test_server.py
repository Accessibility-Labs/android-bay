import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import LocalServer


class FakeEngine:
    def __init__(self):
        self.calls = []

    def devices(self):
        return [{"serial": "authorized", "state": "device", "model": "Test phone"}, {"serial": "locked", "state": "unauthorized"}]

    def inspect(self, serial):
        self.calls.append(("inspect", serial))
        return {"serial": serial, "roots": []}

    def history(self):
        return []

    def start(self, serial, destination, options):
        self.calls.append(("start", serial, destination, options))
        return {"id": "test", "status": "running"}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        (cls.base / "web").mkdir()
        (cls.base / "web/index.html").write_text("<h1>Test UI</h1>")
        cls.engine = FakeEngine()
        cls.server = LocalServer(cls.engine, base=cls.base)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        all_headers = dict(headers or {})
        if data is not None:
            all_headers.setdefault("Content-Type", "application/json")
            if token:
                all_headers.setdefault("X-CSRF-Token", self.server.csrf)
        conn.request("GET" if data is None else "POST", path, None if data is None else json.dumps(data), all_headers)
        response = conn.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        conn.close()
        return result

    def test_config_local_and_no_cache(self):
        status, headers, raw = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["csrfToken"], self.server.csrf)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_analysis_endpoint_requires_token_and_preserves_job_identity(self):
        job = {"id": "test", "status": "partial", "destination": str(self.base)}
        with mock.patch.object(self.engine, "get_job", return_value=job, create=True), mock.patch.object(self.engine, "analyze", return_value={**job, "status": "running"}, create=True) as analyze:
            self.assertEqual(self.request("/api/jobs/test/analyze", {}, token=False)[0], 403)
            analyze.assert_not_called()
            status, _, raw = self.request("/api/jobs/test/analyze", {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["job"]["id"], "test")
            analyze.assert_called_once_with("test")

    def test_derived_report_open_uses_fixed_archive_relative_path(self):
        job = {"id": "test", "destination": str(self.base), "reportPath": str(self.base / "report.html")}
        folder = self.base / "location-data"
        folder.mkdir(exist_ok=True)
        report = folder / "report.html"
        report.write_text("<h1>Location fixture</h1>", encoding="utf-8")
        with mock.patch.object(self.engine, "get_job", return_value=job, create=True), mock.patch("server.os.startfile", create=True) as opened:
            status, _, _ = self.request("/api/jobs/test/open", {"kind": "locations", "path": "C:/unrelated-file"})
            self.assertEqual(status, 200)
            opened.assert_called_once_with(str(report.resolve()))
            self.assertEqual(self.request("/api/jobs/test/open", {"kind": "../location-data"})[0], 400)
            context_report = self.base / "device-context" / "report.html"
            context_report.parent.mkdir(exist_ok=True)
            context_report.write_text("<h1>Device context fixture</h1>", encoding="utf-8")
            opened.reset_mock()
            self.assertEqual(self.request("/api/jobs/test/open", {"kind": "context", "path": "C:/unrelated-file"})[0], 200)
            opened.assert_called_once_with(str(context_report.resolve()))

    def test_dns_rebinding_host_blocked(self):
        self.assertEqual(self.request("/api/config", headers={"Host": "attacker.example"})[0], 403)

    def test_cross_site_read_blocked(self):
        self.assertEqual(self.request("/api/jobs", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_missing_token_blocked(self):
        self.assertEqual(self.request("/api/inspect", {"serial": "authorized"}, token=False)[0], 403)

    def test_foreign_origin_blocked_with_token(self):
        self.assertEqual(self.request("/api/inspect", {"serial": "authorized"}, {"Origin": "https://attacker.example"})[0], 403)

    def test_unauthorized_phone_cannot_start(self):
        self.assertEqual(self.request("/api/inspect", {"serial": "locked"})[0], 400)

    def test_authorized_inspection(self):
        self.assertEqual(self.request("/api/inspect", {"serial": "authorized"})[0], 200)

    def test_path_traversal_static_blocked(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/state/server.json", "/server.py"):
            self.assertEqual(self.request(path)[0], 404)

    def test_static_security_headers(self):
        status, headers, raw = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"Test UI", raw)

    def test_bad_options_rejected(self):
        dest = str(self.base / "output")
        for options in ({"shared": "true"}, {"execute": "command"}, {"roots": "not a list"}):
            self.assertEqual(self.request("/api/jobs", {"serial": "authorized", "destination": dest, "options": options})[0], 400)

    def test_destination_cannot_target_runtime(self):
        self.assertEqual(self.request("/api/jobs", {"serial": "authorized", "destination": str(self.base / "runtime"), "options": {}})[0], 400)

    def test_absolute_destination_accepted(self):
        status, _, raw = self.request("/api/jobs", {"serial": "authorized", "destination": str(self.base / "output"), "options": {"shared": True}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["job"]["status"], "running")

    def test_device_context_is_a_boolean_acquisition_option(self):
        body = {"serial": "authorized", "destination": str(self.base / "context-output"), "options": {"shared": False, "apks": False, "helper": False, "deviceContext": True}}
        self.assertEqual(self.request("/api/jobs", body)[0], 200)
        self.assertTrue(self.engine.calls[-1][3]["deviceContext"])
        body["options"]["deviceContext"] = "true"
        self.assertEqual(self.request("/api/jobs", body)[0], 400)

    def test_unknown_method_and_non_object(self):
        self.assertEqual(self.request("/api/anything", {})[0], 400)
        self.assertEqual(self.request("/api/inspect", ["authorized"])[0], 400)


if __name__ == "__main__":
    unittest.main()
