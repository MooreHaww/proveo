"""Tests for the cloud rungs list, disabled keys, the theme setting and the Running status.

Fake model servers on random 127.0.0.1 ports, made-up keys and a temp ECO_DATA_DIR only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_controls
"""
import json
import os
import re
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import detect
import eco_core
import settings
from test_eco_presets import PAGE
from test_eco_rungs import CHECKS, ENTRY, RetryServer
from test_eco_server import ServerCase
from test_eco_settings import TempDataDir, local_tier


def cloud_tier(tid, base_url, **extra):
    return {"id": tid, "name": tid, "kind": "cloud", "provider": "openai_compatible", "base_url": base_url, "model": "fake",
            "flags": {}, "enabled": True, "price_in_per_m": 1.0, "price_out_per_m": 1.0, **extra}


class DisabledRungs(TempDataDir):
    def setUp(self):
        super().setUp()
        self.local, self.cloud = RetryServer(), RetryServer()
        self.addCleanup(self.local.close)
        self.addCleanup(self.cloud.close)

    def config(self, enabled):
        return {"tiers": [local_tier("box", self.local.root + "/v1"),
                          cloud_tier("rung", self.cloud.root + "/v1", key_name="Made up", enabled=enabled)]}

    def test_a_disabled_cloud_rung_is_skipped_and_never_called(self):
        keys = {"Made up": "made-up-controls-key-0001"}
        on = eco_core.build_tiers(settings.save(self.config(True)), keys=keys, allow_cloud=True)
        self.assertEqual([t.name for t in on], ["box", "rung"], "enabled, it is in the ladder")
        off = eco_core.build_tiers(settings.save(self.config(False)), keys=keys, allow_cloud=True)
        self.assertEqual([t.name for t in off], ["box"])
        task = eco_core.make_custom_task("Add two numbers.", ENTRY, CHECKS)
        result = eco_core.solve(task["prompt"], None, ENTRY, off, log_path=self.log, allow_cloud=True, tests_code=CHECKS)
        self.assertEqual([a["tier"] for a in result["attempts"]], ["box"])
        self.assertEqual(self.cloud.prompts, [])

    def test_it_stays_in_the_saved_list(self):
        saved = settings.save(self.config(False))
        self.assertEqual([(t["id"], t["enabled"]) for t in saved["tiers"]], [("box", True), ("rung", False)])


META = {"id": "m", "name": "Meta rung", "kind": "cloud", "provider": "meta", "base_url": "https://api.meta.ai/v1", "model": "muse-spark-1.3"}
NAMED = {"id": "n", "name": "Named rung", "kind": "cloud", "provider": "openai_compatible", "base_url": "https://example.test/v1", "model": "x"}
KEY_A, KEY_B = "made-up-controls-key-AAAA", "made-up-controls-key-BBBB"


class DisabledKeyRouting(unittest.TestCase):
    def route(self, cfg, keys, providers, disabled=()):
        return eco_core.key_name_for_tier(cfg, keys, providers, disabled)

    def test_a_named_disabled_key_waits_and_never_falls_back(self):
        keys, labels = {"One": KEY_A, "Two": KEY_B}, {"One": "OpenAI", "Two": "OpenAI"}
        cfg = {**NAMED, "key_name": "One"}
        self.assertEqual(self.route(cfg, keys, labels), ("One", ""))
        self.assertEqual(self.route(cfg, keys, labels, {"one"}), (None, "Key disabled"), "ignoring case, and not Two")

    def test_a_disabled_meta_key_does_not_count_for_the_only_key_rule(self):
        labels = {"One": "Meta", "Two": "Meta"}
        self.assertEqual(self.route(META, {"One": KEY_A}, labels), ("One", ""))
        self.assertEqual(self.route(META, {"One": KEY_A}, labels, {"One"}), (None, "Key disabled"))
        both = {"One": KEY_A, "Two": KEY_B}
        self.assertEqual(self.route(META, both, labels), (None, "Choose a key"))
        self.assertEqual(self.route(META, both, labels, {"One", "Two"}), (None, "Key disabled"))
        self.assertEqual(self.route(META, both, labels, {"Two"}), ("One", ""), "the other is now the only usable Meta key")

    def test_the_exception_is_still_only_for_the_default_meta_rung(self):
        self.assertEqual(self.route(NAMED, {"One": KEY_A}, {"One": "Meta"}, {"One"}), (None, "Choose a key"))

    def test_the_meta_host_refusal_comes_first(self):
        cfg = {**META, "key_name": "One"}
        name, reason = self.route(cfg, {"One": KEY_A}, {"One": "OpenAI"}, {"One"})
        self.assertIsNone(name)
        self.assertTrue(reason.startswith(eco_core.META_KEY_REFUSAL), reason)

    def test_it_counts_as_a_wait_and_the_key_stays_out_of_the_tier(self):
        self.assertTrue(eco_core.is_key_wait("Key disabled"))
        config = {"tiers": [{**NAMED, "key_name": "One", "flags": {}, "enabled": True, "price_in_per_m": 1, "price_out_per_m": 1}]}
        tier = eco_core.build_tiers(config, keys={"One": KEY_A}, allow_cloud=True, providers={"One": "OpenAI"}, disabled={"One"})[0]
        self.assertEqual(tier.wait_reason, "Key disabled")


