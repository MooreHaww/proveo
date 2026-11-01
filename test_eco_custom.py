"""Tests for custom tasks (the user's own prompt, entry and assert-based tests).

Fake tiers and temp files only. No real models, no cloud calls.
Run with: python -m unittest -v test_eco_custom
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import bench_coding
import eco_core
import eco_server
from test_eco_server import FAKE_KEY, ServerCase, fake

PROMPT = "Write a Python function is_palindrome(s) that returns True if s reads the same both ways."
ENTRY = "is_palindrome"
TESTS_CODE = 'assert is_palindrome("abba") == True\nassert is_palindrome("abc") == False\n'
GOOD = "```python\ndef is_palindrome(s):\n    return s == s[::-1]\n```"
WRONG = "```python\ndef is_palindrome(s):\n    return True\n```"
BLOCKED_ANSWER = "```python\nimport os\ndef is_palindrome(s):\n    return s == s[::-1]\n```"
LOOP = "```python\ndef is_palindrome(s):\n    while True:\n        pass\n```"
EXITS_EARLY = "```python\ndef is_palindrome(s):\n    return True\nexit(0)\n```"


class CustomCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "eco_log.jsonl")
        self.mem = os.path.join(self.tmp.name, "eco_memory.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def solve(self, tiers, tests_code=TESTS_CODE, memory=False):
        task = eco_core.make_custom_task(PROMPT, ENTRY, tests_code)
        result = eco_core.solve(task["prompt"], None, task["entry"], tiers, log_path=self.log, allow_cloud=False,
                                memory_path=self.mem if memory else None, tests_code=task["tests_code"])
        return task, result

    def test_passing_custom_task_through_the_ladder(self):
        pi, pi_calls = fake("pi", GOOD)
        pc, pc_calls = fake("pc", GOOD)
        task, result = self.solve([pi, pc])
        self.assertEqual(result["solved_by"], "pi")
        self.assertEqual(pc_calls, [])
        self.assertEqual(pi_calls, [PROMPT + " Return only the code."], "same ending as the benchmark prompts")
        self.assertEqual(result["attempts"][0]["first_failure"], "")
        key = eco_core.memory_key(task["prompt"], None, ENTRY, TESTS_CODE)
        self.assertEqual(result["task_id"], "custom:" + key[:8])
        self.assertRegex(result["task_id"], r"^custom:[0-9a-f]{8}$")
        with open(self.log, encoding="utf-8") as f:
            entry = json.loads(f.read().splitlines()[-1])
        self.assertEqual((entry["task_id"], entry["solved_by"]), (result["task_id"], "pi"))
        self.assertNotIn("tests_code", json.dumps(entry), "the log holds results, not the user's tests")

    def test_failing_assertion_escalates_and_reports_first_failure(self):
        pi, _ = fake("pi", WRONG)
        pc, pc_calls = fake("pc", GOOD)
        _, result = self.solve([pi, pc])
        self.assertEqual(result["solved_by"], "pc")
        self.assertEqual(len(pc_calls), 1)
        first = result["attempts"][0]
        self.assertFalse(first["passed"])
        self.assertEqual(first["error"], "tests failed")
        self.assertEqual(first["first_failure"], 'tests line 2: assert is_palindrome("abc") == False')

    def test_the_log_never_holds_the_failure_text(self):
        pi, _ = fake("pi", WRONG)
        pc, _ = fake("pc", GOOD)
        _, result = self.solve([pi, pc])
        self.assertIn("first_failure", result["attempts"][0], "the live result keeps it")
        with open(self.log, encoding="utf-8") as f:
            line = f.read()
        entry = json.loads(line)
        self.assertNotIn("first_failure", line)
        self.assertNotIn("abc", line, "the failed check")
        self.assertEqual((entry["attempts"][0]["passed"], entry["attempts"][0]["error"]), (False, "tests failed"))

    def test_the_log_never_holds_a_benchmark_failure_either(self):
        pi, _ = fake("pi", WRONG)
        result = eco_core.solve(PROMPT, [(("abc",), False)], ENTRY, [pi], log_path=self.log, allow_cloud=False)
        self.assertIn("input=", result["attempts"][0]["first_failure"])
        with open(self.log, encoding="utf-8") as f:
            line = f.read()
        self.assertNotIn("first_failure", line)
        self.assertNotIn("abc", line)

    def test_first_failure_is_capped_and_shows_other_errors(self):
        long_assert = 'assert is_palindrome("ab") == True, "' + "x" * 500 + '"\n'
        r = bench_coding.run_custom_tests("def is_palindrome(s):\n    return False", long_assert)
        self.assertEqual(len(r["first_failure"]), 300)
        r = bench_coding.run_custom_tests("def is_palindrome(s):\n    return 1 / 0", TESTS_CODE)
        self.assertEqual(r["first_failure"],
                         'ZeroDivisionError: division by zero (at tests line 1: assert is_palindrome("abba") == True)')

    def test_blocked_import_in_the_answer_is_rejected(self):
        pi, _ = fake("pi", BLOCKED_ANSWER)
        pc, _ = fake("pc", GOOD)
        _, result = self.solve([pi, pc])
        self.assertEqual(result["attempts"][0]["error"], "blocked: import os")
        self.assertIn("rejected before running", result["attempts"][0]["first_failure"])
        self.assertEqual(result["solved_by"], "pc")

    def test_tests_code_may_use_any_import(self):
        tests = ("import os\nimport sys\nimport subprocess\nfrom pathlib import Path\n"
                 "assert os.getcwd() and sys.version and Path('.').exists()\n" + TESTS_CODE)
        pi, _ = fake("pi", GOOD)
        _, result = self.solve([pi], tests_code=tests)
        self.assertEqual(result["solved_by"], "pi", result["attempts"][0])

    def test_infinite_loop_fails_cleanly_within_the_limit(self):
        pi, _ = fake("pi", LOOP)
        pc, _ = fake("pc", GOOD)
        start = time.time()
        _, result = self.solve([pi, pc])
        elapsed = time.time() - start
        self.assertEqual(result["attempts"][0]["error"], "timeout")
        self.assertIn("timed out after 10 s", result["attempts"][0]["first_failure"])
        self.assertEqual(result["solved_by"], "pc")
        self.assertLess(elapsed, bench_coding.CHECK_TIMEOUT + 5)

    def test_exiting_before_the_tests_is_not_a_pass(self):
        pi, _ = fake("pi", EXITS_EARLY)
        _, result = self.solve([pi])
        self.assertIsNone(result["solved_by"])
        self.assertEqual(result["attempts"][0]["error"], "tests did not finish")

    def test_memory_hit_on_exact_repeat_and_miss_when_tests_change(self):
        pi, calls = fake("pi", GOOD)
        self.solve([pi], memory=True)
        _, again = self.solve([pi], memory=True)
        self.assertEqual((again["solved_by"], len(calls)), ("memory", 1))
        self.assertEqual(again["attempts"][0]["memory"], "hit")
        changed_tests = TESTS_CODE + 'assert is_palindrome("") == True\n'
        task, changed = self.solve([pi], tests_code=changed_tests, memory=True)
        self.assertEqual(changed["attempts"][0]["memory"], "miss")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(changed["task_id"], again["task_id"])
        self.assertNotEqual(eco_core.memory_key(task["prompt"], None, ENTRY, TESTS_CODE),
                            eco_core.memory_key(task["prompt"], None, "other_name", TESTS_CODE))

    def test_benchmark_memory_keys_are_unchanged(self):
        tests = [[[1, 2], 3]]
        self.assertEqual(eco_core.memory_key("p", tests, "add"), eco_core.memory_key("p", tests, "add", None))
        self.assertNotEqual(eco_core.memory_key("p", None, "add", ""), eco_core.memory_key("p", None, "add"))

    def test_invalid_input_is_rejected_in_plain_words(self):
        cases = [
            (("x" * 4001, ENTRY, TESTS_CODE), "prompt is too long: 4,001 characters (the limit is 4,000)"),
            (("   ", ENTRY, TESTS_CODE), "prompt is empty"),
            ((None, ENTRY, TESTS_CODE), "prompt is empty"),
            ((PROMPT, ENTRY, "assert is_palindrome('a')\n" + "#" * 10_000), "tests_code is too long"),
            ((PROMPT, ENTRY, ""), "tests_code is empty"),
            ((PROMPT, "1abc", TESTS_CODE), "entry must be the name of the function"),
            ((PROMPT, "is palindrome", TESTS_CODE), "entry must be the name of the function"),
            ((PROMPT, "class", TESTS_CODE), "entry must be the name of the function"),
            ((PROMPT, ENTRY, "assert is_palindrome(\n"), "tests_code is not valid Python (line 1"),
            ((PROMPT, ENTRY, "print(is_palindrome('a'))"), "tests_code has no assert statement"),
            ((PROMPT, ENTRY, "assert other('a') == True"), "tests_code never calls is_palindrome"),
        ]
        for args, expected in cases:
            with self.assertRaises(ValueError, msg=expected) as ctx:
                eco_core.make_custom_task(*args)
            self.assertIn(expected, str(ctx.exception))
        ok = eco_core.make_custom_task("p" * 4000, ENTRY, "assert is_palindrome('a')\n" + "#" * 9_000)
        self.assertEqual(len(ok["prompt"]), 4000 + len(" Return only the code."))
        self.assertEqual(eco_core.make_custom_task(PROMPT + " Return only the code.", ENTRY, TESTS_CODE)["prompt"],
                         PROMPT + " Return only the code.", "the ending is not doubled")


class CustomServerTests(ServerCase):
    def custom(self, **change):
        return {"prompt": PROMPT, "entry": ENTRY, "tests_code": TESTS_CODE, **change}

    def test_custom_task_through_the_api(self):
        pi, _ = fake("pi", WRONG)
        pc, pc_calls = fake("pc", GOOD)
        self.start([pi, pc], pc, memory=True)
        status, text = self.request("/api/solve", {"custom": self.custom(), "eco": True})
        self.assertEqual(status, 202, text)
        job_id = json.loads(text)["job_id"]
        state = self.wait_for(job_id, lambda s: s["status"] != "running")
        self.assertEqual(state["result"]["solved_by"], "pc")
        self.assertRegex(state["task_id"], r"^custom:[0-9a-f]{8}$")
        self.assertEqual((state["custom"], state["task_file"]), (True, None))
        self.assertIn("NOT a real sandbox", state["check_note"])
        self.assertEqual([a["tier"] for a in state["attempts"]], ["memory", "pi", "pc"])
        self.assertIn('assert is_palindrome("abc") == False', state["attempts"][1]["first_failure"])
        self.assertEqual(pc_calls, [PROMPT + " Return only the code."])
        # Exact repeat: a memory hit; the benchmark path is unaffected.
        again = self.wait_for(json.loads(self.request("/api/solve", {"custom": self.custom()})[1])["job_id"],
                              lambda s: s["status"] != "running")
        self.assertEqual(again["result"]["solved_by"], "memory")
        self.assertNotIn("check_note", self.wait_for(self.solve(eco=True), lambda s: s["status"] != "running"))

    def run_custom(self):
        status, text = self.request("/api/solve", {"custom": self.custom(), "eco": True})
        self.assertEqual(status, 202, text)
        return self.wait_for(json.loads(text)["job_id"], lambda s: s["status"] != "running")

    def test_the_job_carries_the_passing_answer_and_the_log_does_not(self):
        pi, _ = fake("pi", WRONG)
        pc, _ = fake("pc", GOOD)
        self.start([pi, pc], pc, memory=True)
        state = self.run_custom()
        self.assertEqual(state["answer"], {"tier": "pc", "code": "def is_palindrome(s):\n    return s == s[::-1]", "passed": True, "first_failure": ""})
        self.assertNotIn(PROMPT, json.dumps(state), "never the prompt")
        self.assertNotIn("abba", json.dumps(state), "never the checks")
        with open(self.log, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("task_id", log)
        for private in ("s[::-1]", "return True", '"answer"', '"code"', PROMPT, "abba"):
            self.assertNotIn(private, log)
        again = self.run_custom()
        self.assertEqual((again["answer"]["tier"], again["answer"]["passed"]), ("memory", True))
        self.assertIn("s[::-1]", again["answer"]["code"])

    def test_with_no_pass_the_job_carries_the_last_answer_and_its_failed_check(self):
        pi, _ = fake("pi", GOOD.replace("s == s[::-1]", "False"))
        pc, _ = fake("pc", WRONG)
        self.start([pi, pc], pc)
        answer = self.run_custom()["answer"]
        self.assertEqual((answer["tier"], answer["passed"], answer["code"]), ("pc", False, "def is_palindrome(s):\n    return True"))
        self.assertIn('assert is_palindrome("abc") == False', answer["first_failure"])

    def test_a_tier_that_never_answered_leaves_no_answer(self):
        pi, _ = fake("pi", GOOD)
        pi.ask = mock.Mock(side_effect=OSError("down"))
        self.start([pi], pi)
        self.assertNotIn("answer", self.run_custom())

    def test_a_benchmark_job_has_no_answer(self):
        pi, _ = fake("pi", GOOD)
        self.start([pi], pi)
        self.assertNotIn("answer", self.wait_for(self.solve(eco=True), lambda s: s["status"] != "running"))

    def test_a_key_in_an_answer_is_scrubbed_from_the_response(self):
        key = "made-up-answer-key-0123456789"
        pi, _ = fake("pi", f"```python\n# {key}\ndef is_palindrome(s):\n    return s == s[::-1]\n```")
        self.start([pi], pi)
        self.assertEqual(self.request("/api/key", {"name": "Mine", "key": key, "provider": "Other"})[0], 200)
        status, text = self.request("/api/solve", {"custom": self.custom(), "eco": True})
        job_id = json.loads(text)["job_id"]
        self.wait_for(job_id, lambda s: s["status"] != "running")
        raw = self.request(f"/api/jobs/{job_id}")[1]
        self.assertIn('"answer"', raw)
        self.assertNotIn(key, raw)

    def test_large_custom_task_within_the_limits_is_accepted(self):
        pi, _ = fake("pi", GOOD)
        self.start([pi], pi)
        tests = TESTS_CODE + "# " + "\"quoted\" " * 980 + "\n"  # under 10,000 characters, about 14 KB of JSON
        self.assertLess(len(tests), 10_000)
        status, text = self.request("/api/solve", {"custom": self.custom(prompt="p" * 4000, tests_code=tests)})
        self.assertEqual(status, 202, text)

    def test_invalid_custom_requests_are_rejected_in_plain_words(self):
        pi, pi_calls = fake("pi", GOOD)
        self.start([pi], pi)
        cases = [
            ({"custom": self.custom(prompt="x" * 4001)}, "prompt is too long: 4,001 characters"),
            ({"custom": self.custom(tests_code="y" * 10_001)}, "tests_code is too long"),
            ({"custom": self.custom(entry="not-a-name")}, "entry must be the name of the function"),
            ({"custom": {"prompt": PROMPT, "entry": ENTRY}}, "custom is missing tests_code"),
            ({"custom": self.custom(task_id="x")}, "custom has unknown field(s) task_id"),
            ({"custom": "write me a function"}, "custom must be an object"),
            ({"custom": self.custom(), "task_file": "coding_tasks_fake.py"}, "send either custom or task_file"),
        ]
        for body, expected in cases:
            status, text = self.request("/api/solve", body)
            self.assertEqual(status, 400, body)
            self.assertIn(expected, json.loads(text)["error"])
        ok = {"custom": self.custom()}
        self.assertEqual(self.request("/api/solve", ok, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/solve", ok, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/solve", ok, {"Host": "evil.example"})[0], 403)
        self.assertEqual(pi_calls, [])
        self.assertFalse(os.path.exists(self.log), "rejected requests run and log nothing")

    def test_a_failed_check_shows_in_the_job_but_not_in_the_log(self):
        pi, _ = fake("pi", WRONG)
        self.start([pi], pi)
        state = self.run_custom()
        self.assertIn('assert is_palindrome("abc") == False', state["attempts"][0]["first_failure"])
        self.assertIn('assert is_palindrome("abc") == False', state["answer"]["first_failure"])
        with open(self.log, encoding="utf-8") as f:
            log = f.read()
        self.assertTrue(log)
        for private in ("first_failure", "is_palindrome", "abc"):
            self.assertNotIn(private, log)

    def test_api_key_never_in_responses_or_log(self):
        # The user's own assertion message contains both keys (env and in-memory), so the
        # first_failure text would carry them.
        other_key = FAKE_KEY.replace("SENTINEL", "SENTINEL2")
        tests = f'assert is_palindrome("abba") == True\nassert is_palindrome("ab") == True, "{FAKE_KEY} {other_key}"\n'
        with mock.patch.dict(os.environ, {"MODEL_API_KEY": FAKE_KEY}):
            pi, _ = fake("pi", GOOD)
            self.start([pi], pi)
            self.request("/api/key", {"key": other_key})
            status, text = self.request("/api/solve", {"custom": self.custom(tests_code=tests)})
            job_id = json.loads(text)["job_id"]
            state_text = self.request(f"/api/jobs/{job_id}")[1]
            self.wait_for(job_id, lambda s: s["status"] != "running")
            bodies = [text, self.request(f"/api/jobs/{job_id}")[1], self.request("/api/stats")[1],
                      self.request("/api/solve", {"custom": self.custom(prompt=FAKE_KEY * 500)})[1], state_text]
            with open(self.log, encoding="utf-8") as f:
                bodies.append(f.read())
        self.assertIn("[redacted]", bodies[1])
        for body in bodies:
            self.assertNotIn(FAKE_KEY, body)
            self.assertNotIn(other_key, body)
            self.assertNotIn("SENTINEL", body)


if __name__ == "__main__":
    unittest.main()
