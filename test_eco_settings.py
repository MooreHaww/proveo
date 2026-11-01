"""Tests for settings, generic tiers, detection, the in-memory key and the budget guard.

Fake model servers on random 127.0.0.1 ports and a temp ECO_DATA_DIR only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_settings
"""
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import bench_coding
import budget
import detect
import eco_core
import eco_server
import router
import settings
from eco_core import Tier
from test_eco_server import FAKE_KEY, GOOD, BAD, ServerCase, fake

PROMPT = "Write add(a, b) that returns the sum."
TESTS = [[[1, 2], 3], [[5, 5], 10]]


class FakeModelServer:
    """A tiny OpenAI-compatible / Ollama-like server. Records every request it gets."""

    def __init__(self, get_routes=None, reply=None, redirect_to=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path})
                if self.path in (get_routes or {}):
                    return self._reply(200, get_routes[self.path])
                self._reply(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"method": "POST", "path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                if redirect_to:
                    return self._reply(302, {}, {"Location": redirect_to})
                if self.path == "/v1/chat/completions" and reply is not None:
                    return self._reply(200, {"choices": [{"message": {"content": reply}}],
                                             "usage": {"prompt_tokens": 12, "completion_tokens": 34}})
                self._reply(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.root = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def local_tier(tid, base_url, **extra):
    return {"id": tid, "name": tid.upper(), "kind": "local", "base_url": base_url, "model": "m",
            "flags": {}, "enabled": True, **extra}


class TempDataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "user_data")
        patcher = mock.patch.dict(os.environ, {"ECO_DATA_DIR": self.data_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MODEL_API_KEY", None)
        self.log = os.path.join(self.tmp.name, "eco_log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def servers(self, *servers):
        for s in servers:
            self.addCleanup(s.close)
        return servers


class DefaultLadderTests(TempDataDir):
    def test_defaults_reproduce_the_old_ladder(self):
        defaults = settings.default_settings()
        self.assertEqual(settings.validate(defaults), defaults)
        self.assertEqual(settings.load(), defaults, "no file yet: load() gives the defaults")
        self.assertFalse(os.path.exists(settings.settings_path()), "loading never creates the file")
        self.assertEqual(defaults["budget"], {"limit_usd": None, "period": "month"})

        tiers = eco_core.build_tiers(defaults, key="", allow_cloud=False)
        self.assertEqual([(t.name, t.kind) for t in tiers], [("pi", "local"), ("pc", "local"), ("cloud", "cloud")])

        # Capture what the old ask_pi / ask_pc sent, through requests.post.
        old = []

        def fake_post(url, json=None, **kw):
            old.append((url, json, kw.get("timeout")))
            return mock.Mock(json=lambda: {"choices": [{"message": {"content": "x"}}],
                                           "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

        with mock.patch.object(bench_coding.requests, "post", fake_post):
            bench_coding.ask_pi(PROMPT)
            bench_coding.ask_pc(PROMPT)

        # Capture what the settings-built tiers send.
        new = []

        def fake_open(req, timeout=None):
            new.append((req.full_url, json.loads(req.data), timeout, req.get_header("Authorization")))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "x"}}],
                                          "usage": {"prompt_tokens": 1, "completion_tokens": 2}}).encode())

        with mock.patch.object(eco_core._LOCAL_OPENER, "open", fake_open):
            replies = [tiers[0].ask(PROMPT), tiers[1].ask(PROMPT)]

        self.assertEqual(new[0][0], router.LOCAL_URL)
        self.assertEqual(new[1][0], bench_coding.PC_URL)
        for (old_url, old_body, old_timeout), (new_url, new_body, new_timeout, auth) in zip(old, new):
            self.assertEqual(new_url, old_url)
            self.assertEqual({k: v for k, v in new_body.items() if k != "model"}, old_body,
                             "same body as before, plus the model name")
            self.assertEqual(new_timeout, old_timeout)
            self.assertIsNone(auth, "local tiers never get an Authorization header")
        self.assertEqual(new[1][1]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("chat_template_kwargs", new[0][1])
        for reply in replies:
            self.assertEqual((reply["in_tokens"], reply["out_tokens"], reply["cost"]), (1, 2, 0.0))

        # The cloud tier is the existing Meta call at minimal effort, same URL and model.
        with mock.patch.object(bench_coding, "ask_cloud", return_value="x") as ask_cloud:
            tiers[2].ask(PROMPT)
        kwargs = ask_cloud.call_args.kwargs
        self.assertEqual((kwargs["effort"], kwargs["url"], kwargs["model"]),
                         ("minimal", router.CLOUD_URL, router.CLOUD_MODEL))

    def test_ask_cloud_keeps_its_old_defaults(self):
        with mock.patch.dict(os.environ, {"ALLOW_CLOUD": "0"}):
            with self.assertRaises(SystemExit):
                bench_coding.ask_cloud(PROMPT)  # off: no key, no ALLOW_CLOUD, nothing sent
        with self.assertRaises(SystemExit):
            bench_coding.ask_cloud(PROMPT, key="", allowed=True)  # an empty in-memory key never falls back


class SettingsTests(TempDataDir):
    def custom(self):
        return {"tiers": [local_tier("box", "http://192.168.1.50:11434/v1/", flags={"disable_thinking": True}),
                          {"id": "meta", "name": "Meta", "kind": "cloud", "provider": "meta",
                           "base_url": "https://api.meta.ai/v1", "model": "muse-spark-1.3",
                           "flags": {"effort": "high"}, "enabled": False}],
                "budget": {"limit_usd": 5, "period": "month"}}

    def test_round_trip(self):
        saved = settings.save(self.custom())
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "settings.json")))
        self.assertEqual(settings.load(), saved)
        self.assertEqual(saved["tiers"][0]["base_url"], "http://192.168.1.50:11434/v1", "trailing / dropped")
        self.assertEqual(saved["budget"], {"limit_usd": 5, "period": "month"})
        self.assertEqual([t.name for t in eco_core.build_tiers(saved)], ["box"], "disabled tiers are left out")

    def test_validation_errors(self):
        def broken(change):
            obj = self.custom()
            change(obj)
            return obj

        cases = [
            (lambda o: o["tiers"][0].update(base_url="ftp://192.168.1.50/v1"), "base_url"),
            (lambda o: o["tiers"][0].update(base_url="javascript:alert(1)//v1"), "base_url"),
            (lambda o: o["tiers"][0].update(base_url="http://192.168.1.50:11434"), "ending in /v1"),
            (lambda o: o["tiers"][0].update(base_url="http://user:pw@host:1/v1"), "credentials"),
            (lambda o: o["tiers"][0].update(name="  "), "name"),
            (lambda o: o["tiers"][0].update(model=""), "model"),
            (lambda o: o["tiers"][0].update(kind="gpu"), "kind"),
            (lambda o: o["tiers"][0].update(id="memory"), "id"),
            (lambda o: o["tiers"][1].update(id="box"), "unique"),
            (lambda o: o["tiers"][0].update(enabled=False), "at least one tier must be enabled"),
            (lambda o: o.update(tiers=[local_tier(f"t{i}", "http://h:1/v1") for i in range(9)]), "at most 8"),
            (lambda o: o.update(tiers=[]), "non-empty"),
            (lambda o: o["tiers"][0].update(api_key=FAKE_KEY), "unknown field"),
            (lambda o: o.update(api_key=FAKE_KEY), "unknown field"),
            (lambda o: o["tiers"][0]["flags"].update(effort="high"), "effort"),
            (lambda o: o["tiers"][1].update(provider="other"), "provider"),
            (lambda o: o["tiers"][1].update(provider="openai_compatible", flags={}), "price_in_per_m"),
            (lambda o: o["budget"].update(limit_usd=-1), "limit_usd"),
            (lambda o: o["budget"].update(limit_usd="5"), "limit_usd"),
            (lambda o: o["budget"].update(period="week"), "period"),
        ]
        for change, expected in cases:
            with self.assertRaises(ValueError, msg=expected) as ctx:
                settings.validate(broken(change))
            self.assertIn(expected, str(ctx.exception))
            self.assertNotIn(FAKE_KEY, str(ctx.exception), "errors never echo field values")
        self.assertEqual(len(settings.validate({"tiers": [local_tier(f"t{i}", "http://h:1/v1")
                                                          for i in range(8)]})["tiers"]), 8)

    def test_save_refuses_a_secret_and_writes_nothing(self):
        obj = self.custom()
        obj["tiers"][0]["name"] = FAKE_KEY
        with self.assertRaises(ValueError):
            settings.save(obj, secrets=[FAKE_KEY])
        self.assertFalse(os.path.exists(settings.settings_path()))


