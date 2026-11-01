"""Tests for what the provider presets need: request flags, base URLs, cut-off answers, the page's presets.

Fake model servers on random 127.0.0.1 ports, made-up keys and temp folders only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_presets
"""
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import bench_coding
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


class PagePresets(unittest.TestCase):
    """The presets are data in the page: no prices, labelled untested, and valid for the backend."""

    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.page = f.read()
        block = re.search(r"const PRESETS = (\{.*?\n  \});", self.page, re.S).group(1)
        # The block is plain JSON apart from unquoted keys.
        self.presets = json.loads(re.sub(r"^(\s*)([a-zA-Z_]+):", r'\1"\2":', block, flags=re.M))

    def test_three_presets_with_the_documented_urls(self):
        self.assertEqual({k: p["base_url"] for k, p in self.presets.items()},
                         {"openai": "https://api.openai.com/v1", "claude": "https://api.anthropic.com/v1",
                          "gemini": "https://generativelanguage.googleapis.com/v1beta/openai"})

    def test_no_prices_and_every_preset_is_untested_with_a_note(self):
        for name, p in self.presets.items():
            self.assertFalse({"price_in_per_m", "price_out_per_m"} & set(p), name)
            self.assertTrue(p["note"], name)
            self.assertTrue(p["key_name"].endswith("key"), name)
        self.assertIn("testing and comparing", self.presets["claude"]["note"])
        self.assertIn("Check the provider's pricing page", self.page)

    def test_each_preset_saves_as_a_valid_rung_with_its_flags(self):
        for name, p in self.presets.items():
            tier = cloud(p["base_url"], model=p["model"], flags=p["flags"], key_name=p["key_name"])
            clean = settings.validate({"tiers": [tier]})["tiers"][0]
            self.assertEqual(clean["flags"], dict(sorted(p["flags"].items())), name)
            self.assertEqual(clean["key_name"], p["key_name"], name)

    def test_flags_match_what_the_docs_say(self):
        self.assertTrue(self.presets["claude"]["flags"]["omit_temperature"], "Claude refuses temperature below 1")
        self.assertEqual(self.presets["openai"]["flags"]["max_tokens_field"], "max_completion_tokens")


if __name__ == "__main__":
    unittest.main()