class DisabledKeyRoutes(ServerCase):
    def post(self, path, body):
        status, text = self.request(path, body)
        self.assertNotIn(KEY_A, text)
        self.assertNotIn(KEY_B, text)
        return status, json.loads(text)

    def ladder(self):
        self.post("/api/settings", {"tiers": [{**META, "flags": {}, "enabled": True}], "budget": {"limit_usd": 5, "period": "month"}})

    def test_status_carries_the_flag_and_never_the_key(self):
        self.start()
        self.ladder()
        self.post("/api/key", {"name": "One", "key": KEY_A, "provider": "Meta"})
        status, body = self.post("/api/key/disable", {"name": "one", "disabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["keys"], [{"name": "One", "provider": "Meta", "disabled": True, "rungs": ["Meta rung"]}])
        _, text = self.request("/api/key/status")
        self.assertEqual(json.loads(text), body)
        self.assertNotIn(KEY_A, text)
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual((view["key_disabled"], view["rung_keys"]["m"]), (["One"], {"key_name": None, "reason": "Key disabled"}))
        self.assertEqual(view["key_names"], ["One"], "it stays stored")
        status, body = self.post("/api/key/disable", {"name": "One", "disabled": False})
        self.assertEqual((status, body["keys"][0]["disabled"]), (200, False))
        self.assertEqual(json.loads(self.request("/api/settings")[1])["rung_keys"]["m"], {"key_name": "One", "reason": ""})

    def test_bad_requests_change_nothing(self):
        self.start()
        self.post("/api/key", {"name": "One", "key": KEY_A})
        for body in ({"name": "Gone", "disabled": True}, {"name": "One"}, {"name": "One", "disabled": "yes"},
                     {"name": 1, "disabled": True}, {"name": "One", "disabled": True, "key": KEY_A}):
            self.assertEqual(self.post("/api/key/disable", body)[0], 400, body)
        self.assertFalse(json.loads(self.request("/api/key/status")[1])["keys"][0]["disabled"])

    def test_restoring_with_the_flag_is_one_request_and_replacing_keeps_it(self):
        self.start()
        _, body = self.post("/api/key", {"name": "One", "key": KEY_A, "provider": "Meta", "disabled": True})
        self.assertTrue(body["keys"][0]["disabled"])
        _, body = self.post("/api/key", {"name": "ONE", "key": KEY_B, "provider": "Meta"})
        self.assertEqual((body["keys"][0]["name"], body["keys"][0]["disabled"]), ("ONE", True))
        _, body = self.post("/api/key", {"name": "ONE", "key": KEY_B, "disabled": False})
        self.assertFalse(body["keys"][0]["disabled"])
        self.assertEqual(self.post("/api/key", {"name": "ONE", "key": KEY_B, "disabled": "no"})[0], 400)

    def test_removing_a_key_clears_the_flag(self):
        self.start()
        self.post("/api/key", {"name": "One", "key": KEY_A, "disabled": True})
        self.post("/api/key/remove", {"name": "One"})
        _, body = self.post("/api/key", {"name": "One", "key": KEY_A})
        self.assertFalse(body["keys"][0]["disabled"])

    def test_a_run_never_calls_the_rung_and_nothing_logs_the_key(self):
        cloud = RetryServer()
        self.addCleanup(cloud.close)
        self.start(allow_cloud=True)
        rung = cloud_tier("c", cloud.root + "/v1", key_name="One")
        self.post("/api/settings", {"tiers": [rung], "budget": {"limit_usd": 5, "period": "month"}})
        self.post("/api/key", {"name": "One", "key": KEY_A, "provider": "Other", "disabled": True})
        self.post("/api/key", {"name": "Two", "key": KEY_B, "provider": "Other"})
        _, job = self.post("/api/solve", {"custom": {"prompt": "Add two numbers.", "entry": ENTRY, "tests_code": CHECKS}})
        state = self.wait_for(job["job_id"], lambda s: s["status"] != "running")
        self.assertEqual(state["skip_reasons"], {"c": "Key disabled"})
        self.assertEqual(cloud.prompts, [], "no call, and no other key was used")
        self.assertNotIn(KEY_A, json.dumps(state))
        with open(self.log, encoding="utf-8") as f:
            logged = f.read()
        self.assertTrue(logged)
        self.assertNotIn(KEY_A, logged)
        self.assertNotIn(KEY_B, logged)
        with open(settings.settings_path(), encoding="utf-8") as f:
            self.assertNotIn(KEY_A, f.read())


class Theme(ServerCase):
    def test_none_until_chosen_then_remembered(self):
        self.start()
        self.assertIsNone(json.loads(self.request("/api/config")[1])["theme"])
        for theme in ("dark", "light", "dark"):
            status, text = self.request("/api/theme", {"theme": theme})
            self.assertEqual((status, json.loads(text)), (200, {"theme": theme}))
        self.assertEqual(json.loads(self.request("/api/config")[1])["theme"], "dark")
        self.assertEqual(settings.load_theme(), "dark")

    def test_bad_requests_change_nothing_and_the_settings_stay_unsaved(self):
        self.start()
        self.request("/api/theme", {"theme": "dark"})
        for body in ({"theme": "blue"}, {"theme": 1}, {}, {"theme": "light", "extra": 1}):
            self.assertEqual(self.request("/api/theme", body)[0], 400, body)
        self.assertEqual(settings.load_theme(), "dark")
        self.assertFalse(json.loads(self.request("/api/settings")[1])["saved"], "the first-run card still shows")
        self.assertFalse(os.path.exists(settings.settings_path()))

    def test_a_broken_file_means_no_choice(self):
        self.start()
        settings.save_theme("dark")
        for broken in ("not json", "[1]", '{"theme": "blue"}'):
            with open(settings.ui_path(), "w", encoding="utf-8") as f:
                f.write(broken)
            self.assertIsNone(settings.load_theme(), broken)
            self.assertEqual(self.request("/api/config")[0], 200)


class FakeStatus:
    """A fake local server that answers /v1/models in the chosen way and records every request."""

    def __init__(self, mode="up"):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if mode == "slow":
                    time.sleep(3)
                    return
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/v1/models")
                    self.end_headers()
                    return
                body = {"up": b'{"data": [{"id": "fake-model"}]}', "junk": b"<html>hello</html>", "wrong": b'{"status": "ok"}'}[mode]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RunningStatus(ServerCase):
    def fake(self, mode):
        server = FakeStatus(mode)
        self.addCleanup(server.close)
        return server

    def test_rung_running_says_yes_only_for_a_model_list(self):
        up = self.fake("up")
        self.assertTrue(detect.rung_running(up.base))
        self.assertTrue(detect.rung_running(up.base[:-3]), "the /v1 is implied")
        self.assertEqual([path for path, _ in up.requests], ["/v1/models", "/v1/models"])
        for mode in ("junk", "wrong", "redirect"):
            self.assertFalse(detect.rung_running(self.fake(mode).base), mode)
        self.assertFalse(detect.rung_running(f"http://127.0.0.1:{closed_port()}/v1"))

    def test_slow_past_the_limit_is_not_running(self):
        self.assertEqual(detect.RUNNING_TIMEOUT, 1.5)
        began = time.monotonic()
        self.assertFalse(detect.rung_running(self.fake("slow").base, timeout=0.2))
        self.assertLess(time.monotonic() - began, 2)

    def test_the_route_checks_local_rungs_only_and_sends_no_key(self):
        up, cloud = self.fake("up"), self.fake("up")
        self.start()
        self.request("/api/key", {"name": "Made up", "key": KEY_A})
        tiers = [local_tier("up", up.base), local_tier("down", f"http://127.0.0.1:{closed_port()}/v1"),
                 {**local_tier("off", up.base), "enabled": False}, cloud_tier("c", cloud.base, key_name="Made up")]
        self.assertEqual(self.request("/api/settings", {"tiers": tiers})[0], 200)
        status, text = self.request("/api/rungs/status")
        self.assertEqual((status, json.loads(text)), (200, {"rungs": [{"id": "up", "running": True}, {"id": "down", "running": False},
                                                                     {"id": "off", "running": True}]}))
        self.assertEqual(cloud.requests, [], "a cloud rung is never contacted")
        self.assertEqual(set(up.requests), {("/v1/models", None)}, "only the list, and no Authorization header")

    def test_many_slow_rungs_take_about_as_long_as_one(self):
        slow = self.fake("slow")
        self.start()
        self.assertEqual(self.request("/api/settings", {"tiers": [local_tier(f"r{i}", slow.base) for i in range(8)]})[0], 200)
        began = time.monotonic()
        status, text = self.request("/api/rungs/status")
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertEqual((status, [r["running"] for r in json.loads(text)["rungs"]]), (200, [False] * 8))

    def test_it_changes_nothing_on_disk(self):
        self.start()
        self.assertEqual(self.request("/api/rungs/status")[0], 200)
        self.assertFalse(os.path.exists(settings.settings_path()))


class PageRules(unittest.TestCase):
    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        self.script = self.page.split("<script>")[1]

    def test_the_rungs_card_is_in_the_left_column_with_the_provider(self):
        left, right = self.page.split('<div class="grid2 cloud">')[1].split('<div class="stack">')[1:3]
        self.assertIn('id="provTitle"', left)
        self.assertIn('id="rungsTitle">Your cloud rungs<', left)
        self.assertIn('id="budgetTitle"', right)
        self.assertIn('id="keyTitle"', right)

    def test_the_keys_card_lists_only_keys(self):
        card = self.page.split('aria-labelledby="keyTitle"')[1].split("</section>")[0]
        self.assertEqual(re.findall(r'<ul [^>]*id="(\w+)"', card), ["keyList"])
        self.assertNotIn("rungWaits", self.page)

    def test_remove_asks_inline_and_the_list_says_disabled_in_words(self):
        body = self.script.split("function renderCloudRungs")[1].split("async function editCloudRungs")[0]
        for words in ('"Remove?"', '"Yes"', '"No"', '"Disabled"', '"Already added"', '"Key set"'):
            self.assertIn(words, body)

    def test_identical_means_address_model_and_effort(self):
        sig = re.search(r"const rungSignature = .*", self.script).group(0)
        for part in ("endpointKey(t.base_url)", "t.model", "flags.effort"):
            self.assertIn(part, sig)

    def test_models_has_a_worded_button_not_a_switch(self):
        body = self.script.split("function renderRungList")[1].split("function dirtyFlag")[0]
        self.assertIn('powerBtn(t.enabled, t.name', body)
        self.assertNotIn('"switch small"', body)
        self.assertIn('h("div", { class: "acts-row" }, swatch, up, down, rm), toggle', body)
        self.assertIn('on ? "Disable" : "Enable"', self.script)

    def test_running_is_checked_on_open_and_refresh_only(self):
        self.assertEqual(len(re.findall(r"(?<!function )checkRungs\(\)", self.script)), 2, "opening Models, and a direct load of it")
        self.assertIn('$("refreshRungs").addEventListener("click", checkRungs)', self.script)
        self.assertEqual(re.findall(r"setInterval\([^\n]*(?:checkRungs|rungs/status)", self.script), [])
        for word in ('"Running"', '"Not running"', '"Checking"'):
            self.assertIn(word, self.script)
        self.assertEqual(self.script.count('"/api/rungs/status"'), 1)

    def test_the_start_command_follows_the_port_and_there_is_no_start_button(self):
        body = self.script.split("function startCommand")[1].split("function paintRunState")[0]
        for words in ('"ollama serve"', '"lms server start"', "llama-server -m <model file> -c 4096", '"Copy"'):
            self.assertIn(words, body)
        self.assertNotIn('"Start"', self.script)

    def test_the_start_command_opens_the_network_only_for_another_machine(self):
        body = self.script.split("function startCommand")[1].split("function openStartHelp")[0]
        self.assertEqual(body.count("0.0.0.0"), 1)
        self.assertIn('${onThisComputer(t) ? "" : " --host 0.0.0.0"} --port ${port}', body)

    def test_the_unreadable_keys_notice_is_hidden_unless_the_desktop_app_asks(self):
        self.assertIn('id="keyNotice" role="status" hidden', self.page)
        card = self.page.split('aria-labelledby="keyTitle"')[1].split("</section>")[0]
        self.assertIn("<b>Stored keys could not be read.</b> Add them again.", card)
        self.assertIn('data-info="keyfile"', card)
        self.assertIn('const keysProblem = desktop() ? new URLSearchParams(location.search).get("keys") : null;', self.script)
        self.assertIn('if (keysProblem === "aside" || keysProblem === "kept")', self.script)

    def test_the_added_marks_do_not_hide_the_other_labels(self):
        self.assertIn('document.querySelectorAll(".added[data-key]")', self.script)
        self.assertNotIn('document.querySelectorAll(".added")', self.script)

    def test_no_state_fades_its_text_with_opacity(self):
        style = self.page.split("<style>")[1].split("</style>")[0]
        self.assertEqual(re.findall(r"opacity: *\.?[1-9][^;]*;", style), [], "opacity would take text under its contrast")

    def test_rung_cards_and_the_answer_card_share_one_wording_for_a_failed_check(self):
        self.assertEqual(self.script.count("tests line"), 2, "only inside plainFailure's two patterns")
        helper = self.script.split("function plainFailure(text)")[1].split("function askProblem")[0]
        self.assertEqual(helper.count("tests line"), 2)
        self.assertIn('plainFailure(a.first_failure || a.error || "Tests failed")', self.script)
        self.assertIn('$("answerFail").textContent = a && !passed ? plainFailure(a.first_failure) : "";', self.script)
        self.assertNotIn("`Failed check: ${plainFailure", self.script, "the helper says it once")

    def test_a_rung_that_cannot_be_reached_has_plain_words_and_the_raw_text_behind_an_info_button(self):
        self.assertIn('setStep(t.id, "fail", problem[0], problem[1], [problem[2], rawErrorButton(a.error)], a)', self.script)
        self.assertIn('askProblem(a.error, t.kind === "local", host(t.base_url))', self.script)
        button = self.script.split("function rawErrorButton(error)")[1].split("function setStep")[0]
        self.assertIn('class: "info"', button)
        self.assertIn('"aria-label": "Technical detail"', button)
        self.assertIn('h("pre", { class: "codeblock" }, h("code", null, raw))', self.script, "text only, never markup")

    def test_the_slider_value_sits_beside_the_slider_in_a_fixed_box_that_cannot_wrap(self):
        calc = self.page.split('<div class="calc">')[1].split('<div class="result">')[0]
        self.assertNotIn("shareVal", calc.split("</label>", 2)[1].split('<div class="share-row">')[0])
        row = calc.split('<div class="share-row">')[1]
        self.assertLess(row.index('id="share"'), row.index('id="shareVal"'))
        self.assertIn('<span class="share-val num" id="shareVal">', row, "tabular numbers")
        self.assertNotIn("shareVal", calc.split('for="share"')[1].split("</label>")[0], "not in the label")
        style = self.page.split("<style>")[1].split("</style>")[0]
        rule = re.search(r"\.share-val \{([^}]*)\}", style).group(1)
        self.assertRegex(rule, r"(^|[; ])width: *[\d.]+em")
        self.assertIn("flex: none", rule)
        self.assertIn("white-space: nowrap", rule)
        same = re.search(r"\.share-same \{([^}]*)\}", style).group(1)
        self.assertRegex(same, r"(^|[; ])width: *[\d.]+em")
        self.assertIn("white-space: nowrap", same)
        self.assertIn('h("span", { class: "share-same num" }, shareText)', self.script, "the sentence beside it stays put too")
        self.assertIn(".result .big small { display: block; }", style, "and the saving keeps its two lines")


if __name__ == "__main__":
    unittest.main()
