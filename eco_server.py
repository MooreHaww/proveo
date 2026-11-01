"""Eco mode demo server: one page and a small JSON API, standard library only.

  python eco_server.py [--port 8765]

Binds to 127.0.0.1 only. The cloud tier works only if ALLOW_CLOUD=1 was set when the
server started and the monthly budget (settings.py, budget.py) is not used up.
The ladder comes from the saved settings. Named API keys are held in this process's memory only
(POST /api/key, or MODEL_API_KEY at start as "Default"); they are never returned: every response
body is scrubbed, and GET /api/key/status lists names and provider labels only.
"""
import argparse
import glob
import json
import os
import re
import socket
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import bench_coding
import budget
import detect
import eco_core
import settings

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
ASSETS = HERE / "web" / "assets"
# Only these files are served from /assets/: one plain file name, no folders.
ASSET_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}(\.[A-Za-z0-9_-]{1,16})*\.(svg|png|ico)$")
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
# An SVG opened directly could carry script: assets get no script and a sandbox.
ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
DEFAULT_PORT = 8765
TASK_GLOB = "data/coding_tasks_*.py"
MAX_BODY = 10_000
# /api/solve may carry a custom task: 4,000 + 10,000 characters, which JSON escaping can grow.
MAX_SOLVE_BODY = 64_000
CUSTOM_FIELDS = {"prompt", "entry", "tests_code"}
CUSTOM_CHECK_NOTE = ("NOT a real sandbox: the answer and your tests run together in a separate Python "
                     "process with a 10 s time limit; the answer's code is screened for risky words first")
