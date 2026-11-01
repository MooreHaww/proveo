"""Tests for rung colours, the Add rung check, detection without duplicates, duplicate rungs and provider names.

Fake model servers on random 127.0.0.1 ports and a temp ECO_DATA_DIR only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_rungs
"""
import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import detect
import eco_core
import settings
from test_eco_presets import PAGE, page_presets
from test_eco_server import ServerCase
from test_eco_settings import FakeModelServer, TempDataDir, local_tier

INJECTIONS = ("red;background:url(x)", "red", "#12345", "#1234567", "#GGGGGG", "#123456\n", "#123456;", " #123456",
              "url(x)", "var(--x)", "", 123456, True, ["#123456"], {"hex": "#123456"})
ENDPOINTS = [  # (spellings of one endpoint), also in desktop/test/lib.test.js
    ("http://localhost:8080/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1", "http://0.0.0.0:8080/v1",
     "http://LOCALHOST:8080/v1/", "http://localhost:8080", "http://127.0.0.1:8080/"),
    ("http://192.168.1.14:8080/v1", "http://192.168.1.14:8080"),
    ("http://box:80/v1", "http://box/v1"),
    ("https://box:443/v1", "https://box/v1"),
]
ENTRY = "add"
CHECKS = "assert add(1, 2) == 3\n"
RIGHT = "def add(a, b):\n    return a + b\n"
WRONG = "def add(a, b):\n    return a - b\n"


def models_server(ids):
    return FakeModelServer({"/v1/models": {"data": [{"id": i} for i in ids]}})


