"""Tests for named API keys: names, add/replace/remove, status, which rung gets which key.

Fake model servers on random 127.0.0.1 ports, made-up keys and a temp ECO_DATA_DIR only.
No real models, no cloud calls. Run with: python -m unittest -v test_eco_keys
"""
import json
import os
import unittest
from unittest import mock

import bench_coding
import eco_core
import eco_server
import settings
from test_eco_presets import page_presets
from test_eco_server import BAD, GOOD, ServerCase, fake
from test_eco_settings import FakeModelServer, local_tier

KEY_A = "made-up-key-alpha-0123WXYZ"
KEY_B = "made-up-key-bravo-4567QRST"
LABEL = "Use a short label, not the key itself"
TESTS = [[[1, 2], 3]]


def cloud_tier(tid, srv, key_name=None, **extra):
    tier = {"id": tid, "name": tid.upper(), "kind": "cloud", "provider": "openai_compatible",
            "base_url": srv.root + "/v1", "model": "c", "flags": {}, "enabled": True,
            "price_in_per_m": 1.0, "price_out_per_m": 1.0, **extra}
    if key_name:
        tier["key_name"] = key_name
    return tier


class KeyNames(unittest.TestCase):
    def test_good_names_are_trimmed(self):
        for name in ("Meta personal", "Claude key", "openai-prod-2026", "Work key 2", "Aiza's key", "x" * 23):
            self.assertEqual(settings.clean_key_name(f"  {name} "), name)
        self.assertEqual(settings.clean_key_name("word " * 7 + "last!"), "word word word word word word word last!")

    def test_size_and_type(self):
        for bad in ("", "   ", None, 12, ["a"], "ab cd " * 7, "tab\there", "line\nbreak"):
            with self.assertRaises(ValueError) as e:
                settings.clean_key_name(bad)
            self.assertEqual(str(e.exception), settings.KEY_NAME_SIZE, repr(bad))
        self.assertEqual(len(settings.clean_key_name("abcdefghij " * 3 + "abcdefg")), 40)
        with self.assertRaises(ValueError):
            settings.clean_key_name("abcdefghij " * 3 + "abcdefgh")

    def test_names_that_look_like_a_key_are_refused_without_echo(self):
        for bad in ("sk-abc", "sk_live_x", "AIzaSyExample", "gsk_abc", "ghp_abc", "github_pat_abc", "xai-abc",
                    "hf_abc", "my sk-abc key", "Bearer abc", "a1b2c3d4e5f6g7h8", "x" * 24, "abc-def_" * 5, KEY_A):
            with self.assertRaises(ValueError) as e:
                settings.clean_key_name(bad)
            self.assertEqual(str(e.exception), LABEL, bad)
            self.assertNotIn(bad, str(e.exception))

    def test_a_name_containing_a_known_key_is_refused_ignoring_case(self):
        secret = "plainword"
        for name in ("plainword", "my PlainWord key"):
            with self.assertRaises(ValueError) as e:
                settings.clean_key_name(name, [secret])
            self.assertEqual(str(e.exception), LABEL)