CLOUD_DISABLED = "cloud disabled: start the server with ALLOW_CLOUD=1 to use the cloud tier"
NO_CLOUD_TIER = "no cloud tier is enabled in the settings"
PROVIDERS = {"meta": "tested", "openai_compatible": "UNTESTED"}
DEFAULT_KEY_NAME = eco_core.DEFAULT_KEY_NAME


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
    def __init__(self, task_files=None, ladder=None, cloud_tier=None,
                 log_path=eco_core.LOG_PATH, allow_cloud=None, measured=measured_numbers,
                 memory_path=None):
        """ladder() and cloud_tier() default to the saved settings; tests pass fake ones."""
        files = task_files if task_files is not None else sorted(glob.glob(TASK_GLOB))
        self.task_files = {Path(f).name: f for f in files}
        self.ladder = ladder or self.settings_ladder
        self.cloud_tier = cloud_tier or self.settings_cloud_tier
        self.log_path = log_path
        self.allow_cloud = os.environ.get("ALLOW_CLOUD") == "1" if allow_cloud is None else allow_cloud
        self.measured = measured
        self.memory_path = memory_path  # None: no verified memory
        self.started = datetime.now(timezone.utc)
        self.jobs = {}
        self.lock = threading.Lock()
        # API keys live only here, in process memory, by name. Every key ever set is kept (in memory)
        # so that responses and logs stay scrubbed of it after it is replaced or removed.
        env_key = os.environ.get("MODEL_API_KEY", "")
        self._keys = {DEFAULT_KEY_NAME: env_key} if env_key else {}
        self._key_providers = {DEFAULT_KEY_NAME: eco_core.stored_key_label(env_key, eco_core.META_PROVIDER)} if env_key else {}
        self._seen_keys = {env_key} if env_key else set()

    def set_key(self, name, key, provider=None):
        """Add a named key, or replace the one with that name (ignoring case). Returns the name.
        provider is a label from eco_core.KEY_PROVIDERS; None follows eco_core.key_provider."""
        if provider is not None and provider not in eco_core.KEY_PROVIDERS:
            raise ValueError(f"provider must be one of: {', '.join(eco_core.KEY_PROVIDERS)}")
        with self.lock:
            clean = settings.clean_key_name(name, [*self._seen_keys, key])
            # Scrubbing removes key text from responses, so it must not be able to eat a name or a label.
            if any(key.casefold() in n.casefold() for n in self._keys):
                raise ValueError(settings.KEY_NAME_LABEL)
            if any(key.casefold() in p.casefold() for p in eco_core.KEY_PROVIDERS):
                raise ValueError("that key is the same as a provider's name")
            label = provider or eco_core.key_provider(clean)
            problem = eco_core.key_label_problem(key, label)
            if problem:
                raise ValueError(problem)
            self._forget_name(clean)
            self._keys[clean] = key
            self._key_providers[clean] = label
            self._seen_keys.add(key)
        return clean

    def remove_key(self, name):
        """Removing a name that isn't there is fine: the result is the same."""
        with self.lock:
            self._forget_name(name)

    def _forget_name(self, name):
        for existing in [n for n in self._keys if n.casefold() == name.strip().casefold()]:
            del self._keys[existing]
            del self._key_providers[existing]

    def _key_copy(self):
        with self.lock:
            return dict(self._keys)

    def _provider_copy(self):
        with self.lock:
            return dict(self._key_providers)

    def key_status(self):
        """Names, providers and the rungs using each. Never any part of a key: no length, no last 4."""
        keys, providers = self._key_copy(), self._provider_copy()
        try:
            tiers = [t for t in settings.load()["tiers"] if t["kind"] == "cloud"]
        except ValueError:
            tiers = []
        return {"keys": [{"name": n, "provider": eco_core.key_provider(n, providers),
                          "rungs": [t["name"] for t in tiers if eco_core.key_name_for_tier(t, keys, providers)[0] == n]}
                         for n in sorted(keys, key=str.casefold)]}

    def secrets(self):
        with self.lock:
            return sorted(self._seen_keys)

    def settings_ladder(self):
        return eco_core.build_tiers(settings.load(), keys=self._key_copy(), allow_cloud=self.allow_cloud,
                                    providers=self._provider_copy())

    def settings_cloud_tier(self):
        """Eco off: the first enabled cloud tier, or None."""
        return next((t for t in self.settings_ladder() if t.kind == "cloud"), None)

    def settings_view(self):
        # saved: False until the settings file exists (the page shows its first-run card).
        current, keys, providers = settings.load(), self._key_copy(), self._provider_copy()
        rung_keys = {}
        for t in current["tiers"]:
            if t["kind"] == "cloud":
                name, reason = eco_core.key_name_for_tier(t, keys, providers)
                rung_keys[t["id"]] = {"key_name": name, "reason": reason}
        return {**current, "key_set": bool(keys), "key_names": sorted(keys, key=str.casefold), "rung_keys": rung_keys,
                "key_providers": {n: eco_core.key_provider(n, providers) for n in keys},
                "max_tiers": settings.MAX_TIERS, "providers": PROVIDERS,
                "saved": os.path.exists(settings.settings_path()), "defaults": settings.default_settings()["tiers"]}

    def save_settings(self, req):
        if not isinstance(req, dict) or set(req) - {"tiers", "budget"}:
            raise ValueError("send only tiers and/or budget (the API key is set with POST /api/key)")
        current = settings.load() if not {"tiers", "budget"} <= set(req) else {}
        new = {"tiers": req.get("tiers", current.get("tiers")), "budget": req.get("budget", current.get("budget"))}
        if "tiers" in req:
            self._refuse_wrong_provider(settings.validate(new)["tiers"])
        settings.save(new, secrets=self.secrets())
        return self.settings_view()

    def _refuse_wrong_provider(self, tiers):
        """A rung on the Meta host can't be paired with a stored key for anyone else, even a disabled one."""
        keys, providers = self._key_copy(), self._provider_copy()
        for t in tiers:
            found = eco_core.find_key_name(t["key_name"], keys) if t.get("key_name") else None
            refusal = eco_core.meta_key_refusal(t, found, providers) if found else ""
            if refusal:
                raise ValueError(f"{t['name']}: {refusal}")

    def budget_status(self, pending=0.0):
        state = budget.status(settings.load()["budget"], self.log_path, pending=pending)
        return {**state, "reason": budget.block_reason(state)}

    def cloud_reason(self, pending=0.0):
        """Empty if a cloud tier may be called now, else why not. Checked before every cloud call."""
        if not self.allow_cloud:
            return CLOUD_DISABLED
        return self.budget_status(pending)["reason"]

    def detect(self, address=None):
        if address:
            found = detect.probe_address(address)
            return {"address": detect.normalize_address(address), "servers": [found] if found else []}
        return {"servers": detect.detect_local()}

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

    @staticmethod
    def custom_task(custom):
        """A user's own task from the request. Its tests run in run_custom_tests, NOT a real sandbox."""
        if not isinstance(custom, dict):
            raise ValueError("custom must be an object with prompt, entry and tests_code")
        missing, extra = CUSTOM_FIELDS - set(custom), set(custom) - CUSTOM_FIELDS
        if missing:
            raise ValueError("custom is missing " + ", ".join(sorted(missing)))
        if extra:
            raise ValueError("custom has unknown field(s) " + ", ".join(sorted(extra))
                             + "; send only prompt, entry and tests_code")
        return eco_core.make_custom_task(custom["prompt"], custom["entry"], custom["tests_code"])

    def start_job(self, task_file, task_id, eco, custom=None):
        """Benchmark task (task_file, task_id), or a custom task when custom is given."""
        if custom is not None:
            task = self.custom_task(custom)
            task_file, task_id = None, task["id"]
        else:
            if task_file not in self.task_files:
                raise ValueError("unknown task_file")
            task = self.load_tasks(task_file).get(task_id)
            if task is None:
                raise ValueError("unknown task_id")
        job = {"id": uuid.uuid4().hex[:12], "task_file": task_file, "task_id": task_id,
               "custom": custom is not None, "eco": eco,
               "status": "running", "current": None, "attempts": [], "skipped": [], "skip_reasons": {},
               "result": None, "message": ""}
        if custom is not None:
            job["check_note"] = CUSTOM_CHECK_NOTE
        if eco:
            tiers = self.ladder()
        else:
            # Eco off means cloud only. If the cloud may not be called (ALLOW_CLOUD off, budget
            # used up, no cloud tier) the job ends at once: nothing is called or logged.
            cloud = self.cloud_tier()
            reason = NO_CLOUD_TIER if cloud is None else (cloud.wait_reason or self.cloud_reason())
            if reason:
                name = cloud.name if cloud else "cloud"
                job.update(status="done", skipped=[name], skip_reasons={name: reason}, message=reason)
            tiers = [cloud]
        with self.lock:
            self.jobs[job["id"]] = job
        if job["status"] == "done":
            return job["id"]
        memory = self.memory_path if eco else None  # Eco off is the plain cloud path, no memory
        threading.Thread(target=self._run, args=(job, task, tiers, memory), daemon=True).start()
        return job["id"]

    def memory_state(self):
        enabled = self.memory_path is not None
        return {"enabled": enabled, "count": eco_core.memory_count(self.memory_path) if enabled else 0}

    def clear_memory(self):
        if self.memory_path is not None:
            eco_core.clear_memory(self.memory_path)
        return self.memory_state()

    def _run(self, job, task, tiers, memory_path=None):
        def on_start(name):
            with self.lock:
                job["current"] = name

        def on_attempt(attempt):
            with self.lock:
                job["attempts"].append(dict(attempt))
                job["current"] = None

        try:
            result = eco_core.solve(task["prompt"], task.get("tests"), task["entry"], tiers,
                                    task_id=task["id"], log_path=self.log_path,
                                    allow_cloud=self.allow_cloud, on_start=on_start, on_attempt=on_attempt,
                                    memory_path=memory_path, cloud_check=self.cloud_reason,
                                    secrets=self.secrets(), tests_code=task.get("tests_code"))
            message = ""
            if result["skipped"]:
                message = CLOUD_DISABLED if not self.allow_cloud else result.get("skip_reason", "")
                # Every skipped rung is only waiting for a key: that is the thing to say.
                reasons = set(result["skip_reasons"].values())
                if reasons and all(eco_core.is_key_wait(r) for r in reasons):
                    message = sorted(reasons)[-1]
            with self.lock:
                job.update(status="done", current=None, result=result, skipped=result["skipped"],
                           skip_reasons=result.get("skip_reasons", {}), message=message)
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


