"""Android Bay: local-only desktop controller. Python standard library only."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

try:
    import archive_reader
except ImportError:
    archive_reader = None

BASE = Path(__file__).resolve().parent
VERSION = "1.2.0"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ReaderError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _stat_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _checked_archive_path(root, path=None):
    """Check lexical containment before lstat; never resolve through a reparse point."""
    root = Path(os.path.abspath(root))
    target = root if path is None else Path(path)
    if not target.is_absolute():
        target = root / target
    if ".." in target.parts or any(":" in part for part in target.parts[1:]):
        raise ReaderError(409, "The saved file is not safe to open.")
    try:
        target.relative_to(root)
    except ValueError:
        raise ReaderError(409, "The saved file is outside this recovery.") from None
    for candidate in [*reversed(target.parents), target]:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReaderError(409, "Linked archive paths cannot be opened.")
    if path is None and not stat.S_ISDIR(info.st_mode):
        raise ReaderError(404, "The recovery folder is unavailable.")
    if path is not None and not stat.S_ISREG(info.st_mode):
        raise ReaderError(409, "The saved file is not a regular file.")
    return target


def _media_type(header):
    """Only inert browser media with recognizable byte signatures can be inline."""
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"ID3") or (len(header) >= 4 and header[0] == 255 and header[1] & 0xE0 == 0xE0 and header[1] & 6 and header[2] >> 4 not in (0, 15) and header[2] & 12 != 12):
        return "audio/mpeg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        if brand in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"MSNV"):
            return "video/mp4"
        if brand in (b"3gp4", b"3gp5", b"3gp6"):
            return "video/3gpp"
    if header.startswith(b"\x1aE\xdf\xa3") and b"webm" in header[:512]:
        return "video/webm"
    if header.startswith(b"OggS") and (b"OpusHead" in header or b"\x01vorbis" in header):
        return "audio/ogg"
    return "application/octet-stream"


def _byte_range(value, size):
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value or "")
    if not match or not any(match.groups()) or size == 0:
        raise ReaderError(416, "Requested byte range is unavailable.")
    first, last = match.groups()
    if not first:
        length = int(last)
        if length == 0:
            raise ReaderError(416, "Requested byte range is unavailable.")
        return max(0, size - length), size - 1
    first = int(first)
    last = min(int(last), size - 1) if last else size - 1
    if first >= size or last < first:
        raise ReaderError(416, "Requested byte range is unavailable.")
    return first, last


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, engine, port=0, base=BASE, demo=False):
        self.base = Path(base).resolve()
        self.engine = engine
        self.demo = demo
        self.csrf = secrets.token_urlsafe(32)
        self.adb = self.base / "tools/platform-tools/adb.exe"
        self.helper = self.base / "companion/AndroidRescueHelper.apk"
        self.mirror = self.base / "tools/scrcpy-win64-v5.0/scrcpy.exe"
        self.device_lock = threading.Lock()
        self.picker_lock = threading.Lock()
        self.reader = archive_reader
        # Admission is shared by indexing and acquisition/analysis HTTP actions.
        # Status polling has a separate lock and remains available during verification.
        self.archive_admission = threading.Lock()
        self.reader_state_lock = threading.Lock()
        self.reader_states = {}
        self.reader_thread = None
        self.children = []
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)

    def config(self):
        return {"csrfToken": self.csrf, "version": VERSION, "demo": self.demo,
                "defaultDestination": str(self.base / ("Demo recoveries" if self.demo else "Recoveries")),
                "adbAvailable": self.adb.is_file(), "helperAvailable": self.helper.is_file(),
                "mirrorAvailable": self.mirror.is_file(), "archiveChecksAvailable": hasattr(self.engine, "analyze"),
                "readerAvailable": self.reader is not None and not self.demo}

    def reader_busy(self):
        return self.reader_thread is not None and self.reader_thread.is_alive()

    def reader_root(self, jid):
        if self.reader is None or self.demo:
            raise ReaderError(409, "The archive reader is unavailable in this session.")
        job = self.engine.get_job(jid)
        destination = job.get("destination")
        if not isinstance(destination, str) or not Path(destination).is_absolute():
            raise ReaderError(404, "The recovery folder is unavailable.")
        return _checked_archive_path(destination)

    def reader_status(self, jid):
        root = self.reader_root(jid)
        with self.reader_state_lock:
            current = dict(self.reader_states.get(jid, {}))
        if current.get("state") in ("building", "failed"):
            return current
        return self.reader.status(root)

    def start_reader(self, jid, body):
        if set(body) - {"rebuild"} or not isinstance(body.get("rebuild", False), bool):
            raise ReaderError(400, "Expected an optional true or false rebuild setting.")
        root = self.reader_root(jid)
        if self.reader_busy():
            with self.reader_state_lock:
                current = dict(self.reader_states.get(jid, {}))
            if current.get("state") == "building":
                return current
            raise ReaderError(409, "Wait for the current reader index to finish.")
        if any(j.get("status") in ("running", "finalizing", "verifying") for j in self.engine.history()):
            raise ReaderError(409, "Wait for the current recovery or archive check to finish.")
        existing = self.reader.status(root)
        with self.reader_state_lock:
            previous_failed = self.reader_states.get(jid, {}).get("state") == "failed"
        if not body.get("rebuild") and not previous_failed and existing.get("state") in ("ready", "partial"):
            return existing
        state = {"state": "building", "phase": "indexing", "message": "Building the local message and media index."}
        with self.reader_state_lock:
            self.reader_states[jid] = state

        def build():
            try:
                self.reader.build_index(root)
            except Exception:
                # Exceptions may contain record values or private file paths.
                with self.reader_state_lock:
                    self.reader_states[jid] = {"state": "failed", "message": "The reader index could not be completed. Saved originals were kept; retry building the index."}
            else:
                with self.reader_state_lock:
                    self.reader_states.pop(jid, None)

        self.reader_thread = threading.Thread(target=build, name="archive-reader-index", daemon=True)
        try:
            self.reader_thread.start()
        except Exception:
            with self.reader_state_lock:
                self.reader_states.pop(jid, None)
            raise ReaderError(409, "The reader index could not be started.") from None
        return state

    def authorized_device(self, serial):
        if not isinstance(serial, str) or not serial or len(serial) > 256 or any(ord(c) < 32 for c in serial):
            raise ValueError("Choose a connected phone first.")
        if not any(d.get("serial") == serial and d.get("state") == "device" for d in self.engine.devices()):
            raise ValueError("The phone is disconnected or has not authorized USB debugging. Check its screen and refresh.")
        return serial

    def adb_action(self, serial, arguments, timeout=180):
        self.authorized_device(serial)
        if self.demo:
            return "DEMO: action simulated. No phone was changed."
        with self.device_lock:
            result = subprocess.run([str(self.adb), "-s", serial, *arguments], capture_output=True,
                                    timeout=timeout, creationflags=NO_WINDOW)
        output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
        if result.returncode or "Failure [" in output or "Error:" in output:
            raise RuntimeError(output[-2500:] or "The phone rejected the action.")
        return output


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # No request bodies, device identifiers, file paths, or tokens in the access log.
        logging.info("HTTP %s", args[1] if len(args) > 1 else "request")

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        raw = json.dumps(data, ensure_ascii=True).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_request(self, mutation=False):
        if self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port):
            self.respond(403, {"error": "Use the local Android Bay window."})
            return False
        origin = self.headers.get("Origin")
        if (origin and origin != self.server.origin) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Requests from another website are blocked."})
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            self.respond(403, {"error": "Refresh this window before continuing."})
            return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def reader_query(self, allowed):
        query = urlsplit(self.path).query
        if len(query) > 8192:
            raise ReaderError(400, "Reader query is too long.")
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=12, encoding="utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise ReaderError(400, "Invalid reader query.") from None
        if set(values) - set(allowed) or any(len(v) != 1 for v in values.values()):
            raise ReaderError(400, "Invalid reader query fields.")
        return {key: value[0] for key, value in values.items()}

    def reader_get(self, jid, operation, media_id=None):
        root = self.server.reader_root(jid)
        reader = self.server.reader
        if operation in ("status", "metadata"):
            self.reader_query(())
            return self.respond(200, self.server.reader_status(jid))
        if media_id:
            query = self.reader_query(("download",))
            if query.get("download", "0") not in ("0", "1"):
                raise ReaderError(400, "Invalid download setting.")
            return self.reader_media(root, media_id, query.get("download") == "1")
        allowed = {"threads": ("q", "offset", "limit"), "messages": ("thread", "q", "offset", "limit"),
                   "media": ("kind", "q", "offset", "limit")}
        query = self.reader_query(allowed[operation])
        options = {}
        for key, default, maximum in (("offset", 0, 2_000_000), ("limit", 100 if operation == "messages" else 60, 200)):
            value = query.pop(key, str(default))
            if not re.fullmatch(r"[0-9]{1,9}", value) or not (0 if key == "offset" else 1) <= int(value) <= maximum:
                raise ReaderError(400, "Invalid reader pagination.")
            options[key] = int(value)
        if len(query.get("q", "")) > 200 or any(ord(c) < 32 for c in query.get("q", "")):
            raise ReaderError(400, "Search text is too long or contains control characters.")
        if query.get("kind", "") not in ("", "photo", "video", "audio", "other"):
            raise ReaderError(400, "Invalid media category.")
        if query.get("thread") and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", query["thread"]):
            raise ReaderError(400, "Invalid conversation identifier.")
        state = self.server.reader_status(jid)
        if state.get("state") not in ("ready", "partial"):
            raise ReaderError(409, "Build the reader index before browsing this archive.")
        result = getattr(reader, "list_" + operation)(root, **query, **options)
        result["hasMore"] = result.get("offset", 0) + len(result.get("items", [])) < result.get("total", 0)
        return self.respond(200, result)

    def reader_media(self, root, media_id, download):
        record = self.server.reader.lookup_media(root, media_id)
        path = _checked_archive_path(root, record["path"])
        expected = record.get("statIdentity")
        if not isinstance(expected, (tuple, list)) or len(expected) != 5:
            raise ReaderError(409, "The saved file needs verification before it can be opened.")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or _stat_identity(info) != list(expected) or info.st_size != record.get("size"):
                raise ReaderError(409, "The saved file changed after verification. Rebuild the reader index.")
            # Recheck parents after open as well as the verified handle identity.
            _checked_archive_path(root, path)
            content_type = _media_type(source.read(512))
            attachment = download or content_type == "application/octet-stream"
            start, end, response_status = 0, info.st_size - 1, 200
            ranges = self.headers.get_all("Range", [])
            if ranges:
                try:
                    if len(ranges) != 1:
                        raise ReaderError(416, "Requested byte range is unavailable.")
                    start, end = _byte_range(ranges[0], info.st_size)
                    response_status = 206
                except ReaderError:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */" + str(info.st_size))
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                    self.end_headers()
                    return
            length = max(0, end - start + 1)
            name = str(record.get("downloadName") or ("archive-" + media_id))
            name = re.split(r"[/\\]", name)[-1]
            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:200] or "archive-media"
            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if response_status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{info.st_size}")
            self.send_header("Content-Disposition", ("attachment" if attachment else "inline") + f'; filename="archive-{media_id}"; filename*=UTF-8\'\'' + quote(name, safe="", errors="replace"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("X-Archive-Verification", "sha256-verified-local-file")
            self.end_headers()
            if self.command == "HEAD":
                return
            source.seek(start)
            try:
                while length:
                    block = source.read(min(length, 256 * 1024))
                    if not block:
                        self.close_connection = True
                        return
                    self.wfile.write(block)
                    length -= len(block)
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                self.close_connection = True

    def do_GET(self):
        if not self.trusted_request():
            return
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,100})/reader/(status|metadata|threads|messages|media)(?:/([a-f0-9]{24}))?", path)
        if match:
            try:
                if match[3] and match[2] != "media":
                    raise ReaderError(404, "Not found.")
                return self.reader_get(*match.groups())
            except ReaderError as exc:
                return self.respond(exc.status, {"error": str(exc)})
            except (KeyError, FileNotFoundError):
                return self.respond(404, {"error": "That recovery or saved item was not found."})
            except Exception:
                return self.respond(409, {"error": "The reader could not open this saved item. Check the archive and rebuild the index."})
        try:
            if path == "/api/config":
                return self.respond(200, self.server.config())
            if path == "/api/health":
                return self.respond(200, {"ok": True, "version": VERSION, "demo": self.server.demo})
            if path == "/api/devices":
                return self.respond(200, {"devices": self.server.engine.devices()})
            if path == "/api/jobs":
                return self.respond(200, {"jobs": self.server.engine.history()})
            match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,100})", path)
            if match:
                return self.respond(200, {"job": self.server.engine.get_job(match[1])})
            public = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                      "/app.css": "app.css", "/styles.css": "styles.css", "/reader": "reader.html",
                      "/reader.html": "reader.html", "/reader.js": "reader.js", "/reader.css": "reader.css"}
            if path in public:
                file = self.server.base / "web" / public[path]
                kind = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}[file.suffix]
                return self.respond(200, file.read_bytes(), kind + "; charset=utf-8")
            return self.respond(404, {"error": "Not found."})
        except (KeyError, FileNotFoundError):
            self.respond(404, {"error": "That recovery or file was not found."})
        except Exception as exc:
            self.respond(400, {"error": str(exc)[:2500]})

    def do_POST(self):
        if not self.trusted_request(mutation=True):
            self.close_connection = True
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON request.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected an object.")
            path = urlsplit(self.path).path
            result = self.action(path, body)
            self.respond(200, result)
        except ReaderError as exc:
            self.respond(exc.status, {"error": str(exc)})
        except (KeyError, FileNotFoundError):
            self.respond(404, {"error": "That recovery or file was not found."})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.respond(400, {"error": str(exc)[:2500]})
            self.close_connection = True
        except subprocess.TimeoutExpired:
            self.respond(408, {"error": "The phone or folder dialog did not respond in time. Check the screen and try again."})
        except Exception as exc:
            self.respond(409, {"error": str(exc)[:2500]})

    def action(self, path, body):
        match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,100})/reader/build", path)
        if match:
            try:
                with self.server.archive_admission:
                    return self.server.start_reader(match[1], body)
            except ReaderError:
                raise
            except (KeyError, FileNotFoundError):
                raise ReaderError(404, "That recovery was not found.") from None
            except Exception:
                raise ReaderError(409, "The reader index could not be started. Check the saved archive.") from None
        guarded = path in ("/api/jobs", "/api/shutdown") or re.fullmatch(r"/api/jobs/[A-Za-z0-9_-]{1,100}/(resume|analyze|verify)", path)
        if guarded:
            with self.server.archive_admission:
                if self.server.reader_busy():
                    raise ReaderError(409, "Wait for the reader index to finish before changing or checking an archive.")
                return self._action(path, body)
        return self._action(path, body)

    def _action(self, path, body):
        engine = self.server.engine
        if path == "/api/inspect":
            serial = self.server.authorized_device(body.get("serial"))
            return engine.inspect(serial)
        if path == "/api/jobs":
            serial = self.server.authorized_device(body.get("serial"))
            destination = body.get("destination")
            if not isinstance(destination, str) or not destination.strip() or len(destination) > 90:
                raise ValueError("Choose a shorter destination folder (90 characters maximum), such as E:\\Phone backups, so recovered filenames fit Windows limits.")
            destination_path = Path(destination)
            if not destination_path.is_absolute() or destination.startswith(("\\\\", "//")):
                raise ValueError("Choose an absolute folder path on this PC or an attached drive.")
            resolved = destination_path.resolve()
            # Copy output must not be intermingled with application code/runtime.
            protected = [self.server.base / p for p in ("runtime", "tools", "web", "companion", "state", "demo-state", "tests")]
            if resolved == self.server.base or any(resolved == p or p in resolved.parents for p in protected):
                raise ValueError("Choose a recovery folder outside the application program folders.")
            options = body.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("Invalid recovery options.")
            allowed = {"shared", "apks", "helper", "legacy", "privateApps", "existingRoot", "roots", "archiveChecks", "deviceContext"}
            if set(options) - allowed:
                raise ValueError("Unknown recovery option.")
            for key, value in options.items():
                if key != "roots" and not isinstance(value, bool):
                    raise ValueError("Recovery options must be true or false.")
            roots = options.get("roots", [])
            if not isinstance(roots, list) or len(roots) > 32 or any(not isinstance(x, str) or len(x) > 1024 for x in roots):
                raise ValueError("Invalid additional storage paths.")
            if hasattr(engine, "analyze"):
                options.setdefault("archiveChecks", True)
            return {"job": engine.start(serial, str(resolved), options)}
        match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,100})/(cancel|resume|verify|analyze|open)", path)
        if match:
            jid, operation = match.groups()
            job = engine.get_job(jid)
            if operation == "open":
                if body.get("kind") == "folder":
                    target = Path(job["destination"]).resolve()
                    if not target.is_dir():
                        raise ValueError("The recovery folder is no longer available.")
                elif body.get("kind") in ("report", "catalog", "deleted", "locations", "trash", "context"):
                    relative = {"catalog": "catalog.html", "deleted": "deleted-items/report.html",
                                "locations": "location-data/report.html", "trash": "deleted-items/phone-trash/report.html", "context": "device-context/report.html"}
                    target = (Path(job["destination"]) / relative[body["kind"]]).resolve() if body["kind"] in relative else Path(job.get("reportPath") or "").resolve()
                    if not target.is_file() or target.suffix.lower() not in (".html", ".txt", ".json"):
                        raise ValueError("The recovery report is not ready yet.")
                    if Path(job["destination"]).resolve() not in target.parents:
                        raise ValueError("Report is outside this recovery.")
                else:
                    raise ValueError("Choose folder, catalog, report, deleted, locations, trash or context.")
                os.startfile(str(target))
                return {"message": "Opened on this PC."}
            if operation == "verify":
                result = engine.verify(jid)
                return {"job": engine.get_job(jid), "verification": result}
            if operation == "analyze" and not hasattr(engine, "analyze"):
                raise ValueError("Archive checks need a real recovery archive; this demonstration uses synthetic files.")
            return {"job": getattr(engine, operation)(jid)}
        if path == "/api/helper/install":
            if not self.server.helper.is_file():
                raise ValueError("The bundled helper APK is missing. Re-extract the app package.")
            output = self.server.adb_action(body.get("serial"), ["install", "-r", str(self.server.helper)])
            return {"message": "Helper installation finished. Open it on the phone, grant permissions, then tap Create export.", "output": output}
        if path == "/api/helper/open":
            output = self.server.adb_action(body.get("serial"), ["shell", "am", "start", "-n", "org.androidrescue.helper/.MainActivity"])
            return {"message": "Check the phone: Grant permissions → Create export. Wait for its completion report before copying.", "output": output}
        if path == "/api/mirror":
            serial = self.server.authorized_device(body.get("serial"))
            if self.server.demo:
                return {"message": "DEMO: mirroring simulated; no real phone is connected."}
            if not self.server.mirror.is_file():
                raise ValueError("The bundled scrcpy program is missing.")
            logs = self.server.base / "state"
            logs.mkdir(exist_ok=True)
            with (logs / "mirror.log").open("ab") as log:
                child = subprocess.Popen([str(self.server.mirror), "--serial", serial, "--no-audio", "--max-size", "1280", "--window-title", "Android Bay - phone screen"], cwd=str(self.server.mirror.parent), stdout=log, stderr=log, creationflags=NO_WINDOW, env={**os.environ, "ADB": str(self.server.adb)})
            self.server.children.append(child)
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return {"message": "Phone screen opened in a separate window. Close that window to stop mirroring."}
            raise RuntimeError("Mirroring could not start. Android 5 or newer and authorized USB debugging are required. See state/mirror.log.")
        if path == "/api/pick-folder":
            if not self.server.picker_lock.acquire(blocking=False):
                raise ValueError("A folder dialog is already open. Check behind this window.")
            try:
                script = "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding; Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Choose where Android Bay saves phone data'; $d.ShowNewFolderButton=$true; if($d.ShowDialog() -eq 'OK'){[Console]::Write($d.SelectedPath)}; $d.Dispose()"
                result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script], capture_output=True, timeout=180, creationflags=NO_WINDOW)
                if result.returncode:
                    raise RuntimeError("Could not open the folder picker. Enter the full destination path instead.")
                return {"path": result.stdout.decode("utf-8", "replace").strip()}
            finally:
                self.server.picker_lock.release()
        if path == "/api/shutdown":
            if any(j.get("status") in ("running", "finalizing", "verifying") for j in engine.history()):
                raise ValueError("Cancel the active recovery before closing the app.")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return {"message": "Android Bay has stopped. You can close this window."}
        raise ValueError("Unknown action.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    from launcher import existing_server, open_window, acquire_lock
    state_dir = BASE / ("demo-state" if args.demo else "state")
    state_dir.mkdir(exist_ok=True)
    logging.basicConfig(filename=str(state_dir / "server.log"), level=logging.INFO)
    lock = acquire_lock(state_dir)
    if lock is None:
        old = existing_server(state_dir)
        if old and not args.no_browser:
            open_window(old["url"])
        return
    if args.demo:
        from demo import DemoEngine
        engine = DemoEngine(BASE)
    else:
        from engine import RescueEngine
        engine = RescueEngine(BASE, BASE / "tools/platform-tools/adb.exe")
    server = LocalServer(engine, args.port, demo=args.demo)
    state_file = state_dir / "server.json"
    state_file.write_text(json.dumps({"pid": os.getpid(), "url": server.origin, "token": server.csrf, "demo": args.demo}), encoding="utf-8")
    if not args.no_browser:
        threading.Timer(0.4, open_window, args=(server.origin,)).start()
    print(server.origin, flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        for job in engine.history():
            if job.get("status") == "running":
                engine.cancel(job["id"])
    finally:
        server.server_close()
        state_file.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        details = traceback.format_exc()
        (BASE / "startup-error.txt").write_text(details, encoding="utf-8")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "Android Bay could not start. See startup-error.txt in the app folder for details.", "Android Bay", 0x10)
        raise
