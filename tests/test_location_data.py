import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adb import Cancelled
import location_data as locations


class ParserTests(unittest.TestCase):
    def test_gpx_waypoints_tracks_and_recorded_times(self):
        data = b'<gpx xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="33.5" lon="-112.2"><time>2020-01-01T01:02:03Z</time></wpt><trk><trkseg><trkpt lat="34" lon="-113"/></trkseg></trk></gpx>'
        points = list(locations.parse_xml(data, ".gpx"))
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["geometry"]["coordinates"], [-112.2, 33.5])
        self.assertEqual(points[0]["properties"]["timestamp"], "2020-01-01T01:02:03Z")
        self.assertNotIn("timestamp", points[1]["properties"])

    def test_kml_longitude_first_and_gx_times(self):
        data = b'<kml xmlns:gx="http://www.google.com/kml/ext/2.2"><Placemark><Point><coordinates>-112,33,400</coordinates></Point></Placemark><Placemark><gx:Track><when>2020-01-01T01:00:00Z</when><gx:coord>-114 35 0</gx:coord></gx:Track></Placemark></kml>'
        points = list(locations.parse_xml(data, ".kml"))
        self.assertEqual([p["geometry"]["coordinates"] for p in points], [[-112, 33], [-114, 35]])
        self.assertEqual(points[1]["properties"]["timestamp"], "2020-01-01T01:00:00Z")

    def test_takeout_e7_semantic_and_new_timeline_offsets(self):
        data = {"locations": [{"latitudeE7": 335000000, "longitudeE7": -1122000000, "timestampMs": "1577836800000"}],
                "timelineObjects": [{"placeVisit": {"duration": {"startTimestamp": "2020-02-01T00:00:00Z"}, "location": {"latitudeE7": 345000000, "longitudeE7": -1132000000}}}],
                "semanticSegments": [{"startTime": "2020-03-01T00:00:00Z", "timelinePath": [{"point": "geo:33.7,-112.7", "durationMinutesOffsetFromStartTime": 5}]}]}
        points = list(locations.parse_json(data))
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0]["properties"]["timestamp"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(points[1]["properties"]["timestamp"], "2020-02-01T00:00:00Z")
        self.assertEqual(points[2]["properties"]["timestamp"], "2020-03-01T00:05:00+00:00")

    def test_geojson_coordinates_are_not_inferred_from_unlabeled_arrays(self):
        value = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"time": "2020-01-01"}, "geometry": {"type": "LineString", "coordinates": [[-112, 33], [-113, 34]]}}]}
        self.assertEqual(len(list(locations.parse_json(value))), 2)
        self.assertEqual(list(locations.parse_json({"pixels": [30, 40], "lat": 33, "nested": {"lon": -112}})), [])
        with self.assertRaises(ValueError):
            list(locations.parse_json({**value, "crs": {"properties": {"name": "EPSG:3857"}}}))

    def test_invalid_coordinates_and_xml_entities_are_rejected(self):
        for value in ({"lat": 91, "lon": 20}, {"latitude": float("nan"), "longitude": 0}, {"lat": True, "lon": 12}):
            with self.assertRaises(ValueError):
                list(locations.parse_json(value))
        with self.assertRaises(ValueError):
            list(locations.parse_xml(b'<!DOCTYPE x [<!ENTITY x "x">]><gpx/>', ".gpx"))

    def test_media_refs_and_iso6709_preserve_sign_and_utc_uncertainty(self):
        item = {"Main:GPSLatitude": 33.5, "Main:GPSLatitudeRef": "S", "Main:GPSLongitude": 112,
                "Main:GPSLongitudeRef": "W", "Main:DateTimeOriginal": "2020:01:01 12:00:00"}
        point = next(locations._media_points(item))
        self.assertEqual(point["geometry"]["coordinates"], [-112, -33.5])
        self.assertEqual(point["properties"]["timestamp"], "2020:01:01 12:00:00")
        self.assertEqual(next(locations._media_points({"Doc1:GPSCoordinates": "+33.5-112.2+300/"}))["geometry"]["coordinates"], [-112.2, 33.5])

    def test_explicit_map_links_shortlinks_and_address_text_are_references(self):
        value = {"values": {"body": "Meet geo:33.5,-112.2 then https://maps.google.com/?q=34.1%2C-113.2 https://www.google.com/maps/@35.1,-114.2,15z https://maps.app.goo.gl/abc", "date": 1577836800000},
                 "calendar": {"eventLocation": "123 Example Street"}}
        refs = list(locations.parse_references(value))
        self.assertEqual(len(refs), 5)
        self.assertEqual(sum(r["coordinates"] is not None for r in refs), 3)
        self.assertTrue(any(r["value"] == "https://maps.app.goo.gl/abc" and r["coordinates"] is None for r in refs))
        self.assertTrue(any(r["rawTimeFields"].get("date") == 1577836800000 for r in refs))
        self.assertEqual(list(locations.parse_references({"body": "numbers 33.5,-112.2 and https://maps.google.com.evil.invalid/?q=33,44"})), [])

    def test_geo_uri_placeholder_does_not_become_equator_location(self):
        refs = list(locations.parse_references({"body": "geo:0,0?q=123%20Example%20Street"}))
        self.assertEqual(len(refs), 1)
        self.assertIsNone(refs[0]["coordinates"])

    def test_sms_telephone_address_is_not_a_postal_location(self):
        refs = list(locations.parse_references({"values": {"address": "+15555550123", "body": "hello"}}))
        self.assertEqual(refs, [])
        refs = list(locations.parse_references({"values": {"mimetype": "vnd.android.cursor.item/postal-address_v2", "data1": "123 Example Street"}}))
        self.assertEqual(len(refs), 1)
        self.assertIsNone(refs[0]["coordinates"])

    def test_semantic_end_location_uses_end_time_or_remains_unspecified(self):
        value = {"activitySegment": {"duration": {"startTimestamp": "2020-01-01T01:00:00Z", "endTimestamp": "2020-01-01T02:00:00Z"},
                                      "startLocation": {"latitudeE7": 330000000, "longitudeE7": -1120000000},
                                      "endLocation": {"latitudeE7": 340000000, "longitudeE7": -1130000000}}}
        points = list(locations.parse_json(value))
        self.assertEqual([p["properties"]["timestamp"] for p in points], ["2020-01-01T01:00:00Z", "2020-01-01T02:00:00Z"])
        del value["activitySegment"]["duration"]["endTimestamp"]
        points = list(locations.parse_json(value))
        self.assertNotIn("timestamp", points[1]["properties"])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.records = []

    def tearDown(self):
        self.temp.cleanup()

    def add(self, source, data, name=None):
        path = self.root / "originals" / (name or f"file-{len(self.records)}")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        record = {"source": source, "localPath": str(path.relative_to(self.root)), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "status": "copied"}
        self.records.append(record)
        with (self.root / "manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        return path

    def scan(self, **kwargs):
        return locations.check_archive(self.root, exiftool_path=self.root / "missing-exiftool.exe", **kwargs)

    def test_scan_copies_sources_separately_with_hashes_and_is_idempotent(self):
        data = b'{"locations":[{"latitudeE7":335000000,"longitudeE7":-1122000000,"timestampMs":"1577836800000"}]}'
        original = self.add("/sdcard/Takeout/Location History/Records.json", data)
        progress = []
        first = self.scan(progress=lambda phase, message: progress.append((phase, message)))
        second = self.scan()
        self.assertEqual(first["counts"]["features"], 1)
        self.assertEqual(first["status"], "complete", first)
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(original.read_bytes(), data)
        self.assertEqual(len(list((self.root / "location-data/source-files").iterdir())), 1)
        geo = json.loads(Path(first["geojsonPath"]).read_text())
        self.assertEqual(geo["features"][0]["properties"]["sourceSha256"], self.records[0]["sha256"])
        self.assertTrue(progress)
        self.assertEqual(second["previousScans"], 0)

    def test_changed_or_missing_original_preserves_previous_extracted_findings(self):
        path = self.add("/sdcard/location.json", b'{"lat":33,"lon":-112}')
        first = self.scan()
        previous_geo = Path(first["geojsonPath"]).read_bytes()
        previous_report = Path(first["reportPath"]).read_bytes()
        path.write_bytes(b'{"lat":34,"lon":-113}')
        second = self.scan()
        self.assertEqual(second["counts"]["features"], 0)
        self.assertEqual(second["previousScans"], 1)
        snapshot = Path(second["preservedPrevious"])
        self.assertEqual((snapshot / "locations.geojson").read_bytes(), previous_geo)
        self.assertEqual((snapshot / "report.html").read_bytes(), previous_report)
        third = self.scan()
        self.assertEqual(third["previousScans"], 1)
        self.assertIsNone(third["preservedPrevious"])
        self.assertIn("previous scan snapshots preserved", Path(third["reportPath"]).read_text())

    def test_map_references_have_separate_source_index_without_network_calls(self):
        data = b'{"values":{"body":"See https://maps.google.com/?q=33.5,-112.2 and https://maps.app.goo.gl/example","date":1234}}\n'
        self.add("/sdcard/AndroidRescue/exports/run/sms.jsonl", data)
        result = self.scan()
        self.assertEqual(result["counts"]["references"], 2)
        self.assertEqual(result["counts"]["unresolvedReferences"], 1)
        self.assertEqual(result["counts"]["features"], 1)
        refs = [json.loads(line) for line in Path(result["referencesPath"]).read_text().splitlines()]
        self.assertTrue(all(r["sourceSha256"] == self.records[0]["sha256"] for r in refs))
        self.assertTrue(all("not establish a visit" in r["meaning"] for r in refs))

    def test_hash_mismatch_prevents_coordinate_claims(self):
        path = self.add("/sdcard/locations.geojson", b'{"lat":33,"lon":-112}')
        path.write_bytes(b'{"lat":34,"lon":-113}')
        result = self.scan()
        self.assertEqual(result["counts"]["features"], 0)
        self.assertEqual(result["status"], "partial")
        self.assertIn("SHA-256", result["issues"][0]["error"])

    def test_unsupported_location_database_remains_a_candidate(self):
        original = self.add("/sdcard/maps/location-cache.sqlite", b"SQLite fixture")
        result = self.scan()
        self.assertEqual(result["counts"]["candidateFiles"], 1)
        self.assertEqual(result["counts"]["features"], 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(original.read_bytes(), b"SQLite fixture")

    def test_kmz_safe_parse_and_traversal_bomb_bounds(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("nested/doc.kml", '<kml><Placemark><Point><coordinates>-112,33</coordinates></Point></Placemark></kml>')
        self.add("/sdcard/route.kmz", buffer.getvalue())
        self.assertEqual(self.scan()["counts"]["features"], 1)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../outside.kml", "<kml/>")
        self.add("/sdcard/unsafe.kmz", buffer.getvalue())
        result = self.scan()
        self.assertTrue(any("Unsafe KMZ" in i["error"] for i in result["issues"]))
        self.assertFalse((self.root.parent / "outside.kml").exists())
        with mock.patch.object(locations, "MAX_ZIP_BYTES", 3):
            self.assertTrue(any("expanded" in i["error"] for i in self.scan()["issues"]))

    def test_manifest_traversal_and_torn_line_are_reported_without_escape(self):
        (self.root / "manifest.jsonl").write_text('{torn\n' + json.dumps({"status": "copied", "source": "/sdcard/location.json", "localPath": "../outside.json", "size": 2, "sha256": "0" * 64}) + "\n")
        result = self.scan()
        self.assertEqual(result["counts"]["features"], 0)
        self.assertEqual(result["counts"]["issues"], 2)

    def test_cancellation_preserves_completed_reports_and_originals(self):
        original = self.add("/sdcard/track.gpx", b'<gpx><wpt lat="33" lon="-112"/></gpx>')
        first = self.scan()
        report_bytes = Path(first["reportPath"]).read_bytes()
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.scan(cancel=event)
        self.assertEqual(Path(first["reportPath"]).read_bytes(), report_bytes)
        self.assertTrue(original.exists())

    def test_media_results_are_matched_only_to_verified_batch_files(self):
        media = self.add("/sdcard/DCIM/photo.jpg", b"fake photo")
        tool = self.root / "exiftool.exe"
        tool.write_bytes(b"fixture")
        def metadata(executable, paths, cancel):
            return [{"SourceFile": str(media), "Main:GPSLatitude": 33.5, "Main:GPSLongitude": -112.2},
                    {"SourceFile": str(self.root.parent / "outside.jpg"), "Main:GPSLatitude": 10, "Main:GPSLongitude": 20}], ""
        with mock.patch.object(locations, "_exiftool", side_effect=metadata):
            result = locations.check_archive(self.root, exiftool_path=tool)
        self.assertEqual(result["counts"]["gpsMedia"], 1)
        self.assertEqual(result["counts"]["features"], 1)
        self.assertEqual(media.read_bytes(), b"fake photo")

    def test_media_warnings_errors_and_unknown_diagnostics_remain_issues(self):
        media = self.add("/sdcard/DCIM/photo.jpg", b"fake photo")
        tool = self.root / "exiftool.exe"
        tool.write_bytes(b"fixture")
        for diagnostic in ("Warning: malformed metadata", "Error: truncated input", "Unrecognized diagnostic text"):
            with self.subTest(diagnostic=diagnostic):
                with mock.patch.object(locations, "_exiftool", return_value=([{"SourceFile": str(media)}], diagnostic)):
                    result = locations.check_archive(self.root, exiftool_path=tool)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["counts"]["issues"], 1)
                self.assertIn(diagnostic, result["issues"][0]["error"])
        with mock.patch.object(locations, "_exiftool", return_value=(
                [{"SourceFile": str(media), "ExifTool:Warning": "Invalid metadata structure"}], "")):
            result = locations.check_archive(self.root, exiftool_path=tool)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["issues"], 1)
        self.assertIn("Invalid metadata structure", result["issues"][0]["error"])
        self.assertEqual(media.read_bytes(), b"fake photo")

    def test_structured_source_mutation_does_not_commit_unverified_points(self):
        path = self.add("/sdcard/location.json", b'{"lat":33,"lon":-112}')
        original_parser = locations.parse_file
        def mutate(source, suffix, cancel, include_references=False):
            yield from original_parser(source, suffix, cancel, include_references)
            source.write_bytes(b'{"lat":34,"lon":-113}')
        with mock.patch.object(locations, "parse_file", side_effect=mutate):
            result = self.scan()
        self.assertEqual(result["counts"]["features"], 0)
        self.assertEqual(result["status"], "partial")

    def test_media_mutation_after_early_hash_does_not_commit_coordinates(self):
        media = self.add("/sdcard/DCIM/photo.jpg", b"fake photo")
        tool = self.root / "exiftool.exe"
        tool.write_bytes(b"fixture")
        def metadata(executable, paths, cancel):
            media.write_bytes(b"changed!!!")
            return [{"SourceFile": str(media), "Main:GPSLatitude": 33.5, "Main:GPSLongitude": -112.2}], ""
        with mock.patch.object(locations, "_exiftool", side_effect=metadata):
            result = locations.check_archive(self.root, exiftool_path=tool)
        self.assertEqual(result["counts"]["features"], 0)
        self.assertEqual(result["counts"]["gpsMedia"], 0)
        self.assertEqual(result["status"], "partial")

    def test_missing_exiftool_reports_media_gap(self):
        self.add("/sdcard/DCIM/photo.jpg", b"fake photo")
        result = self.scan()
        self.assertEqual(result["counts"]["mediaExamined"], 0)
        self.assertEqual(result["status"], "partial")
        self.assertIn("ExifTool is unavailable", result["issues"][0]["error"])

    def test_html_source_names_are_escaped_and_no_network_map_is_added(self):
        self.add("/sdcard/<script>alert(1)</script>/route.gpx", b'<gpx><wpt lat="33" lon="-112"/></gpx>')
        result = self.scan()
        page = Path(result["reportPath"]).read_text()
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)


