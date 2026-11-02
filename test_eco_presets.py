"""Tests for what the provider presets need: request flags, base URLs, cut-off answers, the page's presets.

Fake model servers on random 127.0.0.1 ports, made-up keys and temp folders only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_presets
"""
import inspect
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import bench_coding
import detect
import eco_core
import settings
from test_eco_server import GOOD, BAD
from test_eco_settings import FakeModelServer, local_tier

KEY = "made-up-preset-key-0123456789"
PROMPT = "Write add(a, b) that returns the sum."
TESTS = [[[1, 2], 3], [[5, 5], 10]]
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")


def cloud(base_url, **extra):
    return {"id": "c", "name": "C", "kind": "cloud", "provider": "openai_compatible", "base_url": base_url, "model": "m",
            "flags": {}, "enabled": True, "price_in_per_m": 1.0, "price_out_per_m": 1.0, **extra}


def page_presets():
    """The page's PRESETS block. It is plain JSON apart from unquoted keys."""
    with open(PAGE, encoding="utf-8") as f:
        block = re.search(r"const PRESETS = (\{.*?\n  \});", f.read(), re.S).group(1)
    return json.loads(re.sub(r"^(\s*)([a-zA-Z_]+):", r'\1"\2":', block, flags=re.M))


class RawServer:
    """Answers every POST with a fixed JSON body, to imitate replies FakeModelServer can't."""

    def __init__(self, body):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                data = json.dumps(body).encode()
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


