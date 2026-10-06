"""Read-only MediaStore retained-trash acquisition for authorized Android 11+.

Never updates flags, restores items, installs software, or accesses raw storage.
Verified existing PC copies are preferred over reading the media bytes again.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from urllib.parse import quote as url_quote

from adb import AdbError, Cancelled, quote

BASE_URI = "content://media/external/file"
PROJECTION = "_id:is_trashed:date_expires:_size:date_modified"
MATCH_TRASHED = r"android\:query-arg-match-trashed:i:3"
MAX_METADATA = 16 * 1024 * 1024
MAX_ERROR = 64 * 1024
ROW = re.compile(r"Row: \d+ _id=(\d+), is_trashed=(\d+), date_expires=(NULL|-?\d+), _size=(NULL|\d+), date_modified=(NULL|-?\d+)")
LIMITATIONS = [
    "Only retained items exposed to the authorized ADB shell by Android MediaStore are checked. This is not recovery of permanently deleted blocks.",
    "Private app recycle bins, encrypted storage, other profiles, cloud-only trash, and items already permanently removed can remain unavailable.",
    "A zero-row result means no trashed rows were returned to this caller at the check time; it does not prove there is no deleted data elsewhere.",
    "Hashes cover the saved bytes. PC reuse is checked against the existing acquisition manifest; direct phone streams are checked by size and stable MediaStore identity/metadata, not an independent phone-side checksum.",
    "This read-only check never restores items, deletes files, or changes trash flags. Android or other applications can independently expire or change trash during acquisition.",
]
SOURCE_REFERENCES = [
    "https://developer.android.com/reference/android/provider/MediaStore#QUERY_ARG_MATCH_TRASHED",
    "https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/android-11.0.0_r1/cmds/content/src/com/android/commands/content/Content.java",
]


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Retained-trash check cancelled; existing copies and partials were preserved")


def _notify(progress, message):
    if progress is not None:
        progress("phone-trash", message)


def _out_of_space(exc):
    return isinstance(exc, OSError) and (
        exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
        or getattr(exc, "winerror", None) in (39, 112)
    )


def _inside(root, relative):
    base = Path(root).resolve()
    name = Path(relative)
    if name.is_absolute() or name.drive or ".." in name.parts:
        raise ValueError("Unsafe PC archive path")
    target = (base / name).resolve()
    if base not in target.parents:
        raise ValueError("PC archive path escapes its root")
    return target


def _atomic_json(path, value):
    temp = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Only this new run's generated report is replaced; original acquisitions are untouched.
    os.replace(temp, path)


def _atomic_text(path, value):
    temp = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
    with temp.open("x", encoding="utf-8", errors="xmlcharrefreplace") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _html_report(root, result, base):
    run = Path(result["folder"])

    def link(path, label):
        # All links refer to this run's generated files, never a source URI or phone path.
        path = Path(path).resolve()
        if run not in path.parents:
            raise ValueError("Report link escapes the retained-trash run")
        relative = Path(os.path.relpath(path, base)).as_posix()
        return '<a href="' + escape(url_quote(relative, safe="/"), quote=True) + '">' + escape(str(label)) + '</a>'

    counts = result["counts"]
    parts = [
        '<!doctype html><html lang="en"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">',
        '<title>Retained phone trash</title><style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#19212d}table{border-collapse:collapse;width:100%;overflow-wrap:anywhere}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ccd3dd;padding:10px}li{margin:10px 0}a{color:#1459b2}</style>',
        '<h1>Retained phone trash</h1><p>Status: <strong>' + escape(result["status"]) + '</strong></p>',
        '<p>Checked: ' + escape(result["checkedAt"]) + '<br>Finished: ' + escape(result["finishedAt"]) + '</p>',
        '<p>' + str(counts["rows"]) + ' retained rows; ' + str(counts["filesCopied"]) + ' files copied; ' + str(counts["bytesCopied"]) + ' bytes copied; ' + str(counts["issues"]) + ' issues.</p>',
        '<p>' + link(run / "report.json", "Full JSON report with source provenance and hashes") + '</p>',
        '<h2>Coverage and limits</h2><ul>',
    ]
    parts.extend('<li>' + escape(text) + '</li>' for text in result["limitations"])
    parts.append('</ul><h2>Items</h2><p>Saved media use neutral MediaStore ID filenames and a safe original extension when available. Original paths and acquisition details are preserved in the JSON report. Partial files are not verified complete copies.</p><table><thead><tr><th>MediaStore ID</th><th>Status</th><th>Saved copy</th><th>Bytes</th><th>Detail</th></tr></thead><tbody>')
    for item in result["items"]:
        saved = item.get("file") or item.get("partialFile")
        saved_link = link(_inside(root, saved), "Verified copy" if item.get("status") == "copied" else "Unverified partial") if saved else "None"
        parts.append('<tr><td>' + escape(str(item["id"])) + '</td><td>' + escape(item["status"]) + '</td><td>' + saved_link + '</td><td>' + escape(str(item.get("bytes", item.get("size", "Unknown")))) + '</td><td>' + escape(str(item.get("error", item.get("method", "")))) + '</td></tr>')
    parts.append('</tbody></table>')
    if result["issues"]:
        parts.append('<h2>Issues</h2><ul>')
        parts.extend('<li>' + escape(str(issue.get("error", "Unknown issue"))) + '</li>' for issue in result["issues"])
        parts.append('</ul>')
    parts.append('</html>')
    return "\n".join(parts)


def _receipt(root, result):
    run = Path(result["folder"])
    _atomic_json(run / "report.json", result)
    _atomic_text(run / "report.html", _html_report(root, result, run))
    # Replace only the convenience page; unique run reports and copies are retained.
    _atomic_text(Path(result["reportPath"]), _html_report(root, result, run.parent))
    return {key: result[key] for key in ("schemaVersion", "status", "checkedAt", "finishedAt", "counts", "reportPath", "folder", "limitations")}


def _publish(partial, target):
    if os.name == "nt":
        os.rename(partial, target)  # Refuses replacement on Windows.
    else:
        os.link(partial, target)
        partial.unlink()


def _hash(path, cancel):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_stream(adb, serial, arguments, output, limit, cancel, *, timeout=60, idle_timeout=30):
    """Pump bytes with a bound enforced before each write, plus cancellation."""
    _check(cancel)
    if not isinstance(limit, int) or limit < 0:
        raise ValueError("Invalid output size bound")
    process = subprocess.Popen(adb.command(serial, arguments), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **adb.process_options())
    state = {"bytes": 0, "last": time.monotonic(), "error": None}
    digest = hashlib.sha256()
    errors = bytearray()

    def copy_stdout():
        try:
            while True:
                chunk = process.stdout.read1(65536)
                if not chunk:
                    break
                _check(cancel)
                if state["bytes"] + len(chunk) > limit:
                    raise AdbError("Phone output exceeded its checked byte limit; partial preserved")
                written = output.write(chunk)
                if written != len(chunk):
                    raise OSError("Incomplete PC output write")
                digest.update(chunk)
                state["bytes"] += len(chunk)
                state["last"] = time.monotonic()
        except BaseException as exc:
            state["error"] = exc

    def copy_stderr():
        try:
            while True:
                chunk = process.stderr.read1(4096)
                if not chunk:
                    break
                if len(errors) < MAX_ERROR:
                    errors.extend(chunk[:MAX_ERROR - len(errors)])
        except (OSError, ValueError):
            pass

    output_thread = threading.Thread(target=copy_stdout, name="trash-read", daemon=True)
    error_thread = threading.Thread(target=copy_stderr, name="trash-stderr", daemon=True)
    started = time.monotonic()
    output_thread.start()
    error_thread.start()
    try:
        while process.poll() is None or output_thread.is_alive():
            _check(cancel)
            if state["error"] is not None:
                raise state["error"]
            now = time.monotonic()
            if now - state["last"] > idle_timeout or now - started > timeout:
                raise AdbError("Read-only phone query/stream timed out; partial preserved")
            if cancel is None:
                time.sleep(.05)
            else:
                cancel.wait(.05)
        output_thread.join(timeout=5)
        error_thread.join(timeout=5)
        if state["error"] is not None:
            raise state["error"]
        if process.returncode != 0 or errors:
            # Device messages can contain private filenames; retain only bounded detail in the report.
            detail = errors.decode("utf-8", "replace").strip()
            raise AdbError("Read-only phone command failed" + (": " + detail[:1000] if detail else " (" + str(process.returncode) + ")"))
        return state["bytes"], digest.hexdigest()
    finally:
        _terminate(process)
        output_thread.join(timeout=5)
        error_thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()


def _capture(adb, serial, args, cancel, limit=MAX_METADATA):
    output = io.BytesIO()
    _run_stream(adb, serial, ["shell", " ".join(quote(arg) for arg in args)], output,
                limit, cancel, timeout=90, idle_timeout=30)
    return output.getvalue().decode("utf-8", "surrogateescape")


def _query(adb, serial, cancel, item_id=None):
    if item_id is not None and not re.fullmatch(r"[0-9]{1,19}", str(item_id)):
        raise ValueError("Invalid MediaStore item identifier")
    uri = BASE_URI if item_id is None else BASE_URI + "/" + str(item_id)
    args = ["content", "query", "--uri", uri, "--projection", PROJECTION]
    if item_id is None:
        args += ["--extra", MATCH_TRASHED]
    text = _capture(adb, serial, args, cancel)
    if text.strip() == "No result found.":
        return []
    rows = []
    seen = set()
    for line in text.splitlines():
        match = ROW.fullmatch(line.strip())
        if not match:
            raise AdbError("MediaStore trash query was denied, unsupported, or returned unrecognized output")
        identifier, trashed, expires, size, modified = match.groups()
        if int(identifier) > (2 ** 63 - 1) or identifier in seen:
            raise AdbError("MediaStore returned an invalid or duplicate item ID")
        seen.add(identifier)
        if item_id is not None and identifier != str(item_id):
            raise AdbError("MediaStore returned a different item than requested")
        row = {"id": identifier, "uri": BASE_URI + "/" + identifier,
               "isTrashed": int(trashed), "dateExpires": None if expires == "NULL" else int(expires),
               "size": None if size == "NULL" else int(size),
               "dateModified": None if modified == "NULL" else int(modified)}
        if item_id is None and row["isTrashed"] != 1:
            raise AdbError("Provider did not honor the trashed-only query; no ordinary media will be acquired")
        rows.append(row)
    if not rows and text.strip() != "No result found.":
        raise AdbError("MediaStore returned empty/unrecognized query output")
    return rows


def _source_path(adb, serial, item, cancel):
    """Optional exact original path, used only to match existing manifest records."""
    text = _capture(adb, serial, ["content", "query", "--uri", item["uri"],
                                  "--projection", "_data"], cancel, limit=1024 * 1024)
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    prefix = "Row: 0 _data="
    if not text.startswith(prefix):
        return None
    value = text[len(prefix):]
    if not value.startswith("/") or "\x00" in value or any(p in (".", "..") for p in value.split("/")):
        return None
    return value


def _manifest(root, cancel):
    records = {}
    path = root / "manifest.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                _check(cancel)
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("status") == "copied" and isinstance(record.get("source"), str):
                        records[record["source"]] = record
                except (ValueError, TypeError):
                    continue
    return records


def _pc_source(root, record, size, cancel):
    if not record or record.get("size") != size:
        return None
    try:
        checksum = record.get("sha256", "")
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
            return None
        path = _inside(root, record["localPath"])
        if path.is_file() and path.stat().st_size == size and _hash(path, cancel) == checksum.lower():
            return path, checksum.lower()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _copy_pc(source, output, expected, cancel):
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as stream:
        while True:
            _check(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            if copied + len(chunk) > expected:
                raise AdbError("Previously acquired PC file grew while copying")
            if output.write(chunk) != len(chunk):
                raise OSError("Incomplete PC output write")
            digest.update(chunk)
            copied += len(chunk)
    return copied, digest.hexdigest()


def _extension(original):
    match = re.search(r"\.([A-Za-z0-9]{1,10})$", original.rsplit("/", 1)[-1]) if original else None
    return "." + match.group(1).lower() if match else ".bin"


def acquire_trash(root, adb, serial, cancel=None, progress=None):
    """Copy retained trash into a new PC-only run; return an aggregate/report receipt.

    ``adb`` is the existing adb.Adb instance. ``progress`` receives (stage, safe text).
    Returns unavailable on unsupported/denied queries, partial on item failures,
    and writes a receipt before raising Cancelled when requested. Disk-full errors
    stop acquisition and propagate; a receipt is attempted if storage permits.
    The returned summary excludes item/issue rows. No phone write occurs.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Choose an existing PC acquisition folder")
    if not isinstance(serial, str) or not serial or "\x00" in serial:
        raise ValueError("Choose the authorized phone")
    parent = _inside(root, "deleted-items/phone-trash")
    parent.mkdir(parents=True, exist_ok=True)
    parent = _inside(root, "deleted-items/phone-trash")
    run = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12])
    run.mkdir()
    result = {"schemaVersion": 1, "status": "running", "checkedAt": _utc(),
              "counts": {"rows": 0, "filesCopied": 0, "bytesCopied": 0, "issues": 0},
              "reportPath": str(parent / "report.html"), "folder": str(run), "limitations": list(LIMITATIONS),
              "sourceReferences": list(SOURCE_REFERENCES),
              "source": {"provider": BASE_URI, "selection": "IS_TRASHED=1 via QUERY_ARG_MATCH_TRASHED=MATCH_ONLY", "deviceSerial": serial},
              "items": [], "issues": []}
    _atomic_json(run / "started.json", {"status": "started", "checkedAt": result["checkedAt"]})
    abort = None
    try:
        _check(cancel)
        devices = adb.devices()
        if not any(d.get("serial") == serial and d.get("state") == "device" for d in devices):
            raise AdbError("Selected phone is unavailable or USB debugging is unauthorized")
        _notify(progress, "Checking retained MediaStore trash without changing the phone")
        rows = _query(adb, serial, cancel)
        result["counts"]["rows"] = len(rows)
        records = _manifest(root, cancel)
        for index, item in enumerate(rows, 1):
            _check(cancel)
            entry = dict(item)
            entry["status"] = "pending"
            result["items"].append(entry)
            _notify(progress, "Checking retained trash item " + str(index) + " of " + str(len(rows)))
            try:
                if item["size"] is None:
                    raise AdbError("MediaStore did not report a byte size; bounded acquisition unavailable")
                current = _query(adb, serial, cancel, item["id"])
                if current != [item] or item["isTrashed"] != 1:
                    raise AdbError("MediaStore item changed or disappeared before acquisition")
                try:
                    original = _source_path(adb, serial, item, cancel)
                except Cancelled:
                    raise
                except AdbError:
                    original = None
                if original is not None:
                    entry["originalPhonePath"] = original
                prior = records.get(original) if original else None
                source = _pc_source(root, prior, item["size"], cancel)
                target = run / ("media-" + item["id"] + _extension(original))
                partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex[:12])
                entry["partialFile"] = str(partial.relative_to(root))
                with partial.open("xb") as output:
                    if source:
                        copied, checksum = _copy_pc(source[0], output, item["size"], cancel)
                        if checksum != source[1]:
                            raise AdbError("Previously acquired PC file changed while copying")
                        entry["method"] = "verified-existing-PC-copy"
                        entry["originalAcquisition"] = {"localPath": prior["localPath"], "sha256": source[1], "acquiredAt": prior.get("acquiredAt")}
                    else:
                        command = "content read --uri " + quote(item["uri"])
                        copied, checksum = _run_stream(adb, serial, ["exec-out", command], output,
                                                       item["size"], cancel, timeout=24 * 60 * 60, idle_timeout=45)
                        entry["method"] = "read-only-content-provider-stream"
                    output.flush()
                    os.fsync(output.fileno())
                if copied != item["size"]:
                    raise AdbError("Captured size does not match the MediaStore item; partial retained")
                if _query(adb, serial, cancel, item["id"]) != [item]:
                    raise AdbError("MediaStore identity, trash state or metadata changed during acquisition; partial retained")
                if partial.stat().st_size != copied or _hash(partial, cancel) != checksum:
                    raise AdbError("Written PC file failed SHA-256 or size verification; partial retained")
                _publish(partial, target)
                entry.update(status="copied", file=str(target.relative_to(root)), bytes=copied, sha256=checksum)
                entry.pop("partialFile", None)
                result["counts"]["filesCopied"] += 1
                result["counts"]["bytesCopied"] += copied
            except Cancelled:
                entry["status"] = "cancelled"
                raise
            except (AdbError, OSError, ValueError, KeyError, TypeError) as exc:
                entry.update(status="failed", error=str(exc))
                result["issues"].append({"id": item["id"], "error": str(exc)})
                if _out_of_space(exc):
                    raise
        result["status"] = "partial" if result["issues"] else "complete"
    except Cancelled as exc:
        result["status"] = "cancelled"
        result["issues"].append({"error": str(exc)})
        abort = exc
    except (AdbError, OSError, ValueError, TypeError) as exc:
        result["status"] = "partial" if result["counts"]["rows"] else "unavailable"
        if not result["issues"] or result["issues"][-1]["error"] != str(exc):
            result["issues"].append({"error": str(exc)})
        if _out_of_space(exc):
            abort = exc
    result["counts"]["issues"] = len(result["issues"])
    result["finishedAt"] = _utc()
    try:
        summary = _receipt(root, result)
    except OSError:
        if abort is not None and _out_of_space(abort):
            raise abort
        raise
    if abort is not None:
        raise abort
    return summary
