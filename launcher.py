"""Launch/stop helpers; no registry changes or machine-wide installation."""
import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import webbrowser


def acquire_lock(folder):
    import msvcrt
    file = (folder / "server.lock").open("a+b")
    try:
        # Windows byte locks also block reads. Lock first, including past EOF,
        # so another launch can detect the owner without reading its lock byte.
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        file.close()
        if error.errno == errno.EACCES:
            return None
        raise
    return file


def existing_server(folder):
    try:
        state = json.loads((folder / "server.json").read_text(encoding="utf-8"))
        from urllib.parse import urlsplit
        url = urlsplit(state["url"])
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
            return None
        with urllib.request.urlopen(state["url"] + "/api/health", timeout=2) as response:
            if json.load(response).get("ok"):
                return state
    except (OSError, ValueError, KeyError):
        pass
    return None


def open_window(url):
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            edge = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                subprocess.Popen([str(edge), "--app=" + url, "--window-size=1280,900"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
    webbrowser.open(url)


def stop():
    base = Path(__file__).resolve().parent
    found = False
    for name in ("state", "demo-state"):
        state = existing_server(base / name)
        if state:
            found = True
            request = urllib.request.Request(state["url"] + "/api/shutdown", b"{}", {"Content-Type": "application/json", "X-CSRF-Token": state["token"]})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    print(json.load(response)["message"])
            except urllib.error.HTTPError as exc:
                print(json.load(exc).get("error", "Could not stop."))
                return 1
    if not found:
        print("Android Bay is not running.")
    return 0


if __name__ == "__main__":
    if "--stop" in sys.argv:
        sys.exit(stop())
