"""Eco mode demo server: one page and a small JSON API, standard library only.

  python eco_server.py [--port 8765]

Binds to 127.0.0.1 only. The cloud tier works only if ALLOW_CLOUD=1 was set when the
server started. The API key is never returned: every response body is scrubbed for it.
"""
import argparse
import glob
import json
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bench_coding
import eco_core
from eco_core import Tier

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
DEFAULT_PORT = 8765
TASK_GLOB = "data/coding_tasks_*.py"
CLOUD_DISABLED = "cloud disabled: start the server with ALLOW_CLOUD=1 to use the cloud tier"


def cloud_minimal_tier():
    return Tier("cloud", "cloud", lambda prompt: bench_coding.ask_cloud(prompt, effort="minimal"))


def default_ladder():
    """Pi -> PC -> cloud at minimal effort, the ladder measured in the README."""
    return [Tier("pi", "local", bench_coding.ask_pi),
            Tier("pc", "local", bench_coding.ask_pc),
            cloud_minimal_tier()]


def measured_numbers():
    """Figures computed from the results CSVs, through simulate_router.py."""
    from simulate_router import load, passed, simulate

    total = local = 0
    for set_name in ("dev", "dev2", "heldout"):
        pi, pc = load("pi", set_name), load("pc", set_name)
        for task_id in pi:
            total += 1
            if passed(pi[task_id]) or (pc and task_id in pc and passed(pc[task_id])):
                local += 1
    held = {t: load(t, "heldout") for t in ("pi", "pc", "minimal")}
    ids = sorted(held["pi"])
    base = simulate(ids, ["minimal"], held)
    router = simulate(ids, ["pc", "minimal"], held)
    return {
        "local_solved": local,
        "total_tasks": total,
        "local_share": local / total if total else 0.0,
        "heldout_tasks": len(ids),
        "always_cloud_minimal": {"solved": base[0], "cost_usd": round(base[1], 6)},
        "pc_then_cloud_minimal": {"solved": router[0], "cost_usd": round(router[1], 6)},
        "saving_share": (base[1] - router[1]) / base[1] if base[1] else 0.0,
        "note": "measured on small coding tasks, one run each",
    }