class BaseUrlRule(unittest.TestCase):
    def errors(self, tier):
        try:
            settings.validate({"tiers": [tier]})
            return ""
        except ValueError as e:
            return str(e)

    def test_documented_provider_urls_are_accepted_and_saved_without_the_slash(self):
        for url in ("https://api.openai.com/v1", "https://api.anthropic.com/v1/",
                    "https://generativelanguage.googleapis.com/v1beta/openai/"):
            self.assertEqual(self.errors(cloud(url)), "", url)
        clean = settings.validate({"tiers": [cloud("https://generativelanguage.googleapis.com/v1beta/openai/")]})
        self.assertEqual(clean["tiers"][0]["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")

    def test_openai_compatible_cloud_still_refuses_bad_urls(self):
        for url in ("https://example.com", "https://example.com/openai", "https://example.com/version1/x",
                    "file:///etc/v1", "ftp://example.com/v1", "https://user:pw@example.com/v1",
                    "https://example.com/v1?key=abc", "https://example.com/v1#x", "https://example.com/av1", "/v1"):
            self.assertIn("base_url must be", self.errors(cloud(url)), url)

    def test_local_and_meta_tiers_keep_the_strict_rule(self):
        self.assertIn("ending in /v1", self.errors(local_tier("l", "http://192.168.1.5:8080/v1beta/openai")))
        meta = {"id": "m", "name": "M", "kind": "cloud", "provider": "meta", "base_url": "https://api.meta.ai/v1beta",
                "model": "m", "flags": {}, "enabled": True}
        self.assertIn("ending in /v1", self.errors(meta))


class RequestFlags(unittest.TestCase):
    def server(self, reply=GOOD):
        srv = FakeModelServer(reply=reply)
        self.addCleanup(srv.close)
        return srv

    def ask(self, srv, **flags):
        tier = eco_core.tier_from_config(cloud(srv.root + "/v1", flags=flags), key=KEY, allow_cloud=True)
        return tier.ask(PROMPT), srv.requests[-1]

    def test_default_request_is_unchanged(self):
        _, req = self.ask(self.server())
        self.assertEqual(req["body"], {"model": "m", "messages": [{"role": "user", "content": PROMPT}],
                                       "max_tokens": eco_core.CLOUD_MAX_TOKENS, "temperature": 0})

    def test_omit_temperature(self):
        reply, req = self.ask(self.server(), omit_temperature=True)
        self.assertNotIn("temperature", req["body"])
        self.assertEqual(req["body"]["max_tokens"], eco_core.CLOUD_MAX_TOKENS)
        self.assertEqual((reply["in_tokens"], reply["out_tokens"]), (12, 34))

    def test_max_completion_tokens_replaces_max_tokens(self):
        _, req = self.ask(self.server(), max_tokens_field="max_completion_tokens")
        self.assertNotIn("max_tokens", req["body"])
        self.assertEqual(req["body"]["max_completion_tokens"], eco_core.CLOUD_MAX_TOKENS)
        self.assertEqual(req["body"]["temperature"], 0)

    def test_the_cloud_limit_leaves_room_for_reasoning(self):
        _, req = self.ask(self.server())
        self.assertEqual(req["body"]["max_tokens"], 25_000)

    def test_the_limit_does_not_change_meta_or_local_rungs(self):
        with open(bench_coding.__file__, encoding="utf-8") as f:
            self.assertIn('"max_output_tokens": 3000', f.read())
        self.assertEqual(eco_core.LOCAL_MAX_TOKENS, 500)

    def test_local_tier_requests_are_unchanged(self):
        srv = self.server()
        eco_core.tier_from_config(local_tier("l", srv.root + "/v1")).ask(PROMPT)
        self.assertEqual(srv.requests[-1]["body"], {"model": "m", "messages": [{"role": "user", "content": PROMPT}],
                                                    "max_tokens": eco_core.LOCAL_MAX_TOKENS, "temperature": 0})

    def test_the_flags_are_validated_and_only_for_openai_compatible_cloud(self):
        ok = settings.validate({"tiers": [cloud("https://x.example/v1", flags={"omit_temperature": True,
                                                                               "max_tokens_field": "max_completion_tokens"})]})
        self.assertEqual(ok["tiers"][0]["flags"], {"max_tokens_field": "max_completion_tokens", "omit_temperature": True})
        for flags in ({"omit_temperature": "yes"}, {"max_tokens_field": "tokens"}, {"max_tokens_field": 5}):
            with self.assertRaises(ValueError, msg=flags):
                settings.validate({"tiers": [cloud("https://x.example/v1", flags=flags)]})
        with self.assertRaisesRegex(ValueError, "only apply to openai_compatible cloud"):
            settings.validate({"tiers": [local_tier("l", "http://h:1/v1", flags={"omit_temperature": True})]})
        with self.assertRaisesRegex(ValueError, "only apply to openai_compatible cloud"):
            settings.validate({"tiers": [{"id": "m", "name": "M", "kind": "cloud", "provider": "meta", "model": "m",
                                          "base_url": "https://api.meta.ai/v1", "enabled": True,
                                          "flags": {"max_tokens_field": "max_tokens"}}]})


class CutOffAnswers(unittest.TestCase):
    """Reasoning tokens count against the limit, so a model can stop before it answers. It still costs money."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "log.jsonl")
        patcher = mock.patch.dict(os.environ, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MODEL_API_KEY", None)

    def solve(self, body):
        srv = RawServer(body)
        self.addCleanup(srv.close)
        tier = eco_core.tier_from_config(cloud(srv.root + "/v1"), key=KEY, allow_cloud=True)
        return eco_core.solve(PROMPT, TESTS, "add", [tier], log_path=self.log, allow_cloud=True)

    def test_an_empty_answer_at_the_limit_fails_with_a_clear_reason_and_counts_its_cost(self):
        result = self.solve({"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": 1500}})
        attempt = result["attempts"][0]
        self.assertFalse(attempt["passed"])
        self.assertEqual(attempt["first_failure"], f"the answer was cut off at the {eco_core.CLOUD_MAX_TOKENS}-token limit")
        self.assertEqual(attempt["out_tokens"], 1500)
        self.assertAlmostEqual(attempt["cost_usd"], 2500 / 1e6, msg="the spend reaches the budget")

    def test_a_cut_off_answer_that_still_passes_is_a_pass(self):
        result = self.solve({"choices": [{"message": {"content": GOOD}, "finish_reason": "length"}], "usage": {}})
        self.assertEqual(result["solved_by"], "c")
        self.assertNotIn("first_failure", result["attempts"][0])

    def test_a_normal_wrong_answer_keeps_its_reason(self):
        result = self.solve({"choices": [{"message": {"content": BAD}, "finish_reason": "stop"}], "usage": {}})
        self.assertIn("tests passed", result["attempts"][0]["first_failure"])


def page_examples():
    """The page's EXAMPLES block, read the same way as the presets."""
    with open(PAGE, encoding="utf-8") as f:
        block = re.search(r"const EXAMPLES = (\{.*?\n  \});", f.read(), re.S).group(1)
    return json.loads(re.sub(r"^(\s*)([a-zA-Z_]+):", r'\1"\2":', block, flags=re.M))


EXAMPLE_ANSWERS = {
    "palindrome": ("def is_palindrome(s):\n    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]\n",
                   "def is_palindrome(s):\n    return s == s[::-1]\n"),
    "fizzbuzz": ("def fizzbuzz(n):\n    out = []\n    for i in range(1, n + 1):\n"
                 "        out.append('Fizz' * (i % 3 == 0) + 'Buzz' * (i % 5 == 0) or str(i))\n    return out\n",
                 "def fizzbuzz(n):\n    return [str(i) for i in range(1, n + 1)]\n"),
    "merge": ("def merge_intervals(intervals):\n    out = []\n    for a, b in sorted(intervals):\n"
              "        if out and a <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], b)\n"
              "        else:\n            out.append([a, b])\n    return out\n",
              "def merge_intervals(intervals):\n    return intervals\n"),
}


class CodeTaskExamples(unittest.TestCase):
    """Each 'Try an example' is complete: the backend accepts it, and the real checker tells right from wrong."""

    def setUp(self):
        self.examples = page_examples()

    def task(self, example):
        # The same text the page sends: see codeTaskPrompt and checksToCode (pinned by a Node test).
        tests = "\n".join(f"assert {call} == {want}" for call, want in example["checks"])
        return eco_core.make_custom_task(f"Write a Python function named {example['entry']}. {example['prompt']}", example["entry"], tests)

    def test_the_three_examples(self):
        self.assertEqual(set(self.examples), set(EXAMPLE_ANSWERS))

    def test_a_correct_solution_passes_and_a_wrong_one_fails_in_the_real_checker(self):
        for name, example in self.examples.items():
            task = self.task(example)
            self.assertIn(f"named {example['entry']}.", task["prompt"], name)
            right, wrong = EXAMPLE_ANSWERS[name]
            result = bench_coding.run_custom_tests(right, task["tests_code"])
            self.assertTrue(result["passed"], f"{name}: {result}")
            self.assertFalse(bench_coding.run_custom_tests(wrong, task["tests_code"])["passed"], name)


class PageCopy(unittest.TestCase):
    """The page stays short: explanations live behind info buttons, and the honest ones stay reachable."""

    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        self.info = re.search(r"const INFO = \{(.*?)\n  \};", self.page, re.S).group(1)

    def test_no_eyebrows_badges_or_old_wording(self):
        lower = self.page.lower()
        for gone in ("rung 0", "budget this month", "your own task", "untested", "(tested)", "<footer", "side-foot", "—"):
            self.assertNotIn(gone, lower)
        self.assertEqual(re.findall(r"text-transform: uppercase[^}]*", self.page.split(".metrics div")[0]), [], "only metrics and status tags")
        self.assertIn(">Code task</button>", self.page)

    def test_every_info_button_has_text_and_the_required_ones_exist(self):
        used = set(re.findall(r'data-info="([a-z]+)"', self.page))
        defined = set(re.findall(r"^\s*([a-z]+): \[", self.info, re.M))
        self.assertEqual(used, defined)
        self.assertLessEqual({"ladder", "memory", "check", "avoided", "measured", "budget", "keys", "provider"}, used)

    def test_the_lines_that_must_stay_honest(self):
        self.assertIn("It runs in a subprocess with a time limit. It is not a sandbox.", self.info)
        self.assertIn("Only Meta Muse Spark has been tested in this app.", self.info)
        self.assertIn("not your provider's account", self.info)

    def test_the_name_is_proveo_and_eco_mode_is_only_the_switch(self):
        self.assertIn("<title>Proveo</title>", self.page)
        self.assertIn('<span class="logo-name">Proveo</span>', self.page)
        self.assertIn("<h1>Proveo</h1>", self.page)
        self.assertNotIn('<h2 class="title">Proveo</h2>', self.page, "the top bar already says it")
        self.assertIn('<p class="pitch">Let your tests choose the model. Cheapest first.</p>', self.page)
        self.assertEqual(self.page.count("pi-edge-router"), 1, "only in the repository link")
        html =self.page.split("<script>")[0]
        labels = re.findall(r">[^<]*Eco mode[^<]*<|aria-label=\"Eco mode\"", html)
        self.assertEqual(labels, ['aria-label="Eco mode"', ">Eco mode<"])

    def test_the_about_version_matches_the_app(self):
        with open(os.path.join(os.path.dirname(PAGE), "..", "desktop", "package.json"), encoding="utf-8") as f:
            self.assertIn(f"<h2>Version</h2>\n          <p class=\"num\">{json.load(f)['version']}</p>", self.page)

    def about(self):
        return self.page.split('id="screen-about"')[1].split("</main>")[0]

    def test_the_about_page_has_its_sections_in_order(self):
        self.assertEqual(re.findall(r"<h2>(.*?)</h2>", self.about()),
                         ["What it is", "Quick setup", "How it works", "What it works for", "Honest limits", "Your keys and your data", "Version"])

    def test_the_about_page_keeps_the_honest_limits(self):
        about = self.about()
        for line in ("small benchmark coding tasks, one run each", "Local model answers can vary between runs", "It is not a real sandbox",
                     "Weak tests let a wrong answer pass", "Savings figures are estimates unless marked as measured",
                     "encrypted on this computer", "held in memory only", "It does not keep your description, your checks or the answers", "not see your provider's account"):
            self.assertIn(line, about)

    def test_the_page_names_no_raspberry_pi_in_its_copy(self):
        copy = re.sub(r"(^|\s)//.*", "", self.page.split("</style>")[1])  # code comments are not copy
        self.assertEqual(re.findall(r"Raspberry|\bPi\b", copy), [])

    def quick_setup(self):
        section = self.about().split("<h2>Quick setup</h2>")[1].split("</section>")[0]
        return re.findall(r"<li>(.*?)</li>", section)

    def test_quick_setup_has_its_six_points(self):
        points = self.quick_setup()
        self.assertEqual(len(points), 6)
        self.assertIn("OpenAI-compatible address", points[0])
        self.assertIn("cheapest first", points[3])
        self.assertIn("Cloud stays off until a key is stored", points[4])
        self.assertIn("Proveo stops at the cheapest rung that passes", points[5])

    def test_quick_setup_says_what_detect_really_does(self):
        point = self.quick_setup()[2]
        for kind, port, _ in detect.KNOWN_SERVERS:
            self.assertIn(kind, point)
            self.assertIn(str(port), point)
        self.assertEqual(sorted(int(n) for n in re.findall(r"\d{4,5}", point)), sorted(p for _, p, _ in detect.KNOWN_SERVERS))
        self.assertIn("this computer only", point)
        self.assertIn("does not scan your network", point)
        self.assertEqual(inspect.signature(detect.detect_local).parameters["host"].default, "127.0.0.1")

    def test_quick_setup_names_controls_that_exist(self):
        screen = self.page.split('id="screen-models"')[1].split('id="screen-cloud"')[0]
        for word in ("Detect local models", "Add a machine by address", ">Probe<", "Save ladder"):
            self.assertIn(word, screen)
        self.assertIn('"Add as rung"', self.page)
        self.assertIn(">Code task<", self.page)

    def test_the_about_page_has_no_provider_status(self):
        lower = self.about().lower().replace("any server with an openai-compatible address works", "")  # Quick setup, about servers
        for word in ("untested", "has been tested", "muse spark", "openai", "anthropic", "gemini"):
            self.assertNotIn(word, lower)

    def test_the_about_repository_link_matches_the_readme(self):
        link = re.search(r'href="(https://github\.com/[^"]+)" target="_blank" rel="noopener"', self.about()).group(1)
        with open(os.path.join(os.path.dirname(PAGE), "..", "README.md"), encoding="utf-8") as f:
            self.assertIn(link + "/", f.read())


class PagePresets(unittest.TestCase):
    """The presets are data in the page: no prices, and valid for the backend."""

    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        self.presets = page_presets()

    def test_three_presets_with_the_documented_urls(self):
        self.assertEqual({k: p["base_url"] for k, p in self.presets.items()},
                         {"openai": "https://api.openai.com/v1", "claude": "https://api.anthropic.com/v1",
                          "gemini": "https://generativelanguage.googleapis.com/v1beta/openai"})

    def test_no_prices_and_every_preset_suggests_a_key_name(self):
        for name, p in self.presets.items():
            self.assertFalse({"price_in_per_m", "price_out_per_m"} & set(p), name)
            self.assertTrue(p["key_name"].endswith("key"), name)
        self.assertIn("see the provider's pricing page", self.page)

    def test_each_preset_saves_as_a_valid_rung_with_its_flags(self):
        for name, p in self.presets.items():
            tier = cloud(p["base_url"], model=p["model"], flags=p["flags"], key_name=p["key_name"])
            clean = settings.validate({"tiers": [tier]})["tiers"][0]
            self.assertEqual(clean["flags"], dict(sorted(p["flags"].items())), name)
            self.assertEqual(clean["key_name"], p["key_name"], name)

    def test_the_page_uses_the_same_key_rule_as_the_backend(self):
        self.assertIn(f'const DEFAULT_META_HOST = "{eco_core.DEFAULT_META_HOST}";', self.page)
        self.assertIn("usableMetaKeys().length === 1 && mayUseOnlyKey(t) ? `Automatic (${usableMetaKeys()[0]})` : \"Choose a key\"", self.page)
        self.assertIn(f'const META_KEY_REFUSAL = "{eco_core.META_KEY_REFUSAL}";', self.page)

    def test_the_provider_labels_are_the_same_everywhere(self):
        select = re.search(r'<select id="keyProvider">(.*?)</select>', self.page).group(1)
        self.assertEqual(tuple(re.findall(r'<option value="(.*?)">', select)), eco_core.KEY_PROVIDERS)
        self.assertEqual(eco_core.KEY_PROVIDERS[0], "Meta", "the form's default")
        with open(os.path.join(os.path.dirname(PAGE), "..", "desktop", "lib.js"), encoding="utf-8") as f:
            line = re.search(r"const KEY_PROVIDERS = \[(.*?)\];", f.read()).group(1)
        self.assertEqual(tuple(json.loads(f"[{line}]")), eco_core.KEY_PROVIDERS)

    def test_the_key_prefixes_are_the_same_everywhere_and_never_a_bare_sk(self):
        with open(os.path.join(os.path.dirname(PAGE), "..", "desktop", "lib.js"), encoding="utf-8") as f:
            sources = [self.page, f.read()]
        for source in sources:
            table = json.loads(re.search(r"const KEY_PREFIXES = (\[\[.*?\]\]);", source).group(1))
            self.assertEqual(tuple(map(tuple, table)), eco_core.KEY_PREFIXES)
        self.assertNotIn("sk-", [prefix for prefix, _ in eco_core.KEY_PREFIXES])
        self.assertFalse([p for p, _ in eco_core.KEY_PREFIXES if len(p) < 4], "only distinctive prefixes")

    def test_each_preset_button_preselects_its_own_provider(self):
        for preset in self.presets.values():
            self.assertIn(preset["name"], eco_core.KEY_PROVIDERS)
        self.assertIn('$("keyProvider").value = keyLabelFor(card);', self.page)
        self.assertIn('card === "custom" ? "Other" : PRESETS[card].name;', self.page)

    def test_flags_match_what_the_docs_say(self):
        self.assertTrue(self.presets["claude"]["flags"]["omit_temperature"], "Claude refuses temperature below 1")
        self.assertEqual(self.presets["openai"]["flags"]["max_tokens_field"], "max_completion_tokens")


if __name__ == "__main__":
    unittest.main()
