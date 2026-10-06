"""Conservative offline checks for trash candidates in an Android Bay archive.

This never opens a phone connection. Original acquisitions are read, verified, and
left untouched. A candidate is not proof of prior deletion or raw recovery.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
import uuid
from urllib.parse import quote

from adb import Cancelled

MAX_MANIFEST_LINE = 1024 * 1024
MAX_MESSAGE_LINE = 8 * 1024 * 1024
CHUNK = 1024 * 1024
TRASH_DIRECTORIES = frozenset({".trash", ".trashes", ".trashbin", ".recyclebin", "recyclebin", "recycle_bin", "$recycle.bin"})
DELETION_FLAGS = frozenset({"deleted", "is_deleted", "isdeleted", "trashed", "is_trashed", "istrashed"})
LIMITATIONS = [
    "This checks only files already acquired on the PC. It does not read unallocated phone storage or recover permanently deleted data.",
    "Trash/recycle paths and explicit provider flags identify candidates, not proof that an item was deleted or can be restored.",
    "Android normally removes deleted SMS/MMS provider rows. Absence of flagged rows does not show that no messages were deleted.",
    "Private app trash, encrypted databases, RCS, cloud trash and overwritten files may be unavailable. App-specific trash may require an export from that app.",
    "Message detection uses explicit deleted/trashed flags only. Mailbox type, read, seen, archived, locked and deletable fields are not treated as deletion evidence.",
    "Raw private-app, root and legacy backup archives are preserved but not unpacked or searched internally by this check.",
    "Rows from multiple helper exports keep their original provenance and may represent the same message more than once; counts are not unique deletion events.",
    "Original PC files must match their acquisition size and SHA-256 before candidates are copied or message rows are examined. This checks the recorded acquisition, not completeness of the phone.",
]


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Deleted-items check cancelled; original acquisitions remain untouched")


def _plain(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Symbolic links and reparse points are not followed")
    return info


def _path(root, relative, *, create_parent=False):
    if not isinstance(relative, str) or not relative or any(ord(c) < 32 for c in relative):
        raise ValueError("Invalid archive-relative path")
    if PureWindowsPath(relative).drive or PureWindowsPath(relative).is_absolute() or relative.startswith(("/", "\\")):
        raise ValueError("Absolute archive paths are not allowed")
    parts = relative.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        raise ValueError("Unsafe archive path component")
    current = root
    for number, part in enumerate(parts):
        current = current / part
        exists = os.path.lexists(current)
        if exists:
            info = _plain(current)
            if number < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise ValueError("Archive parent is not a directory")
        elif create_parent and number < len(parts) - 1:
            current.mkdir()
            _plain(current)
    if root != current.resolve() and root not in current.resolve().parents:
        raise ValueError("Archive path escapes its root")
    return current


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@contextmanager
def _open_original(root, relative):
    path = _path(root, relative)
    before = _plain(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Acquisition is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _signature(opened) != _signature(before):
            raise ValueError("Acquisition changed while opening")
        _path(root, relative)
        yield stream, opened
        if _signature(os.fstat(stream.fileno())) != _signature(opened):
            raise ValueError("Acquisition changed during the check")


def _hash(stream, expected_size, cancel):
    digest, size = hashlib.sha256(), 0
    while True:
        _check(cancel)
        chunk = stream.read(min(CHUNK, max(1, expected_size - size + 1)))
        if not chunk:
            break
        size += len(chunk)
        if size > expected_size:
            raise ValueError("Acquisition exceeds its recorded size")
        digest.update(chunk)
    if size != expected_size:
        raise ValueError("Acquisition size differs from its manifest")
    return digest.hexdigest()


def _verified(root, record, cancel):
    size, digest = record.get("size"), record.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise ValueError("Acquisition has no valid size and SHA-256")
    with _open_original(root, record.get("localPath")) as (stream, info):
        if info.st_size != size or _hash(stream, size, cancel) != digest.lower():
            raise ValueError("Original PC acquisition failed SHA-256 verification")
    return size, digest.lower()


def trash_reason(source):
    """Recognize explicit path markers; never search prose for 'deleted'."""
    if not isinstance(source, str) or not source.startswith("/") or "\x00" in source:
        return None
    parts = source.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts):
        return None
    for part in parts[:-1]:
        folded = part.casefold()
        if folded in TRASH_DIRECTORIES or re.fullmatch(r"\.trash-\d+", folded):
            return "Located beneath recognized trash/recycle directory: " + part
    if parts and re.fullmatch(r"\.trashed-\d+-.+", parts[-1], flags=re.IGNORECASE):
        return "Filename matches Android MediaStore .trashed-<expiration>-<name> convention"
    return None


def _message_kind(source):
    if not isinstance(source, str) or not source.startswith("/"):
        return None
    parts = source.split("/")
    if any(p in (".", "..") for p in parts):
        return None
    if len(parts) >= 5 and parts[-4:-2] == ["AndroidRescue", "exports"] and parts[-2] and parts[-1] in ("sms.jsonl", "mms.jsonl"):
        return parts[-1][:-6]
    return None


def _truthy(value):
    return value is True or (type(value) in (int, float) and value == 1) or (isinstance(value, str) and value.strip().casefold() in {"true", "1"})


def explicit_flags(row):
    values = row.get("values") if isinstance(row, dict) else None
    if not isinstance(values, dict):
        return []
    return [{"field": key, "value": value} for key, value in values.items()
            if key.casefold() in DELETION_FLAGS and _truthy(value)]


def _publish_new(partial, target):
    if os.name == "nt":
        os.rename(partial, target)
    else:
        os.link(partial, target)
        partial.unlink()


def _copy_candidate(root, record, key, size, digest, cancel):
    suffix = PurePosixPath(record["source"]).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ".bin"
    relative = "deleted-items/files/" + key[:2] + "/" + key[:32] + suffix.lower()
    target = _path(root, relative, create_parent=True)
    if target.exists():
        _verified(root, {"localPath": relative, "size": size, "sha256": digest}, cancel)
        return relative
    partial = target.with_name(target.name + ".partial-" + uuid.uuid4().hex)
    # A failed or interrupted new candidate remains a partial; no old file is replaced.
    with _open_original(root, record["localPath"]) as (source, _), partial.open("xb") as output:
        copied, computed = 0, hashlib.sha256()
        while True:
            _check(cancel)
            chunk = source.read(CHUNK)
            if not chunk:
                break
            if copied + len(chunk) > size:
                raise ValueError("Acquisition grew while copying candidate")
            output.write(chunk)
            computed.update(chunk)
            copied += len(chunk)
        output.flush()
        os.fsync(output.fileno())
        if copied != size or computed.hexdigest() != digest:
            raise ValueError("Candidate copy differs from verified acquisition; partial retained")
    _check(cancel)
    try:
        _publish_new(partial, target)
    except FileExistsError:
        _verified(root, {"localPath": relative, "size": size, "sha256": digest}, cancel)
        partial.unlink()  # Only this invocation's duplicate temporary output.
    _verified(root, {"localPath": relative, "size": size, "sha256": digest}, cancel)
    return relative


def _line(stream, bound, cancel):
    _check(cancel)
    value = stream.readline(bound + 1)
    if len(value) > bound:
        raise ValueError("JSONL row exceeds the bounded parsing limit")
    return value


def _json_line(stream, value):
    stream.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")


@contextmanager
def _lock(folder):
    file = folder / ".scan.lock"
    if os.path.lexists(file):
        _plain(file)
    with file.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _file_digest(root, relative, cancel):
    with _open_original(root, relative) as (stream, info):
        return info.st_size, _hash(stream, info.st_size, cancel)


def _preserve_previous(root, temporary, cancel):
    previous = {}
    changed = False
    for name in ("index.jsonl", "messages.jsonl"):
        relative = "deleted-items/" + name
        old = _path(root, relative)
        if not old.exists():
            continue
        size, digest = _file_digest(root, relative, cancel)
        previous[name] = (size, digest)
        new_relative = str(temporary[name].relative_to(root))
        changed = changed or _file_digest(root, new_relative, cancel) != (size, digest)
    if not previous or not changed:
        return None
    key = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()[:24]
    parent = "deleted-items/previous/" + key
    for name, (size, digest) in previous.items():
        relative = parent + "/" + name
        target = _path(root, relative, create_parent=True)
        if target.exists():
            _verified(root, {"localPath": relative, "size": size, "sha256": digest}, cancel)
            continue
        part = target.with_name(target.name + ".partial-" + uuid.uuid4().hex)
        with _open_original(root, "deleted-items/" + name) as (source, _), part.open("xb") as output:
            computed, copied = hashlib.sha256(), 0
            while True:
                _check(cancel)
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                if copied + len(chunk) > size:
                    raise ValueError("Previous findings changed while preserving their history")
                output.write(chunk)
                copied += len(chunk)
                computed.update(chunk)
            output.flush()
            os.fsync(output.fileno())
            if copied != size or computed.hexdigest() != digest:
                raise ValueError("Previous findings changed while preserving their history")
        _publish_new(part, target)
    return str(root / parent)


def _write_report(path, summary, index_path, cancel):
    with path.open("x", encoding="utf-8") as output:
        output.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Deleted-items candidate check</title><style>body{font:16px/1.65 Segoe UI,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#243b36}h1{line-height:1.2}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}a{color:#087f79}.note{padding:18px;background:#fff5df;border-radius:8px}code{overflow-wrap:anywhere}</style><h1>Deleted-items candidate check</h1>')
        output.write('<p class="note"><strong>Candidates only.</strong> This does not recover permanently deleted data. Zero findings do not prove that no items were deleted.</p>')
        if summary.get("previousFindingsFolder"):
            previous = Path(summary["previousFindingsFolder"]).name
            output.write('<p>Previous findings were preserved: <a href="previous/' + previous + '/index.jsonl">previous index</a> · <a href="previous/' + previous + '/messages.jsonl">previous flagged message rows</a>.</p>')
        counts = summary["counts"]
        output.write('<p>' + html.escape(f"Checked {summary['checkedAt']}. {counts['fileCandidates']} file candidates; {counts['fileCopiesSaved']} verified candidate copies; {counts['messageCandidates']} explicitly flagged message rows; {counts['issues']} issues.") + '</p>')
        output.write('<p><a href="messages.jsonl">Flagged message rows and their provenance</a> · <a href="index.jsonl">Full candidate and issue index</a> · <a href="report.json">Machine-readable summary</a></p><h2>Coverage limits</h2><ul>')
        for limitation in LIMITATIONS:
            output.write('<li>' + html.escape(limitation) + '</li>')
        output.write('</ul><h2>File candidates and scan issues</h2><table><tr><th>Source</th><th>Finding</th><th>Result</th></tr>')
        with index_path.open(encoding="utf-8") as index:
            for line in index:
                _check(cancel)
                row = json.loads(line)
                source = html.escape(str(row.get("source", "Manifest")))
                reason = html.escape(str(row.get("reason", "")))
                result = html.escape(str(row.get("status", "")))
                if row.get("candidatePath"):
                    relative = row["candidatePath"].removeprefix("deleted-items/")
                    result = '<a href="' + html.escape(quote(relative, safe="/"), quote=True) + '">' + result + '</a>'
                output.write('<tr><td>' + source + '</td><td>' + reason + '</td><td>' + result + '</td></tr>')
        output.write('</table><p>Original acquisition files were not modified. All paths in the index are relative to the archive root.</p>')


def check_archive(root, cancel=None, progress=None):
    """Check a stable archive; return a JSON-safe summary. Cancelled propagates.

    ``progress(phase, message)`` is optional. Calling code must prevent concurrent
    acquisition writes. Output reports are refreshed atomically; candidate files
    are deterministic, verified, and never overwritten.
    """
    root = Path(root).absolute()
    if not stat.S_ISDIR(_plain(root).st_mode):
        raise ValueError("Archive root must be a plain directory")
    root = root.resolve()
    _check(cancel)
    folder = _path(root, "deleted-items/report.json", create_parent=True).parent
    for name in ("index.jsonl", "messages.jsonl", "report.json", "report.html"):
        existing = _path(root, "deleted-items/" + name)
        if existing.exists() and not stat.S_ISREG(_plain(existing).st_mode):
            raise ValueError("Generated report path is not a regular file")
    with _lock(folder):
        return _scan(root, folder, cancel, progress)


def _scan(root, folder, cancel, progress):
    token = uuid.uuid4().hex
    temporary = {name: _path(root, "deleted-items/.tmp-" + token + "-" + name) for name in ("index.jsonl", "messages.jsonl", "report.json", "report.html", "dedupe.sqlite")}
    counts = dict(manifestRecords=0, filesExamined=0, fileCandidates=0, fileCopiesSaved=0, messageFilesExamined=0, messageRowsExamined=0, messageCandidates=0, issues=0)
    issue_examples = []
    connection = sqlite3.connect(temporary["dedupe.sqlite"])
    connection.execute("CREATE TABLE seen (key TEXT PRIMARY KEY)")
    summary = {"checkedAt": datetime.now(timezone.utc).isoformat(), "status": "complete", "counts": counts,
               "folder": str(folder), "reportPath": str(folder / "report.html"), "indexPath": str(folder / "index.jsonl"),
               "messagesPath": str(folder / "messages.jsonl"), "limitations": list(LIMITATIONS), "issueExamples": issue_examples}
    try:
        with temporary["index.jsonl"].open("x", encoding="utf-8") as index, temporary["messages.jsonl"].open("x", encoding="utf-8") as messages:
            def issue(record, detail):
                counts["issues"] += 1
                row = {"kind": "issue", "source": record.get("source"), "localPath": record.get("localPath"), "status": "not_verified", "reason": str(detail)}
                _json_line(index, row)
                if len(issue_examples) < 30:
                    issue_examples.append(row)
            manifest_hash = hashlib.sha256()
            with _open_original(root, "manifest.jsonl") as (manifest, _):
                line_number = 0
                while True:
                    raw = _line(manifest, MAX_MANIFEST_LINE, cancel)
                    if not raw:
                        break
                    line_number += 1
                    manifest_hash.update(raw)
                    try:
                        record = json.loads(raw)
                        if not isinstance(record, dict):
                            raise ValueError("Manifest row is not an object")
                    except (ValueError, UnicodeError) as exc:
                        issue({}, f"Manifest line {line_number}: {exc}")
                        continue
                    counts["manifestRecords"] += 1
                    if record.get("status") != "copied":
                        continue
                    source, local = record.get("source"), record.get("localPath")
                    if not isinstance(source, str) or not source or "\x00" in source or not isinstance(local, str) or not local:
                        issue(record, "Copied manifest row lacks a valid source and localPath")
                        continue
                    key = hashlib.sha256(json.dumps([source, local, record.get("sha256")], ensure_ascii=True).encode()).hexdigest()
                    if connection.execute("INSERT OR IGNORE INTO seen VALUES (?)", (key,)).rowcount == 0:
                        continue
                    counts["filesExamined"] += 1
                    reason, kind = trash_reason(source), _message_kind(source)
                    if not reason and not kind:
                        continue
                    if reason:
                        counts["fileCandidates"] += 1
                    if progress:
                        progress("deleted-items", "Checking acquired trash candidates and explicit SMS/MMS flags")
                    try:
                        size, digest = _verified(root, record, cancel)
                        if reason:
                            relative = _copy_candidate(root, record, key, size, digest, cancel)
                            counts["fileCopiesSaved"] += 1
                            _json_line(index, {"kind": "file", "source": source, "localPath": local, "candidatePath": relative, "reason": reason, "status": "candidate_verified_copy", "sha256": digest, "size": size, "acquiredAt": record.get("acquiredAt")})
                        if kind:
                            counts["messageFilesExamined"] += 1
                            _scan_messages(root, record, kind, size, digest, messages, counts, cancel, issue)
                    except (OSError, ValueError, TypeError) as exc:
                        if isinstance(exc, OSError) and (exc.errno in (28, 122) or getattr(exc, "winerror", None) == 112):
                            raise
                        issue(record, exc)
            summary["manifestSha256"] = manifest_hash.hexdigest()
        previous = _preserve_previous(root, temporary, cancel)
        if previous:
            summary["previousFindingsFolder"] = previous
        summary["status"] = "partial" if counts["issues"] else "complete"
        summary["meaning"] = "Check finished for available acquired files; candidates are not proof of deletion and permanently deleted data is not recovered."
        with temporary["report.json"].open("x", encoding="utf-8") as output:
            json.dump(summary, output, ensure_ascii=True, indent=2)
        _write_report(temporary["report.html"], summary, temporary["index.jsonl"], cancel)
        _check(cancel)
        for name in ("index.jsonl", "messages.jsonl", "report.json", "report.html"):
            destination = _path(root, "deleted-items/" + name)
            os.replace(temporary[name], destination)
        return summary
    finally:
        connection.close()
        for path in temporary.values():
            if path.exists():
                path.unlink()  # Only temporary work products created by this scan.


def _scan_messages(root, record, kind, size, digest, output, counts, cancel, issue):
    # Stage rows per source so a change during parsing cannot publish unverified messages.
    staging = output.name + ".source-" + uuid.uuid4().hex
    found, rows = 0, 0
    try:
        with open(staging, "x", encoding="utf-8") as staged, _open_original(root, record["localPath"]) as (stream, _):
            checksum, consumed = hashlib.sha256(), 0
            while True:
                raw = _line(stream, MAX_MESSAGE_LINE, cancel)
                if not raw:
                    break
                consumed += len(raw)
                if consumed > size:
                    raise ValueError("Message acquisition grew during parsing")
                checksum.update(raw)
                rows += 1
                try:
                    text = raw.decode("utf-8")
                    row = json.loads(text)
                    if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
                        raise ValueError("Unexpected helper message row schema")
                except (ValueError, UnicodeError) as exc:
                    issue(record, f"Message row {rows}: {exc}")
                    continue
                flags = explicit_flags(row)
                if flags:
                    found += 1
                    _json_line(staged, {"kind": kind, "status": "candidate_explicit_flag", "source": record["source"], "localPath": record["localPath"], "sourceSha256": digest, "line": rows, "reason": "Explicit deleted/trashed provider flag; not proof of physical deletion", "flags": flags, "row": row, "rawJson": text})
            if consumed != size or checksum.hexdigest() != digest:
                raise ValueError("Message source changed after acquisition verification; staged candidates withheld")
        with open(staging, encoding="utf-8") as staged:
            while True:
                _check(cancel)
                chunk = staged.read(CHUNK)
                if not chunk:
                    break
                output.write(chunk)
        counts["messageRowsExamined"] += rows
        counts["messageCandidates"] += found
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
