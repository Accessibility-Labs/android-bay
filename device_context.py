"""Bounded, read-only Android context snapshots through an authorized ADB shell.

No arbitrary commands, state changes, listeners, history enabling, root, or phone
file writes. Raw output stays in unique local files, never in the returned summary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import quote as url_quote

from adb import Cancelled, quote

MIB = 1024 * 1024
MAX_TOTAL_BYTES = 128 * MIB
MAX_STDERR_BYTES = 64 * 1024
PREFIX_BYTES = 8192


@dataclass(frozen=True)
class Command:
    key: str
    args: tuple[str, ...]
    limit: int
    timeout: float = 30
    fallback: tuple[str, ...] | None = None


COMMANDS = (
    Command("notifications", ("dumpsys", "notification", "--noredact"), 16 * MIB),
    Command("usage-current", ("dumpsys", "usagestats"), 16 * MIB, 60),
    Command("usage-retained", ("dumpsys", "usagestats", "file"), 64 * MIB, 120),
    Command("account-types", ("dumpsys", "account", "--checkin"), MIB),
    Command("packages-installed", ("pm", "list", "packages", "-i", "-U", "--show-versioncode", "--user", "0"), 4 * MIB,
            fallback=("pm", "list", "packages", "-i")),
    Command("packages-retained", ("pm", "list", "packages", "-i", "-U", "--show-versioncode", "--user", "0", "-u"), 4 * MIB,
            fallback=("pm", "list", "packages", "-i", "-u")),
    Command("location-state", ("dumpsys", "location"), 4 * MIB),
    Command("users", ("pm", "list", "users"), MIB),
    Command("wifi-networks", ("cmd", "wifi", "list-networks"), MIB),
    Command("wifi-status", ("cmd", "wifi", "status"), MIB),
    Command("recent-logs", ("logcat", "-b", "main", "-b", "system", "-b", "events", "-b", "crash", "-d", "-t", "10000", "-v", "epoch"), 16 * MIB, 60),
    Command("setting-auto-time", ("settings", "get", "global", "auto_time"), 65536),
    Command("setting-auto-time-zone", ("settings", "get", "global", "auto_time_zone"), 65536),
    Command("setting-airplane-mode", ("settings", "get", "global", "airplane_mode_on"), 65536),
    Command("setting-notification-history", ("settings", "get", "secure", "notification_history_enabled"), 65536),
    Command("setting-location-mode", ("settings", "get", "secure", "location_mode"), 65536),
    Command("setting-input-method", ("settings", "get", "secure", "default_input_method"), 65536),
)

LIMITATIONS = [
    "These are live logical snapshots, not a complete phone image or recovery of deleted storage. Empty output does not prove absence of data. Services, permissions, redaction, and formats vary by Android version and manufacturer.",
    "Notifications can contain private message previews. Android 11's standard dump exposes current/enqueued notifications and at most five archive summaries, not a complete persisted notification-history export. A notification does not prove a message was deleted. Existing history is never enabled or changed.",
    "Usage includes retained app activity and aggregate statistics, not message contents. AOSP Android 11 prunes daily/weekly/monthly/yearly intervals after roughly 10 days/4 weeks/6 months/3 years; actual available history can be shorter. No usage flush or check-in is requested.",
    "Account output requests only account types and counts through the account-specific read-only check-in path; it does not export passwords, tokens, or a full account session dump.",
    "Modern package commands select user 0. The compatibility fallback uses the device's default package-list scope and omits UID/version details. Retained uninstalled entries are not a complete uninstall history or proof that app data remains readable.",
    "The location-service snapshot may expose last-known fixes, provider state and existing request metadata. It never activates location or requests a new fix. It is not full travel history or proof of a person's presence at a location.",
    "User/profile output is an inventory only. It does not unlock, switch to or export protected records from other profiles; each profile requires its own authorized unlock and export where available.",
    "Wi-Fi output is saved network names/security types and current connection metadata, not passwords, a new scan, or historical location proof. No Bluetooth capture is attempted.",
    "Logs contain only up to 10,000 recent lines per selected logcat invocation from rolling buffers. Reboots and overwriting can remove older data. Settings are current values, not their change history.",
    "Host deadlines and byte caps bound every command. Truncated, timed-out, cancelled, denied, or unsupported captures are preserved as partials and are not listed as successful manifest copies.",
    "SHA-256 values verify saved PC bytes only; no independent phone-side checksum is claimed. Captures are not atomic across services, and Android may log these read-only requests normally.",
]

SOURCE_REFERENCES = [
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/notification/NotificationManagerService.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/usage/java/com/android/server/usage/UserUsageStatsService.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/usage/java/com/android/server/usage/UsageStatsDatabase.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/accounts/AccountManagerService.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/accounts/AccountsDb.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/pm/PackageManagerShellCommand.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/pm/UserManagerService.java",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/services/core/java/com/android/server/location/LocationManagerService.java",
    "https://android.googlesource.com/platform/frameworks/opt/net/wifi/+/android-11.0.0_r1/service/java/com/android/server/wifi/WifiShellCommand.java",
    "https://developer.android.com/tools/logcat",
]


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Device context capture cancelled; completed files and partials were preserved")


def _safe_path(path: Path):
    """Reject existing symlinks/junctions anywhere along a local output path."""
    path = Path(os.path.abspath(path))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise ValueError("Device context output path contains a link or reparse point")
    return path


def _new_file(path):
    _safe_path(path)
    return path.open("xb")


def _write_new(path, content):
    with _new_file(path) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def _publish(partial, target):
    _safe_path(partial)
    _safe_path(target)
    if os.name == "nt":
        os.rename(partial, target)  # Refuses replacement on Windows.
    else:
        os.link(partial, target)
        partial.unlink()


def _saved_metadata(path, root, limit):
    """Hash the actual closed file, including a short write or failed flush."""
    _safe_path(path)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            data = stream.read(min(65536, limit - total + 1))
            if not data:
                break
            total += len(data)
            if total > limit:
                raise OSError("Saved context output exceeds its byte limit")
            digest.update(data)
    return {"localPath": path.relative_to(root).as_posix(), "size": total, "sha256": digest.hexdigest()}


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def _failure_kind(stdout_prefix, stderr_prefix, exit_code):
    # Classify only common command-level failures. Never expose diagnostic text.
    data = (bytes(stderr_prefix) + b"\n" + bytes(stdout_prefix)).decode("utf-8", "replace")
    if re.search(r"(?im)^\s*(?:error:\s*)?(?:permission denial\b|permission denied\b|java\.lang\.SecurityException\b|security exception\b)", data):
        return "denied"
    if re.search(r"(?im)^\s*(?:error:\s*)?(?:unknown (?:command|option)\b|unrecognized option\b|can't find service\b|cannot find service\b|/system/bin/sh: .*: (?:not found|inaccessible)|.*: not found$)", data):
        return "unsupported"
    return "failed" if exit_code != 0 else "complete"


def _capture(adb, serial, spec, root, run, budget, cancel, receipt):
    """Concurrent bounded pipe draining. The coordinator owns receipts and files."""
    receipt.update(key=spec.key, args=list(spec.args), startedAt=_utc(), status="running",
                   exitCode=None, timeoutSeconds=spec.timeout, stdoutLimit=spec.limit,
                   stderrLimit=MAX_STDERR_BYTES, truncated=False, timedOut=False,
                   cancelled=False, hashScope="saved-PC-bytes-only")
    suffix = ".partial-" + uuid.uuid4().hex
    paths = {name: run / (spec.key + "." + name + suffix) for name in ("stdout", "stderr")}
    states = {name: {"size": 0, "hash": hashlib.sha256(), "prefix": bytearray(), "file": None}
              for name in paths}
    lock = threading.Lock()
    stop = threading.Event()
    failure = {"exception": None, "reason": None}
    process = None
    threads = []
    start = time.monotonic()
    fatal = None

    def reader(name, pipe):
        state = states[name]
        cap = spec.limit if name == "stdout" else MAX_STDERR_BYTES
        try:
            while not stop.is_set():
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                with lock:
                    if stop.is_set():
                        break
                    remaining = max(0, min(cap - state["size"], MAX_TOTAL_BYTES - budget["bytes"]))
                    data = chunk[:remaining]
                    if data:
                        written = state["file"].write(data)
                        if written != len(data):
                            raise OSError("Incomplete PC context output write")
                        state["hash"].update(data)
                        state["size"] += len(data)
                        budget["bytes"] += len(data)
                        state["prefix"].extend(data[:max(0, PREFIX_BYTES - len(state["prefix"]))])
                    if len(data) != len(chunk):
                        failure["reason"] = "total-byte-limit" if budget["bytes"] >= MAX_TOTAL_BYTES else name + "-byte-limit"
                        stop.set()
        except BaseException as exc:
            with lock:
                failure["exception"] = exc
                stop.set()

    try:
        _check(cancel)
        for name in paths:
            states[name]["file"] = _new_file(paths[name])
        process = subprocess.Popen(adb.command(serial, ["shell", " ".join(quote(arg) for arg in spec.args)]),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   **adb.process_options())
        for name in paths:
            thread = threading.Thread(target=reader, args=(name, getattr(process, name)),
                                      name="device-context-" + name, daemon=True)
            threads.append(thread)
            thread.start()
        while True:
            _check(cancel)
            if failure["exception"] is not None:
                raise failure["exception"]
            if stop.is_set():
                receipt.update(status="truncated", truncated=True, reason=failure["reason"])
                break
            if process.poll() is not None and not any(thread.is_alive() for thread in threads):
                receipt["status"] = _failure_kind(states["stdout"]["prefix"], states["stderr"]["prefix"], process.returncode)
                break
            if time.monotonic() - start >= spec.timeout:
                receipt.update(status="timed-out", timedOut=True, reason="host-deadline")
                break
            if cancel is None:
                time.sleep(.025)
            else:
                cancel.wait(.025)
    except Cancelled as exc:
        receipt.update(status="cancelled", cancelled=True)
        fatal = exc
    except OSError as exc:
        # Host I/O failure must stop the run, including ENOSPC/EDQUOT/Windows 112.
        receipt.update(status="failed", reason="host-io-error", hostErrno=exc.errno,
                       hostWinError=getattr(exc, "winerror", None))
        fatal = exc
    except Exception as exc:
        receipt.update(status="failed", reason="capture-error", errorType=type(exc).__name__)
        fatal = exc
    finally:
        stop.set()
        if process is not None:
            try:
                _stop(process)
            except (OSError, subprocess.TimeoutExpired):
                receipt.update(status="failed", reason="process-stop-failed")
            for thread in threads:
                thread.join(timeout=1)
            receipt["exitCode"] = process.poll()
            if any(thread.is_alive() for thread in threads):
                receipt.update(status="failed", reason="pipe-drain-incomplete")
            for name, thread in zip(paths, threads):
                if not thread.is_alive():
                    getattr(process, name).close()
        # A late reader cannot write after this lock and stop flag, even if an
        # abnormal child retained a pipe handle. Do not block closing that pipe.
        with lock:
            if failure["exception"] is not None and fatal is None:
                fatal = failure["exception"]
                receipt.update(status="failed", reason="host-io-error")
            for name, state in states.items():
                stream = state["file"]
                if stream is None:
                    continue
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError as exc:
                    fatal = fatal or exc
                    receipt.update(status="failed", reason="host-io-error")
                finally:
                    try:
                        stream.close()
                    except OSError as exc:
                        fatal = fatal or exc
                        receipt.update(status="failed", reason="host-io-error")
                try:
                    saved = _saved_metadata(paths[name], root, spec.limit if name == "stdout" else MAX_STDERR_BYTES)
                    receipt[name] = saved
                    if receipt["status"] == "complete" and (saved["size"] != state["size"] or saved["sha256"] != state["hash"].hexdigest()):
                        fatal = fatal or OSError("Saved context bytes do not match the captured stream")
                        receipt.update(status="failed", reason="saved-bytes-mismatch")
                except (OSError, ValueError) as exc:
                    fatal = fatal or exc
                    receipt[name] = {"localPath": paths[name].relative_to(root).as_posix(),
                                     "size": None, "sha256": None, "hashError": "Saved bytes could not be verified"}
                    receipt.update(status="failed", reason="saved-bytes-verification-failed")
        receipt["finishedAt"] = _utc()
        receipt["durationSeconds"] = round(time.monotonic() - start, 3)

    if receipt["status"] == "complete":
        try:
            for name in paths:
                target = run / (spec.key + "." + name + ".txt")
                _publish(paths[name], target)
                receipt[name]["localPath"] = target.relative_to(root).as_posix()
        except Exception:
            receipt.update(status="failed", reason="output-publish-error")
            raise
    if fatal is not None:
        raise fatal


def _html(result, base):
    run = Path(result["folder"])

    def link(path, label):
        # Paths originate exclusively from fixed keys and this unique run.
        target = Path(result["archiveRoot"]) / path
        if run not in target.parents:
            raise ValueError("Device context report link escaped its run")
        relative = os.path.relpath(target, base).replace("\\", "/")
        return '<a href="' + escape(url_quote(relative, safe="/"), quote=True) + '">' + escape(label) + '</a>'

    counts = result["counts"]
    parts = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">',
        '<title>Device context</title><style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#19212d}table{border-collapse:collapse;width:100%;overflow-wrap:anywhere}th,td{padding:10px;text-align:left;vertical-align:top;border-bottom:1px solid #ccd3dd}li{margin:10px 0}a{color:#1459b2}</style>',
        '<h1>Device context</h1><p>Status: <strong>' + escape(result["status"]) + '</strong></p>',
        '<p>Started: ' + escape(result["checkedAt"]) + '<br>Finished: ' + escape(result["finishedAt"]) + '</p>',
        '<p>' + str(counts["commandsSucceeded"]) + ' complete commands; ' + str(counts["filesCopied"]) + ' successful output files; ' + str(counts["bytesSaved"]) + ' captured bytes including partials and diagnostics; ' + str(counts["issues"]) + ' command gaps.</p>',
        '<p>' + link((run / "report.json").relative_to(Path(result["archiveRoot"])).as_posix(), "Full JSON capture receipt") + '</p>',
        '<h2>Coverage and limits</h2><ul>',
    ]
    parts.extend('<li>' + escape(item) + '</li>' for item in result["limitations"])
    parts.append('</ul><h2>Captures</h2><p>Raw files may contain private data. Failed or bounded captures retain partials; they are not verified complete snapshots.</p><table><thead><tr><th>Command</th><th>Status</th><th>Output</th><th>Diagnostics</th></tr></thead><tbody>')
    for item in result["commands"]:
        links = []
        for name in ("stdout", "stderr"):
            output = item.get(name)
            links.append(link(output["localPath"], str(output["size"]) + " bytes" + (" (partial)" if item["status"] != "complete" else "")) if output else "No file")
        parts.append('<tr><td>' + escape(item["key"]) + '</td><td>' + escape(item["status"]) + '</td><td>' + links[0] + '</td><td>' + links[1] + '</td></tr>')
    parts.append('</tbody></table></html>')
    return "\n".join(parts).encode("utf-8")


def _receipt(result):
    run = Path(result["folder"])
    _write_new(run / "report.json", _json_bytes(result))
    _write_new(run / "report.html", _html(result, run))
    stable = Path(result["reportPath"])
    temp = stable.with_name("report.html.partial-" + uuid.uuid4().hex)
    _write_new(temp, _html(result, run.parent))
    _safe_path(stable)
    os.replace(temp, stable)  # Only this convenience page is updated.


def _compact(result):
    summary = {key: result[key] for key in ("schemaVersion", "status", "checkedAt", "finishedAt", "counts", "reportPath", "folder", "limitations", "records")}
    summary["receiptSaved"] = result.get("receiptSaved", False)
    if result.get("receiptErrorType"):
        summary["receiptErrorType"] = result["receiptErrorType"]
    return summary


def _capture_with_receipt(adb, serial, spec, root, run, budget, cancel, item):
    failure = None
    try:
        _capture(adb, serial, spec, root, run, budget, cancel, item)
    except BaseException as exc:
        failure = exc
    try:
        _write_new(run / (spec.key + ".receipt.json"), _json_bytes(item))
    except Exception as exc:
        item["receiptErrorType"] = type(exc).__name__
        failure = failure or exc
    if failure is not None:
        raise failure


def capture_context(root: Path, adb, serial, cancel=None, progress=None):
    """Capture the fixed allowlist, returning compact metadata and manifest rows."""
    root = _safe_path(Path(root))
    if not root.is_dir():
        raise ValueError("An existing PC archive directory is required")
    base = _safe_path(root / "device-context")
    base.mkdir(exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    run = _safe_path(base / run_id)
    run.mkdir()  # Never reuse a run or overwrite an acquisition.
    budget = {"bytes": 0}
    result = {"schemaVersion": 1, "status": "running", "checkedAt": _utc(), "finishedAt": None,
              "folder": str(run), "archiveRoot": str(root), "reportPath": str(base / "report.html"),
              "totalByteLimit": MAX_TOTAL_BYTES, "limitations": list(LIMITATIONS),
              "sourceReferences": SOURCE_REFERENCES, "commands": [], "records": [], "counts": {}}
    plan = {"startedAt": result["checkedAt"], "totalByteLimit": MAX_TOTAL_BYTES,
            "commands": [{"key": spec.key, "args": spec.args, "fallback": spec.fallback,
                          "stdoutLimit": spec.limit, "stderrLimit": MAX_STDERR_BYTES,
                          "timeoutSeconds": spec.timeout} for spec in COMMANDS]}
    failure = None
    try:
        _write_new(run / "plan.json", _json_bytes(plan))
        for spec in COMMANDS:
            _check(cancel)
            if progress is not None:
                progress("device-context", "Capturing " + spec.key)
            item = {"key": spec.key, "args": list(spec.args), "status": "not-started"}
            result["commands"].append(item)
            if budget["bytes"] >= MAX_TOTAL_BYTES:
                item.update(status="skipped", reason="total-byte-limit", startedAt=None, finishedAt=_utc())
                continue
            _capture_with_receipt(adb, serial, spec, root, run, budget, cancel, item)
            if item["status"] == "unsupported" and spec.fallback:
                fallback = Command(spec.key + "-compat", spec.fallback, spec.limit, spec.timeout)
                item = {"key": fallback.key, "args": list(fallback.args), "status": "not-started",
                        "compatibilityFallbackFor": spec.key, "reducedMetadata": True}
                result["commands"].append(item)
                if budget["bytes"] >= MAX_TOTAL_BYTES:
                    item.update(status="skipped", reason="total-byte-limit", startedAt=None, finishedAt=_utc())
                else:
                    _capture_with_receipt(adb, serial, fallback, root, run, budget, cancel, item)
        _check(cancel)
    except BaseException as exc:
        failure = exc
    finally:
        for item in result["commands"]:
            if item["status"] == "complete":
                for name in ("stdout", "stderr"):
                    saved = item[name]
                    if name == "stderr" and saved["size"] == 0:
                        continue
                    result["records"].append({"source": "device-context:" + run_id + "/" + item["key"] + (".stderr" if name == "stderr" else ""),
                                              **saved, "status": "copied", "category": "device-context",
                                              "acquiredAt": item["finishedAt"], "hashScope": "saved-PC-bytes-only"})
        completed = sum(item["status"] == "complete" for item in result["commands"])
        issues = sum(item["status"] != "complete" for item in result["commands"])
        missing = max(0, len(COMMANDS) - sum(not item.get("compatibilityFallbackFor") for item in result["commands"]))
        unattempted = missing + sum(item["status"] in ("skipped", "not-started") for item in result["commands"])
        saved_bytes = sum(item.get(name, {}).get("size") or 0 for item in result["commands"] for name in ("stdout", "stderr"))
        result["counts"] = {"commandsPlanned": len(COMMANDS), "commandsAttempted": sum(item["status"] not in ("skipped", "not-started") for item in result["commands"]),
                            "commandsSucceeded": completed, "commandsNotAttempted": unattempted,
                            "filesCopied": len(result["records"]), "bytesCopied": sum(item["size"] for item in result["records"]),
                            "bytesSaved": saved_bytes, "issues": issues + missing}
        result["status"] = ("cancelled" if isinstance(failure, Cancelled) else "failed" if failure else
                            "complete" if not issues else "partial" if completed else "unavailable")
        result["finishedAt"] = _utc()
        try:
            _receipt(result)
            result["receiptSaved"] = True
        except Exception as exc:
            # A full disk may also prevent a final receipt; preserve the original
            # failure and all existing outputs instead of retrying more commands.
            failure = failure or exc
            result.update(status="failed", receiptSaved=False, receiptErrorType=type(exc).__name__, reportPath=None)
    if failure is not None:
        failure.result = _compact(result)
        raise failure
    return _compact(result)
