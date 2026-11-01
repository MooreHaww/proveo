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
        self.assertEqual(f({}, {"One": KEY_A}), ("One", ""))
        self.assertEqual(f({}, {}), (None, "No API key set yet"))

    def test_meta_rung_gets_only_its_own_key(self):
        seen = []
        with mock.patch.object(bench_coding, "ask_cloud",
                               lambda prompt, **kw: seen.append(kw["key"]) or {"text": "x"}):
            cfg = {"tiers": [{**self.tiers(key_name="Two")["tiers"][0], "id": "a"},
                             {**self.tiers(key_name="One")["tiers"][0], "id": "b"},
                             {**self.tiers(key_name="Gone")["tiers"][0], "id": "c"}]}
            tiers = eco_core.build_tiers(cfg, allow_cloud=True, keys={"One": KEY_A, "Two": KEY_B})
            tiers[0].ask("p")
            tiers[1].ask("p")
        self.assertEqual(seen, [KEY_B, KEY_A])
        self.assertEqual([t.wait_reason for t in tiers], ["", "", "No API key set yet"])


class KeyApi(ServerCase):
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

    def test_add_replace_remove(self):
        self.start()
        self.assertEqual(self.status(), {"keys": []})
        status, body = self.post("/api/key", {"name": " Meta personal ", "key": KEY_A})
        self.assertEqual((status, body), (200, {"keys": [{"name": "Meta personal", "rungs": ["Meta Muse Spark 1.3"]}]}),
                         "the default ladder's cloud rung uses the only key")
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
        self.assertEqual(json.loads(text), {"keys": [{"name": "One", "rungs": ["C"]}, {"name": "Two", "rungs": []}]},
                         "exactly names and rungs: no length, no last 4, no other field")
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
        self.assertEqual(self.status(), {"keys": [{"name": "One", "rungs": ["C1"]}, {"name": "Two", "rungs": ["C2"]}]})
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            saved = f.read()
        with open(self.log, encoding="utf-8") as f:
            log = f.read()
        self.assert_no_key_material(saved + log + self.request(f"/api/jobs/{state['id']}")[1], KEY_A, KEY_B)

    def test_no_key_name_uses_the_only_key_otherwise_choose_a_key(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv)]})
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"], {"c": {"key_name": None, "reason": "No API key set yet"}})

        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.assertEqual(self.status(), {"keys": [{"name": "One", "rungs": ["C"]}]}, "the only key counts as used")
        self.assertEqual(self.done()["result"]["solved_by"], "c")
        self.assertEqual(srv.requests[-1]["auth"], "Bearer " + KEY_A)

        self.post("/api/key", {"name": "Two", "key": KEY_B})
        before = len(srv.requests)
        state = self.done()
        self.assertEqual(state["skip_reasons"], {"c": "Choose a key"})
        self.assertEqual(state["status"], "done")
        self.assertEqual((len(srv.requests), state["attempts"][0:]), (before, []), "nothing is sent")
        view = json.loads(self.request("/api/settings")[1])
        self.assertEqual(view["rung_keys"], {"c": {"key_name": None, "reason": "Choose a key"}})
        self.assertEqual(view["key_names"], ["One", "Two"])

    def test_removing_a_key_leaves_its_rung_waiting_not_failed(self):
        srv, = self.servers(GOOD)
        self.start(allow_cloud=True)
        self.post("/api/key", {"name": "One", "key": KEY_A})
        self.post("/api/key", {"name": "Two", "key": KEY_B})
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv, "One")]})
        self.assertEqual(self.status()["keys"][0], {"name": "One", "rungs": ["C"]})
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
        self.post("/api/settings", {"tiers": [cloud_tier("c", srv)]})
        self.assertEqual(self.post("/api/key", {"key": KEY_A}), (200, {"set": True}))
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "rungs": ["C"]}]})
        self.assertEqual(self.done()["result"]["solved_by"], "c")
        self.assertEqual(srv.requests[-1]["auth"], "Bearer " + KEY_A)
        self.post("/api/key", {"name": "Other", "key": KEY_B})
        self.assertEqual(self.post("/api/key", {"key": None}), (200, {"set": False}))
        self.assertEqual([k["name"] for k in self.status()["keys"]], ["Other"], "null removes Default only")

    def test_model_api_key_at_start_is_default(self):
        os.environ["MODEL_API_KEY"] = KEY_A
        self.start()
        self.assertEqual(self.status(), {"keys": [{"name": "Default", "rungs": ["Meta Muse Spark 1.3"]}]})
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


if __name__ == "__main__":
    unittest.main()
