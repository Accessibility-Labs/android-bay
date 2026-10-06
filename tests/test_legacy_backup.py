"""Legacy command version selection; never starts a real phone backup."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import RescueEngine, legacy_backup_command


BASE = ["backup", "-all", "-apk", "-obb", "-shared"]
APP_DATA = ["backup", "-all", "-noapk", "-noobb", "-noshared"]


class LegacyBackupCommandTests(unittest.TestCase):
    def test_options_follow_aosp_parser_version_boundaries(self):
        fixtures = {
            14: BASE,
            19: BASE,
            20: BASE,
            21: BASE + ["-widgets"],
            25: BASE + ["-widgets"],
            26: BASE + ["-widgets", "-keyvalue"],
            30: BASE + ["-widgets", "-keyvalue"],
            36: BASE + ["-widgets", "-keyvalue"],
        }
        for api, expected in fixtures.items():
            for sdk in (api, str(api)):
                with self.subTest(sdk=sdk):
                    self.assertEqual(legacy_backup_command(sdk), expected)

    def test_unknown_api_does_not_guess_new_options(self):
        for sdk in (None, "Unknown", "", True, 30.5, "30.0", "30; -keyvalue", [], {}, "-1"):
            with self.subTest(sdk=sdk):
                self.assertEqual(legacy_backup_command(sdk), BASE)

    def test_inclusion_options_preserve_app_data_and_version_gates(self):
        combinations = (
            (False, False, APP_DATA),
            (False, True, ["backup", "-all", "-noapk", "-obb", "-shared"]),
            (True, False, ["backup", "-all", "-apk", "-noobb", "-noshared"]),
            (True, True, BASE),
        )
        for sdk, extra in (("19", []), ("25", ["-widgets"]), ("30", ["-widgets", "-keyvalue"]), (None, [])):
            for apks, shared, expected in combinations:
                with self.subTest(sdk=sdk, apks=apks, shared=shared):
                    self.assertEqual(legacy_backup_command(sdk, include_apks=apks, include_shared=shared), expected + extra)

    def test_worker_uses_current_sdk_and_inclusion_options(self):
        combinations = (
            ({"apks": False, "shared": False}, APP_DATA),
            ({"apks": False, "shared": True}, ["backup", "-all", "-noapk", "-obb", "-shared"]),
            ({"apks": True, "shared": False}, ["backup", "-all", "-apk", "-noobb", "-noshared"]),
            ({"apks": True, "shared": True}, BASE),
            ({}, BASE),
        )
        cases = [(sdk, options, flags + extra) for sdk, extra in
                 (("19", []), ("25", ["-widgets"]), ("30", ["-widgets", "-keyvalue"]), ("Unknown", []))
                 for options, flags in combinations]
        for sdk, options, expected in cases:
            with self.subTest(sdk=sdk, options=options), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root = base / "recovery"
                root.mkdir()
                rescue = RescueEngine(base, base / "missing-adb")
                # Deliberately conflicting release text ensures the worker uses SDK,
                # not release guessing or stale job metadata.
                info = {"serial": "FIXTURE", "sdk": sdk, "android": "99", "model": "Fixture", "properties": {},
                        "roots": [{"path": "/fixture", "accessible": True}]}
                job = {"id": "a" * 32, "serial": "FIXTURE", "sdk": "99", "status": "running",
                       "destination": str(root), "filesCopied": 0, "filesTotal": 0, "bytesCopied": 0,
                       "errors": [], "coverage": [], "options": {"helper": False, "legacy": True, **options}}
                with patch.object(rescue, "inspect", return_value=info), \
                        patch.object(rescue, "_canonical", return_value="/fixture"), \
                        patch.object(rescue, "_walk"), \
                        patch.object(rescue, "_apk_inventory", return_value=[]), \
                        patch.object(rescue, "_stream_archive", return_value="fixture.ab") as capture, \
                        patch.object(rescue, "_write_report"):
                    rescue._run(job, threading.Event())
                capture.assert_called_once()
                self.assertEqual(capture.call_args.args[2], expected)
                self.assertEqual(capture.call_args.args[3:], ("legacy-backup", "legacy", "legacy-adb-backup"))
                self.assertEqual(capture.call_args.kwargs, {"output_arg": True})
                self.assertEqual(job["errors"], [])
                legacy = next(row for row in job["coverage"] if row["category"] == "Legacy Android backup")
                self.assertEqual(legacy["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
