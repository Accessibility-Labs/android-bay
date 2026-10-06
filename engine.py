"""Account-free, read-only Android rescue jobs with an honest coverage ledger."""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import queue
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

from adb import Adb, AdbError, Cancelled, ConnectionLost, Entry, quote

HELPER = "org.androidrescue.helper"
HELPER_EXPORTS = "/sdcard/AndroidRescue/exports"
MAX_HELPER_REPORT_BYTES = 128 * 1024 * 1024
LIMITATIONS = [
    "This is a live logical acquisition, not a complete forensic image or an atomic snapshot.",
    "ADB cannot normally read private app databases, passwords, tokens, account history, or permanently erased data. An unlocked screen does not remove Android app isolation.",
    "Cloud-only data and account history must be exported separately from their services. Offline devices only contain locally retained data.",
    "App sandboxes, work/secondary profiles, encrypted secure folders, hardware-backed keys, DRM, and some Android/data folders may remain inaccessible.",
    "APK copies contain application installers, not the applications' private data or login sessions.",
    "The phone must already be unlocked and USB debugging authorized. This app does not root, unlock bootloaders, bypass locks, or delete phone data.",
    "PC verification checks copies against their recorded acquisition hashes. Phone-side hashes are additionally checked where explicitly reported, including helper exports and shell-fallback APKs.",
]


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def legacy_backup_command(sdk, *, include_apks=True, include_shared=True):
    """Honor selected content and device-side bu options for this Android API."""
    command = ["backup", "-all", "-apk" if include_apks else "-noapk",
               "-obb" if include_shared else "-noobb",
               "-shared" if include_shared else "-noshared"]
    # Unknown/malformed API levels retain the older command's compatibility.
    value = str(sdk).strip() if isinstance(sdk, (str, int)) and not isinstance(sdk, bool) else ""
    if not re.fullmatch(r"[0-9]{1,3}", value):
        return command
    api = int(value)
    # AOSP cmds/bu/.../Backup.java: absent in android-4.4w_r1 (API 20),
    # present in android-5.0.0_r1 (API 21).
    if api >= 21:
        command.append("-widgets")
    # AOSP parser: absent in android-7.1.0_r1 (API 25), present in
    # android-8.0.0_r1 (API 26); key/value-only apps are excluded by default.
    if api >= 26:
        command.append("-keyvalue")
    return command


class BoundedOutput:
    """Enforce the byte bound before accepting a remote metadata chunk."""
    def __init__(self, stream, limit):
        self.stream, self.limit, self.size = stream, limit, 0

    def write(self, data):
        if self.size + len(data) > self.limit:
            raise AdbError("Helper report exceeds the 128 MiB metadata limit; raw copied report remains available for review")
        written = self.stream.write(data)
        self.size += written
        return written


