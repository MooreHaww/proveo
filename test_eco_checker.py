"""Tests for the code checker (run_tests, run_custom_tests): how its child process is started.

The desktop app once made every benchmark check time out: its watchdog thread blocked reading a
stdin pipe, and the checker child inherited that pipe and hung during Python startup.
No models, no network. Run with: python -m unittest -v test_eco_checker
"""
import json
import os
import subprocess
import sys
import textwrap
import unittest
import uuid
from unittest import mock

import bench_coding
import eco_core

REPO = os.path.dirname(os.path.abspath(__file__))
FAKE_KEY = "sk-test-SENTINEL-checker-0123456789"
TASK = {"id": "t", "entry": "add", "tests": [[[1, 2], 3], [[5, 5], 10]]}
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
LOOP = "def add(a, b):\n    while True:\n        pass\n"
TESTS_CODE = "assert add(1, 2) == 3\nassert add(5, 5) == 10\n"


def app_like_env():
    """What the desktop app gives its backend (see desktop/lib.js backendEnv)."""
    env = {k: v for k, v in os.environ.items() if k not in ("MODEL_API_KEY", "ALLOW_CLOUD")}
    env.update(ECO_DATA_DIR=os.path.join(REPO, "does-not-exist"), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return env


# A parent shaped like the backend at its worst: stdin is a pipe that a thread is blocked reading.
PARENT = textwrap.dedent("""
    import json, sys, threading, time
    threading.Thread(target=sys.stdin.buffer.read, daemon=True).start()
    time.sleep(0.3)
    sys.path.insert(0, sys.argv[1])
    import bench_coding
    good, tests = json.loads(sys.argv[2])
    task = {"id": "t", "entry": "add", "tests": [[[1, 2], 3], [[5, 5], 10]]}
    out = {"bench": bench_coding.run_tests(good, task, timeout=6),
           "custom": bench_coding.run_custom_tests(good, tests, timeout=6)}
    print(json.dumps(out), flush=True)
    import os
    os._exit(0)
""")


@unittest.skipUnless(sys.platform == "win32", "the hang needs a Windows synchronous named pipe")
class UnderABlockedStdinPipe(unittest.TestCase):
    def named_pipe(self):
        """A pipe of the kind Electron (libuv) gives a child as stdin. An anonymous pipe, as from
        subprocess.PIPE, does NOT show the hang: the old code passed under one."""
        import _winapi
        import msvcrt
        name = r"\\.\pipe\eco-checker-test-" + uuid.uuid4().hex
        server = _winapi.CreateNamedPipe(name, _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED,
                                         _winapi.PIPE_WAIT, 1, 65536, 65536, 0, _winapi.NULL)
        self.addCleanup(_winapi.CloseHandle, server)
        client = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                                    _winapi.OPEN_EXISTING, 0, _winapi.NULL)
        fd = msvcrt.open_osfhandle(client, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        return fd

    def test_both_checkers_pass_with_a_pipe_stdin_being_read(self):
        p = subprocess.run([sys.executable, "-c", PARENT, REPO, json.dumps([GOOD, TESTS_CODE])],
                           stdin=self.named_pipe(), capture_output=True, text=True, env=app_like_env(), timeout=40)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((out["bench"].get("passed"), out["bench"].get("error")), (2, None), out["bench"])
        self.assertTrue(out["custom"]["passed"], out["custom"])


class Reasons(unittest.TestCase):
    def test_correct_and_wrong_answers(self):
        self.assertEqual(eco_core.check_detail(GOOD, TASK), (True, "", ""))
        ok, error, first = eco_core.check_detail(BAD, TASK)
        self.assertEqual((ok, error), (False, ""))
        self.assertIn("0 of 2 tests passed", first)
        self.assertIn("expected=3 got=-1", first)

    def test_a_real_timeout_says_so(self):
        r = bench_coding.run_tests(LOOP, TASK, timeout=2)
        self.assertEqual(r["error"], "timeout")
        self.assertIn("timed out after 2 s", r["first_failure"])
        self.assertEqual(bench_coding.run_custom_tests(LOOP, TESTS_CODE, timeout=2)["error"], "timeout")

    def test_the_default_limit_is_ten_seconds(self):
        with mock.patch.object(bench_coding.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("python", 10)) as run:
            r = bench_coding.run_tests(GOOD, TASK)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertIn("timed out after 10 s", r["first_failure"])

    def test_a_child_that_cannot_start_is_not_called_a_timeout(self):
        with mock.patch.object(bench_coding.sys, "executable", os.path.join(REPO, "no-such-python.exe")):
            bench = bench_coding.run_tests(GOOD, TASK)
            custom = bench_coding.run_custom_tests(GOOD, TESTS_CODE)
        for r in (bench, custom):
            self.assertEqual(r["error"], "check did not start")
            self.assertIn("could not start", r["first_failure"])

    def test_an_abnormal_exit_reports_the_status(self):
        r = bench_coding.run_tests("quit(7)\n", TASK, timeout=6)
        self.assertEqual(r["error"], "check exited (7)")
        self.assertIn("status 7", r["first_failure"])
        ok, error, first = eco_core.check_detail("quit(7)\n", TASK)
        self.assertEqual((ok, error), (False, "check exited (7)"))
        self.assertIn("status 7", first)

    def test_the_rung_attempt_carries_the_reason(self):
        tier = eco_core.Tier("fake", "local", lambda prompt: BAD)
        with mock.patch.dict(os.environ, {"ECO_DATA_DIR": os.path.join(REPO, "does-not-exist")}):
            result = eco_core.solve("p", TASK["tests"], "add", [tier], task_id="t", log_path=os.devnull)
        self.assertIn("0 of 2 tests passed", result["attempts"][0]["first_failure"])


class ChildProperties(unittest.TestCase):
    def test_spawn_arguments(self):
        real_run = subprocess.run
        seen = []

        def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return real_run(*args, **kwargs)

        with mock.patch.dict(os.environ, {"MODEL_API_KEY": FAKE_KEY}), \
                mock.patch.object(bench_coding.subprocess, "run", side_effect=spy):
            bench_coding.run_tests(GOOD, TASK, timeout=6)
            bench_coding.run_custom_tests(GOOD, TESTS_CODE, timeout=6)
        self.assertEqual(len(seen), 2)
        for args, kwargs in seen:
            self.assertEqual(args[0][:2], [sys.executable, "-I"])
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("timeout", kwargs)
            self.assertNotIn("MODEL_API_KEY", kwargs["env"])
            self.assertNotIn(FAKE_KEY, kwargs["env"].values())

    def test_the_key_is_not_in_the_childs_environment_but_the_rest_is(self):
        # The user's own tests may import anything, so they can look at the real child environment.
        tests = TESTS_CODE + ("import os\nassert 'MODEL_API_KEY' not in os.environ\n"
                              "assert os.environ.get('ECO_CHECKER_MARK') == 'kept'\n")
        with mock.patch.dict(os.environ, {"MODEL_API_KEY": FAKE_KEY, "ECO_CHECKER_MARK": "kept"}):
            r = bench_coding.run_custom_tests(GOOD, tests, timeout=6)
        self.assertTrue(r["passed"], r)


if __name__ == "__main__":
    unittest.main()
