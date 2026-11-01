"""Tests for eco_server with fake tiers only. No model servers, no cloud calls.

Run with: python -m unittest -v test_eco_server
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import eco_server
from eco_core import Tier

GOOD = "```python\ndef add(a, b):\n    return a + b\n```"
BAD = "```python\ndef add(a, b):\n    return a - b\n```"
TASK_FILE = """TASKS = [{"id": "t_add", "prompt": "Write add(a, b) that returns the sum.",
          "entry": "add", "tests": [[[1, 2], 3], [[5, 5], 10]]}]
"""
FAKE_KEY = "sk-test-SENTINEL-0123456789abcdef"


def fake(name, reply, kind="local", gate=None, error=None):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        if gate is not None:
            gate.wait(10)
        if error is not None:
            raise error
        return reply

    return Tier(name, kind, ask), calls


class ServerCase(unittest.TestCase):
    def start(self, ladder, cloud, allow_cloud=False, measured=None, memory=False):
        self.tmp = tempfile.TemporaryDirectory()
        task_path = os.path.join(self.tmp.name, "coding_tasks_fake.py")
        with open(task_path, "w", encoding="utf-8") as f:
            f.write(TASK_FILE)
        self.log = os.path.join(self.tmp.name, "eco_log.jsonl")
        self.memory = os.path.join(self.tmp.name, "eco_memory.jsonl") if memory else None
        app = eco_server.EcoApp(task_files=[task_path], ladder=lambda: ladder, cloud_tier=lambda: cloud,
                                log_path=self.log, allow_cloud=allow_cloud,
                                measured=measured or (lambda: {"local_share": 0.5}),
                                memory_path=self.memory)
        self.server = eco_server.make_server(app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        """Return (status, text) without raising on HTTP errors."""
        data = None if body is None else json.dumps(body).encode()
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def solve(self, eco):
        status, text = self.request("/api/solve", {"task_file": "coding_tasks_fake.py", "task_id": "t_add", "eco": eco})
        self.assertEqual(status, 202, text)
        return json.loads(text)["job_id"]

    def job(self, job_id):
        return json.loads(self.request(f"/api/jobs/{job_id}")[1])

    def wait_for(self, job_id, condition, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            state = self.job(job_id)
            if condition(state):
                return state
            time.sleep(0.05)
        self.fail(f"condition not reached; last state: {state}")


class JobApiTests(ServerCase):
    def test_attempts_appear_incrementally(self):
        gate = threading.Event()
        pi, _ = fake("pi", BAD)
        pc, pc_calls = fake("pc", GOOD, gate=gate)
        cloud, cloud_calls = fake("cloud", GOOD, kind="cloud")
        self.start([pi, pc, cloud], cloud)
        job_id = self.solve(eco=True)

        # The Pi has failed and is visible while the PC is still working.
        mid = self.wait_for(job_id, lambda s: len(s["attempts"]) == 1 and s["current"] == "pc")
        self.assertEqual(mid["status"], "running")
        self.assertEqual((mid["attempts"][0]["tier"], mid["attempts"][0]["passed"]), ("pi", False))
        for field in ("seconds", "in_tokens", "out_tokens", "cost_usd"):
            self.assertIn(field, mid["attempts"][0])

        gate.set()
        done = self.wait_for(job_id, lambda s: s["status"] != "running")
        self.assertEqual(done["status"], "done")
        self.assertEqual([(a["tier"], a["passed"]) for a in done["attempts"]], [("pi", False), ("pc", True)])
        self.assertEqual(done["result"]["solved_by"], "pc")
        self.assertEqual(len(pc_calls), 1)
        self.assertEqual(cloud_calls, [])

        stats = json.loads(self.request("/api/stats")[1])
        self.assertEqual(stats["session"]["tasks_run"], 1)
        self.assertEqual(stats["session"]["solved_by"], {"pc": 1})

    def test_eco_off_with_cloud_disabled_calls_nothing(self):
        pi, pi_calls = fake("pi", GOOD)
        cloud, cloud_calls = fake("cloud", GOOD, kind="cloud")
        self.start([pi, cloud], cloud, allow_cloud=False)
        job_id = self.solve(eco=False)
        state = self.wait_for(job_id, lambda s: s["status"] != "running")
        self.assertEqual(state["status"], "done")
        self.assertIn("cloud disabled", state["message"])
        self.assertEqual(state["attempts"], [])
        self.assertEqual(state["skipped"], ["cloud"])
        self.assertEqual((pi_calls, cloud_calls), ([], []))
        self.assertFalse(os.path.exists(self.log), "nothing should be logged when nothing ran")
        self.assertFalse(json.loads(self.request("/api/config")[1])["cloud_enabled"])

    def test_api_key_never_in_any_response(self):
        with mock.patch.dict(os.environ, {"MODEL_API_KEY": FAKE_KEY}):
            # A tier whose error message leaks the key, to prove responses are scrubbed.
            leaky, _ = fake("pi", None, error=RuntimeError(f"auth failed with key {FAKE_KEY}"))
            cloud, cloud_calls = fake("cloud", GOOD, kind="cloud")
            self.start([leaky, cloud], cloud, allow_cloud=False, measured=eco_server.measured_numbers)
            job_id = self.solve(eco=True)
            self.wait_for(job_id, lambda s: s["status"] != "running")
            bodies = {
                path: self.request(path)[1]
                for path in ("/", "/api/config", "/api/tasks", "/api/stats", "/api/measured",
                             f"/api/jobs/{job_id}", "/api/jobs/missing", "/nope")
            }
            bodies["bad solve"] = self.request("/api/solve", {"task_file": FAKE_KEY, "task_id": "x"})[1]
        for name, body in bodies.items():
            self.assertNotIn(FAKE_KEY, body, f"key leaked in {name}")
            self.assertNotIn("SENTINEL", body, f"part of the key leaked in {name}")
        self.assertIn("[redacted]", bodies[f"/api/jobs/{job_id}"])
        self.assertEqual(cloud_calls, [])
        self.assertIn("local_share", bodies["/api/measured"])


class GuardTests(ServerCase):
    def test_rejects_cross_site_and_unknown_requests(self):
        pi, pi_calls = fake("pi", GOOD)
        self.start([pi], pi)
        ok = {"task_file": "coding_tasks_fake.py", "task_id": "t_add", "eco": True}
        self.assertEqual(self.request("/api/solve", ok, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/solve", ok, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/tasks", None, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/solve", {**ok, "task_file": "../../router.py"})[0], 400)
        self.assertEqual(self.request("/api/solve", {**ok, "task_id": "nope"})[0], 400)
        self.assertEqual(pi_calls, [])


class BusyPortTests(unittest.TestCase):
    def app(self):
        return eco_server.EcoApp(task_files=[], ladder=list, cloud_tier=lambda: None,
                                 log_path=os.devnull, allow_cloud=False, measured=dict)

    def test_second_server_on_same_port_fails(self):
        first = eco_server.make_server(self.app(), port=0)
        try:
            with self.assertRaises(OSError):
                eco_server.make_server(self.app(), port=first.server_address[1]).server_close()
        finally:
            first.server_close()

    def test_port_held_by_reuseaddr_listener_fails(self):
        # Simulates another server that set SO_REUSEADDR, as the old code did.
        other = socket.socket()
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        try:
            with self.assertRaises(OSError):
                eco_server.make_server(self.app(), port=other.getsockname()[1]).server_close()
        finally:
            other.close()

    def test_main_explains_busy_port(self):
        first = eco_server.make_server(self.app(), port=0)
        port = first.server_address[1]
        try:
            with mock.patch("sys.argv", ["eco_server.py", "--port", str(port)]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ALLOW_CLOUD", None)
                with self.assertRaises(SystemExit) as ctx:
                    eco_server.main()
        finally:
            first.server_close()
        self.assertIn(f"Port {port} is already in use", str(ctx.exception))
        self.assertIn("--port", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