class RetryServer:
    """Answers wrongly the first time and rightly the second, like a model that varies between runs."""

    def __init__(self):
        self.prompts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.prompts.append(body["messages"][0]["content"])
                code = WRONG if len(outer.prompts) == 1 else RIGHT
                data = json.dumps({"choices": [{"message": {"content": code}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.root = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ColourSettings(TempDataDir):
    def ladder(self, **extra):
        return {"tiers": [local_tier("box", "http://h:1/v1", **extra)]}

    def test_a_valid_colour_and_mode_are_kept(self):
        saved = settings.save({**self.ladder(color="#1b69ae"), "color_mode": "custom"})
        self.assertEqual((saved["tiers"][0]["color"], saved["color_mode"]), ("#1B69AE", "custom"))
        self.assertEqual(settings.load(), saved)

    def test_the_colour_is_optional_and_the_mode_defaults(self):
        self.assertEqual(settings.default_settings()["color_mode"], "default")
        clean = settings.validate(self.ladder())
        self.assertEqual(clean["color_mode"], "default")
        self.assertNotIn("color", clean["tiers"][0])
        self.assertNotIn("color", settings.validate(self.ladder(color=None))["tiers"][0])

    def test_a_settings_file_from_before_colours_still_loads(self):
        os.makedirs(self.data_dir)
        with open(settings.settings_path(), "w", encoding="utf-8") as f:
            json.dump({"tiers": self.ladder()["tiers"], "budget": {"limit_usd": None, "period": "month"}}, f)
        self.assertEqual(settings.load()["color_mode"], "default")

    def test_anything_but_a_hex_colour_is_refused(self):
        for bad in INJECTIONS:
            with self.assertRaisesRegex(ValueError, "color must look like", msg=repr(bad)):
                settings.validate(self.ladder(color=bad))
        self.assertFalse(os.path.exists(settings.settings_path()))

    def test_only_the_two_modes_are_accepted(self):
        for bad in ("Custom", "", "red;background:url(x)", None, 1, ["custom"]):
            with self.assertRaisesRegex(ValueError, "color_mode", msg=repr(bad)):
                settings.validate({**self.ladder(), "color_mode": bad})

    def test_the_ladder_ignores_the_colour(self):
        saved = settings.save(self.ladder(color="#1B69AE"))
        self.assertEqual([t.name for t in eco_core.build_tiers(saved, allow_cloud=False)], ["box"])


class Detection(TempDataDir):
    def test_every_spelling_of_an_endpoint_is_one_key(self):
        for spellings in ENDPOINTS:
            self.assertEqual(len({detect.endpoint_key(s) for s in spellings}), 1, spellings)
        self.assertEqual(len({detect.endpoint_key(s[0]) for s in ENDPOINTS}), len(ENDPOINTS))
        for a, b in (("http://localhost:8080/v1", "http://localhost:8081/v1"), ("http://box:1/v1", "https://box:1/v1"),
                     ("http://localhost:1/v1", "http://192.168.1.14:1/v1"), ("http://box:1/v1", "http://box:1/v2")):
            self.assertNotEqual(detect.endpoint_key(a), detect.endpoint_key(b))

    def test_a_server_listing_a_model_twice_shows_it_once(self):
        server, = self.servers(models_server(["Qwen3", "Qwen3", "Llama"]))
        found = detect.detect_local(servers=[("LM Studio", server.port, "/v1/models")])
        self.assertEqual([s["models"] for s in found], [["Qwen3", "Llama"]])
        self.assertEqual(detect.probe_address(server.root)["models"], ["Qwen3", "Llama"])

    def test_the_same_server_found_twice_is_one_entry(self):
        server, = self.servers(models_server(["Qwen3"]))
        found = detect.detect_local(servers=[("llama.cpp", server.port, "/v1/models"), ("llama.cpp", server.port, "/v1/models")])
        self.assertEqual([(s["port"], s["models"]) for s in found], [(server.port, ["Qwen3"])])

    def test_spellings_of_this_computer_are_one_endpoint(self):
        servers = [{"kind": "x", "base_url": f"http://{host}:8080/v1", "models": ["Qwen3"]}
                   for host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")]
        kept = detect.dedupe_servers(servers)
        self.assertEqual([(s["base_url"], s["models"]) for s in kept], [("http://localhost:8080/v1", ["Qwen3"])])

    def test_different_servers_and_models_stay_separate(self):
        a, b = self.servers(models_server(["Qwen3"]), models_server(["Qwen3", "Llama"]))
        found = detect.detect_local(servers=[("llama.cpp", a.port, "/v1/models"), ("llama.cpp", b.port, "/v1/models")])
        self.assertEqual([(s["port"], s["models"]) for s in found], [(a.port, ["Qwen3"]), (b.port, ["Qwen3", "Llama"])])

    def test_a_server_with_no_models_is_still_reported(self):
        server, = self.servers(models_server([]))
        found = detect.detect_local(servers=[("llama.cpp", server.port, "/v1/models")])
        self.assertEqual([s["models"] for s in found], [[]])


class DuplicateRungs(TempDataDir):
    def setUp(self):
        super().setUp()
        self.server = RetryServer()
        self.addCleanup(self.server.close)
        base = self.server.root + "/v1"
        self.config = {"tiers": [{**local_tier("qwen3", base, model="Qwen3"), "name": "Qwen3"},
                                 {**local_tier("qwen3-2", base, model="Qwen3"), "name": "Qwen3 (2)"}]}

    def test_two_rungs_with_the_same_address_and_model_save(self):
        saved = settings.save(self.config)
        self.assertEqual([(t["id"], t["name"]) for t in saved["tiers"]], [("qwen3", "Qwen3"), ("qwen3-2", "Qwen3 (2)")])
        self.assertEqual(settings.load()["tiers"], saved["tiers"])

    def test_the_same_id_twice_is_still_refused(self):
        same = {"tiers": [self.config["tiers"][0], self.config["tiers"][0]]}
        with self.assertRaisesRegex(ValueError, "unique"):
            settings.validate(same)

    def test_they_run_in_order_and_the_second_acts_as_a_retry(self):
        tiers = eco_core.build_tiers(settings.save(self.config), allow_cloud=False)
        self.assertEqual([t.name for t in tiers], ["qwen3", "qwen3-2"])
        task = eco_core.make_custom_task("Add two numbers.", ENTRY, CHECKS)
        result = eco_core.solve(task["prompt"], None, ENTRY, tiers, log_path=self.log, allow_cloud=False, tests_code=CHECKS)
        self.assertEqual(len(self.server.prompts), 2)
        self.assertEqual([(a["tier"], a["passed"]) for a in result["attempts"]], [("qwen3", False), ("qwen3-2", True)])
        self.assertEqual(result["solved_by"], "qwen3-2")
        with open(self.log, encoding="utf-8") as f:
            logged = json.loads(f.read())
        self.assertEqual([a["tier"] for a in logged["attempts"]], ["qwen3", "qwen3-2"])


class ServerRoutes(ServerCase):
    def post(self, path, body, **headers):
        status, text = self.request(path, body, headers or None)
        return status, json.loads(text) if text else None

    def full(self, **extra):
        return {"tiers": [local_tier("box", "http://192.168.1.50:8080/v1", color="#1B69AE")],
                "budget": {"limit_usd": 3, "period": "month"}, **extra}

    def test_the_view_carries_the_colour_and_mode(self):
        self.start()
        self.assertEqual(json.loads(self.request("/api/settings")[1])["color_mode"], "default")
        status, view = self.post("/api/settings", self.full(color_mode="custom"))
        self.assertEqual((status, view["color_mode"], view["tiers"][0]["color"]), (200, "custom", "#1B69AE"))

    def test_saving_other_things_keeps_the_mode(self):
        self.start()
        self.post("/api/settings", self.full(color_mode="custom"))
        for change in ({"tiers": self.full()["tiers"]}, {"budget": {"limit_usd": 1, "period": "month"}}, self.full()):
            status, view = self.post("/api/settings", change)
            self.assertEqual((status, view["color_mode"]), (200, "custom"), change)

    def test_a_bad_colour_through_the_api_changes_nothing(self):
        self.start()
        bad = {"tiers": [local_tier("box", "http://h:1/v1", color="red;background:url(x)")]}
        status, body = self.post("/api/settings", bad)
        self.assertEqual(status, 400)
        self.assertIn("color must look like", body["error"])
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "settings.json")))
        self.assertEqual(self.post("/api/settings", {"color_mode": "red"})[0], 400)

    def test_a_full_save_can_still_replace_a_file_that_no_longer_loads(self):
        self.start()
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8") as f:
            f.write("not json{")
        status, view = self.post("/api/settings", self.full())
        self.assertEqual((status, view["color_mode"]), (200, "default"))

    def test_the_rung_check_uses_the_settings_rules_and_saves_nothing(self):
        self.start()
        status, body = self.post("/api/settings/check", {"tier": local_tier("box", "http://h:1/v1/")})
        self.assertEqual((status, body["tier"]["base_url"]), (200, "http://h:1/v1"))
        for tier, why in ((local_tier("box", "http://h:1"), "base_url"), (local_tier("box", "ftp://h:1/v1"), "base_url"),
                          (local_tier("box", "http://u:p@h:1/v1"), "base_url"), (local_tier("box", "http://h:1/v1", name=""), "name"),
                          (local_tier("box", "http://h:1/v1", model=""), "model"), (local_tier("box", "http://h:1/v1", key_name="Mine"), "key_name")):
            status, body = self.post("/api/settings/check", {"tier": tier})
            self.assertEqual(status, 400, tier)
            self.assertIn(why, body["error"])
        self.assertEqual(self.post("/api/settings/check", {"tier": {}, "extra": 1})[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "settings.json")))

    def test_the_rung_check_refuses_other_sites(self):
        self.start()
        body = {"tier": local_tier("box", "http://h:1/v1")}
        self.assertEqual(self.request("/api/settings/check", body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/settings/check", body, {"Content-Type": "text/plain"})[0], 415)


def contrast_on_white(hex_color):
    def channel(i):
        c = int(hex_color[i:i + 2], 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 1.05 / (0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5) + 0.05)


class PageRules(unittest.TestCase):
    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        self.script = self.page.split("<script>")[1]
        block = re.search(r"const SWATCHES = (\[.*?\]\]);", self.page, re.S).group(1)
        self.swatches = json.loads(block)

    def test_twelve_swatches_that_pass_the_guard(self):
        hexes = [c for _, c in self.swatches]
        self.assertEqual((len(hexes), len(set(hexes))), (12, 12))
        for name, c in self.swatches:
            self.assertTrue(settings.COLOR_RE.fullmatch(c), name)
            self.assertGreaterEqual(contrast_on_white(c), 3, name)
        self.assertNotIn("#3F4A5A", hexes, "slate belongs to the memory rung")

    def test_colours_reach_the_page_only_as_custom_properties(self):
        self.assertEqual(re.findall(r"background:\s*\$\{", self.script), [])
        self.assertEqual(re.findall(r"\.style\.(?:background|color|border)", self.script), [])
        self.assertIn("function validHex(c)", self.script)
        self.assertIn('/^#[0-9A-Fa-f]{6}$/.test(c)', self.script)
        self.assertIn("mode === \"custom\" && validHex(t.color)", self.script)

    def test_the_memory_rung_stays_slate(self):
        self.assertIn('k === "memory" ? HEX.slate', self.script)
        self.assertRegex(self.page, r"--c: var\(--slate\)")

    def test_one_row_of_provider_cards(self):
        self.assertEqual(re.findall(r'name="prov" value="(\w+)"><b>(\w+)</b>', self.page),
                         [("meta", "Meta"), ("openai", "OpenAI"), ("claude", "Claude"), ("gemini", "Gemini"), ("custom", "Custom")])
        self.assertNotIn("Fill in for a provider", self.page)
        self.assertNotIn("data-preset", self.page)
        self.assertEqual(set(page_presets()), {"openai", "claude", "gemini"})
        self.assertIn('data-info="provider"', self.page)

    def test_the_old_name_is_only_in_customs_popover(self):
        self.assertEqual(self.page.count("OpenAI-compatible"), 1)
        line = next(l for l in self.page.splitlines() if "OpenAI-compatible" in l)
        self.assertTrue(line.strip().startswith('custom: ["About Custom"'), line)

    def test_stored_labels_keep_their_values_under_the_new_names(self):
        select = re.search(r'<select id="keyProvider">(.*?)</select>', self.page).group(1)
        self.assertEqual(re.findall(r'<option value="(.*?)">(.*?)</option>', select),
                         [("Meta", "Meta"), ("OpenAI", "OpenAI"), ("Claude (Anthropic)", "Claude"), ("Gemini (Google)", "Gemini"), ("Other", "Custom")])

    def test_the_budget_and_keys_share_the_right_column(self):
        cloud = self.page.split('id="screen-cloud"')[1].split('id="screen-about"')[0]
        columns = re.split(r'<div class="stack">', cloud)[1:]
        self.assertEqual(len(columns), 2)
        self.assertIn('id="provTitle"', columns[0])
        self.assertNotIn('id="keyTitle"', columns[0])
        self.assertLess(columns[1].index('id="budgetTitle"'), columns[1].index('id="keyTitle"'))


if __name__ == "__main__":
    unittest.main()
