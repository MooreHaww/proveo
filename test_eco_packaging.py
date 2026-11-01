"""Tests for packaging: where the log and memory go, the checker's bundled mode, the built backend.

The Built* classes run the real build/eco-backend/eco-backend.exe, and skip when it has not been built
(python build_backend.py). Fake model servers, made-up keys and temp folders only. No real models,
no cloud calls. Run with: python -m unittest -v test_eco_packaging
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import bench_coding
import build_backend
import eco_core
import eco_server
from test_eco_settings import FakeModelServer, local_tier

REPO = os.path.dirname(os.path.abspath(__file__))
BUILT = os.path.join(REPO, "build", "eco-backend")
EXE = os.path.join(BUILT, "eco-backend.exe")
KEY = "made-up-packaging-key-0123456789"
TASK = {"id": "t", "entry": "add", "tests": [[[1, 2], 3], [[5, 5], 10]]}
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
LOOP = "def add(a, b):\n    while True:\n        pass\n"
CUSTOM = {"prompt": "Write add(a, b) that returns the sum.", "entry": "add", "tests_code": "assert add(1, 2) == 3\nassert add(5, 5) == 10\n"}
# Uses less common standard library modules, none of which the backend itself imports.
GOOD_STDLIB = """
import cmath, colorsys, decimal, fractions, graphlib, heapq, sqlite3, statistics, textwrap, zoneinfo
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor

@dataclass
class Pair:
    a: int
    b: int

def add(a, b):
    db = sqlite3.connect(":memory:")
    total = db.execute("select ? + ?", (a, b)).fetchone()[0]
    assert fractions.Fraction(total) == decimal.Decimal(total) == statistics.mean([total])
    assert heapq.nlargest(1, [total]) == [total] and ET.fromstring("<a/>").tag == "a"
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda p: p.a + p.b, Pair(a, b)).result()
"""
# Gets past the word filter on purpose, as a hostile answer would: the checks below are what really holds.
NO_KEY_NO_STDIN = """
def add(a, b):
    imp = __builtins__["__imp" + "ort__"]
    env, stdin = imp("o" + "s").environ, imp("sy" + "s").stdin
    if "MODEL_API_KEY" in env or stdin.read() != "":
        return "the key or stdin reached the checked code"
    return a + b
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DataLocation(unittest.TestCase):
    def start_main(self, *argv):
        """Run eco_server.main() up to the point where it would listen; return what EcoApp got."""
        with mock.patch.object(sys, "argv", ["eco_server.py", *argv]), mock.patch("os.chdir") as chdir, \
                mock.patch("eco_server.EcoApp") as app, mock.patch("eco_server.make_server", side_effect=OSError("stop")):
            with self.assertRaises(SystemExit):
                eco_server.main()
        chdir.assert_called_once_with(eco_server.HERE)
        return app.call_args.kwargs

    def test_without_flags_the_cli_uses_results_as_before(self):
        self.assertEqual(self.start_main(), {"log_path": "results/eco_log.jsonl", "memory_path": "results/eco_memory.jsonl"})
        self.assertEqual((eco_core.LOG_PATH, eco_core.MEMORY_PATH), ("results/eco_log.jsonl", "results/eco_memory.jsonl"))

    def test_flags_are_used_and_relative_ones_start_from_where_it_was_run(self):
        got = self.start_main("--log", "here/log.jsonl", "--memory", os.path.abspath("mem.jsonl"))
        self.assertEqual(got, {"log_path": os.path.abspath("here/log.jsonl"), "memory_path": os.path.abspath("mem.jsonl")})


class ServerProcess(unittest.TestCase):
    """A backend process with a temp data folder and a fake local or cloud rung."""

    def start_process(self, command, env=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "profile", "eco")
        self.log = os.path.join(self.data, "eco_log.jsonl")
        self.memory = os.path.join(self.data, "eco_memory.jsonl")
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        full_env = {k: v for k, v in os.environ.items() if k not in ("MODEL_API_KEY", "ALLOW_CLOUD")}
        full_env.update(ECO_DATA_DIR=self.data, **(env or {}))
        self.output = open(os.path.join(self.tmp.name, "output.txt"), "w+", encoding="utf-8")
        self.addCleanup(self.output.close)
        self.proc = subprocess.Popen([*command, "--port", str(port), "--log", self.log, "--memory", self.memory],
                                     stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT, env=full_env, cwd=self.tmp.name)
        self.addCleanup(self.stop_process)
        end = time.time() + 30
        while time.time() < end:
            try:
                self.get("/api/config")
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        self.fail("the backend did not start")

    def stop_process(self):
        self.proc.kill()
        self.proc.wait()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def fake(self, reply):
        srv = FakeModelServer(reply=reply)
        self.addCleanup(srv.close)
        return srv

    def run_job(self, body, tier, timeout=30):
        self.post("/api/settings", {"tiers": [tier]})
        self.post("/api/memory/clear", {})
        job = self.post("/api/solve", body)["job_id"]
        end = time.time() + timeout
        while time.time() < end:
            state = self.get(f"/api/jobs/{job}")
            if state["status"] != "running":
                return state["result"]
            time.sleep(0.1)
        self.fail("the job did not finish")


