"""Clearly labeled synthetic fixture backend. Never communicates with a phone."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid


class DemoEngine:
    def __init__(self, base):
        self.base = Path(base)
        self.jobs = {}
        self.lock = threading.RLock()

    def devices(self):
        return [{"serial": "DEMO-PHONE", "state": "device", "model": "Demo Android phone (simulated)"}]

    def inspect(self, serial):
        return {"serial": serial, "model": "Demo Android phone (simulated)", "android": "10 (simulated)", "sdk": 29,
                "roots": [{"path": "/sdcard", "label": "Internal shared storage", "accessible": True}, {"path": "/storage/1234-ABCD", "label": "Removable SD card", "accessible": True}],
                "helperInstalled": True, "limitations": ["DEMO ONLY. No phone data is read or copied.", "Private app databases require an app export or supported advanced method."]}

    def get_job(self, jid):
        with self.lock:
            return copy.deepcopy(self.jobs[jid])

    def history(self):
        with self.lock:
            return [copy.deepcopy(j) for j in reversed(list(self.jobs.values()))]

    def start(self, serial, destination, options):
        with self.lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise ValueError("A demo recovery is already running.")
            jid = uuid.uuid4().hex
            folder = Path(destination) / ("DEMO-" + jid[:8])
            folder.mkdir(parents=True, exist_ok=False)
            job = {"id": jid, "serial": serial, "status": "running", "phase": "Scanning", "message": "DEMO: creating synthetic example files.",
                   "filesCopied": 0, "filesTotal": 6, "bytesCopied": 0, "errors": [], "destination": str(folder),
                   "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "finishedAt": None, "coverage": [], "reportPath": str(folder / "DEMO-REPORT.txt")}
            self.jobs[jid] = job
            threading.Thread(target=self._copy, args=(jid,), daemon=True).start()
            return copy.deepcopy(job)

    def _copy(self, jid):
        for i in range(6):
            time.sleep(0.4)
            with self.lock:
                job = self.jobs[jid]
                if job["status"] != "running":
                    return
                data = ("SYNTHETIC DEMO DATA. No real phone data.\n" * (i + 1)).encode()
                (Path(job["destination"]) / ("example-" + str(i + 1) + ".txt")).write_bytes(data)
                job.update(phase="Copying demo files", filesCopied=i + 1, bytesCopied=job["bytesCopied"] + len(data))
        with self.lock:
            job["status"] = "partial"
            job["phase"] = "Finished"
            job["message"] = "DEMO complete. Six synthetic files saved; private app data is a simulated gap."
            job["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            job["coverage"] = [{"category": "Shared storage", "status": "copied", "detail": "Synthetic example files only"}, {"category": "Private app data", "status": "unavailable", "detail": "Simulated Android access restriction"}]
            Path(job["reportPath"]).write_text("DEMO ONLY\n" + job["message"], encoding="utf-8")
            (Path(job["destination"]) / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>Demo report</title><h1>DEMO ONLY</h1><p>Six synthetic files; no phone recovery was performed.</p><a href="catalog.html">Browse synthetic files</a>', encoding="utf-8")
            entries = [{"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in Path(job["destination"]).glob("example-*.txt")]
            (Path(job["destination"]) / "demo-manifest.json").write_text(json.dumps(entries), encoding="utf-8")
            with (Path(job["destination"]) / "manifest.jsonl").open("w", encoding="utf-8") as manifest:
                for entry in entries:
                    manifest.write(json.dumps({"source": "DEMO/" + entry["path"], "localPath": entry["path"], "status": "copied", "size": (Path(job["destination"]) / entry["path"]).stat().st_size, "sha256": entry["sha256"], "category": "DEMO ONLY"}) + "\n")
            from catalog import write_catalog
            write_catalog(Path(job["destination"]))

    def cancel(self, jid):
        with self.lock:
            job = self.jobs[jid]
            if job["status"] == "running":
                job.update(status="cancelled", phase="Cancelled", message="Demo stopped. Existing synthetic files kept.")
            return copy.deepcopy(job)

    def resume(self, jid):
        job = self.get_job(jid)
        if job["status"] == "running":
            raise ValueError("Recovery is already running.")
        # New demo output keeps the original demo untouched.
        return self.start(job["serial"], str(Path(job["destination"]).parent), {})

    def verify(self, jid):
        job = self.get_job(jid)
        folder = Path(job["destination"])
        if job["status"] == "running":
            raise ValueError("Wait for the recovery to finish before verifying.")
        manifest = folder / "demo-manifest.json"
        if not manifest.exists():
            raise ValueError("This cancelled demo has no finished manifest. Resume it first.")
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        failed = [e["path"] for e in entries if not (folder / e["path"]).is_file() or hashlib.sha256((folder / e["path"]).read_bytes()).hexdigest() != e["sha256"]]
        result = {"status": "failed" if failed else "verified", "checked": len(entries), "failed": failed, "message": "Demo file hashes checked. This does not verify a real phone."}
        with self.lock:
            self.jobs[jid]["verification"] = result
        return result
