"""Tests for verified memory, with fake tiers and temp files only. No models, no cloud calls.

Run with: python -m unittest -v test_eco_memory
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import eco_core
from eco_core import Tier
from test_eco_server import ServerCase

GOOD = "```python\ndef add(a, b):\n    return a + b\n```"
BAD = "```python\ndef add(a, b):\n    return a - b\n```"
GOOD_CODE = "def add(a, b):\n    return a + b"
BAD_CODE = "def add(a, b):\n    return a - b"
TESTS = [[[1, 2], 3], [[5, 5], 10]]
PROMPT = "Write add(a, b) that returns the sum."
FAKE_KEY = "sk-test-SENTINEL-0123456789abcdef"


def fake(name, reply, kind="local"):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return reply

    return Tier(name, kind, ask), calls


class MemoryCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "eco_log.jsonl")
        self.mem = os.path.join(self.tmp.name, "eco_memory.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def solve(self, tiers, prompt=PROMPT, tests=TESTS, entry="add", memory=True):
        return eco_core.solve(prompt, tests, entry, tiers, task_id="t_add", log_path=self.log,
                              allow_cloud=False, memory_path=self.mem if memory else None)

    def stored(self):
        if not os.path.exists(self.mem):
            return []
        with open(self.mem, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_a_second_solve_is_a_memory_hit_that_calls_no_tier(self):
        pc, calls = fake("pc", GOOD)
        first = self.solve([pc])
        self.assertEqual(first["solved_by"], "pc")
        self.assertEqual([(a["tier"], a.get("memory")) for a in first["attempts"]], [("memory", "miss"), ("pc", None)])

        second = self.solve([pc])
        self.assertEqual(len(calls), 1, "no tier should be called on a memory hit")
        self.assertEqual(second["solved_by"], "memory")
        self.assertEqual(len(second["attempts"]), 1)
        hit = second["attempts"][0]
        self.assertEqual((hit["tier"], hit["memory"], hit["passed"], hit["stored_tier"]), ("memory", "hit", True, "pc"))
        self.assertEqual(second["cost_usd"], 0.0)
        self.assertEqual(second["estimated_cloud_cost_avoided_usd"], round(eco_core.estimate_cloud_cost(PROMPT), 6))
        self.assertEqual(eco_core.memory_count(self.mem), 1)

    def test_b_failing_answers_are_never_stored(self):
        pi, _ = fake("pi", BAD)
        result = self.solve([pi])
        self.assertIsNone(result["solved_by"])
        self.assertEqual(self.stored(), [], "nothing is stored when nobody solved it")

        pc, _ = fake("pc", GOOD)
        self.solve([pi, pc])
        entries = self.stored()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["code"], entries[0]["tier"]), (GOOD_CODE, "pc"))
        for field in ("key", "code", "tier", "seconds", "stored_at"):
            self.assertIn(field, entries[0])

    def test_c_stored_answer_that_now_fails_is_ignored(self):
        key = eco_core.memory_key(PROMPT, TESTS, "add")
        eco_core.memory_store(key, BAD_CODE, "pi", 1.0, self.mem)
        pc, calls = fake("pc", GOOD)
        result = self.solve([pc])
        self.assertEqual(len(calls), 1, "the ladder must run after a failed re-check")
        self.assertEqual(result["solved_by"], "pc")
        miss = result["attempts"][0]
        self.assertEqual((miss["tier"], miss["memory"], miss["passed"]), ("memory", "miss", False))
        self.assertIn("re-check", miss["error"])
        # The fresh, verified answer replaces the stale one for next time.
        self.assertEqual(eco_core.memory_lookup(key, self.mem)["code"], GOOD_CODE)
        self.assertEqual(self.solve([pc])["solved_by"], "memory")

    def test_d_changed_prompt_tests_or_entry_is_a_miss(self):
        pc, calls = fake("pc", GOOD)
        self.solve([pc])
        variants = [
            {"prompt": PROMPT + " "},
            {"tests": TESTS + [[[0, 0], 0]]},
            {"tests": [TESTS[1], TESTS[0]]},
        ]
        for i, change in enumerate(variants, start=2):
            result = self.solve([pc], **change)
            self.assertEqual(result["attempts"][0]["memory"], "miss", f"variant {change} should miss")
            self.assertEqual(len(calls), i)
        self.assertNotEqual(eco_core.memory_key(PROMPT, TESTS, "add"), eco_core.memory_key(PROMPT, TESTS, "plus"))

    def test_e_memory_disabled_behaves_as_before(self):
        pc, calls = fake("pc", GOOD)
        first = self.solve([pc], memory=False)
        second = self.solve([pc], memory=False)
        self.assertEqual(len(calls), 2)
        for result in (first, second):
            self.assertEqual(result["solved_by"], "pc")
            self.assertEqual([a["tier"] for a in result["attempts"]], ["pc"])
            self.assertEqual(set(result), {"task_id", "solved_by", "attempts", "skipped", "cost_usd", "seconds",
                                           "estimated_cloud_cost_avoided_usd"})
            self.assertNotIn("memory", result["attempts"][0])
        self.assertFalse(os.path.exists(self.mem))

    def test_clear_memory(self):
        pc, _ = fake("pc", GOOD)
        self.solve([pc])
        self.assertEqual(eco_core.memory_count(self.mem), 1)
        eco_core.clear_memory(self.mem)
        self.assertEqual(eco_core.memory_count(self.mem), 0)
        eco_core.clear_memory(self.mem)  # clearing an empty memory is fine


class MemoryServerTests(ServerCase):
    def test_hit_then_clear_through_the_api(self):
        pc, calls = fake("pc", GOOD)
        self.start([pc], pc, memory=True)
        self.assertEqual(json.loads(self.request("/api/memory")[1]), {"enabled": True, "count": 0})
        first = self.wait_for(self.solve(eco=True), lambda s: s["status"] != "running")
        self.assertEqual([a["tier"] for a in first["attempts"]], ["memory", "pc"])
        self.assertEqual(first["attempts"][0]["memory"], "miss")
        second = self.wait_for(self.solve(eco=True), lambda s: s["status"] != "running")
        self.assertEqual(second["result"]["solved_by"], "memory")
        self.assertEqual(second["attempts"][0]["memory"], "hit")
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(self.request("/api/memory")[1])["count"], 1)

        # Clearing uses the same guards as /api/solve.
        self.assertEqual(self.request("/api/memory/clear", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/memory/clear", {}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(json.loads(self.request("/api/memory")[1])["count"], 1)
        status, text = self.request("/api/memory/clear", {})
        self.assertEqual((status, json.loads(text)["count"]), (200, 0))

    def test_eco_off_does_not_use_memory(self):
        pc, _ = fake("pc", GOOD)
        cloud, cloud_calls = fake("cloud", GOOD, kind="cloud")
        self.start([pc], cloud, allow_cloud=True, memory=True)
        self.wait_for(self.solve(eco=True), lambda s: s["status"] != "running")
        state = self.wait_for(self.solve(eco=False), lambda s: s["status"] != "running")
        self.assertEqual([a["tier"] for a in state["attempts"]], ["cloud"])
        self.assertEqual(len(cloud_calls), 1)

    def test_f_api_key_never_in_memory_or_job_responses(self):
        leaky_answer = f"```python\n# {FAKE_KEY}\ndef add(a, b):\n    return a + b\n```"
        with mock.patch.dict(os.environ, {"MODEL_API_KEY": FAKE_KEY}):
            leaky, _ = fake("pc", leaky_answer)
            self.start([leaky], leaky, memory=True)
            first_id = self.solve(eco=True)
            first = self.wait_for(first_id, lambda s: s["status"] != "running")
            second_id = self.solve(eco=True)
            self.wait_for(second_id, lambda s: s["status"] != "running")
            bodies = [self.request(p)[1] for p in ("/api/memory", f"/api/jobs/{first_id}", f"/api/jobs/{second_id}")]
            bodies.append(self.request("/api/memory/clear", {})[1])
        self.assertEqual(first["result"]["solved_by"], "pc")
        for body in bodies:
            self.assertNotIn(FAKE_KEY, body)
            self.assertNotIn("SENTINEL", body)
        # Code containing the key is never written to memory, so the repeat is a miss.
        self.assertFalse(os.path.exists(self.memory) and FAKE_KEY in open(self.memory, encoding="utf-8").read())
        self.assertIn('"memory": "miss"', bodies[2])


if __name__ == "__main__":
    unittest.main()