class DataLocationEndToEnd(ServerProcess):
    def test_the_log_memory_and_budget_all_use_the_given_paths(self):
        self.start_process([sys.executable, os.path.join(REPO, "eco_server.py")], {"ALLOW_CLOUD": "1"})
        self.post("/api/key", {"name": "Fake", "key": KEY})
        tier = {"id": "c", "name": "C", "kind": "cloud", "provider": "openai_compatible", "base_url": self.fake(GOOD).root + "/v1",
                "model": "m", "flags": {}, "enabled": True, "price_in_per_m": 1000.0, "price_out_per_m": 1000.0}
        result = self.run_job({"custom": CUSTOM}, tier)
        self.assertEqual(result["solved_by"], "c")
        self.assertTrue(os.path.exists(self.log))
        self.assertEqual(self.get("/api/memory")["count"], 1)
        self.assertTrue(os.path.exists(self.memory))
        self.assertEqual(self.get("/api/stats")["log"], self.log)
        self.assertGreater(self.get("/api/budget")["spent"], 0, "the budget reads the log the app writes")


class CheckerCommand(unittest.TestCase):
    def test_a_normal_python_uses_dash_i(self):
        self.assertEqual(bench_coding._check_command("x.py"), [sys.executable, "-I", "x.py"])

    def test_the_bundle_uses_its_own_check_mode(self):
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", "eco-backend.exe"):
            self.assertEqual(bench_coding._check_command("x.py"), ["eco-backend.exe", "--eco-check", "x.py"])


class CheckMode(unittest.TestCase):
    """eco_backend.py --eco-check, on a normal Python. The Built class repeats the important part on the exe."""

    def run_script(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "s.py"), "w", encoding="utf-8") as f:
                f.write(text)
            return subprocess.run([sys.executable, os.path.join(REPO, "eco_backend.py"), "--eco-check", "s.py"], cwd=tmp,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def test_runs_as_main_without_importing_the_server(self):
        p = self.run_script("import sys\nprint(__name__, sys.argv, 'eco_server' in sys.modules, 'requests' in sys.modules)\n")
        self.assertEqual(p.stdout.strip(), "__main__ ['s.py'] False False", p.stderr)

    def test_exit_codes_and_errors_pass_through(self):
        self.assertEqual(self.run_script("import sys\nsys.exit(3)\n").returncode, 3)
        self.assertEqual(self.run_script("").returncode, 0)
        p = self.run_script("assert 1 == 2, 'the reason'\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("AssertionError: the reason", p.stderr)

    def test_the_real_checkers_work_through_it(self):
        command = lambda script: [sys.executable, os.path.join(REPO, "eco_backend.py"), "--eco-check", script]
        with mock.patch.object(bench_coding, "_check_command", command):
            self.assertEqual(bench_coding.run_tests(GOOD, TASK)["passed"], 2)
            bad = bench_coding.run_tests(BAD, TASK)
            self.assertEqual((bad["passed"], bad["total"]), (0, 2))
            self.assertEqual(bench_coding.run_tests(LOOP, TASK, timeout=2)["error"], "timeout")
            self.assertTrue(bench_coding.run_custom_tests(GOOD, CUSTOM["tests_code"])["passed"])
            self.assertFalse(bench_coding.run_custom_tests(BAD, CUSTOM["tests_code"])["passed"])


class BuildChoices(unittest.TestCase):
    def test_only_the_benchmark_csvs_are_bundled(self):
        self.assertEqual(len(build_backend.RESULT_FILES), 9)
        for rel in build_backend.RESULT_FILES:
            self.assertRegex(rel, r"^results/coding_(pi|pc|cloud_minimal)_coding_tasks_(dev|dev2|heldout)\.csv$")

    def test_those_csvs_are_enough_for_the_measured_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "results"))
            for rel in build_backend.RESULT_FILES:
                shutil.copy(os.path.join(REPO, rel), os.path.join(tmp, "results"))
            old = os.getcwd()
            os.chdir(tmp)
            try:
                got = eco_server.measured_numbers()
            finally:
                os.chdir(old)
        os.chdir(REPO)
        self.assertEqual(got, eco_server.measured_numbers())

    def test_the_standard_library_is_listed_without_the_large_unneeded_parts(self):
        names = set(build_backend.stdlib_modules())
        for wanted in ("heapq", "fractions", "sqlite3", "_sqlite3", "xml.etree.ElementTree", "email.mime.text",
                       "concurrent.futures", "collections.abc", "zoneinfo", "json.decoder"):
            self.assertIn(wanted, names)
        for unwanted in ("tkinter", "_tkinter", "test.support", "idlelib", "lib2to3", "venv", "unittest.__main__", ""):
            self.assertNotIn(unwanted, names)