class TierKeyName(unittest.TestCase):
    def tiers(self, **extra):
        return {"tiers": [{"id": "c", "name": "C", "kind": "cloud", "provider": "meta",
                           "base_url": "https://api.meta.ai/v1", "model": "m", "flags": {}, "enabled": True, **extra}]}

    def test_saved_trimmed_only_when_set(self):
        self.assertEqual(settings.validate(self.tiers(key_name=" Meta personal "))["tiers"][0]["key_name"], "Meta personal")
        self.assertNotIn("key_name", settings.validate(self.tiers())["tiers"][0])
        self.assertNotIn("key_name", settings.validate(self.tiers(key_name=None))["tiers"][0])

    def test_refused_on_a_local_tier_or_when_it_looks_like_a_key(self):
        with self.assertRaisesRegex(ValueError, "key_name only applies to cloud tiers"):
            settings.validate({"tiers": [local_tier("l", "http://h:1/v1", key_name="Meta")]})
        with self.assertRaises(ValueError) as e:
            settings.validate(self.tiers(key_name=KEY_A))
        self.assertIn(LABEL, str(e.exception))
        self.assertNotIn(KEY_A, str(e.exception))

    def test_resolution(self):
        keys = {"One": KEY_A, "Two": KEY_B}
        f = eco_core.key_name_for_tier
        self.assertEqual(f({"key_name": "two"}, keys), ("Two", ""), "ignoring case")
        self.assertEqual(f({"key_name": "Gone"}, keys), (None, eco_core.NO_KEY_REASON), "never falls back")
        self.assertEqual(f({"key_name": "Gone"}, {"One": KEY_A}), (None, eco_core.NO_KEY_REASON), "never falls back")
        self.assertEqual(f({}, keys), (None, "Choose a key"))
        self.assertEqual(f({}, {"One": KEY_A}), (None, "Choose a key"), "no name, not the default Meta rung: no key")
        self.assertEqual(f({}, {}), (None, "No API key set yet"))

    def test_only_meta_on_its_own_https_host_may_use_the_only_key(self):
        keys = {"One": KEY_A}
        labels = {"One": "Meta"}
        f = lambda cfg, stored: eco_core.key_name_for_tier(cfg, stored, {**labels, "Two": "Meta"})
        for url in ("https://api.meta.ai/v1", "https://API.META.AI/v1", "https://api.meta.ai:443/v1"):
            self.assertEqual(f({"provider": "meta", "base_url": url}, keys), ("One", ""), url)
        for url in ("http://api.meta.ai/v1", "https://api.meta.ai:8443/v1", "https://api.meta.ai.evil.example/v1",
                    "https://evil.example/api.meta.ai/v1", "https://evilapi.meta.ai/v1", "https://sub.api.meta.ai/v1",
                    "http://127.0.0.1:9/v1", "not a url", "https://api.meta.ai:bad/v1", ""):
            self.assertEqual(f({"provider": "meta", "base_url": url}, keys), (None, "Choose a key"), url)
        self.assertEqual(f({"provider": "openai_compatible", "base_url": "https://api.meta.ai/v1"}, keys), (None, "Choose a key"))
        self.assertEqual(f({"provider": "meta", "base_url": "https://api.meta.ai/v1"}, {"One": KEY_A, "Two": KEY_B}),
                         (None, "Choose a key"), "two keys: it must be told which")

    def test_key_provider_without_a_label(self):
        f = eco_core.key_provider
        self.assertEqual((f("Default"), f("default"), f("Mine")), ("Meta", "Meta", "Other"))
        self.assertEqual((f("Default", {"Default": "OpenAI"}), f("Mine", {"Mine": "Meta"})), ("OpenAI", "Meta"), "a label wins")

    def test_the_meta_host_whatever_the_rest_of_the_url(self):
        for url in ("https://api.meta.ai/v1", "http://api.meta.ai/v1", "https://api.meta.ai:8443/v1", "https://API.META.AI./v1",
                    "https://api.meta.ai:bad/v1"):
            self.assertTrue(eco_core.calls_meta_host({"base_url": url}), url)
        for url in ("https://api.meta.ai.evil.example/v1", "https://sub.api.meta.ai/v1", "https://evil.example/api.meta.ai/v1",
                    "http://127.0.0.1:9/v1", "not a url", ""):
            self.assertEqual(eco_core.meta_key_refusal({"base_url": url}, "Mine"), "", url)

    def test_only_a_meta_key_is_ever_used_automatically(self):
        f = eco_core.key_name_for_tier
        meta = {"provider": "meta", "base_url": "https://api.meta.ai/v1"}
        for label in ("OpenAI", "Claude (Anthropic)", "Gemini (Google)", "Other"):
            self.assertEqual(f(meta, {"One": KEY_A}, {"One": label}), (None, "Choose a key"), label)
        self.assertEqual(f(meta, {"One": KEY_A}), (None, "Choose a key"), "no label, not named Default")
        self.assertEqual(f(meta, {"Default": KEY_A}), ("Default", ""), "legacy")
        self.assertEqual(f(meta, {"One": KEY_A, "Two": KEY_B}, {"One": "OpenAI", "Two": "Meta"}), ("Two", ""))
        self.assertEqual(f(meta, {"One": KEY_A, "Two": KEY_B}, {"One": "Meta", "Two": "Meta"}), (None, "Choose a key"))

    def test_a_plain_key_follows_the_same_rule(self):
        cfg = {"tiers": [{"id": "meta", "name": "M", "kind": "cloud", "provider": "meta", "base_url": "https://api.meta.ai/v1",
                          "model": "m", "flags": {}, "enabled": True},
                         {"id": "other", "name": "O", "kind": "cloud", "provider": "openai_compatible", "base_url": "https://x.example/v1",
                          "model": "m", "flags": {}, "enabled": True, "price_in_per_m": 1.0, "price_out_per_m": 1.0}]}
        self.assertEqual([t.wait_reason for t in eco_core.build_tiers(cfg, key=KEY_A, allow_cloud=True)], ["", "Choose a key"])

    def test_meta_rung_gets_only_its_own_key(self):
        seen = []
        with mock.patch.object(bench_coding, "ask_cloud",
                               lambda prompt, **kw: seen.append(kw["key"]) or {"text": "x"}):
            cfg = {"tiers": [{**self.tiers(key_name="Two")["tiers"][0], "id": "a"},
                             {**self.tiers(key_name="One")["tiers"][0], "id": "b"},
                             {**self.tiers(key_name="Gone")["tiers"][0], "id": "c"}]}
            tiers = eco_core.build_tiers(cfg, allow_cloud=True, keys={"One": KEY_A, "Two": KEY_B},
                                         providers={"One": "Meta", "Two": "Meta"})
            tiers[0].ask("p")
            tiers[1].ask("p")
        self.assertEqual(seen, [KEY_B, KEY_A])
        self.assertEqual([t.wait_reason for t in tiers], ["", "", "No API key set yet"])