def load_helper_report(stream):
    """Parse an already byte-bounded binary stream without retaining a bytes copy."""
    text = io.TextIOWrapper(stream, encoding="utf-8")
    try:
        report = json.load(text)
    finally:
        text.detach()
    if not isinstance(report, dict):
        raise ValueError("Invalid helper report: expected an object")
    return report


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def file_hash(path: Path, cancel=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Cancelled while checking a PC copy")
            data = stream.read(1024 * 1024)
            if not data:
                return digest.hexdigest()
            digest.update(data)


def safe_name(name: str) -> str:
    """Readable Windows-safe components with stable case/collision disambiguation.

    A 128-bit source-name suffix avoids case-insensitive aliases, device names,
    trailing-dot aliases, Unicode normalization aliases, and percent ambiguity.
    Full original names remain in the manifest; source bytes use surrogateescape.
    """
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("Unsafe remote path component")
    raw = name.encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    clean = "".join(c if c.isalnum() and ord(c) < 128 or c in "._- " else "_" for c in name)
    clean = clean.strip(" .") or "file"
    stem, extension = os.path.splitext(clean)
    if len(extension) > 12:
        stem, extension = clean, ""
    stem = (stem or "file")[:70].rstrip(" .")
    return f"{stem}~{digest}{extension}"


def local_remote_path(root: Path, category: str, remote: str) -> Path:
    if not remote.startswith("/") or "\x00" in remote:
        raise ValueError("Remote path must be absolute")
    parts = remote.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Unsafe remote path")
    mapped = Path(category, *(safe_name(part) for part in parts))
    # Keep legacy Windows path lengths manageable for deeply nested phone data.
    if len(str(root / mapped)) > 210:
        key = hashlib.sha256(remote.encode("utf-8", "surrogateescape")).hexdigest()[:32]
        extension = Path(safe_name(parts[-1])).suffix
        if len(extension) > 12:
            extension = ""
        mapped = Path(category, "_long_paths", key[:2], key + extension)
    return safe_join(root, str(mapped))


def safe_join(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.drive:
        raise ValueError("Unsafe backup path")
    resolved_root = root.resolve()
    target = (root / path).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError("Backup path escapes destination (possibly a symbolic link)")
    return target


def install_new(partial: Path, target: Path):
    """Publish a complete file without replacing any existing file."""
    if os.name == "nt":
        os.rename(partial, target)  # Windows refuses an existing destination.
    else:
        os.link(partial, target)
        partial.unlink()


class RescueEngine:
    def __init__(self, base_dir: Path, adb_path: Path):
        self.base_dir = Path(base_dir).resolve()
        self.state_dir = self.base_dir / "state" / "jobs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.adb = Adb(Path(adb_path))
        self._lock = threading.RLock()
        self._jobs = {}
        self._cancel = {}
        self._threads = {}
        self._last_save = {}
        self._verifying = set()
        for path in self.state_dir.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(job, dict):
                    continue
                if job.get("id") != path.stem or not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                    continue
                if job.get("status") == "running":
                    if job.get("analysisOnly"):
                        job.update(status=job.pop("statusBeforeAnalysis", "partial"), analysisOnly=False,
                                   phase="interrupted", message="Archive checks were interrupted. Run Check deleted items & locations again.")
                        job["analysisStatus"] = "interrupted"
                    else:
                        job.update(status="partial", phase="interrupted", message="The app stopped. Reconnect the same phone and resume.")
                self._jobs[job["id"]] = job
            except (OSError, ValueError, TypeError):
                continue

    def devices(self):
        return self.adb.devices()

    def _canonical(self, serial, path):
        command = f"cd {quote(path)} 2>/dev/null && pwd -P"
        output = self.adb.shell(serial, command, timeout=15, check=False).strip()
        if output.startswith("/") and "\n" not in output and "\r" not in output and "\x00" not in output:
            return output.rstrip("/") or "/"
        return None

    def inspect(self, serial):
        states = {d["serial"]: d for d in self.devices()}
        if serial not in states:
            raise AdbError("Selected phone is not connected")
        if states[serial]["state"] != "device":
            raise AdbError("Phone is " + states[serial]["state"] + ". Unlock it and approve USB debugging on the phone.")
        props = self.adb.shell_args(serial, ["getprop"])
        parsed = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", props.replace("\r", ""), re.M))
        roots = []
        discovered = set()
        candidates = ["/sdcard", "/mnt/sdcard", "/storage/sdcard0", "/storage/sdcard1", "/mnt/extSdCard", "/mnt/external_sd", "/sdcard1"]
        for parent in ("/storage", "/storage/emulated"):
            try:
                with self.adb.sync(serial) as sync:
                    entries = sync.list(parent)
                found = [parent + "/" + e.name for e in entries
                         if e.name not in ("self", "emulated") and (stat.S_ISDIR(e.mode) or stat.S_ISLNK(e.mode))]
                candidates.extend(found)
                discovered.update(found)
            except AdbError:
                pass
        seen = set()
        for path in candidates:
            canonical = self._canonical(serial, path)
            if not canonical:
                if path in discovered:
                    roots.append({"path": path, "label": "Unavailable storage " + path.rsplit("/", 1)[-1], "accessible": False})
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            roots.append({"path": canonical, "label": "Internal shared storage" if path == "/sdcard" else "Storage " + path.rsplit("/", 1)[-1], "accessible": True})
        helper = self.adb.shell_args(serial, ["pm", "path", HELPER], check=False).startswith("package:")
        return {"serial": serial, "model": parsed.get("ro.product.model", states[serial]["model"]),
                "android": parsed.get("ro.build.version.release", "Unknown"),
                "sdk": parsed.get("ro.build.version.sdk", "Unknown"), "roots": roots,
                "helperInstalled": helper, "helperExport": self._helper_export_state(serial),
                "limitations": list(LIMITATIONS), "properties": parsed}

    def _helper_export_state(self, serial):
        """Return provider counts/status only; never expose exported personal rows."""
        try:
            with self.adb.sync(serial) as sync:
                entries = sync.list(HELPER_EXPORTS)
            runs = sorted(e.name for e in entries if stat.S_ISDIR(e.mode))
            if not runs:
                return {"state": "missing", "detail": "No phone helper export found. Open the helper and create an export."}
            latest = runs[-1]
            report_path = HELPER_EXPORTS + "/" + latest + "/report.json"
            try:
                with self.adb.sync(serial) as sync:
                    report_entry = sync.stat(report_path)
            except AdbError:
                return {"state": "incomplete", "detail": "The newest phone export has no completion report. It may still be running or was interrupted.", "run": latest}
            if report_entry.size > MAX_HELPER_REPORT_BYTES:
                return {"state": "partial", "detail": "Helper report exceeds the 128 MiB metadata limit. Its raw file can still be copied for review.", "run": latest}
            # A large MMS history can create tens of thousands of category entries.
            # Stream metadata bytes to a temporary file with an independent cap.
            with tempfile.TemporaryFile(mode="w+b") as output:
                with self.adb.sync(serial) as sync:
                    sync.receive(report_path, BoundedOutput(output, MAX_HELPER_REPORT_BYTES))
                output.seek(0)
                report = load_helper_report(output)
            ready = report.get("runFinished") is True and report.get("status") == "complete" and report.get("complete") is True and not report.get("errors")
            state = "ready" if ready else "partial"
            return {"state": state, "detail": f"Newest helper export: {state}; {report.get('exportedRows', 0)} provider rows and {report.get('exportedBinaryFiles', 0)} binary files reported. Transfer verifies their hashes.", "run": latest}
        except (AdbError, OSError, ValueError, UnicodeError, RecursionError, MemoryError) as exc:
            return {"state": "partial", "detail": "Could not verify helper export readiness: " + str(exc)[:300]}

    def _snapshot(self, job):
        return json.loads(json.dumps(job, ensure_ascii=True))

    def _public_snapshot(self, job):
        result = self._snapshot(job)
        if job["id"] in self._verifying:
            result.update(status="verifying", phase="verifying", message="Checking saved file hashes…")
            return result
        thread = self._threads.get(job["id"])
        if thread and thread.is_alive() and result["status"] != "running":
            result.update(status="finalizing", phase="finalizing", message="Writing and checking reports…")
        return result

    def _save(self, job, force=False):
        now = time.monotonic()
        with self._lock:
            if force or now - self._last_save.get(job["id"], 0) > 1:
                try:
                    atomic_json(self.state_dir / (job["id"] + ".json"), job)
                except OSError as exc:
                    message = "Cannot persist job state: " + str(exc)
                    if job.get("statePersistenceError") != message:
                        job["errors"].append(message)
                    job["statePersistenceError"] = message
                self._last_save[job["id"]] = now

    def _update(self, job, **changes):
        with self._lock:
            job.update(changes)
            self._save(job)

    def _error(self, job, message):
        with self._lock:
            job["errors"].append(str(message))
            self._save(job)

    def _coverage(self, job, category, status, detail):
        with self._lock:
            job["coverage"] = [c for c in job["coverage"] if c["category"] != category]
            job["coverage"].append({"category": category, "status": status, "detail": detail})
            self._save(job)

    def start(self, serial, destination, options=None):
        options = options or {}
        if not isinstance(options, dict):
            raise ValueError("Options must be an object")
        allowed = {"shared", "apks", "helper", "legacy", "privateApps", "existingRoot", "roots", "archiveChecks", "deviceContext"}
        options = {k: v for k, v in options.items() if k in allowed}
        for key in allowed - {"roots"}:
            if key in options and not isinstance(options[key], bool):
                raise ValueError(key + " must be true or false")
        roots = options.get("roots", [])
        if not isinstance(roots, list) or any(not isinstance(r, str) or not r.startswith("/") or "\x00" in r or ".." in PurePosixPath(r).parts for r in roots):
            raise ValueError("Storage roots must be absolute Android paths")
        if not any(options.get(k, k in ("shared", "apks", "helper")) for k in allowed - {"roots", "archiveChecks"}):
            raise ValueError("Select at least one acquisition method")
        dest = Path(destination).expanduser()
        if not dest.is_absolute():
            raise ValueError("Choose an absolute PC destination folder")
        dest = dest.resolve()
        if len(str(dest)) > 90:
            raise ValueError("Choose a shorter PC destination (90 characters maximum), such as E:\\PhoneBackups. This leaves space for safe Windows filenames and preserved revisions.")
        dest.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._verifying or any(j["status"] == "running" for j in self._jobs.values()) or any(t.is_alive() for t in self._threads.values()):
                raise ValueError("Another extraction is running. Wait or cancel it first.")
            job_id = uuid.uuid4().hex
            folder = dest / ("AndroidRescue_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + job_id[:8])
            folder.mkdir()
            job = {"id": job_id, "serial": serial, "status": "running", "phase": "connecting",
                   "message": "Inspecting device permissions and storage", "filesCopied": 0, "filesTotal": 0,
                   "bytesCopied": 0, "errors": [], "destination": str(folder), "startedAt": utcnow(),
                   "finishedAt": None, "coverage": [], "reportPath": str(folder / "report.html"),
                   "options": options, "limitations": list(LIMITATIONS), "resumeCount": 0}
            self._jobs[job_id] = job
            self._save(job, True)
            self._launch(job)
            return self._snapshot(job)

    def _launch(self, job):
        event = threading.Event()
        self._cancel[job["id"]] = event
        thread = threading.Thread(target=self._run, args=(job, event), daemon=True, name="rescue-" + job["id"][:8])
        self._threads[job["id"]] = thread
        thread.start()

    def get_job(self, job_id):
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("Unknown extraction job")
            return self._public_snapshot(self._jobs[job_id])

    def history(self):
        with self._lock:
            return [self._public_snapshot(j) for j in sorted(self._jobs.values(), key=lambda j: j["startedAt"], reverse=True)]

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("Unknown extraction job")
            if job["status"] == "running":
                self._cancel[job_id].set()
                self._update(job, message="Cancelling; completed files will be kept", phase="cancelling")
            return self._snapshot(job)

    def resume(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("Unknown extraction job")
            if any(j["status"] == "running" for j in self._jobs.values()) or any(t.is_alive() for t in self._threads.values()):
                raise ValueError("An extraction is already running")
            if job_id in self._verifying:
                raise ValueError("Wait for verification to finish before resuming")
            # Previous error history is retained in an audit file before the new attempt.
            root = Path(job["destination"])
            root.mkdir(parents=True, exist_ok=True)
            count = job.get("resumeCount", 0) + 1
            atomic_json(root / (f"attempt-{count}.json"), job)
            if "verification" in job:
                job["previousVerification"] = job.pop("verification")
            job.update(status="running", phase="connecting", message="Resuming and checking previously copied files",
                       errors=[], coverage=[], finishedAt=None, filesCopied=0, filesTotal=0, bytesCopied=0, resumeCount=count)
            self._save(job, True)
            self._launch(job)
            return self._snapshot(job)

    @staticmethod
    def _check(cancel):
        if cancel.is_set():
            raise Cancelled("Cancelled; completed files and partial transfer files were preserved")

    @staticmethod
    def _fatal_storage_error(exc):
        return isinstance(exc, OSError) and (exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)) or getattr(exc, "winerror", None) == 112)

    def _manifest(self, root):
        records = {}
        path = root / "manifest.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and record.get("status") == "copied":
                            records[record["source"]] = record
                    except (ValueError, KeyError, TypeError):
                        continue  # A final interrupted line must not invalidate earlier entries.
        return records

    def _append(self, root, record):
        path = root / "manifest.jsonl"
        needs_separator = False
        if path.exists() and path.stat().st_size:
            with path.open("rb") as tail:
                tail.seek(-1, os.SEEK_END)
                needs_separator = tail.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as stream:
            if needs_separator:
                stream.write("\n")  # Preserve a torn tail without swallowing the next record.
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _progress(self, job, size):
        with self._lock:
            job["bytesCopied"] += size
            self._save(job)

    def _copy(self, job, cancel, remote, entry, category, records):
        root = Path(job["destination"])
        self._check(cancel)
        old = records.get(remote)
        if old and old.get("sourceSize32") == entry.size and old.get("sourceMtime") == entry.mtime:
            previous = safe_join(root, old["localPath"])
            if previous.is_file() and previous.stat().st_size == old["size"] and file_hash(previous, cancel) == old["sha256"]:
                old["_checkedThisAttempt"] = True
                with self._lock:
                    job["filesCopied"] += 1
                    job["bytesCopied"] += old["size"]
                return
        target = local_remote_path(root, category, remote)
        if target.exists():
            # Never overwrite an earlier verified acquisition, even if the phone changed.
            target = target.with_name(target.stem + "_revision_" + uuid.uuid4().hex[:10] + target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_join(root, str(target.relative_to(root)))
        partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex[:8])
        self._update(job, phase="copying", message=remote)
        with partial.open("xb") as output, self.adb.sync(job["serial"], cancel) as sync:
            actual = sync.receive(remote, output, lambda n: self._progress(job, n))
            output.flush()
            os.fsync(output.fileno())
        # Sync v1 reports a 32-bit size; compare modulo 2^32 for files over 4 GiB.
        if actual % (2 ** 32) != entry.size:
            raise AdbError("File size changed or transfer incomplete: " + remote)
        with self.adb.sync(job["serial"], cancel) as sync:
            after = sync.stat(remote)
        if after.size != entry.size or after.mtime != entry.mtime:
            raise AdbError("File changed during acquisition; partial kept for review: " + remote)
        digest = file_hash(partial, cancel)
        try:
            os.utime(partial, (entry.mtime, entry.mtime))
        except (OSError, OverflowError, ValueError):
            pass
        install_new(partial, target)
        record = {"source": remote, "localPath": str(target.relative_to(root)), "size": actual,
                  "sourceSize32": entry.size, "sourceMtime": entry.mtime, "sourceMode": entry.mode,
                  "sha256": digest, "status": "copied", "category": category, "acquiredAt": utcnow()}
        self._append(root, record)
        record["_checkedThisAttempt"] = True
        records[remote] = record
        with self._lock:
            job["filesCopied"] += 1

    def _walk(self, job, cancel, path, category, records, visited, exclude=()):
        serial = job["serial"]
        queue = [path]
        while queue:
            self._check(cancel)
            current = queue.pop()
            if any(current == prefix or current.startswith(prefix.rstrip("/") + "/") for prefix in exclude):
                continue  # Deferred trees must remain unvisited for the later acquisition stage.
            if current in visited:
                continue
            visited.add(current)
            self._update(job, phase="scanning", message="Scanning " + current)
            try:
                with self.adb.sync(serial, cancel) as sync:
                    entries = sync.list(current)
                if not entries:
                    # Legacy LIST encodes permission/missing errors as an empty DONE.
                    readable = self.adb.shell(serial, f"if [ -d {quote(current)} ] && [ -r {quote(current)} ] && [ -x {quote(current)} ]; then echo RESCUE_READABLE; fi", check=False)
                    if "RESCUE_READABLE" not in readable.splitlines():
                        raise AdbError("Cannot enumerate directory (missing or permission denied): " + current)
                    folder = local_remote_path(Path(job["destination"]), category, current)
                    folder.mkdir(parents=True, exist_ok=True)
                for entry in sorted(entries, key=lambda e: e.name.encode("utf-8", "surrogateescape")):
                    self._check(cancel)
                    remote = current.rstrip("/") + "/" + entry.name
                    if stat.S_ISDIR(entry.mode):
                        queue.append(remote)
                    elif stat.S_ISREG(entry.mode):
                        with self._lock:
                            job["filesTotal"] += 1
                        try:
                            self._copy(job, cancel, remote, entry, category, records)
                        except (Cancelled, ConnectionLost):
                            raise
                        except (AdbError, OSError, ValueError) as exc:
                            if self._fatal_storage_error(exc):
                                raise
                            self._error(job, remote + ": " + str(exc))
                    else:
                        kind = "symbolic link" if stat.S_ISLNK(entry.mode) else "special file"
                        self._error(job, f"Not followed ({kind}): {remote}. Prevents loops and unintended private/device reads.")
                        self._append(Path(job["destination"]), {"source": remote, "status": "skipped", "reason": kind, "sourceMode": entry.mode, "category": category})
            except (Cancelled, ConnectionLost):
                raise
            except (AdbError, OSError, ValueError) as exc:
                if self._fatal_storage_error(exc):
                    raise
                self._error(job, str(exc))

    def _apk_inventory(self, job, cancel, records):
        serial = job["serial"]
        output = self.adb.shell_args(serial, ["pm", "list", "packages", "-f"])
        packages = []
        for line in output.splitlines():
            if line.startswith("package:") and "=" in line:
                fallback, package = line[8:].rsplit("=", 1)
                if re.fullmatch(r"[A-Za-z0-9_.]+", package):
                    packages.append((package, fallback))
        if not packages:
            raise AdbError("No installed packages returned by package manager")
        inventory = []
        before = len(job["errors"])
        for package, fallback in packages:
            self._check(cancel)
            paths = self.adb.shell_args(serial, ["pm", "path", package], check=False)
            apk_paths = [line[8:].strip() for line in paths.splitlines() if line.startswith("package:/")]
            if not apk_paths:
                apk_paths = [fallback]
                self._error(job, "Package manager did not enumerate split APKs for " + package + "; trying its base path")
            inventory.append({"package": package, "apkPaths": apk_paths})
            for path in apk_paths:
                with self._lock:
                    job["filesTotal"] += 1
                try:
                    with self.adb.sync(serial, cancel) as sync:
                        entry = sync.stat(path)
                    self._copy(job, cancel, path, entry, "apks", records)
                except (Cancelled, ConnectionLost):
                    raise
                except AdbError as exc:
                    # Some OEM SELinux policies allow shell to read system APKs
                    # while denying the adbd sync service. This is APK-only and
                    # requires independently matching before/after phone hashes.
                    try:
                        self._copy_apk_shell(job, cancel, path, records, str(exc))
                    except (Cancelled, ConnectionLost):
                        raise
                    except (AdbError, OSError, ValueError) as fallback_error:
                        if self._fatal_storage_error(fallback_error):
                            raise
                        self._error(job, "APK " + package + ": " + str(exc) + "; shell fallback: " + str(fallback_error))
                except (AdbError, OSError, ValueError) as exc:
                    if self._fatal_storage_error(exc):
                        raise
                    self._error(job, "APK " + package + ": " + str(exc))
        atomic_json(Path(job["destination"]) / "installed-apps.json", inventory)
        self._coverage(job, "App installers", "partial" if len(job["errors"]) > before else "copied",
                       f"Enumerated {len(packages)} packages and their APK splits. Installers do not contain private app data.")
        return [package for package, _ in packages]

    def _shell_apk_metadata(self, serial, remote, cancel):
        if not remote.startswith("/") or not remote.lower().endswith(".apk") or "\x00" in remote:
            raise AdbError("Shell fallback is restricted to absolute package-manager APK paths")
        def read_stat():
            self._check(cancel)
            output = self.adb.shell_args(serial, ["stat", "-c", "%s %Y %f", remote], timeout=60, cancel=cancel)
            match = re.fullmatch(r"(\d+) (-?\d+) ([0-9a-fA-F]+)", output.strip())
            if not match:
                raise AdbError("Shell could not provide reliable APK size, time and mode")
            size, mtime, mode = int(match[1]), int(match[2]), int(match[3], 16)
            if not stat.S_ISREG(mode):
                raise AdbError("Shell APK source is not a regular file")
            return {"size": size, "mtime": mtime, "mode": mode}
        before = read_stat()
        self._check(cancel)
        checksum = self.adb.shell_args(serial, ["sha256sum", remote], timeout=120, cancel=cancel)
        match = re.match(r"^([0-9a-fA-F]{64})\s", checksum)
        if not match:
            raise AdbError("Phone sha256sum is unavailable; unverified APK fallback refused")
        if read_stat() != before:
            raise AdbError("APK metadata changed while hashing on the phone")
        before["sha256"] = match[1].lower()
        return before

    def _copy_apk_shell(self, job, cancel, remote, records, sync_reason):
        root, serial = Path(job["destination"]), job["serial"]
        self._update(job, phase="checking-apk", message="Checking shell-readable APK: " + remote)
        before = self._shell_apk_metadata(serial, remote, cancel)
        old = records.get(remote)
        if old and old.get("sha256") == before["sha256"] and old.get("size") == before["size"]:
            previous = safe_join(root, old["localPath"])
            if previous.is_file() and previous.stat().st_size == before["size"] and file_hash(previous, cancel) == before["sha256"]:
                old["_checkedThisAttempt"] = True
                with self._lock:
                    job["filesCopied"] += 1
                    job["bytesCopied"] += old["size"]
                return
        target = local_remote_path(root, "apks", remote)
        if target.exists():
            target = target.with_name(target.stem + "_revision_" + uuid.uuid4().hex[:10] + target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_join(root, str(target.relative_to(root)))
        partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex[:8])
        stderr_tail = bytearray()
        self._update(job, phase="copying", message="Copying APK through verified shell fallback: " + remote)
        command = self.adb.command(serial, ["exec-out", "cat " + quote(remote)])
        with partial.open("xb") as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, bufsize=0, **self.adb.process_options())
            chunks = queue.Queue(maxsize=8)
            stop_readers = threading.Event()
            def enqueue(value):
                while not stop_readers.is_set():
                    try:
                        chunks.put(value, timeout=.1)
                        return
                    except queue.Full:
                        continue
            def read_stdout():
                try:
                    while not stop_readers.is_set():
                        chunk = process.stdout.read(65536)
                        if not chunk:
                            enqueue(None)
                            return
                        enqueue(chunk)
                except (OSError, ValueError) as exc:
                    enqueue(ConnectionLost("Cannot read APK shell stream: " + str(exc)))
            def drain_stderr():
                try:
                    while True:
                        chunk = process.stderr.read(65536)
                        if not chunk:
                            break
                        available = 65536 - len(stderr_tail)
                        if available > 0:
                            stderr_tail.extend(chunk[:available])
                except (OSError, ValueError):
                    pass
            drain = threading.Thread(target=drain_stderr, daemon=True, name="apk-stderr")
            reader = threading.Thread(target=read_stdout, daemon=True, name="apk-stdout")
            drain.start()
            reader.start()
            copied, last_progress, stdout_done = 0, time.monotonic(), False
            try:
                while not stdout_done or process.poll() is None:
                    self._check(cancel)
                    if time.monotonic() - last_progress > 45:
                        raise ConnectionLost("APK shell stream stopped responding; reconnect and resume")
                    if stdout_done:
                        cancel.wait(.1)
                        continue
                    try:
                        chunk = chunks.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if chunk is None:
                        stdout_done = True
                        continue
                    if isinstance(chunk, Exception):
                        raise chunk
                    if copied + len(chunk) > before["size"]:
                        raise AdbError("APK stream exceeds its measured size; partial retained")
                    output.write(chunk)  # Local disk errors surface here and stop the job.
                    copied += len(chunk)
                    self._progress(job, len(chunk))
                    last_progress = time.monotonic()
                self._check(cancel)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise
            finally:
                stop_readers.set()
                reader.join(timeout=5)
                drain.join(timeout=5)
                process.stdout.close()
                process.stderr.close()
            output.flush()
            os.fsync(output.fileno())
        size = partial.stat().st_size
        error = stderr_tail.decode("utf-8", "replace").strip()
        if process.returncode or error:
            raise AdbError("APK shell stream failed: " + (error[:2000] or str(process.returncode)))
        if size != before["size"]:
            raise AdbError("APK shell stream has the wrong size; incomplete partial retained")
        digest = file_hash(partial, cancel)
        if digest != before["sha256"]:
            raise AdbError("APK shell stream SHA-256 mismatch; unverified partial retained")
        after = self._shell_apk_metadata(serial, remote, cancel)
        if after != before:
            raise AdbError("APK changed on the phone during shell copying; unverified partial retained")
        try:
            os.utime(partial, (before["mtime"], before["mtime"]))
        except (OSError, OverflowError, ValueError):
            pass
        install_new(partial, target)
        record = {"source": remote, "localPath": str(target.relative_to(root)), "size": size,
                  "sourceSize32": size % (2 ** 32), "sourceMtime": before["mtime"], "sourceMode": before["mode"],
                  "sha256": digest, "sourceSha256Before": before["sha256"], "sourceSha256After": after["sha256"],
                  "status": "copied", "category": "apks", "acquiredAt": utcnow(),
                  "acquisitionMethod": "adb-exec-out-cat", "syncFallbackReason": sync_reason[:500]}
        self._append(root, record)
        record["_checkedThisAttempt"] = True
        records[remote] = record
        with self._lock:
            job["filesCopied"] += 1

    def _stream_archive(self, job, cancel, args, name, category, source, output_arg=False):
        root = Path(job["destination"])
        folder = safe_join(root, category)
        folder.mkdir(parents=True, exist_ok=True)
        if len(name) > 55:
            name = name[:20] + "~" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        target = folder / (name + "_" + uuid.uuid4().hex[:8] + (".ab" if output_arg else ".tar"))
        partial = target.with_name(target.name + ".partial")
        error_path = target.with_name(target.name + ".stderr.txt")
        command = self.adb.command(job["serial"], args + (["-f", str(partial)] if output_arg else []))
        self._update(job, phase="archive", message=("Confirm backup on the phone, then wait" if output_arg else "Archiving " + source))
        with error_path.open("xb") as stderr, (open(os.devnull, "wb") if output_arg else partial.open("xb")) as stdout:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **self.adb.process_options())
            previous = 0
            try:
                while process.poll() is None:
                    self._check(cancel)
                    size = partial.stat().st_size if partial.exists() else 0
                    if size > previous:
                        if output_arg and previous == 0:
                            self._update(job, message="Receiving app backup from the phone")
                        self._progress(job, size - previous)
                    previous = size
                    cancel.wait(.2)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise
        errors = error_path.read_text(encoding="utf-8", errors="replace").strip()
        if process.returncode or not partial.exists() or partial.stat().st_size < (24 if output_arg else 1024):
            raise AdbError("Archive failed or empty: " + source + (": " + errors[:2000] if errors else ""))
        final_size = partial.stat().st_size
        if final_size > previous:
            self._progress(job, final_size - previous)
        if output_arg:
            with partial.open("rb") as stream:
                if stream.read(15) != b"ANDROID BACKUP\n":
                    raise AdbError("Legacy output is not an Android backup header; unverified partial preserved")
        if not output_arg:
            with partial.open("rb") as stream:
                header = stream.read(512)
            try:
                checksum = int(header[148:156].strip(b"\x00 ") or b"0", 8)
            except ValueError:
                checksum = -1
            if sum(header[:148]) + 8 * 32 + sum(header[156:]) != checksum:
                raise AdbError("Output is not a valid tar header; unverified partial preserved: " + source)
            self._update(job, phase="verifying-archive", message="Checking archive structure: " + source)
            try:
                with partial.open("rb") as stream:
                    if partial.stat().st_size % 512:
                        raise AdbError("Archive length is not a complete tar block")
                    stream.seek(-1024, os.SEEK_END)
                    if stream.read(1024) != bytes(1024):
                        raise AdbError("Archive has no complete tar end marker")
                with tarfile.open(partial, "r:") as archive:
                    count = 0
                    while archive.next() is not None:
                        self._check(cancel)
                        count += 1
                        archive.members.clear()  # Do not retain millions of metadata objects.
                    if not count:
                        raise AdbError("Archive contains no entries")
            except tarfile.TarError as exc:
                raise AdbError("Archive structure is incomplete; partial preserved: " + str(exc)) from exc
        digest = file_hash(partial, cancel)
        size = partial.stat().st_size
        install_new(partial, target)
        record = {"source": source + "#" + target.name, "localPath": str(target.relative_to(root)), "size": size,
                  "sha256": digest, "status": "copied", "category": category, "acquiredAt": utcnow(),
                  "contentVerification": "unverified legacy archive" if output_arg else "tar structure and end marker checked; not a full restore test"}
        self._append(root, record)
        with self._lock:
            job["filesCopied"] += 1
            job["filesTotal"] += 1
        if errors:
            self._error(job, "Archive reported warnings for " + source + ": " + errors[:2000])
        return str(target)

    def _private_apps(self, job, cancel, packages):
        if not packages:
            text = self.adb.shell_args(job["serial"], ["pm", "list", "packages"])
            packages = [line[8:].strip() for line in text.splitlines() if line.startswith("package:")]
        copied, unavailable = [], []
        for package in packages:
            self._check(cancel)
            if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
                continue
            self._update(job, phase="permissions", message="Checking private-data access: " + package)
            result = self.adb.shell_args(job["serial"], ["run-as", package, "id"], check=False, timeout=15)
            if not re.search(r"\buid=\d+", result):
                unavailable.append({"package": package, "reason": result[:500] or "run-as is unavailable or app is not debuggable"})
                continue
            try:
                command = "run-as " + quote(package) + " sh -c " + quote("tar -cf - .")
                self._stream_archive(job, cancel, ["exec-out", command], safe_name(package), "private-apps", "run-as:" + package)
                copied.append(package)
            except Cancelled:
                raise
            except (OSError, AdbError, ValueError) as exc:
                unavailable.append({"package": package, "reason": str(exc)})
                self._error(job, "Private app " + package + ": " + str(exc))
        atomic_json(Path(job["destination"]) / "private-app-access.json", {"copied": copied, "unavailable": unavailable})
        self._coverage(job, "Private app data", "partial" if copied else "blocked",
                       f"run-as archived {len(copied)} debuggable apps; {len(unavailable)} unavailable. See private-app-access.json. Secondary profiles and device-encrypted app storage are not covered by run-as.")

    def _existing_root(self, job, cancel):
        result = self.adb.shell_args(job["serial"], ["su", "-c", "id"], timeout=30, check=False)
        if not re.search(r"\buid=0\b", result):
            self._coverage(job, "Existing root", "blocked", "Existing su did not grant root; no rooting was attempted.")
            return
        # /data includes private apps, secondary users, credentials and system DBs.
        # Streaming a read-only archive avoids host-side untrusted tar extraction.
        self._stream_archive(job, cancel, ["exec-out", "su -c " + quote("tar -cf - /data")],
                             "existing-root-data", "root-archive", "existing-root:/data")
        self._coverage(job, "Existing root", "partial", "Read-only /data tar captured with existing su. Live databases may be inconsistent; encrypted keys, cloud data and deleted blocks are not guaranteed. Shared data may also occur in this archive.")

    def _helper(self, job, cancel, records, visited):
        canonical = self._canonical(job["serial"], HELPER_EXPORTS)
        if not canonical:
            self._coverage(job, "Contacts, messages and call history", "blocked", "No helper export folder found. Install/open the offline helper, grant permissions, run its export, then resume.")
            return
        before = len(job["errors"])
        with self.adb.sync(job["serial"], cancel) as sync:
            run_entries = sync.list(canonical)
        runs = sorted(e.name for e in run_entries if stat.S_ISDIR(e.mode))
        self._walk(job, cancel, canonical, "helper-exports", records, visited)
        helper_records = [record for path, record in records.items() if path.startswith(canonical.rstrip("/") + "/")]
        reports = {}
        latest = runs[-1] if runs else None
        latest_report = None
        for record in helper_records:
            if PurePosixPath(record["source"]).name in ("report.json", "export-report.json"):
                run = record["source"][len(canonical.rstrip("/")) + 1:].split("/")[0]
                reports[run] = {"localPath": record["localPath"], "bytes": record["size"], "sha256": record["sha256"]}
                if run != latest:
                    continue  # All originals stay copied; only the latest controls coverage.
                try:
                    local = safe_join(Path(job["destination"]), record["localPath"])
                    if local.stat().st_size > MAX_HELPER_REPORT_BYTES:
                        raise ValueError("Latest helper report exceeds the 128 MiB metadata limit; original retained")
                    if local.stat().st_size != record["size"] or (not record.get("_checkedThisAttempt") and file_hash(local, cancel) != record["sha256"]):
                        raise ValueError("Preserved helper report no longer matches its acquisition hash or size")
                    record["_checkedThisAttempt"] = True
                    with local.open("rb") as stream:
                        latest_report = load_helper_report(stream)
                    reports[run].update({key: latest_report.get(key) for key in ("runFinished", "status", "complete", "exportedRows", "exportedBinaryFiles")})
                    reports[run]["categoryCount"] = len(latest_report.get("categories", [])) if isinstance(latest_report.get("categories"), list) else 0
                except (ValueError, OSError, RecursionError, MemoryError) as exc:
                    reports[run]["parseError"] = str(exc)
                    self._error(job, "Latest helper report could not be parsed: " + str(exc))
        summary = {"latestRun": latest, "runs": runs, "reports": reports,
                   "note": "The newest folder is authoritative; an older completed export cannot establish that a newer interrupted run finished. Each report localPath points to the preserved full report, including every category and error."}
        atomic_json(Path(job["destination"]) / "helper-reports.json", summary)
        if not latest_report or latest_report.get("runFinished") is not True:
            self._coverage(job, "Contacts, messages and call history", "partial",
                           f"Copied {len(helper_records)} helper-export files. Latest run {latest or '(none)'} has no valid completion marker; reopen the phone helper and complete an export, then resume.")
            return
        categories = latest_report.get("categories", [])
        if not isinstance(categories, list):
            categories = []
        complete = (latest_report.get("status") == "complete" and latest_report.get("complete") is True
                    and not latest_report.get("errors") and bool(categories) and len(job["errors"]) == before)
        def verified_local(record, checksum, size):
            if not record or record.get("sha256") != checksum or record.get("size") != size:
                return False
            try:
                path = safe_join(Path(job["destination"]), record["localPath"])
                if not path.is_file() or path.stat().st_size != size:
                    return False
                if not record.get("_checkedThisAttempt"):
                    if file_hash(path, cancel) != checksum:
                        return False
                    record["_checkedThisAttempt"] = True
                return True
            except (OSError, ValueError, KeyError):
                return False
        address_summary = {"count": 0, "verified": 0, "rows": 0, "assetErrors": 0}
        for index, category in enumerate(categories):
            self._check(cancel)
            if index % 250 == 0:
                self._update(job, phase="verifying-helper", message=f"Checking phone export files: {index:,} / {len(categories):,} categories")
            if not isinstance(category, dict):
                complete = False
                continue
            name = str(category.get("category", "Unknown"))
            is_mms_address = re.fullmatch(r"mms_addresses_\d+", name) is not None
            relative = category.get("file")
            record = records.get(canonical + "/" + latest + "/" + relative) if isinstance(relative, str) else None
            verified = verified_local(record, category.get("sha256"), category.get("bytes"))
            asset_count = 0
            asset_errors = 0
            if verified:
                category_file = safe_join(Path(job["destination"]), record["localPath"])
                with category_file.open(encoding="utf-8") as stream:
                    for line in stream:
                        self._check(cancel)
                        try:
                            envelope = json.loads(line)
                            exports = envelope.get("export", {}) if isinstance(envelope, dict) else {}
                            if not isinstance(exports, dict):
                                raise ValueError("Invalid helper export references")
                            for asset in exports.values():
                                if not isinstance(asset, dict) or "file" not in asset:
                                    continue
                                asset_count += 1
                                asset_file = asset.get("file")
                                asset_record = records.get(canonical + "/" + latest + "/" + asset_file) if isinstance(asset_file, str) else None
                                if not verified_local(asset_record, asset.get("sha256"), asset.get("bytes")):
                                    asset_errors += 1
                        except (ValueError, TypeError):
                            asset_errors += 1
                if asset_errors:
                    verified = False
                    if not is_mms_address:
                        self._error(job, f"Phone export {name}: {asset_errors} missing, mismatched or unreadable attachment references in the latest helper run")
            status = "copied" if verified and category.get("status") == "complete" else "partial"
            complete = complete and status == "copied"
            if is_mms_address:
                address_summary["count"] += 1
                address_summary["verified"] += int(status == "copied")
                rows = category.get("exportedRows", 0)
                address_summary["rows"] += rows if isinstance(rows, int) and rows >= 0 else 0
                address_summary["assetErrors"] += asset_errors
                continue
            detail = f"Latest export: {category.get('exportedRows', 0)} rows; helper status {category.get('status', 'unknown')}; "
            detail += "export file matches the phone helper SHA-256." if verified else "export file missing or helper hash/size not verified."
            if asset_count:
                detail += f" {asset_count} attachment references checked; {asset_errors} issues."
            self._coverage(job, "Phone export: " + name, status, detail)
        if address_summary["count"]:
            good, total = address_summary["verified"], address_summary["count"]
            if good != total:
                self._error(job, f"Phone export MMS addresses: {total - good} of {total} category files missing, mismatched or reported incomplete; full category details are in the preserved helper report")
            self._coverage(job, "Phone export: MMS addresses", "copied" if good == total else "partial",
                           f"Checked every per-message address export: {good:,} / {total:,} files verified; {address_summary['rows']:,} provider rows. {address_summary['assetErrors']} attachment-reference issues. Full per-message metadata remains in the preserved helper report.")
        self._coverage(job, "Contacts, messages and call history", "copied" if complete else "partial",
                       f"Latest helper run {latest}: {latest_report.get('status', 'unknown')}, {latest_report.get('exportedRows', 0)} provider rows. Copied {len(helper_records)} files across {len(runs)} runs. Review helper-reports.json; scope is granted content providers in this phone profile.")

    def analyze(self, job_id):
        """Run archive checks asynchronously without recopying or changing originals."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError("Unknown extraction job")
            if self._verifying or any(t.is_alive() for t in self._threads.values()) or any(j["status"] == "running" for j in self._jobs.values()):
                raise ValueError("Wait for copying, verification or archive checks to finish first")
            if not (Path(job["destination"]) / "manifest.jsonl").is_file():
                raise ValueError("This archive has no copied-file manifest to check")
            job["statusBeforeAnalysis"] = job["status"]
            job.update(status="running", analysisOnly=True, analysisStatus="running",
                       phase="archive-checks", message="Checking saved data for deleted items and locations")
            cancel = threading.Event()
            self._cancel[job_id] = cancel
            thread = threading.Thread(target=self._run_analysis, args=(job, cancel), daemon=True,
                                      name="archive-checks-" + job_id[:8])
            self._threads[job_id] = thread
            self._save(job, True)
            thread.start()
            return self._snapshot(job)

    def _run_analysis(self, job, cancel):
        message = "Deleted-item and location checks finished. Review their separate reports."
        execution_state = None
        if os.name == "nt":
            try:
                import ctypes
                execution_state = ctypes.windll.kernel32.SetThreadExecutionState
                execution_state.argtypes = [ctypes.c_uint]
                execution_state.restype = ctypes.c_uint
                if not execution_state(0x80000001):
                    execution_state = None
            except (AttributeError, OSError):
                execution_state = None
        try:
            self._archive_checks(job, cancel)
            if job.get("analysisStatus") != "complete":
                message = "Archive checks finished with gaps. Review the deleted-item and location reports."
        except Cancelled:
            job["analysisStatus"] = "cancelled"
            message = "Archive checks stopped. Existing results and original copies were kept."
        except Exception as exc:
            job["analysisStatus"] = "partial"
            message = "Archive checks need review: " + str(exc)
        finally:
            with self._lock:
                job.update(status=job.pop("statusBeforeAnalysis", "partial"), analysisOnly=False,
                           analysisFinishedAt=utcnow(), phase="finished", message=message)
            try:
                self._write_report(job)
            except Exception as exc:
                self._error(job, "Cannot write archive-check report: " + str(exc))
                job.update(analysisStatus="partial", message="Archive checks stopped, but the report could not be saved. Review available results and run the checks again.")
            self._save(job, True)
            if execution_state:
                execution_state(0x80000000)

    def _capture_device_context(self, job, cancel, records):
        from device_context import capture_context

        def register(result):
            captures = result.get("records", [])
            for record in captures:
                self._append(Path(job["destination"]), record)
                records[record["source"]] = record
            with self._lock:
                job["filesCopied"] += len(captures)
                job["filesTotal"] += len(captures)
                job["bytesCopied"] += sum(record["size"] for record in captures)
            compact = {key: value for key, value in result.items() if key != "records"}
            self._update(job, deviceContext=compact)
            counts = result.get("counts", {})
            status = "copied" if result.get("status") == "complete" else "partial"
            self._coverage(job, "Device activity and settings", status,
                           f"Saved {len(captures)} command captures; {counts.get('issues', 0)} unavailable or incomplete results. "
                           "These are device-returned snapshots and retained traces, not complete activity, account or deleted-message history. See the device activity report.")

        try:
            result = capture_context(Path(job["destination"]), self.adb, job["serial"], cancel=cancel,
                                     progress=lambda phase, message: self._update(job, phase=phase, message=message))
        except Exception as exc:
            if isinstance(getattr(exc, "result", None), dict):
                register(exc.result)
            raise
        register(result)

    def _archive_checks(self, job, cancel):
        """Collect only readable trash, then analyze verified PC copies locally."""
        root = Path(job["destination"])
        for item in job.get("coverage", []):
            if item.get("category") == "Cloud, secure and deleted data":
                item["detail"] = "Cloud-only records, inaccessible encrypted stores, hardware-backed secrets and permanently erased files are outside this logical acquisition. Accessible retained trash is checked separately below."
        self._update(job, analysisStatus="running")
        progress = lambda phase, message: self._update(job, phase=phase, message=message)
        operations = [
            ("phoneTrash", "Phone retained trash", "phone"),
            ("deletedItems", "Deleted-item archive check", "deleted"),
            ("locationData", "Location archive check", "location"),
        ]
        for key, label, kind in operations:
            self._check(cancel)
            progress("archive-checks", "Checking " + label.lower())
            try:
                if kind == "phone":
                    connected = any(d["serial"] == job["serial"] and d["state"] == "device" for d in self.devices())
                    if not connected:
                        result = {"status": "unavailable", "counts": {}, "limitations": ["Connect the original authorized phone to check retained MediaStore trash."]}
                    else:
                        from phone_trash import acquire_trash
                        result = acquire_trash(root, self.adb, job["serial"], cancel=cancel, progress=progress)
                    counts = result.get("counts", {})
                    detail = f"Retained trash: {counts.get('rows', 0)} rows found; {counts.get('filesCopied', 0)} files saved separately. Permanently erased storage is not accessible through this check."
                elif kind == "deleted":
                    from deleted_items import check_archive
                    result = check_archive(root, cancel=cancel, progress=progress)
                    counts = result.get("counts", {})
                    detail = f"{counts.get('fileCopiesSaved', 0)} of {counts.get('fileCandidates', 0)} trash-path file candidates saved; {counts.get('messageCandidates', 0)} explicitly flagged message rows. Originals retained; findings are in deleted-items. This does not undelete erased blocks."
                else:
                    from location_data import check_archive
                    result = check_archive(root, cancel=cancel, exiftool_path=self.base_dir / "tools/exiftool/exiftool.exe", progress=progress)
                    counts = result.get("counts", {})
                    detail = f"{counts.get('features', 0)} location features; {counts.get('gpsMedia', 0)} media files with GPS. Sources and reports are in location-data. Private app histories and cloud-only Timeline are not implied."
                with self._lock:
                    job[key] = result
                status = "checked" if result.get("status") in ("complete", "completed") else "partial"
                if result.get("status") == "unavailable":
                    status = "blocked"
                    detail = " ".join(result.get("limitations", [])) or detail
                self._coverage(job, label, status, detail)
            except Cancelled:
                raise
            except Exception as exc:
                with self._lock:
                    job[key] = {"status": "partial", "error": str(exc), "counts": {"issues": 1}}
                self._coverage(job, label, "partial", str(exc))
                if self._fatal_storage_error(exc):
                    raise
        status = "complete" if all(job[key].get("status") in ("complete", "completed") for key, _, _ in operations) else "partial"
        self._update(job, analysisStatus=status, analysisFinishedAt=utcnow())

    def _run(self, job, cancel):
        root = Path(job["destination"])
        execution_state = None
        if os.name == "nt":
            try:
                import ctypes
                execution_state = ctypes.windll.kernel32.SetThreadExecutionState
                execution_state.argtypes = [ctypes.c_uint]
                execution_state.restype = ctypes.c_uint
                # Keep the system awake for this worker without keeping the display on.
                if not execution_state(0x80000000 | 0x00000001):
                    execution_state = None
            except (AttributeError, OSError):
                execution_state = None
        try:
            info = self.inspect(job["serial"])
            old_info = root / "device.json"
            if old_info.exists():
                previous = json.loads(old_info.read_text(encoding="utf-8"))
                if previous.get("serial") != info["serial"] or previous.get("properties", {}).get("ro.serialno") not in (None, "", info["properties"].get("ro.serialno")):
                    raise AdbError("Connected device identity differs from the original acquisition")
            atomic_json(old_info, info)
            self._update(job, model=info["model"], android=info["android"])
            records = self._manifest(root)
            visited = set()
            options = job["options"]
            if options.get("deviceContext", False):
                self._capture_device_context(job, cancel, records)
            if options.get("shared", True):
                deferred = set()
                if options.get("helper", True):
                    helper_root = self._canonical(job["serial"], HELPER_EXPORTS)
                    if not helper_root:
                        shared_alias = self._canonical(job["serial"], "/sdcard")
                        if shared_alias:
                            helper_root = shared_alias.rstrip("/") + "/AndroidRescue/exports"
                    if helper_root:
                        deferred.add(helper_root)
                requested = options.get("roots") or [r["path"] for r in info["roots"] if r["accessible"]]
                before = len(job["errors"])
                for unavailable in [r for r in info["roots"] if not r["accessible"]]:
                    self._error(job, "Discovered storage/profile is inaccessible: " + unavailable["path"])
                if not requested:
                    self._error(job, "No accessible shared-storage root was found")
                canonical_roots = []
                for path in requested:
                    canonical = self._canonical(job["serial"], path)
                    if not canonical:
                        self._error(job, "Storage unavailable: " + path)
                    elif canonical not in canonical_roots:
                        canonical_roots.append(canonical)
                for path in sorted(canonical_roots, key=len):
                    self._walk(job, cancel, path, "shared-storage", records, visited, exclude=deferred)
                self._coverage(job, "Shared storage and SD cards", "partial" if len(job["errors"]) > before else "copied",
                               f"Scanned {len(canonical_roots)} selected accessible roots, including hidden files. Reported permission and special-file gaps remain; inaccessible profiles/volumes are not included."
                               + (" Phone helper exports are deferred to the final acquisition stage." if deferred else ""))
            else:
                self._coverage(job, "Shared storage and SD cards", "skipped", "Not selected")
            packages = []
            if options.get("apks", True):
                try:
                    packages = self._apk_inventory(job, cancel, records)
                except (Cancelled, ConnectionLost):
                    raise
                except (AdbError, OSError, ValueError) as exc:
                    if self._fatal_storage_error(exc):
                        raise
                    self._error(job, "APK inventory: " + str(exc))
                    self._coverage(job, "App installers", "partial", str(exc))
            else:
                self._coverage(job, "App installers", "skipped", "Not selected")
            if options.get("privateApps", False):
                self._private_apps(job, cancel, packages)
            else:
                self._coverage(job, "Private app data", "blocked", "Normal ADB cannot read app sandboxes. Optional run-as supports only debuggable apps; app-specific exports may be required.")
            if options.get("existingRoot", False):
                try:
                    self._existing_root(job, cancel)
                except Cancelled:
                    raise
                except (AdbError, OSError, ValueError) as exc:
                    self._error(job, "Existing root: " + str(exc))
                    self._coverage(job, "Existing root", "partial", str(exc))
            if options.get("legacy", False):
                try:
                    command = legacy_backup_command(info.get("sdk"), include_apks=options.get("apks", True), include_shared=options.get("shared", True))
                    self._stream_archive(job, cancel, command, "legacy-backup", "legacy", "legacy-adb-backup", output_arg=True)
                    self._coverage(job, "Legacy Android backup", "unverified", "ADB backup file preserved. Apps may opt out and recent Android versions remove/restrict this feature. File existence does not prove data coverage; no restore was tested.")
                except Cancelled:
                    raise
                except (AdbError, OSError, ValueError) as exc:
                    self._error(job, "Legacy backup: " + str(exc))
                    self._coverage(job, "Legacy Android backup", "partial", str(exc))
            if options.get("helper", True):
                self._helper(job, cancel, records, visited)
            else:
                self._coverage(job, "Contacts, messages and call history", "skipped", "Helper export not selected")
            if options.get("archiveChecks", False):
                self._archive_checks(job, cancel)
            self._coverage(job, "Cloud, secure and deleted data", "blocked", "Cloud-only records, inaccessible encrypted stores, hardware-backed secrets and permanently erased files are outside this logical acquisition. Accessible retained trash is checked separately when archive checks are enabled.")
            # The status describes the chosen accessible transfer, never whole-phone completeness.
            baseline_limits = {"Cloud, secure and deleted data"}
            if not options.get("privateApps", False):
                baseline_limits.add("Private app data")
            incomplete = bool(job["errors"]) or any(c["status"] in ("partial", "blocked", "unverified", "review") and c["category"] not in baseline_limits for c in job["coverage"])
            self._update(job, status="partial" if incomplete else "completed", phase="finished",
                         message="Selected accessible files copied. Review the coverage report for missing or restricted data.", finishedAt=utcnow())
        except Cancelled as exc:
            if job.get("analysisStatus") == "running":
                job["analysisStatus"] = "cancelled"
            self._update(job, status="cancelled", phase="cancelled", message=str(exc), finishedAt=utcnow())
        except Exception as exc:
            if job.get("analysisStatus") == "running":
                job["analysisStatus"] = "partial"
            self._error(job, str(exc))
            self._update(job, status="partial" if job["filesCopied"] else "failed", phase="failed", message=str(exc), finishedAt=utcnow())
        finally:
            try:
                try:
                    self._write_report(job)
                except Exception as exc:
                    self._error(job, "Cannot write report: " + str(exc))
                    if job["status"] in ("completed", "partial"):
                        self._update(job, status="partial" if job["filesCopied"] else "failed",
                                     message="Copying stopped, but the report could not be finished. Free disk space, then resume.")
                self._save(job, True)
            finally:
                if execution_state:
                    execution_state(0x80000000)

    def _write_report(self, job):
        import html
        root = Path(job["destination"])
        atomic_json(root / "report.json", job)
        try:
            from catalog import write_catalog
        except ImportError:
            write_catalog = None
        if write_catalog:
            write_catalog(root)
        escaped = lambda value: html.escape(str(value))
        rows = "".join("<tr><td>" + escaped(c["category"]) + "</td><td>" + escaped(c["status"]) + "</td><td>" + escaped(c["detail"]) + "</td></tr>" for c in job["coverage"])
        errors = "".join("<li>" + escaped(e) + "</li>" for e in job["errors"])
        limits = "".join("<li>" + escaped(e) + "</li>" for e in LIMITATIONS)
        content = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Android Bay acquisition report</title><style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#182638}}h1{{font-size:30px}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;border:1px solid #ccd5df;padding:12px;vertical-align:top}}li{{margin:10px 0}}code{{overflow-wrap:anywhere}}</style><h1>Android Bay acquisition report</h1><p><strong>This is not proof of a complete phone backup.</strong> Review missing and restricted categories before changing or retiring the phone.</p><p>Status: {escaped(job['status'])} · Files: {job['filesCopied']} / {job['filesTotal']} · Bytes handled: {job['bytesCopied']}</p><p>Device: {escaped(job.get('model', 'Unknown'))} · Serial: {escaped(job['serial'])}</p><p>Started: {escaped(job['startedAt'])} · Finished: {escaped(job['finishedAt'])}</p><p>Destination: <code>{escaped(job['destination'])}</code></p><h2>Coverage</h2><table><tr><th>Category</th><th>Status</th><th>Detail</th></tr>{rows}</table><h2>Errors and omissions ({len(job['errors'])})</h2><ul>{errors or '<li>No transfer errors recorded. Restricted categories still apply.</li>'}</ul><h2>Limits</h2><ul>{limits}</ul><h2>Archive and verification</h2><p>manifest.jsonl maps every copied file to its original Android path and SHA-256. PC filenames include stable suffixes to preserve names that collide on Windows. Long paths are mapped into _long_paths. .partial files are unfinished and are not counted as verified copies. Archives are kept raw; never extract an untrusted archive over existing files.</p><p>Use Verify in the app to re-hash PC copies. Resume rechecks finished files and retries missing/changed files, preserving previous versions. Reported source sizes use the legacy 32-bit ADB field; actual PC sizes are recorded separately. Live app databases can change during acquisition.</p></html>"""
        if (root / "catalog.html").exists():
            content = content.replace("<h2>Archive and verification</h2>", '<h2>Browse copied files</h2><p><a href="catalog.html">Open the searchable file catalog using original Android names</a></p><h2>Archive and verification</h2>')
        if job.get("verification"):
            receipt = job["verification"]
            content = content.replace("<h2>Archive and verification</h2>", "<h2>Latest PC verification</h2><p>" + escaped(receipt.get("checkedAt")) + ": " + str(receipt.get("verified", 0)) + " files matched, " + str(len(receipt.get("issues", []))) + ' issues. <a href="verification.json">Verification receipt</a>.</p><h2>Archive and verification</h2>')
        check_links = []
        for key, relative, label in (("phoneTrash", "deleted-items/phone-trash/report.html", "Retained phone trash"),
                                     ("deletedItems", "deleted-items/report.html", "Deleted-item archive check"),
                                     ("locationData", "location-data/report.html", "Location data"),
                                     ("deviceContext", "device-context/report.html", "Device activity and settings")):
            if key == "deviceContext":
                context = job.get(key) or {}
                report_path = context.get("reportPath")
                if context.get("receiptSaved") is False or not isinstance(report_path, str) or not report_path:
                    continue
                try:
                    if Path(report_path).resolve() != safe_join(root, relative):
                        continue
                except (OSError, ValueError):
                    continue
            if job.get(key) and safe_join(root, relative).is_file():
                check_links.append('<li><a href="' + relative + '">' + label + '</a>: ' + escaped(job[key].get("status", "unknown")) + '</li>')
        if check_links:
            content = content.replace("<h2>Archive and verification</h2>", '<h2>Additional recovery reports</h2><p>Separate reports retain provenance and coverage limits. A completed check does not establish whole-phone recovery or recover permanently erased storage.</p><ul>' + ''.join(check_links) + '</ul><h2>Archive and verification</h2>')
        (root / "report.html").write_text(content, encoding="utf-8", errors="xmlcharrefreplace")

    def verify(self, job_id):
        with self._lock:
            job = self.get_job(job_id)
            if job["status"] == "running" or job_id in self._verifying or (job_id in self._threads and self._threads[job_id].is_alive()):
                raise ValueError("Wait until copying or verification stops before verifying")
            self._verifying.add(job_id)
        try:
            result = self._verify_files(job)
            with self._lock:
                self._jobs[job_id]["verification"] = result
                self._save(self._jobs[job_id], True)
                job = self._snapshot(self._jobs[job_id])
            self._write_report(job)
            return result
        finally:
            with self._lock:
                self._verifying.discard(job_id)

    def _verify_files(self, job):
        job_id = job["id"]
        root = Path(job["destination"])
        records = []
        issues = []
        manifest = root / "manifest.jsonl"
        if not manifest.exists():
            raise ValueError("This job has no completed-file manifest")
        with manifest.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("Expected a manifest object")
                    if record.get("status") == "copied":
                        records.append(record)
                except (ValueError, TypeError):
                    issues.append({"path": "manifest.jsonl", "error": f"Unreadable record on line {number}"})
        verified = 0
        seen = set()
        for record in records:
            relative = record.get("localPath", "")
            if relative in seen:
                continue
            seen.add(relative)
            try:
                path = safe_join(root, relative)
                if path.stat().st_size != record["size"] or file_hash(path) != record["sha256"]:
                    issues.append({"path": relative, "error": "SHA-256 or file-size mismatch"})
                else:
                    verified += 1
            except (OSError, ValueError, KeyError) as exc:
                issues.append({"path": relative, "error": str(exc)})
        result = {"jobId": job_id, "checkedAt": utcnow(), "verified": verified, "total": len(seen),
                  "issues": issues, "ok": not issues and bool(seen), "meaning": "Checks PC copies against acquisition hashes; does not prove whole-phone completeness or restorability."}
        atomic_json(root / "verification.json", result)
        return result