@unittest.skipUnless(os.path.exists(EXE), "build/eco-backend has not been built")
class BuiltCheckMode(unittest.TestCase):
    def run_script(self, text, sibling=False):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "s.py"), "w", encoding="utf-8") as f:
                f.write(text)
            if sibling:
                with open(os.path.join(tmp, "sibling_module.py"), "w", encoding="utf-8") as f:
                    f.write("VALUE = 1\n")
            return subprocess.run([EXE, "--eco-check", "s.py"], cwd=tmp, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)

    def test_like_python_dash_i(self):
        p = self.run_script("import sys\nprint(__name__, sys.argv, sys.frozen, sys.flags.isolated, sys.flags.no_user_site)\n")
        self.assertEqual(p.stdout.strip(), "__main__ ['s.py'] True 1 1", p.stderr)
        p = self.run_script("import sibling_module\n", sibling=True)
        self.assertEqual(p.returncode, 1, "neither the script's folder nor the working directory is importable")
        self.assertIn("No module named 'sibling_module'", p.stderr)

    def test_python_variables_do_not_reach_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "sibling_module.py"), "w", encoding="utf-8") as f:
                f.write("VALUE = 1\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": tmp}):
                p = self.run_script("import sibling_module\n")
        self.assertEqual(p.returncode, 1)

    def test_exit_codes_and_errors_pass_through(self):
        self.assertEqual(self.run_script("import sys\nsys.exit(3)\n").returncode, 3)
        p = self.run_script("assert 1 == 2, 'the reason'\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("AssertionError: the reason", p.stderr)

    def test_https_certificates_load(self):
        p = self.run_script("import ssl\nprint(len(ssl.create_default_context().get_ca_certs()) > 0)\n")
        self.assertEqual(p.stdout.strip(), "True", p.stderr)


@unittest.skipUnless(os.path.exists(EXE), "build/eco-backend has not been built")
class BuiltServer(ServerProcess):
    @staticmethod
    def listing():
        found = {}
        for folder, _, files in os.walk(BUILT):
            for name in files:
                path = os.path.join(folder, name)
                found[os.path.relpath(path, BUILT)] = os.path.getsize(path)
        return found

    def setUp(self):
        before = self.listing()
        self.addCleanup(lambda: self.assertEqual(self.listing(), before, "nothing is written into the bundle"))
        self.start_process([EXE], {"MODEL_API_KEY": KEY})  # must not reach the checked code

    def test_serves_the_page_and_the_measured_figures_from_the_bundle(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertIn("Eco mode", r.read().decode("utf-8"))
        with urllib.request.urlopen(self.base + "/assets/logo.svg", timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.get("/api/measured"), eco_server.measured_numbers())
        self.assertGreater(len(self.get("/api/tasks")["tasks"]), 0)

    def test_nothing_personal_is_bundled(self):
        names = {os.path.basename(p).lower() for p in self.listing()}
        self.assertFalse(names & {"router_log.csv", "cloud_calls.csv", "eco_log.jsonl", "eco_memory.jsonl", ".env", "settings.json"})
        self.assertEqual(sum(n.endswith(".csv") for n in names), 9)

    def check(self, reply, body=None):
        tier = local_tier("rung", self.fake(reply).root + "/v1")
        return self.run_job(body or {"custom": CUSTOM}, tier)

    def test_a_correct_answer_using_less_common_stdlib_modules_passes(self):
        self.assertEqual(self.check(GOOD_STDLIB)["solved_by"], "rung")

    def test_a_benchmark_task_goes_through_the_harness(self):
        good = ("def roman_to_int(s):\n    v = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}\n"
                "    total = 0\n    for i, c in enumerate(s):\n"
                "        total += -v[c] if i + 1 < len(s) and v[c] < v[s[i + 1]] else v[c]\n    return total\n")
        body = {"task_file": "coding_tasks_heldout.py", "task_id": "h01_roman_to_int"}
        self.assertEqual(self.check(good, body)["solved_by"], "rung")
        wrong = self.check("def roman_to_int(s):\n    return len(s)\n", body)
        self.assertIsNone(wrong["solved_by"])
        self.assertIn("1 of 7 tests passed", wrong["attempts"][-1]["first_failure"])

    def test_the_key_and_stdin_do_not_reach_the_checked_code(self):
        self.assertEqual(self.check(NO_KEY_NO_STDIN)["solved_by"], "rung")

    def test_a_wrong_answer_gives_the_same_reason_as_on_a_normal_python(self):
        wrong = self.check(BAD)
        self.assertIsNone(wrong["solved_by"])
        self.assertEqual(wrong["attempts"][-1]["first_failure"],
                         bench_coding.run_custom_tests(BAD, CUSTOM["tests_code"])["first_failure"])

    def test_the_time_limit(self):
        started = time.time()
        looped = self.check("def roman_to_int(s):\n    while True:\n        pass\n",
                            {"task_file": "coding_tasks_heldout.py", "task_id": "h01_roman_to_int"})
        self.assertIn("timed out after 10 s", looped["attempts"][-1]["first_failure"])
        self.assertLess(time.time() - started, 25)

    def test_the_log_and_memory_go_to_the_given_folder(self):
        self.assertEqual(self.check(GOOD)["solved_by"], "rung")
        self.assertTrue(os.path.exists(self.log))
        self.assertTrue(os.path.exists(self.memory))


if __name__ == "__main__":
    unittest.main()