class KeyCase(ServerCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        os.environ.pop("MODEL_API_KEY", None)
        self.addCleanup(self._env.stop)

    def post(self, path, body, **headers):
        status, text = self.request(path, body, headers or None)
        return status, json.loads(text)

    def status(self):
        return json.loads(self.request("/api/key/status")[1])

    def done(self, eco=True):
        return self.wait_for(self.solve(eco=eco), lambda s: s["status"] != "running")

    def assert_no_key_material(self, text, *keys):
        for key in keys:
            self.assertNotIn(key, text)
            self.assertNotIn(key[-4:], text, "not even the last 4")
            self.assertNotIn(key[:8], text, "not even the first 8")

    def servers(self, *replies):
        made = [FakeModelServer(reply=r) for r in replies]
        for srv in made:
            self.addCleanup(srv.close)
        return made

    def default_meta(self, **extra):
        return {**settings.default_settings()["tiers"][2], **extra}

    def meta_host_is_fake(self, srv):
        real = bench_coding.requests.post

        def post(url, **kw):
            self.assertEqual(url, "https://api.meta.ai/v1/responses")
            return real(srv.root + "/v1/responses", **kw)
        patcher = mock.patch.object(bench_coding.requests, "post", post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, *tiers):
        status, text = self.post("/api/settings", {"tiers": list(tiers)})
        self.assertEqual(status, 200, text)


class KeyApi(KeyCase):
    def test_add_replace_remove(self):
        self.start()
        self.assertEqual(self.status(), {"keys": []})
        status, body = self.post("/api/key", {"name": " Meta personal ", "key": KEY_A, "provider": "Meta"})
        self.assertEqual((status, body), (200, {"keys": [{"name": "Meta personal", "provider": "Meta", "rungs": ["Meta Muse Spark 1.3"]}]}),
                         "the default ladder's cloud rung uses the only Meta key")
        self.post("/api/key", {"name": "Claude key", "key": KEY_B})
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["Claude key", "Meta personal"])

        self.post("/api/key", {"name": "META PERSONAL", "key": KEY_B + "x"})
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["Claude key", "META PERSONAL"],
                         "the same name in another case replaces, it does not add")
        self.assertEqual(self.server.app._key_copy()["META PERSONAL"], KEY_B + "x")

        status, body = self.post("/api/key/remove", {"name": "meta personal"})
        self.assertEqual((status, [k["name"] for k in body["keys"]]), (200, ["Claude key"]))
        self.assertEqual(self.post("/api/key/remove", {"name": "meta personal"})[0], 200, "removing twice is fine")
        self.assertEqual(self.post("/api/key/remove", {"name": "Claude key"})[1], {"keys": []})

    def test_status_never_contains_key_material(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv, "One")]})
        self.request("/api/key", {"name": "One", "key": KEY_A})
        self.request("/api/key", {"name": "Two", "key": KEY_B})
        text = self.request("/api/key/status")[1]
        self.assert_no_key_material(text, KEY_A, KEY_B)
        self.assertEqual(json.loads(text), {"keys": [{"name": "One", "provider": "Other", "rungs": ["C"]},
                                                     {"name": "Two", "provider": "Other", "rungs": []}]},
                         "exactly names, providers and rungs: no length, no last 4, no other field")
        self.assertNotIn(str(len(KEY_A)), text)
        for path in ("/", "/api/settings", "/api/config", "/api/budget", "/api/memory", "/api/stats"):
            self.assert_no_key_material(self.request(path)[1], KEY_A, KEY_B)

    def test_bad_requests_are_refused_and_add_nothing(self):
        self.start()
        for body in ({"name": KEY_A, "key": KEY_A}, {"name": "has " + KEY_A, "key": KEY_A},
                     {"name": "sk-" + "abc", "key": KEY_A}, {"name": "x" * 60, "key": KEY_A}):
            status, text = self.request("/api/key", body)
            self.assertEqual(status, 400, body)
            self.assertIn(LABEL, json.loads(text)["error"])
            self.assert_no_key_material(text, KEY_A)
        for body in ({"name": "", "key": KEY_A}, {"name": "tab\there", "key": KEY_A}, {"name": 12, "key": KEY_A}):
            status, text = self.request("/api/key", body)
            self.assertEqual(status, 400, body)
            self.assert_no_key_material(text, KEY_A)
        for body in ({"name": "Ok", "key": "short"}, {"name": "Ok", "key": None}, {"name": "Ok"},
                     {"name": "Ok", "key": KEY_A, "extra": 1}, {"value": KEY_A}):
            status, text = self.request("/api/key", body)
            self.assertEqual(status, 400, body)
            self.assert_no_key_material(text, KEY_A)
        for body in ({}, {"name": 12}, {"name": "Ok", "key": KEY_A}):
            status, text = self.request("/api/key/remove", body)
            self.assertEqual(status, 400, body)
            self.assert_no_key_material(text, KEY_A)
        self.assertEqual(self.status(), {"keys": []})

    def test_a_key_that_appears_in_an_existing_name_is_refused(self):
        self.start()
        self.post("/api/key", {"name": "my personalword", "key": KEY_A})
        status, body = self.post("/api/key", {"name": "Other", "key": "personalword"})
        self.assertEqual((status, body["error"]), (400, LABEL))
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["my personalword"])

    def test_each_cloud_rung_gets_only_its_own_key_and_local_gets_none(self):
        lan, one, two = self.servers(BAD, BAD, GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.post("/api/key", {"name": "Two", "key": KEY_B})
        self.post("/api/settings", {"tiers": [local_tier("lan", lan.root + "/v1"), cloud_tier("c1", one, "One"),
                                              cloud_tier("c2", two, "Two")]})
        state = self.done()
        self.assertEqual(state["result"]["solved_by"], "c2")
        self.assertIsNone(lan.requests[0]["auth"])
        self.assertEqual([r["auth"] for r in one.requests], ["Bearer " + KEY_A])
        self.assertEqual([r["auth"] for r in two.requests], ["Bearer " + KEY_B])
        self.assertEqual(self.status(), {"keys": [{"name": "One", "provider": "Other", "rungs": ["C1"]},
                                                  {"name": "Two", "provider": "Other", "rungs": ["C2"]}]})
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            saved = f.read()
        with open(self.log, encoding="utf-8") as f:
            log = f.read()
        self.assert_no_key_material(saved + log + self.request(f"/api/jobs/{state['id']}")[1], KEY_A, KEY_B)

    def test_no_key_name_gets_no_key_however_many_are_stored(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv)]})
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"], {"c": {"key_name": None, "reason": "No API key set yet"}})

        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.assertEqual(self.status(), {"keys": [{"name": "One", "provider": "Other", "rungs": []}]}, "the only key is not used by it")
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"c": "Choose a key"})
        self.assertEqual(srv.requests, [], "nothing is sent")

        self.post("/api/key", {"name": "Two", "key": KEY_B})
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"c": "Choose a key"})
        self.assertEqual(state["status"], "done")
        self.assertEqual((srv.requests, state["attempts"][0:]), ([], []), "nothing is sent")
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"], {"c": {"key_name": None, "reason": "Choose a key"}})
        self.assertEqual(view["key_names"], ["One", "Two"])

    def test_removing_a_key_leaves_its_rung_waiting_not_failed(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.post("/api/key", {"name": "Two", "key": KEY_B})
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv, "One")]})
        self.assertEqual(self.status()["keys"][0], {"name": "One", "provider": "Other", "rungs": ["C"]})
        self.post("/api/key/remove", {"name": "One"})

        state = self.done()
        self.assertEqual((state["status"], state["attempts"]), ("done", []), "skipped, not a failed attempt")
        self.assertEqual(state["skipped"], ["c"])
        self.assertEqual(state["skip_reasons"], {"c": "No API key set yet"})
        self.assertEqual(state["message"], "No API key set yet")
        self.assertEqual(srv.requests, [], "it never falls back to the other key")
        off = self.done(eco=False)
        self.assertEqual((off["message"], off["attempts"], srv.requests), ("No API key set yet", [], []))
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["tiers"][0]["key_name"], "One", "the choice stays, shown as missing")
        self.assertEqual(view["rung_keys"]["c"]["reason"], "No API key set yet")

        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.assertEqual(self.done()["result"]["solved_by"], "c", "adding the name back reconnects it")
        self.assertEqual(srv.requests[-1]["auth"], "Bearer " + KEY_A)

    def test_a_removed_key_stays_scrubbed(self):
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.post("/api/key/remove", {"name": "One"})
        leaky, _ = fake("leaky", None, error=RuntimeError(f"auth failed with key {KEY_A}"))
        self.server.app.ladder = lambda: [leaky]
        state = self.done()
        with open(self.log, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("[redacted]", state["attempts"][0]["error"])
        self.assertNotIn(KEY_A, json.dumps(state) + log)
        status, body = self.post("/api/key", {"name": "Has " + KEY_A, "key": KEY_B})
        self.assertEqual((status, body["error"]), (400, LABEL), "not reusable inside a name either")

    def test_keys_do_not_turn_the_cloud_on(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=False)
        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv, "One")]})
        state = self.done()
        self.assertEqual((state["skipped"], srv.requests), (["c"], []))
        self.assertEqual(state["message"], eco_server.CLOUD_DISABLED)
        self.assertEqual(json.loads(self.request("/api/config")[1])["cloud_enabled"], False)

    def test_old_single_key_request_means_default(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv, "Default")]})
        self.assertEqual(self.post("/api/key", {"key": KEY_A}), (200, {"set": True}))
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "provider": "Meta", "rungs": ["C"]}]})
        self.assertEqual(self.done()["result"]["solved_by"], "c")
        self.assertEqual(srv.requests[-1]["auth"], "Bearer " + KEY_A)
        self.post("/api/key", {"name": "Other", "key": KEY_B})
        self.assertEqual(self.post("/api/key", {"key": None}), (200, {"set": False}))
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["Other"], "null removes Default only")

    def test_model_api_key_at_start_is_default(self):
        os.environ["MODEL_API_KEY"] = KEY_A
        self.start()
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "provider": "Meta", "rungs": ["Meta Muse Spark 1.3"]}]})
        self.assertIn(KEY_A, self.server.app.secrets())

    def test_remove_endpoint_has_the_same_guards(self):
        self.start()
        self.post("/api/key", {"name": "One", "key": KEY_A})
        body = {"name": "One"}
        self.assertEqual(self.request("/api/key/remove", body, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/key/remove", body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/key/remove", body, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/key/remove")[0], 404, "POST only")
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["One"])


class KeyRouting(KeyCase):
    """What each fake server's raw Authorization header really was. Reaches no real service: the default
    Meta host is only ever redirected to a fake server, inside requests.post."""

    def test_one_stored_key_and_a_custom_rung_with_no_name(self):
        srv, next_rung = self.servers(GOOD, GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Only", "key": KEY_A})
        self.save(cloud_tier("custom", srv), local_tier("after", next_rung.root + "/v1"))
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"custom": "Choose a key"})
        self.assertEqual(state["result"]["solved_by"], "after", "the ladder goes on past it")
        self.assertEqual(srv.requests, [], "no request, so no Authorization header at all")
        self.assertIsNone(next_rung.requests[0]["auth"])
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"]["custom"], {"key_name": None, "reason": "Choose a key"})
        self.assertEqual(self.status(), {"keys": [{"name": "Only", "provider": "Other", "rungs": []}]})

    def test_eco_off_does_not_send_it_either(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Only", "key": KEY_A})
        self.save(cloud_tier("custom", srv))
        state = self.done(eco=False)
        self.assertEqual(state["skip_reasons"], {"custom": "Choose a key"})
        self.assertEqual(srv.requests, [])

    def test_the_default_meta_rung_uses_the_only_key(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Only", "key": KEY_A, "provider": "Meta"})
        self.save(self.default_meta())
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])
        self.assertEqual(self.status(), {"keys": [{"name": "Only", "provider": "Meta", "rungs": ["Meta Muse Spark 1.3"]}]})

    def test_legacy_default_key_on_the_default_ladder(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.assertEqual(self.post("/api/key", {"key": KEY_A}), (200, {"set": True}))
        self.save(self.default_meta())  # the default ladder's cloud rung, without its unreachable local rungs
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])

    def test_model_api_key_on_the_cli_reaches_the_default_rung_only(self):
        meta_srv, custom = self.servers(GOOD, GOOD)
        self.meta_host_is_fake(meta_srv)
        os.environ["MODEL_API_KEY"] = KEY_A
        self.start(allow_cloud=True)
        self.save(cloud_tier("custom", custom), self.default_meta())
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"custom": "Choose a key"})
        self.assertEqual(state["result"]["solved_by"], "cloud")
        self.assertEqual(custom.requests, [])
        self.assertEqual([r["auth"] for r in meta_srv.requests], ["Bearer " + KEY_A])

    def test_each_preset_rung_names_its_own_key(self):
        presets = page_presets()
        self.start(allow_cloud=True)
        for preset in presets.values():
            self.post("/api/key", {"name": preset["key_name"], "key": "made-up-" + preset["key_name"].split()[0] + "-0123456789"})
        for pid, preset in presets.items():
            srv, = self.servers(GOOD)
            tier = cloud_tier(pid, srv, preset["key_name"], flags=preset["flags"])
            self.save(tier)
            self.assertEqual(self.done()["result"]["solved_by"], pid)
            self.assertEqual([r["auth"] for r in srv.requests], ["Bearer made-up-" + preset["key_name"].split()[0] + "-0123456789"])

    def test_a_preset_rung_never_takes_another_providers_only_key(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Meta personal", "key": KEY_A})
        self.save(cloud_tier("claude", srv, page_presets()["claude"]["key_name"]))
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"claude": "No API key set yet"})
        self.assertEqual(srv.requests, [])

    def test_a_meta_rung_on_an_edited_host_loses_the_exception(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Only", "key": KEY_A})
        edited = self.default_meta(base_url=srv.root + "/v1")
        self.save(edited)
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"cloud": "Choose a key"})
        self.assertEqual(srv.requests, [], "no request, so no Authorization header at all")
        self.assertEqual(self.status(), {"keys": [{"name": "Only", "provider": "Other", "rungs": []}]})
        # Naming the key is the way to use it there.
        self.save({**edited, "key_name": "Only"})
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])

    def test_a_removed_name_never_falls_back_even_on_the_default_meta_rung(self):
        srv, custom = self.servers(GOOD, GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "Gone", "key": KEY_A, "provider": "Meta"})
        self.post("/api/key", {"name": "Kept", "key": KEY_B, "provider": "Meta"})
        self.save(self.default_meta(key_name="Gone"), cloud_tier("custom", custom, "Gone"))
        self.post("/api/key/remove", {"name": "Gone"})
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"cloud": "No API key set yet", "custom": "No API key set yet"})
        self.assertEqual((srv.requests, custom.requests), ([], []))


