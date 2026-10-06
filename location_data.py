"""Offline location-data inventory. Reads verified acquisitions; never edits them.

Format references: tkrajina/gpxpy, Makeshit/Timeline-GPX-Exporter,
DovarFalcone/google-takeout-location-parser, Google KML reference and ExifTool.
No repository implementation is vendored. No networking or reverse geocoding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import shutil
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import parse_qs, unquote, urlsplit

from adb import Cancelled

MAX_DOCUMENT = 128 * 1024 * 1024
MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_ZIP_MEMBERS = 2000
MAX_FEATURES = 2_000_000
MAX_REFERENCES = 500_000
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw", ".webp", ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mts", ".m2ts", ".mpg", ".mpeg", ".wav"}
LOCATION_HINT = re.compile(r"location|timeline|takeout|gps|geotrack|navigation|geodata|\.gpx|\.kml|\.kmz|\.geojson|(?:^|/)records\.json$", re.I)
LIMITATIONS = [
    "This checks copied files only. It cannot establish that all location data on the phone was acquired.",
    "Private Google Maps/Play services databases, inaccessible app data and cloud-only Timeline history are not unlocked or downloaded by this scan.",
    "Coordinates may describe saved places, planned routes, maps, or media received from another person. They do not by themselves prove the phone or its owner was present.",
    "Only explicit coordinates are parsed. Recognized map links and postal/calendar address text are kept as references, not visit evidence. Addresses, shortlinks, map tiles and place names are not geocoded or resolved online.",
    "GPX/KML/KMZ and common GeoJSON/Timeline/Takeout coordinate structures are supported. Unsupported or oversized candidates remain preserved with an explicit issue.",
    "ExifTool reads photo/video GPS metadata and supported embedded GPS streams. Missing GPS tags do not prove a file never had location data; stripped or unsupported metadata may be unavailable.",
    "Timestamps are retained where explicit. Camera times without an offset remain local/unspecified; no phone location or time zone is inferred.",
    "No coordinates are uploaded. The output has no external map tiles, geocoding calls, scripts or remote fonts.",
]
_LOCK = threading.Lock()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Location scan cancelled; originals and previous completed reports remain intact")


def _safe(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or any(":" in p for p in relative.parts):
        raise ValueError("Unsafe archive-relative path")
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError("Location scan path escapes the archive")
    return target


def _hash(path, cancel=None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                return result.hexdigest()
            result.update(block)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a coordinate")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite coordinate")
    return number


def _point(lat, lon, **props):
    lat, lon = _number(lat), _number(lon)
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Coordinate outside latitude/longitude ranges")
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: v for k, v in props.items() if v is not None}}


def _time(value, milliseconds=False):
    if value is None:
        return None
    if milliseconds:
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return str(value)[:200]
    return str(value)[:200]


def _timestamp(obj, inherited=None):
    if not isinstance(obj, dict):
        return inherited
    for key in ("timestamp", "time", "dateTime", "datetime", "startTime", "startTimestamp", "GPSDateTime"):
        if key in obj and isinstance(obj[key], (str, int, float)):
            return _time(obj[key])
    for key in ("timestampMs", "startTimestampMs"):
        if key in obj:
            return _time(obj[key], True)
    duration = obj.get("duration")
    if isinstance(duration, dict):
        return _timestamp(duration, inherited)
    return inherited


def _text_coordinates(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(?:geo:)?\s*([+-]?\d+(?:\.\d+)?)\s*[, ]\s*([+-]?\d+(?:\.\d+)?)(?:\s*[, ]\s*[+-]?\d+(?:\.\d+)?)?\s*", value)
    if match:
        return float(match[1]), float(match[2])
    match = re.fullmatch(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?", value)
    return (float(match[1]), float(match[2])) if match else None


def parse_json(value, cancel=None):
    """Yield explicit coordinates; pair latitude/longitude only within one object."""
    stack = [(value, None, "json", 0)]
    visited = 0
    while stack:
        _check(cancel)
        obj, inherited, locator, depth = stack.pop()
        visited += 1
        if depth > 100 or visited > 5_000_000:
            raise ValueError("Location JSON nesting/item limit exceeded")
        if isinstance(obj, list):
            stack.extend((item, inherited, locator + "/" + str(i), depth + 1) for i, item in reversed(list(enumerate(obj))))
            continue
        if not isinstance(obj, dict):
            continue
        timestamp = _timestamp(obj, inherited)
        if obj.get("crs"):
            name = str(obj["crs"].get("properties", {}).get("name", "")) if isinstance(obj["crs"], dict) else ""
            if name not in ("urn:ogc:def:crs:OGC:1.3:CRS84", "EPSG:4326", "urn:ogc:def:crs:EPSG::4326"):
                raise ValueError("Projected/unknown GeoJSON coordinate system is not interpreted as GPS")
        if obj.get("type") == "FeatureCollection":
            stack.append((obj.get("features", []), timestamp, locator + "/features", depth + 1))
            continue
        if obj.get("type") == "Feature":
            feature_props = obj.get("properties") or {}
            stack.append((obj.get("geometry"), _timestamp(feature_props, timestamp), locator + "/geometry", depth + 1))
            continue
        geometry = obj.get("type")
        if geometry in ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
            todo = [obj.get("coordinates")]
            index = 0
            while todo:
                coords = todo.pop()
                if isinstance(coords, list) and len(coords) >= 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords[:2]):
                    yield _point(coords[1], coords[0], timestamp=timestamp, kind="GeoJSON " + geometry + " coordinate", locator=locator + "/" + str(index))
                    index += 1
                elif isinstance(coords, list):
                    todo.extend(reversed(coords))
                else:
                    raise ValueError("Invalid GeoJSON coordinate structure")
            continue
        if geometry == "GeometryCollection":
            stack.append((obj.get("geometries", []), timestamp, locator + "/geometries", depth + 1))
            continue
        pair = None
        if "latitudeE7" in obj and "longitudeE7" in obj:
            pair = (_number(obj["latitudeE7"]) / 10_000_000, _number(obj["longitudeE7"]) / 10_000_000)
        elif "latitude" in obj and "longitude" in obj:
            pair = (obj["latitude"], obj["longitude"])
        elif "lat" in obj and ("lon" in obj or "lng" in obj):
            pair = (obj["lat"], obj.get("lon", obj.get("lng")))
        if pair:
            yield _point(*pair, timestamp=timestamp, kind="Explicit JSON coordinate", locator=locator)
        for key in ("point", "latLng", "geo", "location"):
            coords = _text_coordinates(obj.get(key))
            if coords:
                point_time = timestamp
                offset = obj.get("durationMinutesOffsetFromStartTime")
                if offset is not None and inherited:
                    try:
                        point_time = (datetime.fromisoformat(inherited.replace("Z", "+00:00")) + timedelta(minutes=float(offset))).isoformat()
                    except (ValueError, OverflowError):
                        point_time = timestamp
                yield _point(*coords, timestamp=point_time, kind="Explicit JSON " + key, locator=locator + "/" + key)
        for key, child in reversed(list(obj.items())):
            if isinstance(child, (dict, list)):
                child_time = timestamp
                if key == "endLocation":
                    duration = obj.get("duration") if isinstance(obj.get("duration"), dict) else {}
                    child_time = None
                    for time_source in (obj, duration):
                        if time_source.get("endTimestamp") is not None or time_source.get("endTime") is not None:
                            child_time = _time(time_source.get("endTimestamp", time_source.get("endTime")))
                            break
                        if time_source.get("endTimestampMs") is not None:
                            child_time = _time(time_source["endTimestampMs"], True)
                            break
                stack.append((child, child_time, locator + "/" + str(key)[:200], depth + 1))


def parse_references(value, cancel=None):
    """Find explicit map references, never ordinary number pairs or guessed places."""
    stack = [(value, None, {}, "json", 0)]
    url_pattern = re.compile(r"(?:https?://)?(?:maps\.google\.com|(?:www\.)?google\.com/maps|maps\.app\.goo\.gl|goo\.gl/maps)[^\s<>\"']*", re.I)
    geo_pattern = re.compile(r"\bgeo:([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", re.I)
    seen = set()
    visited = 0
    while stack:
        _check(cancel)
        obj, timestamp, time_fields, locator, depth = stack.pop()
        visited += 1
        if depth > 100 or visited > 5_000_000:
            raise ValueError("Location reference nesting/item limit exceeded")
        if isinstance(obj, list):
            stack.extend((item, timestamp, time_fields, locator + "/" + str(i), depth + 1) for i, item in reversed(list(enumerate(obj))))
            continue
        if not isinstance(obj, dict):
            continue
        timestamp = _timestamp(obj, timestamp)
        time_fields = {**time_fields, **{k: v for k, v in obj.items() if k in ("date", "date_sent", "dtstart", "dtend") and isinstance(v, (str, int, float))}}
        for key, child in obj.items():
            child_locator = locator + "/" + str(key)[:200]
            if isinstance(child, (dict, list)):
                stack.append((child, timestamp, time_fields, child_locator, depth + 1))
                continue
            if not isinstance(child, str):
                continue
            if len(child) > 2 * 1024 * 1024:
                raise ValueError("Text value exceeds the 2 MiB reference scan limit")
            refs = []
            address_key = str(key).replace("_", "").lower()
            postal_context = any(k in obj for k in ("latitude", "longitude", "latitudeE7", "longitudeE7", "placeId", "postalCode", "postal_code", "street", "city", "country"))
            if (address_key in ("eventlocation", "formattedaddress", "postaladdress", "streetaddress") or address_key == "address" and postal_context) and child.strip():
                refs.append({"kind": "Unresolved address/event location text", "value": child, "coordinates": None})
            if key == "data1" and "postal-address" in str(obj.get("mimetype", "")) and child.strip():
                refs.append({"kind": "Unresolved contact postal address", "value": child, "coordinates": None})
            # Timeline's declared point strings are already represented by parse_json.
            if key not in ("point", "latLng", "geo", "location"):
                for match in geo_pattern.finditer(child):
                    point = _point(match[1], match[2])
                    original = re.split(r"[\s<>\"']", child[match.start():], maxsplit=1)[0].rstrip(".,);]")
                    query = parse_qs(urlsplit(original).query)
                    coords = point["geometry"]["coordinates"]
                    if "q" in query:
                        query_pair = _text_coordinates(query["q"][0].split("(", 1)[0])
                        coords = _point(*query_pair)["geometry"]["coordinates"] if query_pair else None
                    refs.append({"kind": "geo URI location reference" if coords else "Unresolved geo URI query", "value": original, "coordinates": coords})
            for match in url_pattern.finditer(child):
                original = match[0].rstrip(".,);]")
                parsed = urlsplit(original if original.lower().startswith(("http://", "https://")) else "https://" + original)
                host = (parsed.hostname or "").lower()
                # The regex can match a prefix of an attacker-controlled hostname.
                if host not in ("maps.google.com", "google.com", "www.google.com", "maps.app.goo.gl", "goo.gl"):
                    continue
                if host in ("google.com", "www.google.com") and not parsed.path.startswith("/maps"):
                    continue
                if host == "goo.gl" and not parsed.path.startswith("/maps"):
                    continue
                coords = None
                if host not in ("maps.app.goo.gl", "goo.gl"):
                    at = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", unquote(parsed.path))
                    if at:
                        coords = _point(at[1], at[2])["geometry"]["coordinates"]
                    if coords is None:
                        query = parse_qs(parsed.query)
                        for query_key in ("q", "query", "ll", "destination", "origin"):
                            pair = _text_coordinates(query.get(query_key, [None])[0])
                            if pair:
                                coords = _point(*pair)["geometry"]["coordinates"]
                                break
                refs.append({"kind": "Google Maps coordinate reference" if coords else "Unresolved map URL (not opened)", "value": original, "coordinates": coords})
            for reference in refs:
                identity = (child_locator, reference["kind"], reference["value"])
                if identity in seen:
                    continue
                seen.add(identity)
                reference.update({"locator": child_locator, "timestamp": timestamp, "rawTimeFields": time_fields,
                                  "meaning": "Location reference only; does not establish a visit. Raw time-field units are not inferred."})
                yield reference


def parse_xml(data, suffix, cancel=None):
    if len(data) > MAX_DOCUMENT:
        raise ValueError("Location XML exceeds the 128 MiB parse limit")
    upper = data.upper()
    if b"\x00" in data:
        raise ValueError("UTF-16/32 XML is retained but not parsed by this scanner")
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML entities/DOCTYPE are not accepted")
    tree = ET.fromstring(data)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    if suffix == ".gpx":
        for index, element in enumerate(tree.iter()):
            _check(cancel)
            if local(element.tag) not in ("wpt", "rtept", "trkpt"):
                continue
            children = {local(c.tag): c.text for c in element}
            yield _point(element.attrib.get("lat"), element.attrib.get("lon"),
                         timestamp=children.get("time"), kind="GPX " + local(element.tag), locator="xml/" + str(index))
        return
    placemarks = [e for e in tree.iter() if local(e.tag) == "Placemark"] or [tree]
    for index, mark in enumerate(placemarks):
        _check(cancel)
        stamp = next((e.text for e in mark.iter() if local(e.tag) == "when"), None)
        for element in mark.iter():
            name = local(element.tag)
            if name == "coordinates" and element.text:
                for offset, text in enumerate(element.text.split()):
                    pieces = text.split(",")
                    if len(pieces) < 2:
                        raise ValueError("Invalid KML coordinate tuple")
                    yield _point(pieces[1], pieces[0], timestamp=stamp, kind="KML coordinate", locator=f"placemark/{index}/coordinate/{offset}")
            elif name == "Track":
                times = [e.text for e in element if local(e.tag) == "when"]
                points = [e.text for e in element if local(e.tag) == "coord"]
                for offset, text in enumerate(points):
                    pieces = (text or "").split()
                    if len(pieces) < 2:
                        raise ValueError("Invalid KML gx:Track coordinate")
                    yield _point(pieces[1], pieces[0], timestamp=times[offset] if offset < len(times) else None,
                                 kind="KML gx:Track", locator=f"placemark/{index}/track/{offset}")


def parse_file(path, suffix, cancel=None, include_references=False):
    if path.stat().st_size > MAX_DOCUMENT:
        raise ValueError("Location document exceeds the 128 MiB parse limit; original preserved")
    if suffix == ".kmz":
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ValueError("KMZ member count exceeds safety limit")
            total = 0
            found = False
            for member in members:
                _check(cancel)
                parts = PurePosixPath(member.filename.replace("\\", "/"))
                if parts.is_absolute() or ".." in parts.parts or ":" in member.filename:
                    raise ValueError("Unsafe KMZ member path")
                if not member.filename.lower().endswith(".kml"):
                    continue
                found = True
                total += member.file_size
                if total > MAX_ZIP_BYTES or member.file_size > MAX_DOCUMENT:
                    raise ValueError("KMZ expanded KML size exceeds safety limit")
                with archive.open(member) as stream:
                    data = stream.read(min(MAX_DOCUMENT, MAX_ZIP_BYTES - (total - member.file_size)) + 1)
                if len(data) != member.file_size or len(data) > MAX_DOCUMENT:
                    raise ValueError("KMZ member size mismatch or expansion limit exceeded")
                for point in parse_xml(data, ".kml", cancel):
                    point["properties"]["containerMember"] = member.filename
                    yield point
            if not found:
                raise ValueError("KMZ contains no KML document")
        return
    if path.stat().st_size > MAX_DOCUMENT:
        raise ValueError("Location document exceeds the 128 MiB parse limit; original preserved")
    if suffix in (".gpx", ".kml"):
        yield from parse_xml(path.read_bytes(), suffix, cancel)
    elif suffix in (".jsonl", ".ndjson"):
        with path.open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, 1):
                _check(cancel)
                if len(line) > 2 * 1024 * 1024:
                    raise ValueError("JSONL row exceeds 2 MiB parse limit")
                if not line.strip():
                    continue
                value = json.loads(line)
                for point in parse_json(value, cancel):
                    point["properties"]["line"] = number
                    yield point
                if include_references:
                    for reference in parse_references(value, cancel):
                        reference["line"] = number
                        yield {"_reference": reference}
    else:
        with path.open(encoding="utf-8-sig") as stream:
            value = json.load(stream)
            yield from parse_json(value, cancel)
            if include_references:
                for reference in parse_references(value, cancel):
                    yield {"_reference": reference}


def _exiftool(executable, paths, cancel=None):
    # One -q suppresses normal summaries; a second would also hide warnings.
    # https://exiftool.org/exiftool_pod2.html (the -q / -quiet option)
    command = [str(executable), "-config", "", "-q", "-json", "-n", "-a", "-G1:3", "-ee",
               "-GPSLatitude", "-GPSLongitude", "-GPSLatitudeRef", "-GPSLongitudeRef",
               "-GPSAltitude", "-GPSCoordinates", "-GPSPosition", "-GPSDateTime",
               "-DateTimeOriginal", "-OffsetTimeOriginal", "-CreateDate", "-MediaCreateDate"] + [str(p) for p in paths]
    env = dict(os.environ, LC_ALL="C", LANG="C", LC_CTYPE="C")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output, errors, overflow = bytearray(), bytearray(), threading.Event()
    def read(stream, destination, limit):
        while True:
            data = stream.read(65536)
            if not data:
                return
            room = limit - len(destination)
            if len(data) > room:
                overflow.set()
            if room > 0:
                destination.extend(data[:room])
    readers = [threading.Thread(target=read, args=(process.stdout, output, 64 * 1024 * 1024), daemon=True),
               threading.Thread(target=read, args=(process.stderr, errors, 1024 * 1024), daemon=True)]
    for thread in readers:
        thread.start()
    deadline = time.monotonic() + 180
    try:
        while process.poll() is None:
            _check(cancel)
            if overflow.is_set():
                raise ValueError("ExifTool output exceeds the bounded metadata limit")
            if time.monotonic() >= deadline:
                raise ValueError("ExifTool timed out on this batch; source files remain untouched")
            if cancel is not None:
                cancel.wait(.1)
            else:
                time.sleep(.1)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for thread in readers:
            thread.join(5)
        process.stdout.close()
        process.stderr.close()
    _check(cancel)
    if overflow.is_set():
        raise ValueError("ExifTool output exceeds the bounded metadata limit")
    if not output:
        raise ValueError("ExifTool produced no JSON metadata: " + errors.decode("utf-8", "replace")[:500])
    result = json.loads(output.decode("utf-8"))
    if not isinstance(result, list):
        raise ValueError("Unexpected ExifTool metadata format")
    return result, errors.decode("utf-8", "replace")[:2000]


def _media_points(metadata):
    groups = {}
    for key, value in metadata.items():
        if ":" in key:
            group, tag = key.rsplit(":", 1)
        else:
            group, tag = "Main", key
        groups.setdefault(group, {})[tag] = value
    fallback_time = next((_timestamp(group) or group.get("DateTimeOriginal") or group.get("CreateDate") or group.get("MediaCreateDate") for group in groups.values()
                          if _timestamp(group) or group.get("DateTimeOriginal") or group.get("CreateDate") or group.get("MediaCreateDate")), None)
    emitted = set()
    for name, group in groups.items():
        coords = None
        if "GPSLatitude" in group and "GPSLongitude" in group:
            lat, lon = _number(group["GPSLatitude"]), _number(group["GPSLongitude"])
            if str(group.get("GPSLatitudeRef", "")).upper() == "S":
                lat = -abs(lat)
            if str(group.get("GPSLongitudeRef", "")).upper() == "W":
                lon = -abs(lon)
            coords = (lat, lon)
        else:
            coords = _text_coordinates(group.get("GPSCoordinates")) or _text_coordinates(group.get("GPSPosition"))
        if coords:
            stamp = _timestamp(group, fallback_time)
            key = (coords[0], coords[1], stamp)
            if key in emitted:
                continue
            emitted.add(key)
            yield _point(*coords, timestamp=stamp, kind="Media GPS metadata", metadataGroup=name,
                         timestampNote="Original metadata time; offset/zone is not inferred")


def check_archive(root, cancel=None, exiftool_path=None, progress=None):
    """Scan immutable completed manifest records; write only location-data derivatives."""
    if not _LOCK.acquire(blocking=False):
        raise ValueError("A location scan is already running")
    try:
        return _scan(Path(root).resolve(), cancel, exiftool_path, progress)
    finally:
        _LOCK.release()


def _preserve_previous(root, folder, new_files, cancel):
    """Keep a content-addressed snapshot when substantive derived results change."""
    existing = {}
    for name in ("index.jsonl", "locations.geojson", "references.jsonl", "report.json", "report.html"):
        path = folder / name
        if path.is_symlink():
            raise ValueError("Refusing a symbolic link in existing location reports")
        if path.is_file():
            existing[name] = _safe(root, str(path.relative_to(root)))
    if not existing:
        return None
    changed = any(name not in existing or _hash(existing[name], cancel) != _hash(new_path, cancel) for name, new_path in new_files.items())
    if not changed:
        return None
    digests = {name: _hash(path, cancel) for name, path in existing.items()}
    key = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()[:24]
    snapshot = _safe(root, str(Path("location-data", "history", key)))
    snapshot.mkdir(parents=True, exist_ok=True)
    for name, path in existing.items():
        _check(cancel)
        target = _safe(root, str((snapshot / name).relative_to(root)))
        if target.exists():
            if _hash(target, cancel) == digests[name]:
                continue
            raise ValueError("An existing preserved location snapshot was changed; refusing to overwrite it")
        partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex[:8])
        with path.open("rb") as source, partial.open("xb") as output:
            while True:
                _check(cancel)
                block = source.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
        if _hash(partial, cancel) != digests[name]:
            raise ValueError("Previous derived output changed during preservation")
        if os.name == "nt":
            os.rename(partial, target)
        else:
            os.link(partial, target)
            partial.unlink()
    return str(snapshot)


def _scan(root, cancel, exiftool_path, progress):
    _check(cancel)
    manifest = _safe(root, "manifest.jsonl")
    if not manifest.is_file():
        raise ValueError("No acquisition manifest exists in this archive")
    folder = _safe(root, "location-data")
    folder.mkdir(exist_ok=True)
    exports = _safe(root, "location-data/source-files")
    exports.mkdir(exist_ok=True)
    counts = {"manifestRecords": 0, "filesExamined": 0, "candidateFiles": 0, "filesParsed": 0,
              "features": 0, "mediaExamined": 0, "gpsMedia": 0, "references": 0, "unresolvedReferences": 0, "issues": 0}
    issues, preview, media, seen = [], [], [], set()
    token = uuid.uuid4().hex[:10]
    index_temp, geo_temp = folder / ("index." + token + ".partial"), folder / ("locations." + token + ".partial")
    references_temp = folder / ("references." + token + ".partial")
    report_path, index_path, geo_path = folder / "report.html", folder / "index.jsonl", folder / "locations.geojson"
    references_path = folder / "references.jsonl"
    tool = Path(exiftool_path) if exiftool_path is not None else Path(__file__).resolve().parent / "tools" / "exiftool" / "exiftool.exe"
    def issue(record, message):
        counts["issues"] += 1
        detail = {"source": record.get("source", ""), "localPath": record.get("localPath", ""), "error": str(message)[:1000]}
        index.write(json.dumps({"status": "issue", **detail}, ensure_ascii=True) + "\n")
        if len(issues) < 1000:
            issues.append(detail)
    def update(phase, message):
        _check(cancel)
        if progress:
            progress(phase, message)
    def verified(record):
        path = _safe(root, record["localPath"])
        if not path.is_file() or path.stat().st_size != record["size"] or _hash(path, cancel) != record["sha256"]:
            raise ValueError("Original PC file is missing or fails its acquisition SHA-256/size")
        return path
    def copied_source(path, record):
        extension = PurePosixPath(record["source"]).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        destination = exports / (record["sha256"][:40] + extension)
        if destination.is_symlink():
            raise ValueError("Refusing a symbolic link in location source-files")
        if destination.exists():
            if _hash(destination, cancel) == record["sha256"]:
                return str(destination.relative_to(root))
            destination = exports / (record["sha256"][:32] + "_" + uuid.uuid4().hex[:8] + extension)
        temporary = destination.with_name(destination.name + ".partial-" + token)
        digest = hashlib.sha256()
        with path.open("rb") as source, temporary.open("xb") as output:
            while True:
                _check(cancel)
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise ValueError("Source changed while copying location candidate; partial preserved")
        if os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        return str(destination.relative_to(root))
    with index_temp.open("x", encoding="utf-8") as index, geo_temp.open("x", encoding="utf-8") as geo, references_temp.open("x", encoding="utf-8") as references:
        geo.write('{"type":"FeatureCollection","features":[')
        def emit(feature, record):
            if counts["features"] >= MAX_FEATURES:
                raise ValueError("Location feature limit reached; additional originals remain preserved")
            feature["properties"].update({"source": record["source"], "localPath": record["localPath"], "sourceSha256": record["sha256"]})
            if counts["features"]:
                geo.write(",\n")
            geo.write(json.dumps(feature, ensure_ascii=True, allow_nan=False))
            counts["features"] += 1
            if len(preview) < 200:
                preview.append(feature)
        def emit_reference(reference, record):
            if counts["references"] >= MAX_REFERENCES:
                raise ValueError("Location reference limit reached; source files remain preserved")
            reference.update({"source": record["source"], "localPath": record["localPath"], "sourceSha256": record["sha256"]})
            references.write(json.dumps(reference, ensure_ascii=True) + "\n")
            counts["references"] += 1
            coords = reference.get("coordinates")
            if coords:
                emit(_point(coords[1], coords[0], timestamp=reference.get("timestamp"), kind=reference["kind"],
                            locator=reference["locator"], meaning="Map reference; not proof of a visit"), record)
            else:
                counts["unresolvedReferences"] += 1
        with manifest.open(encoding="utf-8") as source_manifest:
            for line in source_manifest:
                _check(cancel)
                record = {}
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("status") != "copied":
                        continue
                    counts["manifestRecords"] += 1
                    if record.get("localPath") in seen:
                        continue
                    seen.add(record.get("localPath"))
                    source_name = record.get("source", "")
                    if not isinstance(source_name, str) or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
                        raise ValueError("Invalid manifest location record")
                    suffix = PurePosixPath(source_name).suffix.lower()
                    is_media = suffix in MEDIA_EXTENSIONS or (suffix == ".bin" and ("mms-attachments/" in source_name or "contact-photos/" in source_name))
                    structured = suffix in (".gpx", ".kml", ".kmz", ".geojson", ".json", ".jsonl", ".ndjson")
                    candidate = structured or (bool(LOCATION_HINT.search(source_name)) and suffix in (".db", ".sqlite", ".sqlite3", ".mbtiles", ".zip"))
                    if not candidate and not is_media:
                        continue
                    counts["filesExamined"] += 1
                    if counts["filesExamined"] % 100 == 1:
                        update("location-scan", f"Checking archive location candidates: {counts['filesExamined']:,} files")
                    if is_media and not tool.is_file():
                        media.append((record, None))
                        continue
                    path = verified(record)
                    if is_media:
                        media.append((record, path))
                        continue
                    before = counts["features"]
                    references_before = counts["references"]
                    parse_error = None
                    if structured:
                        counts["filesParsed"] += 1
                        # Keep parsed output staged until the same original bytes
                        # are verified again. A changed source must not contribute
                        # coordinates carrying an older acquisition hash.
                        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as staged:
                            try:
                                for feature in parse_file(path, suffix, cancel, include_references=True):
                                    staged.write(json.dumps(feature, ensure_ascii=True, allow_nan=False) + "\n")
                            except (ValueError, TypeError, OverflowError, OSError, ET.ParseError, zipfile.BadZipFile, RecursionError, UnicodeError) as exc:
                                parse_error = str(exc)
                            verified(record)
                            staged.seek(0)
                            for line in staged:
                                feature = json.loads(line)
                                if "_reference" in feature:
                                    emit_reference(feature["_reference"], record)
                                else:
                                    emit(feature, record)
                    found = counts["features"] - before
                    found_references = counts["references"] - references_before
                    likely = suffix in (".gpx", ".kml", ".kmz", ".geojson") or bool(LOCATION_HINT.search(source_name))
                    if found or found_references or likely:
                        counts["candidateFiles"] += 1
                        copied = copied_source(path, record)
                        status = "parsed" if found and not parse_error else "partial" if found else "references" if found_references else "unresolved"
                        index.write(json.dumps({"source": source_name, "localPath": record["localPath"], "sha256": record["sha256"],
                                                "copiedSource": copied, "status": status, "features": found, "references": found_references, "error": parse_error}, ensure_ascii=True) + "\n")
                        if not found and not found_references:
                            issue(record, parse_error or "Candidate preserved; no supported explicit coordinate structure was found")
                        elif parse_error:
                            issue(record, parse_error)
                    elif parse_error:
                        issue(record, parse_error)
                except Cancelled:
                    raise
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    issue(record if isinstance(record, dict) else {}, exc)
        if media and not tool.is_file():
            issue({}, f"ExifTool is unavailable; GPS metadata in {len(media):,} media files was not examined")
        elif media:
            for start in range(0, len(media), 48):
                batch = media[start:start + 48]
                update("location-media", f"Reading photo/video GPS metadata: {start:,} / {len(media):,} media files")
                mapping = {os.path.normcase(str(path.resolve())): (record, path) for record, path in batch}
                try:
                    metadata, warnings = _exiftool(tool, [path for _, path in batch], cancel)
                    counts["mediaExamined"] += len(batch)
                    returned = set()
                    for item in metadata:
                        if not isinstance(item, dict):
                            continue
                        file_name = item.get("SourceFile") or item.get("Main:SourceFile")
                        if not isinstance(file_name, str):
                            continue
                        key = os.path.normcase(str(Path(file_name).resolve()))
                        if key not in mapping:
                            continue
                        returned.add(key)
                        record, path = mapping[key]
                        try:
                            verified(record)
                        except (ValueError, OSError) as exc:
                            issue(record, "Source changed or failed validation after metadata extraction: " + str(exc))
                            continue
                        before = counts["features"]
                        errors = [v for k, v in item.items() if k.rsplit(":", 1)[-1] in ("Error", "Warning")]
                        if errors:
                            issue(record, "; ".join(str(e)[:500] for e in errors))
                        try:
                            for feature in _media_points(item):
                                emit(feature, record)
                        except (ValueError, TypeError, OverflowError) as exc:
                            issue(record, exc)
                        found = counts["features"] - before
                        if found:
                            counts["gpsMedia"] += 1
                            index.write(json.dumps({"source": record["source"], "localPath": record["localPath"], "sha256": record["sha256"],
                                                    "status": "media-gps", "features": found, "copiedSource": None}, ensure_ascii=True) + "\n")
                    for key in mapping.keys() - returned:
                        issue(mapping[key][0], "ExifTool returned no metadata record for this file")
                    if warnings:
                        issue({}, "ExifTool batch diagnostics: " + warnings)
                except Cancelled:
                    raise
                except (ValueError, OSError, UnicodeError) as exc:
                    issue({}, f"ExifTool batch of {len(batch)} media files failed: {exc}")
        geo.write("]}")
        index.flush()
        geo.flush()
        references.flush()
        os.fsync(index.fileno())
        os.fsync(geo.fileno())
        os.fsync(references.fileno())
    _check(cancel)
    preserved = _preserve_previous(root, folder, {"index.jsonl": index_temp, "locations.geojson": geo_temp, "references.jsonl": references_temp}, cancel)
    os.replace(index_temp, index_path)
    os.replace(geo_temp, geo_path)
    os.replace(references_temp, references_path)
    checked_at = datetime.now(timezone.utc).isoformat()
    history_folder = _safe(root, "location-data/history")
    history_reports = sorted(path for path in history_folder.iterdir() if path.is_dir() and re.fullmatch(r"[a-f0-9]{24}", path.name) and (path / "report.html").is_file()) if history_folder.is_dir() else []
    result = {"checkedAt": checked_at, "status": "partial" if counts["issues"] else "complete", "counts": counts,
              "reportPath": str(report_path), "folder": str(folder), "indexPath": str(index_path),
              "geojsonPath": str(geo_path), "referencesPath": str(references_path), "preservedPrevious": preserved,
              "previousScans": len(history_reports),
              "limitations": list(LIMITATIONS), "issues": issues,
              "exiftoolAvailable": tool.is_file(), "scope": "Offline scan of hash-verified completed PC acquisitions"}
    report_json = folder / "report.json"
    temporary = folder / ("report." + token + ".json.partial")
    temporary.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(temporary, report_json)
    escaped = lambda value: html.escape(str(value), quote=True)
    rows = "".join("<tr><td>" + escaped(p["properties"]["source"]) + "</td><td>" + escaped(p["geometry"]["coordinates"][1]) + "</td><td>" + escaped(p["geometry"]["coordinates"][0]) + "</td><td>" + escaped(p["properties"].get("timestamp", "Unspecified")) + "</td><td>" + escaped(p["properties"]["kind"]) + "</td></tr>" for p in preview)
    limitations = "".join("<li>" + escaped(item) + "</li>" for item in LIMITATIONS)
    issues_html = "".join("<li>" + escaped(item["source"]) + ": " + escaped(item["error"]) + "</li>" for item in issues[:100])
    content = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"><title>Location data check</title><style>body{{font:15px system-ui;max-width:1200px;margin:36px auto;padding:0 24px;color:#163143}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border:1px solid #c9d8df;text-align:left;overflow-wrap:anywhere}}li{{margin:8px 0}}</style><h1>Location data check</h1><p>{counts['features']:,} explicit coordinates · {counts['candidateFiles']:,} location candidates · {counts['gpsMedia']:,} media files with GPS metadata · {counts['issues']:,} issues</p><p>These are location references found in copied files, not proof of the phone owner's movements. Completed means the supported archive scan finished; it does not mean all phone location history was recovered.</p><p><a href="locations.geojson">GeoJSON coordinate export</a> · <a href="index.jsonl">Source index</a> · <a href="report.json">Complete report</a> · <a href="../report.html">Acquisition report</a></p><h2>Coordinate preview (first {len(preview)})</h2><table><tr><th>Original phone path</th><th>Latitude</th><th>Longitude</th><th>Recorded time</th><th>Meaning</th></tr>{rows}</table><h2>Limits</h2><ul>{limitations}</ul><h2>Issues (first {min(len(issues), 100)})</h2><ul>{issues_html or '<li>No scan errors recorded. Access and format limits still apply.</li>'}</ul><p>Checked {escaped(checked_at)}. Originals remain in the acquisition; recognized export candidates are copied into source-files. GPS-tagged media remain at their indexed original PC paths.</p></html>'''
    content = content.replace('<h2>Coordinate preview', f'<p>{counts["references"]:,} explicit map/address references; {counts["unresolvedReferences"]:,} remain unresolved. <a href="references.jsonl">Reference index (no links were opened)</a></p><h2>Coordinate preview')
    if history_reports:
        links = " · ".join('<a href="' + escaped(path.relative_to(folder).as_posix() + '/report.html') + '">' + escaped(path.name[:10]) + '</a>' for path in history_reports[-20:])
        content = content.replace('<h2>Limits</h2>', f'<p>{len(history_reports)} previous scan snapshots preserved (latest 20): {links}</p><h2>Limits</h2>')
    temporary_html = folder / ("report." + token + ".html.partial")
    temporary_html.write_text(content, encoding="utf-8", errors="xmlcharrefreplace")
    os.replace(temporary_html, report_path)
    return result
