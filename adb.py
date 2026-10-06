"""Read-only Android Debug Bridge transport, compatible with legacy sync v1.

Protocol reference: AOSP packages/modules/adb/SYNC.TXT. The local official
adb server handles USB authentication; this module never changes device files.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import threading
import time
from typing import Callable, Optional


class AdbError(RuntimeError):
    pass


class Cancelled(AdbError):
    pass


class ConnectionLost(AdbError):
    """Abort the job instead of retrying a disconnected device for every file."""
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    mode: int
    size: int
    mtime: int


def quote(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL is not allowed in an Android command argument")
    return shlex.quote(value)


class Sync:
    """One short-lived sync connection per operation; FAIL cannot poison reuse."""
    def __init__(self, serial: str, host="127.0.0.1", port=5037,
                 cancel: Optional[threading.Event] = None, idle_timeout=45):
        if not serial or "\x00" in serial or len(serial) > 4096:
            raise AdbError("Invalid device serial")
        self.serial, self.host, self.port = serial, host, port
        self.cancel = cancel or threading.Event()
        self.idle_timeout = idle_timeout
        self.sock = None

    def __enter__(self):
        self._check()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=10)
        except OSError as exc:
            raise ConnectionLost("Cannot connect to the local ADB server: " + str(exc)) from exc
        self.sock.settimeout(.5)
        try:
            self._host_request("host:transport:" + self.serial)
            self._host_request("sync:")
        except BaseException:
            self.sock.close()
            raise
        return self

    def __exit__(self, *_):
        if self.sock:
            self.sock.close()

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled("Transfer cancelled; finished files were preserved")

    def _read(self, size: int) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + self.idle_timeout
        while len(result) < size:
            self._check()
            try:
                part = self.sock.recv(min(size - len(result), 65536))
            except socket.timeout:
                if time.monotonic() >= deadline:
                    raise ConnectionLost("Device stopped responding; reconnect and resume")
                continue
            except OSError as exc:
                raise ConnectionLost("USB/ADB connection lost: " + str(exc)) from exc
            if not part:
                raise ConnectionLost("USB/ADB connection closed before transfer finished")
            deadline = time.monotonic() + self.idle_timeout
            result.extend(part)
        return bytes(result)

    def _send(self, data: bytes):
        self._check()
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise ConnectionLost("Cannot write to ADB connection: " + str(exc)) from exc

    def _host_request(self, request: str):
        data = request.encode("utf-8")
        if len(data) > 65535:
            raise AdbError("ADB request too long")
        self._send(f"{len(data):04x}".encode("ascii") + data)
        status = self._read(4)
        if status == b"OKAY":
            return
        if status == b"FAIL":
            try:
                length = int(self._read(4), 16)
            except ValueError as exc:
                raise AdbError("Malformed ADB error") from exc
            raise ConnectionLost(self._read(length).decode("utf-8", "replace"))
        raise AdbError("Unexpected ADB handshake")

    def _request(self, op: bytes, path: str):
        if not path.startswith("/") or "\x00" in path:
            raise AdbError("Expected an absolute Android path")
        data = path.encode("utf-8", "surrogateescape")
        if len(data) > 4096:
            raise AdbError("Android path exceeds sync protocol limit")
        self._send(op + struct.pack("<I", len(data)) + data)

    def _fail(self):
        length, = struct.unpack("<I", self._read(4))
        if length > 1048576:
            raise AdbError("Malformed sync error length")
        raise AdbError(self._read(length).decode("utf-8", "replace"))

    def stat(self, path: str) -> Entry:
        self._request(b"STAT", path)
        kind = self._read(4)
        if kind == b"FAIL":
            self._fail()
        if kind != b"STAT":
            raise AdbError("Malformed STAT response")
        mode, size, mtime = struct.unpack("<III", self._read(12))
        if not mode:
            raise AdbError("Path is unavailable or permission denied: " + path)
        return Entry(path.rsplit("/", 1)[-1], mode, size, mtime)

    def list(self, path: str) -> list[Entry]:
        self._request(b"LIST", path)
        entries = []
        while True:
            kind = self._read(4)
            if kind == b"FAIL":
                self._fail()
            if kind not in (b"DENT", b"DONE"):
                raise AdbError("Malformed LIST response")
            # DONE has the entire 20-byte dent header, not the RECV header.
            mode, size, mtime, length = struct.unpack("<IIII", self._read(16))
            if kind == b"DONE":
                return entries
            if not 0 < length <= 4096:
                raise AdbError("Malformed remote filename length")
            raw = self._read(length)
            if raw in (b".", b".."):
                continue
            if b"/" in raw or b"\x00" in raw:
                raise AdbError("Unsafe filename returned by device")
            entries.append(Entry(raw.decode("utf-8", "surrogateescape"), mode, size, mtime))

    def receive(self, path: str, output, progress: Optional[Callable[[int], None]] = None):
        self._request(b"RECV", path)
        total = 0
        while True:
            kind = self._read(4)
            if kind == b"FAIL":
                self._fail()
            length, = struct.unpack("<I", self._read(4))
            if kind == b"DONE":
                return total
            if kind != b"DATA" or length > 65536:
                raise AdbError("Malformed RECV data packet")
            data = self._read(length)
            output.write(data)
            total += length
            if progress:
                progress(length)


class Adb:
    def __init__(self, executable: Path, host="127.0.0.1", port=5037):
        self.executable = Path(executable)
        self.host, self.port = host, port

    def command(self, serial: Optional[str], args: list[str]) -> list[str]:
        command = [str(self.executable), "-H", self.host, "-P", str(self.port)]
        if serial is not None:
            if not serial or "\x00" in serial:
                raise AdbError("Invalid device serial")
            command.extend(["-s", serial])
        return command + list(args)

    @staticmethod
    def process_options():
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def run(self, serial: Optional[str], args: list[str], timeout=30, check=True, cancel=None) -> str:
        try:
            if cancel is None:
                result = subprocess.run(self.command(serial, args), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=timeout, **self.process_options())
            else:
                if cancel.is_set():
                    raise Cancelled("Cancelled before phone metadata command")
                command = self.command(serial, args)
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, **self.process_options())
                deadline = time.monotonic() + timeout
                try:
                    while True:
                        if cancel.is_set():
                            raise Cancelled("Cancelled while reading phone metadata")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        try:
                            stdout, stderr = process.communicate(timeout=min(.2, remaining))
                            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    process.stdout.close()
                    process.stderr.close()
        except FileNotFoundError as exc:
            raise AdbError("ADB is missing. Install the bundled USB tools first.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB command timed out; unlock the phone and check USB authorization") from exc
        output = result.stdout.decode("utf-8", "replace").strip()
        if check and result.returncode:
            raise AdbError(result.stderr.decode("utf-8", "replace").strip() or output or "ADB failed")
        return output

    def shell(self, serial: str, command: str, timeout=30, check=True, cancel=None) -> str:
        return self.run(serial, ["shell", command], timeout=timeout, check=check, cancel=cancel)

    def shell_args(self, serial: str, args: list[str], **kwargs) -> str:
        return self.shell(serial, " ".join(quote(arg) for arg in args), **kwargs)

    def sync(self, serial: str, cancel=None):
        return Sync(serial, self.host, self.port, cancel)

    def devices(self):
        output = self.run(None, ["devices", "-l"], timeout=20)
        devices = []
        for line in output.splitlines():
            if not line.strip() or line.startswith(("List of devices", "*")):
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            attrs = dict(x.split(":", 1) for x in fields[2:] if ":" in x)
            devices.append({"serial": fields[0], "state": fields[1],
                            "model": attrs.get("model", "Unknown device").replace("_", " ")})
        return devices