class GenericTierTests(TempDataDir):
    def test_settings_tiers_run_through_solve(self):
        bad, good = self.servers(FakeModelServer(reply=BAD), FakeModelServer(reply=GOOD))
        config = settings.validate({"tiers": [local_tier("small", bad.root + "/v1"),
                                              local_tier("big", good.root + "/v1", flags={"disable_thinking": True})]})
        result = eco_core.solve(PROMPT, TESTS, "add", eco_core.build_tiers(config, key=FAKE_KEY),
                                log_path=self.log, allow_cloud=False)
        self.assertEqual(result["solved_by"], "big")
        self.assertEqual([(a["tier"], a["kind"], a["passed"]) for a in result["attempts"]],
                         [("small", "local", False), ("big", "local", True)])
        self.assertEqual((result["attempts"][1]["in_tokens"], result["attempts"][1]["out_tokens"]), (12, 34))
        self.assertEqual(result["cost_usd"], 0.0)
        sent = good.requests[0]
        self.assertEqual(sent["body"]["model"], "m")
        self.assertEqual(sent["body"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIsNone(sent["auth"], "the key is never sent to a local tier")
        self.assertNotIn(FAKE_KEY, open(self.log, encoding="utf-8").read())

    def test_openai_compatible_cloud_tier_untested_path(self):
        cloud = self.servers(FakeModelServer(reply=GOOD))[0]
        cfg = {"id": "other", "name": "Other cloud", "kind": "cloud", "provider": "openai_compatible",
               "base_url": cloud.root + "/v1", "model": "c", "flags": {}, "enabled": True,
               "price_in_per_m": 1.0, "price_out_per_m": 2.0}
        config = settings.validate({"tiers": [cfg]})
        off = eco_core.solve(PROMPT, TESTS, "add", eco_core.build_tiers(config, key=FAKE_KEY, allow_cloud=False),
                             log_path=self.log, allow_cloud=False)
        self.assertEqual((off["skipped"], cloud.requests), (["other"], []))
        on = eco_core.solve(PROMPT, TESTS, "add", eco_core.build_tiers(config, key=FAKE_KEY, allow_cloud=True),
                            log_path=self.log, allow_cloud=True)
        self.assertEqual(on["solved_by"], "other")
        self.assertEqual(on["attempts"][0]["kind"], "cloud")
        self.assertAlmostEqual(on["cost_usd"], 12 * 1.0 / 1e6 + 34 * 2.0 / 1e6)
        self.assertEqual(cloud.requests[0]["auth"], "Bearer " + FAKE_KEY)
        self.assertEqual(cloud.requests[0]["body"]["max_tokens"], eco_core.CLOUD_MAX_TOKENS)

    def test_redirects_are_not_followed(self):
        target = self.servers(FakeModelServer(reply=GOOD))[0]
        bouncer = self.servers(FakeModelServer(redirect_to=target.root + "/v1/chat/completions"))[0]
        config = settings.validate({"tiers": [local_tier("bounce", bouncer.root + "/v1")]})
        result = eco_core.solve(PROMPT, TESTS, "add", eco_core.build_tiers(config), log_path=self.log)
        self.assertIsNone(result["solved_by"])
        self.assertIn("302", result["attempts"][0]["error"])
        self.assertEqual(target.requests, [], "a redirect never sends the request on")


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.ollama = FakeModelServer({"/api/tags": {"models": [{"name": "llama3.2:3b"}, {"name": "qwen3:14b"}]}})
        self.lmstudio = FakeModelServer({"/v1/models": {"data": [{"id": "qwen2.5-coder-7b-instruct"}]}})
        self.llama = FakeModelServer({"/v1/models": {"object": "list", "data": [{"id": "Qwen3-14B-Q4_K_M.gguf"}]}})
        self.closed = closed_port()

    def tearDown(self):
        for s in (self.ollama, self.lmstudio, self.llama):
            s.close()

    def test_finds_fake_servers_and_skips_a_closed_port(self):
        found = detect.detect_local(servers=[("Ollama", self.ollama.port, "/api/tags"),
                                             ("LM Studio", self.lmstudio.port, "/v1/models"),
                                             ("llama.cpp", self.llama.port, "/v1/models"),
                                             ("llama.cpp", self.closed, "/v1/models")])
        self.assertEqual([(s["kind"], s["port"], s["models"]) for s in found], [
            ("Ollama", self.ollama.port, ["llama3.2:3b", "qwen3:14b"]),
            ("LM Studio", self.lmstudio.port, ["qwen2.5-coder-7b-instruct"]),
            ("llama.cpp", self.llama.port, ["Qwen3-14B-Q4_K_M.gguf"]),
        ])
        self.assertEqual(found[0]["base_url"], f"http://127.0.0.1:{self.ollama.port}/v1",
                         "Ollama models are called through its /v1 endpoint")
        for s in (self.ollama, self.lmstudio, self.llama):
            self.assertTrue(all(r["method"] == "GET" for r in s.requests), "detection only reads")

    def test_defaults(self):
        self.assertEqual([(k, p, path) for k, p, path in detect.KNOWN_SERVERS],
                         [("Ollama", 11434, "/api/tags"), ("LM Studio", 1234, "/v1/models"),
                          ("llama.cpp", 8080, "/v1/models"), ("llama.cpp", 8081, "/v1/models")])
        self.assertEqual(detect.TIMEOUT, 1.0)

    def test_probe_one_address(self):
        got = detect.probe_address(self.llama.root)
        self.assertEqual((got["kind"], got["base_url"], got["models"]),
                         ("OpenAI-compatible", self.llama.root + "/v1", ["Qwen3-14B-Q4_K_M.gguf"]))
        self.assertEqual(detect.probe_address(self.ollama.root + "/v1")["kind"], "Ollama")
        self.assertIsNone(detect.probe_address(f"http://127.0.0.1:{self.closed}"))
        for bad in ("ftp://127.0.0.1:21", "127.0.0.1:8080", "http://127.0.0.1:8080/other",
                    "http://u:p@127.0.0.1:8080", "http://127.0.0.1:99999", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                detect.probe_address(bad)


def log_line(when, attempts):
    return json.dumps({"timestamp": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
                       "attempts": attempts}) + "\n"


class BudgetTests(TempDataDir):
    def write_log(self, cloud_this_month):
        now = datetime.now().astimezone()
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(log_line(now, [{"tier": "pi", "kind": "local", "cost_usd": 9.0},
                                   {"tier": "meta", "kind": "cloud", "cost_usd": cloud_this_month / 2}]))
            # An older log line without "kind": tier "cloud" counts as cloud.
            f.write(log_line(now, [{"tier": "cloud", "cost_usd": cloud_this_month / 2}]))
            f.write(log_line(now.replace(day=1) - timedelta(days=3), [{"tier": "cloud", "kind": "cloud",
                                                                        "cost_usd": 100.0}]))
            f.write("\n")

    def test_thresholds(self):
        for spent, level, percent in ((0.79, "ok", 79.0), (0.80, "warn", 80.0), (1.00, "blocked", 100.0),
                                      (1.30, "blocked", 130.0)):
            self.write_log(spent)
            state = budget.status({"limit_usd": 1.0, "period": "month"}, self.log)
            self.assertEqual((state["level"], state["percent"], state["spent"]), (level, percent, spent), spent)
            self.assertEqual(state["remaining"], round(max(1.0 - spent, 0), 6))
        self.assertIn("budget limit reached", budget.block_reason(state))

    def test_no_limit_raise_and_zero(self):
        self.write_log(1.0)
        self.assertEqual(budget.status({"limit_usd": None, "period": "month"}, self.log)["level"], "ok")
        self.assertEqual(budget.status({"limit_usd": 2.0, "period": "month"}, self.log)["level"], "ok")
        self.assertEqual(budget.status({"limit_usd": 0, "period": "month"}, self.log)["level"], "blocked")
        self.assertEqual(budget.spent(os.path.join(self.tmp.name, "missing.jsonl")), 0.0)
        # The default comes from the saved settings.
        settings.save({"tiers": settings.default_settings()["tiers"], "budget": {"limit_usd": 1.0, "period": "month"}})
        self.assertEqual(budget.status(log_path=self.log)["level"], "blocked")

    def test_blocked_check_stops_the_cloud_tier(self):
        pi, _ = fake("pi", BAD)
        first, first_calls = fake("cloud_a", BAD, kind="cloud")
        first.ask = lambda p: (first_calls.append(p), {"text": BAD, "cost": 0.6})[1]
        second, second_calls = fake("cloud_b", GOOD, kind="cloud")
        limit = {"limit_usd": 0.5, "period": "month"}

        def check(pending):
            return budget.block_reason(budget.status(limit, self.log, pending=pending))

        result = eco_core.solve(PROMPT, TESTS, "add", [pi, first, second], log_path=self.log,
                                allow_cloud=True, cloud_check=check)
        self.assertEqual(len(first_calls), 1)
        self.assertEqual(second_calls, [], "this job's own cloud spend counts before the next cloud call")
        self.assertEqual(result["skipped"], ["cloud_b"])
        self.assertIn("budget limit reached", result["skip_reason"])
        # The spend is now in the log, so the next job is blocked before any cloud call.
        again = eco_core.solve(PROMPT, TESTS, "add", [first], log_path=self.log, allow_cloud=True, cloud_check=check)
        self.assertEqual((len(first_calls), again["skipped"]), (1, ["cloud_a"]))


class SettingsServerTests(ServerCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        os.environ.pop("MODEL_API_KEY", None)
        self.addCleanup(self._env.stop)

    def done(self, eco):
        return self.wait_for(self.solve(eco=eco), lambda s: s["status"] != "running")

    def post(self, path, body, **headers):
        status, text = self.request(path, body, headers or None)
        return status, json.loads(text) if text else None

    def test_settings_round_trip_and_validation_via_api(self):
        self.start()
        status, text = self.request("/api/settings")
        view = json.loads(text)
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in view["tiers"]], ["pi", "pc", "cloud"])
        self.assertEqual((view["key_set"], view["providers"]["openai_compatible"]), (False, "UNTESTED"))

        new = {"tiers": [local_tier("box", "http://192.168.1.50:8080/v1")], "budget": {"limit_usd": 3, "period": "month"}}
        status, body = self.post("/api/settings", new)
        self.assertEqual((status, body["tiers"][0]["id"], body["budget"]["limit_usd"]), (200, "box", 3))
        self.assertEqual(json.loads(self.request("/api/settings")[1])["tiers"], body["tiers"])
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["tiers"][0]["base_url"], "http://192.168.1.50:8080/v1")

        status, body = self.post("/api/settings", {"budget": {"limit_usd": None, "period": "month"}})
        self.assertEqual((status, body["tiers"][0]["id"], body["budget"]["limit_usd"]), (200, "box", None),
                         "budget alone keeps the tiers")
        for bad in ({"tiers": [local_tier("x", "file:///etc/v1")]}, {"tiers": [{**local_tier("x", "http://h:1/v1"), "enabled": False}]},
                    {"tiers": [local_tier(f"t{i}", "http://h:1/v1") for i in range(9)]}, {"key": FAKE_KEY}):
            status, body = self.post("/api/settings", bad)
            self.assertEqual(status, 400, bad)
            self.assertNotIn(FAKE_KEY, json.dumps(body))
        self.assertEqual(json.loads(self.request("/api/settings")[1])["tiers"][0]["id"], "box", "bad saves change nothing")

    def test_solve_builds_its_ladder_from_saved_settings(self):
        bad, good = FakeModelServer(reply=BAD), FakeModelServer(reply=GOOD)
        self.addCleanup(bad.close)
        self.addCleanup(good.close)
        self.start()
        self.post("/api/settings", {"tiers": [local_tier("little", bad.root + "/v1"),
                                              local_tier("off", good.root + "/v1", enabled=False),
                                              local_tier("big", good.root + "/v1")]})
        state = self.done(eco=True)
        self.assertEqual(state["result"]["solved_by"], "big")
        self.assertEqual([(a["tier"], a["kind"]) for a in state["attempts"]], [("little", "local"), ("big", "local")])
        self.assertEqual(len(good.requests), 1, "the disabled tier is never called")
        off = self.done(eco=False)
        self.assertEqual((off["message"], off["attempts"]), (eco_server.NO_CLOUD_TIER, []))
        self.assertEqual(self.request("/api/stats")[0], 200)
        self.assertEqual(json.loads(self.request("/api/config")[1]), {"cloud_enabled": False, "cloud_effort": "minimal"})

    def test_key_is_memory_only_and_never_leaks(self):
        cloud_srv, local_srv = FakeModelServer(reply=GOOD), FakeModelServer(reply=BAD)
        self.addCleanup(cloud_srv.close)
        self.addCleanup(local_srv.close)
        self.start(allow_cloud=True)
        self.assertEqual(json.loads(self.request("/api/key/status")[1]), {"set": False})
        for bad in ({"key": "short"}, {"key": 12345678}, {"key": "has a space in it"}, {"value": FAKE_KEY}):
            status, text = self.request("/api/key", bad)
            self.assertEqual(status, 400)
            self.assertNotIn("short", text)
            self.assertNotIn(FAKE_KEY, text)
        status, text = self.request("/api/key", {"key": FAKE_KEY})
        self.assertEqual((status, json.loads(text)), (200, {"set": True}))
        self.assertTrue(json.loads(self.request("/api/settings")[1])["key_set"])
        self.assertNotIn("MODEL_API_KEY", os.environ, "the key is not put in the environment")

        self.post("/api/settings", {"tiers": [
            local_tier("lan", local_srv.root + "/v1"),
            {"id": "other", "name": "Other", "kind": "cloud", "provider": "openai_compatible",
             "base_url": cloud_srv.root + "/v1", "model": "c", "flags": {}, "enabled": True,
             "price_in_per_m": 1.0, "price_out_per_m": 1.0}]})
        status, _ = self.post("/api/settings", {"tiers": [local_tier("x", "http://h:1/v1", name=FAKE_KEY)]})
        self.assertEqual(status, 400, "the key can't be smuggled into settings")
        job_id = self.solve(eco=True)
        state = self.wait_for(job_id, lambda s: s["status"] != "running")
        self.assertEqual(state["result"]["solved_by"], "other")
        self.assertEqual(cloud_srv.requests[0]["auth"], "Bearer " + FAKE_KEY, "the in-memory key is used")
        self.assertIsNone(local_srv.requests[0]["auth"])

        # A tier whose error message contains the key: responses and the log are scrubbed.
        leaky, _ = fake("leaky", None, error=RuntimeError(f"auth failed with key {FAKE_KEY}"))
        self.server.app.ladder = lambda: [leaky]
        leak_id = self.solve(eco=True)
        self.wait_for(leak_id, lambda s: s["status"] != "running")
        bodies = [self.request(p)[1] for p in ("/", "/api/config", "/api/tasks", "/api/stats", "/api/settings",
                                               "/api/budget", "/api/key/status", "/api/memory",
                                               f"/api/jobs/{job_id}", f"/api/jobs/{leak_id}")]
        bodies.append(self.request("/api/detect", {"address": "http://127.0.0.1:1"})[1])
        self.assertIn("[redacted]", bodies[-2])
        files = [os.path.join(self.data_dir, "settings.json"), self.log]
        self.assertTrue(all(os.path.exists(f) for f in files))
        for text in bodies + [open(f, encoding="utf-8").read() for f in files]:
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("SENTINEL", text)

        self.assertEqual(self.post("/api/key", {"key": None})[1], {"set": False})
        self.assertEqual(json.loads(self.request("/api/key/status")[1]), {"set": False})

    def test_blocked_budget_stops_the_cloud_and_says_why(self):
        pi, pi_calls = fake("pi", BAD)
        cloud, cloud_calls = fake("cloud", GOOD, kind="cloud")
        self.start([pi, cloud], cloud, allow_cloud=True)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(log_line(datetime.now().astimezone(), [{"tier": "cloud", "kind": "cloud", "cost_usd": 1.0}]))
        self.post("/api/settings", {"budget": {"limit_usd": 1.0, "period": "month"}})
        self.assertEqual(json.loads(self.request("/api/budget")[1])["level"], "blocked")

        state = self.done(eco=True)
        self.assertEqual((len(pi_calls), cloud_calls), (1, []))
        self.assertEqual(state["result"]["skipped"], ["cloud"])
        self.assertIn("budget limit reached", state["result"]["skip_reason"])
        self.assertIn("budget limit reached", state["message"])
        off = self.done(eco=False)
        self.assertIn("budget limit reached", off["message"])
        self.assertEqual((off["attempts"], cloud_calls), ([], []))

        self.post("/api/settings", {"budget": {"limit_usd": 1.25, "period": "month"}})
        self.assertEqual(json.loads(self.request("/api/budget")[1])["level"], "warn", "1.00 of 1.25 is 80%")
        self.assertEqual(self.done(eco=False)["result"]["solved_by"], "cloud", "raising the limit unlocks it")
        self.post("/api/settings", {"budget": {"limit_usd": None, "period": "month"}})
        self.assertEqual(json.loads(self.request("/api/budget")[1])["level"], "ok")

    def test_detect_endpoint(self):
        llama = FakeModelServer({"/v1/models": {"data": [{"id": "Qwen3-14B-Q4_K_M.gguf"}]}})
        self.addCleanup(llama.close)
        self.start()
        status, body = self.post("/api/detect", {"address": llama.root})
        self.assertEqual((status, body["servers"][0]["models"]), (200, ["Qwen3-14B-Q4_K_M.gguf"]))
        real = detect.detect_local
        with mock.patch.object(detect, "detect_local",
                               lambda: real(servers=[("llama.cpp", llama.port, "/v1/models"),
                                                     ("Ollama", closed_port(), "/api/tags")])):
            status, body = self.post("/api/detect", {})
        self.assertEqual((status, [s["port"] for s in body["servers"]]), (200, [llama.port]))
        self.assertEqual(self.post("/api/detect", {"address": "file:///etc/passwd"})[0], 400)

    def test_new_endpoints_reject_wrong_origin_host_and_type(self):
        self.start()
        evil = {"Origin": "http://evil.example"}
        for path in ("/api/settings", "/api/budget", "/api/key/status"):
            self.assertEqual(self.request(path)[0], 200, path)
            self.assertEqual(self.request(path, None, evil)[0], 403, path)
            self.assertEqual(self.request(path, None, {"Host": "evil.example"})[0], 403, path)
        posts = {"/api/settings": {"budget": {"limit_usd": 0, "period": "month"}},
                 "/api/detect": {"address": "http://127.0.0.1:1"}, "/api/key": {"key": FAKE_KEY}}
        for path, body in posts.items():
            self.assertEqual(self.request(path, body, {"Content-Type": "text/plain"})[0], 415, path)
            self.assertEqual(self.request(path, body, evil)[0], 403, path)
            self.assertEqual(self.request(path, body, {"Host": "evil.example"})[0], 403, path)
            self.assertEqual(self.request(path, body, {"Host": "127.0.0.1:1"})[0], 403, path)
        self.assertEqual(json.loads(self.request("/api/key/status")[1]), {"set": False})
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "settings.json")))


if __name__ == "__main__":
    unittest.main()