def scrub(body, secrets=()):
    """Remove the API key (MODEL_API_KEY and any key set in memory) from a response body."""
    return eco_core.redact(body, secrets)


def asset_file(raw_name, folder=None):
    """The Path of an allowed file directly inside web/assets/, or None.

    raw_name is the URL path after /assets/ (still percent-encoded). Anything with a folder,
    a dot segment, an unusual character or another extension is refused, and the resolved
    path must still sit directly in the assets folder (so a link pointing elsewhere is refused too).
    """
    folder = Path(folder or ASSETS)
    name = unquote(raw_name)
    if not ASSET_NAME.match(name) or ".." in name:
        return None
    try:
        root = folder.resolve()
        path = (folder / name).resolve()
    except (OSError, ValueError):
        return None
    if path.parent != root or not path.is_file():
        return None
    return path


def valid_key(key):
    return (isinstance(key, str) and 8 <= len(key) <= 512 and key.isprintable()
            and not any(c.isspace() for c in key))


class Handler(BaseHTTPRequestHandler):
    server_version = "EcoServer"

    @property
    def app(self):
        return self.server.app

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _origin_ok(self):
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _read_json(self, limit=MAX_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError(f"request body too large: {length:,} bytes (the limit is {limit:,})")
        return json.loads(self.rfile.read(length) or b"{}")

    def _send(self, status, body, ctype, csp=PAGE_CSP):
        """body is text (scrubbed for the API key) or bytes (a PNG or ICO asset)."""
        data = body if isinstance(body, bytes) else scrub(body, self.app.secrets()).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(data)

    def _asset(self, raw_name):
        path = asset_file(raw_name)
        if path is None:
            return self._json({"error": "not found"}, 404)
        ctype = ASSET_TYPES[path.suffix.lower()]
        body = path.read_text(encoding="utf-8") if path.suffix.lower() == ".svg" else path.read_bytes()
        self._send(200, body, ctype + ("; charset=utf-8" if isinstance(body, str) else ""), ASSET_CSP)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if not self._origin_ok():
            return self._json({"error": "bad origin"}, 403)
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if path.startswith("/assets/"):
                return self._asset(path[len("/assets/"):])
            if path == "/api/config":
                return self._json({"cloud_enabled": self.app.allow_cloud, "cloud_effort": "minimal"})
            if path == "/api/tasks":
                return self._json({"tasks": self.app.list_tasks()})
            if path == "/api/stats":
                return self._json(self.app.stats())
            if path == "/api/measured":
                return self._json(self.app.measured())
            if path == "/api/memory":
                return self._json(self.app.memory_state())
            if path == "/api/settings":
                return self._json(self.app.settings_view())
            if path == "/api/budget":
                return self._json(self.app.budget_status())
            if path == "/api/key/status":
                return self._json(self.app.key_status())
            if path.startswith("/api/jobs/"):
                job = self.app.job_state(path[len("/api/jobs/"):])
                return self._json(job) if job else self._json({"error": "no such job"}, 404)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        self._json({"error": "not found"}, 404)

    def _drain(self):
        """Read a refused request's body. On Windows, closing a socket with unread data resets the
        connection, and the client can lose the answer."""
        try:
            self.rfile.read(min(int(self.headers.get("Content-Length") or 0), MAX_SOLVE_BODY))
        except (ValueError, OSError):
            pass

    def do_POST(self):
        if not self._host_ok():
            self._drain()
            return self._json({"error": "bad host"}, 403)
        if not self._origin_ok():
            self._drain()
            return self._json({"error": "bad origin"}, 403)
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._drain()
            return self._json({"error": "send JSON"}, 415)
        routes = {"/api/memory/clear", "/api/solve", "/api/settings", "/api/detect", "/api/key", "/api/key/remove"}
        if self.path not in routes:
            return self._json({"error": "not found"}, 404)
        try:
            req = self._read_json(MAX_SOLVE_BODY if self.path == "/api/solve" else MAX_BODY)
            if not isinstance(req, dict):
                raise ValueError("send a JSON object")
            if self.path == "/api/memory/clear":
                return self._json(self.app.clear_memory())
            if self.path == "/api/settings":
                return self._json(self.app.save_settings(req))
            if self.path == "/api/detect":
                return self._json(self.app.detect(req.get("address")))
            if self.path == "/api/key":
                # The key is never echoed, not even in an error message.
                if set(req) in ({"name", "key"}, {"name", "key", "provider"}) and valid_key(req["key"]):
                    self.app.set_key(req["name"], req["key"], req.get("provider"))
                    return self._json(self.app.key_status())
                # The old single-key request still works and answers as it did: the name is "Default".
                if set(req) == {"key"} and valid_key(req["key"]):
                    self.app.set_key(DEFAULT_KEY_NAME, req["key"])
                    return self._json({"set": True})
                if set(req) == {"key"} and req["key"] is None:
                    self.app.remove_key(DEFAULT_KEY_NAME)
                    return self._json({"set": False})
                raise ValueError('send {"name": "<a label>", "key": "<8-512 printable characters, '
                                 'no spaces>", "provider": "<optional provider label>"}; POST /api/key/remove forgets one')
            if self.path == "/api/key/remove":
                if set(req) != {"name"} or not isinstance(req["name"], str):
                    raise ValueError('send {"name": "<the key\'s name>"}')
                self.app.remove_key(req["name"])
                return self._json(self.app.key_status())
            if "custom" in req:
                if "task_file" in req or "task_id" in req:
                    raise ValueError("send either custom or task_file and task_id, not both")
                job_id = self.app.start_job(None, None, bool(req.get("eco", True)), custom=req["custom"])
            else:
                job_id = self.app.start_job(str(req.get("task_file", "")), str(req.get("task_id", "")),
                                            bool(req.get("eco", True)))
        except (ValueError, json.JSONDecodeError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
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
    parser.add_argument("--log", help=f"spend log file (default: {eco_core.LOG_PATH})")
    parser.add_argument("--memory", help=f"verified memory file (default: {eco_core.MEMORY_PATH})")
    args = parser.parse_args()
    log_path = os.path.abspath(args.log) if args.log else eco_core.LOG_PATH
    memory_path = os.path.abspath(args.memory) if args.memory else eco_core.MEMORY_PATH
    os.chdir(HERE)  # data/ and results/ paths are relative to the repo root
    app = EcoApp(log_path=log_path, memory_path=memory_path)
    try:
        server = make_server(app, args.port)
    except OSError as e:
        raise SystemExit(f"Port {args.port} is already in use ({e.strerror}). Is another eco_server.py "
                         f"running? Stop it, or start this one with --port <other port>.")
    print(f"Eco mode demo: http://127.0.0.1:{args.port}   cloud: "
          f"{'enabled (minimal effort)' if app.allow_cloud else 'disabled (set ALLOW_CLOUD=1 before starting)'}",
          flush=True)
    print(f"Settings: {settings.settings_path()}"
          f"{'' if os.path.exists(settings.settings_path()) else ' (not saved yet: using the default ladder)'}",
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