class KeyProviders(KeyCase):
    """Which provider a stored key is for decides whether the Meta host may ever receive it. Raw Authorization
    headers from fake servers; the default Meta host is only ever redirected to a fake server, inside requests.post."""

    def add(self, name, key, provider=None):
        body = {"name": name, "key": key}
        if provider is not None:
            body["provider"] = provider
        status, reply = self.post("/api/key", body)
        self.assertEqual(status, 200, reply)

    def test_a_lone_claude_key_is_never_sent_to_the_default_meta_rung(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Claude key", KEY_A, "Claude (Anthropic)")
        self.save(self.default_meta())
        for eco in (True, False):
            state = self.done(eco=eco)
            self.assertEqual(state["skip_reasons"], {"cloud": "Choose a key"})
            self.assertEqual(state["attempts"], [])
        self.assertEqual(srv.requests, [], "no request, so no Authorization header at all")
        self.assertEqual(self.status(), {"keys": [{"name": "Claude key", "provider": "Claude (Anthropic)", "rungs": []}]})
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"]["cloud"], {"key_name": None, "reason": "Choose a key"})
        self.assertEqual(view["key_providers"], {"Claude key": "Claude (Anthropic)"})

    def test_no_other_provider_is_used_automatically_either(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.save(self.default_meta())
        for provider in ("OpenAI", "Gemini (Google)", "Other", None):
            self.add("Lone", KEY_A, provider)
            self.assertEqual(self.done()["skip_reasons"], {"cloud": "Choose a key"}, provider)
        self.assertEqual(srv.requests, [])

    def test_a_lone_meta_key_still_works_on_the_default_meta_rung(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Meta personal", KEY_A, "Meta")
        self.save(self.default_meta())
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])
        self.assertEqual(self.status(), {"keys": [{"name": "Meta personal", "provider": "Meta", "rungs": ["Meta Muse Spark 1.3"]}]})

    def test_the_one_meta_key_is_used_beside_other_providers_and_two_need_a_choice(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Claude key", KEY_B, "Claude (Anthropic)")
        self.add("Meta personal", KEY_A, "Meta")
        self.save(self.default_meta())
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A], "the Meta key, never the Claude one")
        self.add("Meta work", "made-up-key-third-89ABCDEF", "Meta")
        self.assertEqual(self.done()["skip_reasons"], {"cloud": "Choose a key"})
        self.assertEqual(len(srv.requests), 1, "two Meta keys: it must be told which")

    def test_a_meta_key_goes_to_a_non_meta_rung_only_when_named(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.add("Meta personal", KEY_A, "Meta")
        self.save(cloud_tier("custom", srv))
        self.assertEqual(self.done()["skip_reasons"], {"custom": "Choose a key"})
        self.assertEqual(srv.requests, [])
        self.save(cloud_tier("custom", srv, "Meta personal"))
        self.assertEqual(self.done()["result"]["solved_by"], "custom")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])

    def test_other_rungs_accept_any_named_key(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.add("Claude key", KEY_A, "Claude (Anthropic)")
        self.save(cloud_tier("openai", srv, "Claude key"))
        self.assertEqual(self.done()["result"]["solved_by"], "openai")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A], "a warning on the page, not a refusal")

    def test_naming_a_non_meta_key_on_the_meta_host_is_refused(self):
        self.start(allow_cloud=True)
        self.add("Claude key", KEY_A, "Claude (Anthropic)")
        self.add("Mine", KEY_B)
        before = self.request("/api/settings")[1]
        meta = self.default_meta()
        for tier, why in (({**meta, "key_name": "Claude key"}, "Claude (Anthropic)"),
                          ({**meta, "key_name": "CLAUDE KEY"}, "Claude (Anthropic)"),
                          ({**meta, "key_name": "Mine"}, "Other"),
                          ({**meta, "key_name": "Claude key", "enabled": False}, "Claude (Anthropic)"),
                          ({**meta, "key_name": "Claude key", "base_url": "http://api.meta.ai/v1"}, "Claude (Anthropic)"),
                          ({**meta, "key_name": "Claude key", "base_url": "https://api.meta.ai:8443/v1"}, "Claude (Anthropic)"),
                          ({**meta, "key_name": "Claude key", "base_url": "https://API.META.AI./v1"}, "Claude (Anthropic)"),
                          ({**cloud_tier("c", self.servers(GOOD)[0], "Claude key"), "name": meta["name"], "base_url": "https://api.meta.ai/v1"},
                           "Claude (Anthropic)")):
            status, text = self.request("/api/settings", {"tiers": [tier, local_tier("lan", "http://127.0.0.1:9/v1")]})
            self.assertEqual(status, 400, tier)
            self.assertEqual(json.loads(text)["error"], f"{meta['name']}: This rung calls Meta, but that key is for {why}")
            self.assert_no_key_material(text, KEY_A, KEY_B)
        self.assertEqual(self.request("/api/settings")[1], before, "nothing was saved")
        self.save({**meta, "key_name": "Not stored yet"})

    def test_a_named_meta_key_is_accepted_on_the_meta_host(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Meta work", KEY_A, "Meta")
        self.add("Meta personal", KEY_B, "Meta")
        self.save(self.default_meta(key_name="Meta personal"))
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_B])

    def test_a_saved_pairing_is_checked_again_when_the_ladder_runs(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Mine", KEY_A, "Meta")
        self.save(self.default_meta(key_name="Mine"))
        self.add("Mine", KEY_B, "OpenAI")  # the same name now holds a key for someone else
        state = self.done()
        why = "This rung calls Meta, but that key is for OpenAI"
        self.assertEqual((state["skip_reasons"], state["message"], state["attempts"]), ({"cloud": why}, why, []))
        self.assertEqual(srv.requests, [])
        self.assertEqual(self.status(), {"keys": [{"name": "Mine", "provider": "OpenAI", "rungs": []}]})
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"]["cloud"], {"key_name": None, "reason": why})

    def test_the_budget_can_still_be_saved_beside_a_pairing_gone_bad(self):
        self.start(allow_cloud=True)
        self.add("Mine", KEY_A, "Meta")
        self.save(self.default_meta(key_name="Mine"))
        self.add("Mine", KEY_B, "OpenAI")
        self.assertEqual(self.post("/api/settings", {"budget": {"limit_usd": 5.0, "period": "month"}})[0], 200)

    def test_a_meta_rung_on_an_edited_host_takes_a_named_key_of_any_provider(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.add("Meta personal", KEY_A, "Meta")
        self.save(self.default_meta(base_url=srv.root + "/v1"))
        self.assertEqual(self.done()["skip_reasons"], {"cloud": "Choose a key"}, "even a Meta key: the exception is gone")
        self.add("Claude key", KEY_B, "Claude (Anthropic)")
        self.save(self.default_meta(base_url=srv.root + "/v1", key_name="Claude key"))
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_B])

    def test_legacy_default_behaves_as_meta(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.save(self.default_meta())
        for body in ({"key": KEY_A}, {"name": "Default", "key": KEY_A}, {"name": "DEFAULT", "key": KEY_A}):
            self.assertEqual(self.post("/api/key", body)[0], 200)
            self.assertEqual(self.status()["keys"][0]["provider"], "Meta", body)
            self.assertEqual(self.status()["keys"][0]["rungs"], ["Meta Muse Spark 1.3"], body)
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])

    def test_model_api_key_counts_as_meta(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        os.environ["MODEL_API_KEY"] = KEY_A
        self.start(allow_cloud=True)
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "provider": "Meta", "rungs": ["Meta Muse Spark 1.3"]}]})
        self.save(self.default_meta())
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + KEY_A])

    def test_a_named_key_with_no_provider_counts_as_other(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        self.add("Old key", KEY_A)
        self.save(self.default_meta())
        self.assertEqual(self.status(), {"keys": [{"name": "Old key", "provider": "Other", "rungs": []}]})
        self.assertEqual(self.done()["skip_reasons"], {"cloud": "Choose a key"})
        self.assertEqual(srv.requests, [])

    def test_status_lists_providers_and_no_key_material(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.save(cloud_tier("c", srv, "One"))
        self.add("One", KEY_A, "OpenAI")
        self.add("Two", KEY_B, "Gemini (Google)")
        text = self.request("/api/key/status")[1]
        self.assert_no_key_material(text, KEY_A, KEY_B)
        self.assertEqual(json.loads(text), {"keys": [{"name": "One", "provider": "OpenAI", "rungs": ["C"]},
                                                     {"name": "Two", "provider": "Gemini (Google)", "rungs": []}]},
                         "exactly names, providers and rungs")
        self.assert_no_key_material(self.request("/api/settings")[1], KEY_A, KEY_B)

    def test_every_label_is_accepted_and_nothing_else(self):
        self.start()
        for label in eco_core.KEY_PROVIDERS:
            self.add("Name", KEY_A, label)
            self.assertEqual(self.status()["keys"][0]["provider"], label)
        self.post("/api/key/remove", {"name": "Name"})
        for bad in ("", "meta", "Anthropic", "Anyone", 12, ["Meta"], {"a": 1}, True):
            status, text = self.request("/api/key", {"name": "Name", "key": KEY_A, "provider": bad})
            self.assertEqual(status, 400, bad)
            self.assert_no_key_material(text, KEY_A)
        self.assertEqual(self.status(), {"keys": []})

    def test_removing_a_key_forgets_its_label(self):
        self.start()
        self.add("Mine", KEY_A, "Claude (Anthropic)")
        self.post("/api/key/remove", {"name": "MINE"})
        self.add("Mine", KEY_B)
        self.assertEqual(self.status()["keys"][0]["provider"], "Other")

    def test_a_key_that_appears_in_a_provider_label_is_refused(self):
        self.start()
        status, text = self.request("/api/key", {"name": "Odd", "key": "anthropic", "provider": "Other"})
        self.assertEqual(status, 400)
        self.assertNotIn("anthropic", text.replace("Anthropic", ""), "the key is not echoed")
        self.assertEqual(self.status(), {"keys": []})


class KeyPrefixGuard(KeyCase):
    """A key that starts like another provider's can't be saved under the wrong label. Made-up keys that only
    START with the real prefixes, and every raw response is checked for them."""

    LOOKS = {"Claude (Anthropic)": "sk-ant-made-up-guard-QQ11", "OpenAI": "sk-proj-made-up-guard-QQ22",
             "Gemini (Google)": "AIzamade-up-guard-QQ33", "Groq": "gsk_made-up-guard-QQ44"}

    def message(self, looks_like, label):
        article = "an" if looks_like == "OpenAI" else "a"
        text = f"This key looks like {article} {looks_like} key, but it is labelled {label}. Choose the right provider."
        return text if looks_like != "Groq" else text + " Groq is not on the list, so choose Other."

    def refused(self, body, why):
        status, text = self.request("/api/key", body)
        self.assertEqual((status, json.loads(text)["error"]), (400, why), body["name"] if "name" in body else "no name")
        self.assert_no_key_material(text, body["key"])

    def test_a_key_that_looks_like_another_provider_labelled_meta_is_refused(self):
        self.start()
        for looks_like, key in self.LOOKS.items():
            self.refused({"name": "Mine", "key": key, "provider": "Meta"}, self.message(looks_like, "Meta"))
        self.assertEqual(self.status(), {"keys": []})

    def test_every_other_known_label_is_refused_too(self):
        self.start()
        for looks_like, key in self.LOOKS.items():
            for label in eco_core.KEY_PROVIDERS:
                if label not in (looks_like, "Other"):
                    self.refused({"name": "Mine", "key": key, "provider": label}, self.message(looks_like, label))
        self.assertEqual(self.status(), {"keys": []})

    def test_the_right_label_and_other_are_accepted(self):
        self.start()
        for looks_like, key in self.LOOKS.items():
            for label in (looks_like, "Other"):
                if label in eco_core.KEY_PROVIDERS:
                    status, text = self.request("/api/key", {"name": "Mine", "key": key, "provider": label})
                    self.assertEqual(status, 200, label)
                    self.assert_no_key_material(text, key)
                    self.assertEqual(self.status()["keys"][0]["provider"], label)
        status, text = self.request("/api/key", {"name": "No label", "key": self.LOOKS["OpenAI"]})
        self.assertEqual(status, 200)
        self.assertEqual(next(k for k in self.status()["keys"] if k["name"] == "No label")["provider"], "Other")

    def test_a_bare_sk_key_is_accepted_as_meta_and_used_on_the_default_rung(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        self.start(allow_cloud=True)
        key = "sk-made-up-bare-prefix-QQ55"
        status, text = self.request("/api/key", {"name": "Meta personal", "key": key, "provider": "Meta"})
        self.assertEqual(status, 200, "Meta's key format is unknown, so sk- alone says nothing")
        self.assert_no_key_material(text, key)
        self.save(self.default_meta())
        self.assertEqual(self.done()["result"]["solved_by"], "cloud")
        self.assertEqual([r["auth"] for r in srv.requests], ["Bearer " + key])

    def test_keys_with_no_known_prefix_pass_under_any_label(self):
        self.start()
        for key in ("sk-made-up-QQ66-long", "sk-antelope-made-up-QQ77", "sk-project-made-up-QQ88", "SK-ANT-made-up-QQ99",
                    "aizamade-up-lower-QQ00", "gsk-made-up-dash-QQAA", "made-up-no-prefix-QQBB", "x-sk-ant-made-up-QQCC"):
            for label in eco_core.KEY_PROVIDERS:
                self.assertEqual(eco_core.key_label_problem(key, label), "", (key, label))
            self.assertEqual(self.request("/api/key", {"name": "Mine", "key": key, "provider": "Meta"})[0], 200, key)

    def test_the_legacy_requests_count_as_meta(self):
        self.start()
        why = self.message("Claude (Anthropic)", "Meta")
        self.refused({"key": self.LOOKS["Claude (Anthropic)"]}, why)
        self.refused({"name": "Default", "key": self.LOOKS["Claude (Anthropic)"]}, why)
        self.assertEqual(self.status(), {"keys": []})

    def test_a_refused_key_changes_nothing(self):
        self.start()
        self.post("/api/key", {"name": "Mine", "key": KEY_A, "provider": "Meta"})
        self.refused({"name": "MINE", "key": self.LOOKS["OpenAI"], "provider": "Meta"}, self.message("OpenAI", "Meta"))
        self.assertEqual(self.server.app._key_copy(), {"Mine": KEY_A}, "the old key stays")
        self.assertNotIn(self.LOOKS["OpenAI"], self.server.app.secrets())

    def test_model_api_key_that_looks_like_another_provider_never_goes_to_meta(self):
        srv, = self.servers(GOOD)
        self.meta_host_is_fake(srv)
        os.environ["MODEL_API_KEY"] = self.LOOKS["Claude (Anthropic)"]
        self.start(allow_cloud=True)
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "provider": "Other", "rungs": []}]})
        self.save(self.default_meta())
        self.assertEqual(self.done()["skip_reasons"], {"cloud": "Choose a key"})
        self.assertEqual(srv.requests, [])

    def test_model_api_key_with_a_bare_sk_prefix_stays_meta(self):
        os.environ["MODEL_API_KEY"] = "sk-made-up-env-prefix-QQDD"
        self.start()
        self.assertEqual(self.status()["keys"][0]["provider"], "Meta")

    def test_a_plain_key_that_looks_like_another_provider_waits(self):
        meta = settings.default_settings()["tiers"][2]
        built = eco_core.build_tiers({"tiers": [meta]}, key=self.LOOKS["OpenAI"], allow_cloud=True)
        self.assertEqual([t.wait_reason for t in built], ["Choose a key"])

    def test_the_page_checks_before_it_sends(self):
        with open(eco_server.PAGE, encoding="utf-8") as f:
            page = f.read()
        body = page[page.index("async function saveKey()"):]
        self.assertLess(body.index("keyLabelProblem(key, provider)"), body.index('post("/api/key"'))
        self.assertNotIn("${key", body[:body.index('post("/api/key"')], "the key never goes into a message")


if __name__ == "__main__":
    unittest.main()
