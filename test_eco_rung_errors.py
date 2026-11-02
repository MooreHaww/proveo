"""What a rung's failure looks like when it reaches the page: real errors from fake servers, and the key scrub.

desktop/test/lib.test.js checks that the page's askProblem and plainFailure word these texts plainly. These tests
pin the raw texts it reads, so a change of error text here can't quietly turn a plain message into the generic one.
Fake servers on 127.0.0.1 and temp files only. No real models, no cloud calls.
Run with: python -m unittest -v test_eco_rung_errors
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bench_coding
import eco_core
from eco_core import Tier
from test_eco_server import FAKE_KEY, GOOD, ServerCase, fake

GOOD_NAME = "Made up"


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Misbehaving(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = self.server.mode
        if mode == "slow":
            time.sleep(1.5)
        elif mode == "drop":
            self.close_connection = True
            return
        status, body = (500, b"{}") if mode == "error" else (200, b"this is not JSON" if mode == "junk" else b"{}")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RawErrorTexts(unittest.TestCase):
    """solve() records these as "ask: <repr>", and the page picks its wording from them."""

    def error_from(self, base_url, timeout=5):
        tier = Tier("t", "local", lambda prompt: eco_core.ask_openai_chat(base_url, "m", prompt, timeout=timeout))
        with tempfile.TemporaryDirectory() as tmp:
            result = eco_core.solve("p", [((1,), 1)], "f", [tier], log_path=os.path.join(tmp, "log.jsonl"), allow_cloud=False)
        return result["attempts"][0]["error"]

    def misbehaving(self, mode):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Misbehaving)
        server.daemon_threads = True
        server.mode = mode
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def test_a_closed_port(self):
        error = self.error_from(f"http://127.0.0.1:{closed_port()}/v1")
        self.assertTrue(error.startswith("ask: "), error)
        self.assertRegex(error, "refused|10061")

    def test_a_timeout(self):
        error = self.error_from(self.misbehaving("slow"), timeout=0.2)
        self.assertTrue(error.startswith("ask: "), error)
        self.assertRegex(error, "timed out")

    def test_a_dropped_connection(self):
        self.assertRegex(self.error_from(self.misbehaving("drop")), "RemoteDisconnected|Reset|10054")

    def test_an_http_error_has_its_status(self):
        self.assertRegex(self.error_from(self.misbehaving("error")), r"HTTPError\D{0,4}500\b")

    def test_a_reply_that_is_not_json(self):
        self.assertIn("JSONDecodeError", self.error_from(self.misbehaving("junk")))

    def test_a_reply_with_no_choices(self):
        self.assertIn("KeyError", self.error_from(self.misbehaving("empty")))


class RawErrorsNeverCarryTheKey(ServerCase):
    def test_a_key_in_an_error_reaches_neither_the_job_nor_the_log(self):
        down, _ = fake("down", GOOD, error=ConnectionRefusedError(f"refused, sent {FAKE_KEY}"))
        self.start([down])
        status, text = self.request("/api/key", {"name": GOOD_NAME, "key": FAKE_KEY, "provider": "Other"})
        self.assertEqual(status, 200, "the status has no key either")
        self.assertNotIn(FAKE_KEY, text)
        job_id = self.solve(eco=True)
        self.wait_for(job_id, lambda s: s["status"] != "running")
        status, raw = self.request(f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY, raw)
        error = next(a["error"] for a in json.loads(raw)["attempts"] if a["tier"] == "down")
        self.assertTrue(error.startswith("ask: "), error)
        self.assertIn("refused", error, "the rest of the detail stays")
        with open(self.log, encoding="utf-8") as f:
            self.assertNotIn(FAKE_KEY, f.read())


class FailureTextsForThePage(unittest.TestCase):
    """The checker's real texts, which plainFailure rewrites. Table checks arrive as assert lines too."""

    def test_a_crash_in_a_check_names_the_check(self):
        tests = 'assert calc("(2+3)*4") == 20\n'
        answer = "def calc(s):\n    return [].pop()\n"
        r = bench_coding.run_custom_tests(answer, "assert 1 == 1\n" + tests)
        self.assertEqual(r["first_failure"], 'IndexError: pop from empty list (at tests line 2: assert calc("(2+3)*4") == 20)')

    def test_a_failed_assert_names_the_check(self):
        r = bench_coding.run_custom_tests("def f(x):\n    return 1\n", "assert f(1) == 1\nassert f(2) == 2\n")
        self.assertEqual(r["first_failure"], "tests line 2: assert f(2) == 2")

    def test_a_benchmark_failure_lists_the_input(self):
        task = {"entry": "f", "tests": [[[1], 1], [[2], 2]]}
        ok, _, first = eco_core.check_detail("def f(x):\n    return 1\n", task)
        self.assertFalse(ok)
        self.assertEqual(first, "1 of 2 tests passed; input=[2] expected=2 got=1")


if __name__ == "__main__":
    unittest.main()