class EcoApp:
    def __init__(self, task_files=None, ladder=default_ladder, cloud_tier=cloud_minimal_tier,
                 log_path=eco_core.LOG_PATH, allow_cloud=None, measured=measured_numbers):
        files = task_files if task_files is not None else sorted(glob.glob(TASK_GLOB))
        self.task_files = {Path(f).name: f for f in files}
        self.ladder = ladder
        self.cloud_tier = cloud_tier
        self.log_path = log_path
        self.allow_cloud = os.environ.get("ALLOW_CLOUD") == "1" if allow_cloud is None else allow_cloud
        self.measured = measured
        self.started = datetime.now(timezone.utc)
        self.jobs = {}
        self.lock = threading.Lock()

    def load_tasks(self, name):
        return {t["id"]: t for t in bench_coding.load_tasks(self.task_files[name])}

    def list_tasks(self):
        out = []
        for name in self.task_files:
            for task in self.load_tasks(name).values():
                first = task["prompt"].strip().splitlines()[0] if task["prompt"].strip() else ""
                out.append({"id": task["id"], "task_file": name,
                            "preview": first if len(first) <= 120 else first[:117] + "..."})
        return out

    def start_job(self, task_file, task_id, eco):
        if task_file not in self.task_files:
            raise ValueError("unknown task_file")
        task = self.load_tasks(task_file).get(task_id)
        if task is None:
            raise ValueError("unknown task_id")
        job = {"id": uuid.uuid4().hex[:12], "task_file": task_file, "task_id": task_id, "eco": eco,
               "status": "running", "current": None, "attempts": [], "skipped": [],
               "result": None, "message": ""}
        with self.lock:
            self.jobs[job["id"]] = job
        if not eco and not self.allow_cloud:
            # Eco off means cloud only; with the cloud disabled nothing is called or logged.
            with self.lock:
                job.update(status="done", skipped=["cloud"], message=CLOUD_DISABLED)
            return job["id"]
        tiers = self.ladder() if eco else [self.cloud_tier()]
        threading.Thread(target=self._run, args=(job, task, tiers), daemon=True).start()
        return job["id"]

    def _run(self, job, task, tiers):
        def on_start(name):
            with self.lock:
                job["current"] = name

        def on_attempt(attempt):
            with self.lock:
                job["attempts"].append(dict(attempt))
                job["current"] = None

        try:
            result = eco_core.solve(task["prompt"], task["tests"], task["entry"], tiers,
                                    task_id=task["id"], log_path=self.log_path,
                                    allow_cloud=self.allow_cloud, on_start=on_start, on_attempt=on_attempt)
            with self.lock:
                job.update(status="done", current=None, result=result, skipped=result["skipped"],
                           message=CLOUD_DISABLED if result["skipped"] else "")
        except BaseException as e:  # keep the job visible instead of losing the thread
            with self.lock:
                job.update(status="error", current=None, message="error: " + repr(e))

    def job_state(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def stats(self):
        totals = {"tasks_run": 0, "solved_by": {}, "cost_usd": 0.0, "estimated_cloud_cost_avoided_usd": 0.0}
        session = json.loads(json.dumps(totals))
        if os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    groups = [totals]
                    try:
                        if datetime.fromisoformat(entry["timestamp"]) >= self.started.replace(microsecond=0):
                            groups.append(session)
                    except (KeyError, ValueError):
                        pass
                    for g in groups:
                        g["tasks_run"] += 1
                        key = entry.get("solved_by") or "none"
                        g["solved_by"][key] = g["solved_by"].get(key, 0) + 1
                        g["cost_usd"] += entry.get("cost_usd", 0.0)
                        g["estimated_cloud_cost_avoided_usd"] += entry.get("estimated_cloud_cost_avoided_usd", 0.0)
        for g in (totals, session):
            g["cost_usd"] = round(g["cost_usd"], 6)
            g["estimated_cloud_cost_avoided_usd"] = round(g["estimated_cloud_cost_avoided_usd"], 6)
        return {"all_time": totals, "session": session, "log": self.log_path,
                "session_started": self.started.isoformat(timespec="seconds")}


def scrub(body):
    """Remove the API key from a response body if it ever appears in it."""
    key = os.environ.get("MODEL_API_KEY", "")
    if len(key) >= 8:
        for form in {key, json.dumps(key)[1:-1]}:
            body = body.replace(form, "[redacted]")
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "EcoServer"

    @property
    def app(self):
        return self.server.app

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _send(self, status, body, ctype):
        data = scrub(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if path == "/api/config":
                return self._json({"cloud_enabled": self.app.allow_cloud, "cloud_effort": "minimal"})
            if path == "/api/tasks":
                return self._json({"tasks": self.app.list_tasks()})
            if path == "/api/stats":
                return self._json(self.app.stats())
            if path == "/api/measured":
                return self._json(self.app.measured())
            if path.startswith("/api/jobs/"):
                job = self.app.job_state(path[len("/api/jobs/"):])
                return self._json(job) if job else self._json({"error": "no such job"}, 404)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return self._json({"error": "bad origin"}, 403)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"error": "send JSON"}, 415)
        if self.path != "/api/solve":
            return self._json({"error": "not found"}, 404)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            req = json.loads(self.rfile.read(length) or b"{}")
            job_id = self.app.start_job(str(req.get("task_file", "")), str(req.get("task_id", "")),
                                        bool(req.get("eco", True)))
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        self._json({"job_id": job_id}, 202)

    def log_request(self, code="-", size="-"):
        # Keep the console readable: polling requests are only logged when they fail.
        if str(code).startswith(("4", "5")) or not self.path.startswith(("/api/jobs/", "/api/stats")):
            super().log_request(code, size)


class EcoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already in use, so
    # two servers would silently share it. Bind exclusively there; elsewhere SO_REUSEADDR
    # only allows a quick restart and a second bind still fails.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app, port=DEFAULT_PORT):
    """Raises OSError if the port is already in use."""
    server = EcoHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description="Eco mode demo server (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    os.chdir(HERE)  # data/ and results/ paths are relative to the repo root
    app = EcoApp()
    try:
        server = make_server(app, args.port)
    except OSError as e:
        raise SystemExit(f"Port {args.port} is already in use ({e.strerror}). Is another eco_server.py "
                         f"running? Stop it, or start this one with --port <other port>.")
    print(f"Eco mode demo: http://127.0.0.1:{args.port}   cloud: "
          f"{'enabled (minimal effort)' if app.allow_cloud else 'disabled (set ALLOW_CLOUD=1 before starting)'}",
          flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