class RealExifToolTests(unittest.TestCase):
    def test_official_exiftool_batch_has_no_false_issues_and_keeps_inputs_unchanged(self):
        executable = Path(__file__).resolve().parents[1] / "tools/exiftool/exiftool.exe"
        if not executable.is_file():
            self.skipTest("Portable ExifTool not bundled in this environment")
        # Minimal valid JPEG fixture, generated for this test only.
        jpeg = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = [root / f"synthetic-{number}.jpg" for number in range(2)]
            for path in paths:
                path.write_bytes(jpeg)
            env = dict(os.environ, LC_ALL="C", LANG="C", LC_CTYPE="C")
            result = subprocess.run([str(executable), "-config", "", "-overwrite_original", "-GPSLatitude=33.5", "-GPSLatitudeRef=N", "-GPSLongitude=112.2", "-GPSLongitudeRef=W", "-DateTimeOriginal=2020:01:01 12:00:00"] + [str(path) for path in paths], capture_output=True, timeout=60, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            before = {path: path.read_bytes() for path in paths}
            metadata, diagnostics = locations._exiftool(executable, paths)
            self.assertEqual(len(metadata), 2)
            self.assertEqual(diagnostics, "")
            for item in metadata:
                points = list(locations._media_points(item))
                self.assertTrue(points, item)
                self.assertAlmostEqual(points[0]["geometry"]["coordinates"][0], -112.2)
            records = [{"source": "/sdcard/DCIM/" + path.name, "localPath": path.name,
                        "status": "copied", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                       for path, content in before.items()]
            (root / "manifest.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            scan = locations.check_archive(root, exiftool_path=executable)
            self.assertEqual(scan["status"], "complete", scan["issues"])
            self.assertEqual(scan["counts"]["issues"], 0)
            self.assertEqual(scan["counts"]["mediaExamined"], 2)
            self.assertEqual(scan["counts"]["gpsMedia"], 2)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_official_exiftool_keeps_real_format_warnings(self):
        executable = Path(__file__).resolve().parents[1] / "tools/exiftool/exiftool.exe"
        if not executable.is_file():
            self.skipTest("Portable ExifTool not bundled in this environment")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "synthetic-malformed-exif.jpg"
            segment = b"Exif\0\0garbage"
            original = b"\xff\xd8\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment + b"\xff\xd9"
            path.write_bytes(original)
            metadata, diagnostics = locations._exiftool(executable, [path])
            self.assertEqual(len(metadata), 1)
            self.assertIn("Warning: Malformed APP1 EXIF segment", diagnostics)
            record = {"source": "/sdcard/DCIM/" + path.name, "localPath": path.name, "status": "copied",
                      "size": len(original), "sha256": hashlib.sha256(original).hexdigest()}
            (root / "manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
            scan = locations.check_archive(root, exiftool_path=executable)
            self.assertEqual(scan["status"], "partial")
            self.assertGreater(scan["counts"]["issues"], 0)
            self.assertTrue(any("Malformed APP1 EXIF segment" in item["error"] for item in scan["issues"]))
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
