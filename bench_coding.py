import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests

import router  # reuse LOCAL_URL and the cloud settings

PC_URL = "http://localhost:8081/v1/chat/completions"

BLOCKED = ["import os", "from os", "import sys", "from sys", "subprocess", "shutil",
           "socket", "open(", "eval(", "exec(", "__import__", "pathlib",
           "requests", "urllib"]
# Every benchmark prompt ends with this sentence; custom task prompts get the same ending.
CODE_SUFFIX = " Return only the code."
CHECK_TIMEOUT = 10  # seconds for run_custom_tests, the same limit run_tests uses

HARNESS = """
import json, sys
src = open("answer.py", encoding="utf-8").read()
spec = json.load(open("spec.json", encoding="utf-8"))
ns = {}
try:
    exec(compile(src, "answer", "exec"), ns)
    fn = ns[spec["entry"]]
except Exception as e:
    print(json.dumps({"error": "load: " + repr(e)}))
    sys.exit(0)
passed = 0
fails = []
for args, expected in spec["tests"]:
    try:
        got = fn(*args)
        if got == expected:
            passed += 1
        else:
            fails.append([args, expected, got])
    except Exception as e:
        fails.append([args, expected, "EXC " + repr(e)])
print(json.dumps({"passed": passed, "total": len(spec["tests"]), "fails": fails}, default=repr))
"""

FIELDS = ["id", "passed", "tests_passed", "tests_total", "seconds", "in_tokens",
          "out_tokens", "cost_usd", "error", "first_failure", "code"]


def load_tasks(path):
    spec = importlib.util.spec_from_file_location("tasks_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TASKS


def ask_pi(prompt):
    body = {"messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500, "temperature": 0}
    start = time.time()
    r = requests.post(router.LOCAL_URL, json=body, timeout=600).json()
    return {"text": r["choices"][0]["message"]["content"],
            "seconds": time.time() - start,
            "in_tokens": r["usage"]["prompt_tokens"],
            "out_tokens": r["usage"]["completion_tokens"],
            "cost": 0.0}


def ask_pc(prompt):
    # enable_thinking off so Qwen3 does not spend tokens on a thinking phase
    body = {"messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    start = time.time()
    r = requests.post(PC_URL, json=body, timeout=600).json()
    return {"text": r["choices"][0]["message"]["content"],
            "seconds": time.time() - start,
            "in_tokens": r["usage"]["prompt_tokens"],
            "out_tokens": r["usage"]["completion_tokens"],
            "cost": 0.0}


def ask_cloud(prompt, effort=None, key=None, allowed=None, url=None, model=None):
    # key/allowed None: read MODEL_API_KEY / ALLOW_CLOUD from the environment, as before.
    # The Eco server passes its in-memory key ("" when none) and its ALLOW_CLOUD snapshot.
    key = os.environ.get("MODEL_API_KEY") if key is None else key
    allowed = os.environ.get("ALLOW_CLOUD") == "1" if allowed is None else allowed
    if not key or not allowed:
        raise SystemExit("Cloud is off. Set MODEL_API_KEY and ALLOW_CLOUD=1 in this window first.")
    effort = effort or os.environ.get("BENCH_EFFORT", router.CLOUD_EFFORT)
    body = {"model": model or router.CLOUD_MODEL, "input": prompt, "max_output_tokens": 3000,
            "reasoning": {"effort": effort}}
    start = time.time()
    r = requests.post(url or router.CLOUD_URL, json=body,
                      headers={"Authorization": "Bearer " + key}, timeout=600)
    seconds = time.time() - start
    r.raise_for_status()
    data = r.json()
    text = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text += part.get("text", "")
    usage = data.get("usage", {})
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return {"text": text, "seconds": seconds, "in_tokens": tin, "out_tokens": tout,
            "cost": tin * 1.25 / 1e6 + tout * 4.25 / 1e6}


def extract_code(text):
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def _checker_env():
    """The checker child's environment: the API key never reaches the process running model code."""
    return {k: v for k, v in os.environ.items() if k.upper() != "MODEL_API_KEY"}


def _check_command(script):
    """In the installed app sys.executable is the bundled backend, which acts as the child when told to."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--eco-check", script]
    return [sys.executable, "-I", script]


def _spawn_check(script, cwd, timeout):
    """Run script in a separate `python -I`: stdin closed, time limit, no API key.

    stdin must stay closed: on Windows a child that inherits a pipe which another thread here is
    reading blocks during Python startup until that read ends (the desktop app's watchdog did that).
    """
    return subprocess.run(_check_command(script), cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=_checker_env(), timeout=timeout)


def _timed_out(timeout):
    return f"timed out after {timeout} s (an infinite loop?)"


def _exit_reason(p):
    """A checker child that ended without a result: its exit status and the end of its stderr."""
    tail = p.stderr.strip()[-200:]
    return f"the check exited with status {p.returncode}" + (f": {tail}" if tail else " and no output")


def run_tests(code, task, timeout=CHECK_TIMEOUT):
    """Returns {"passed", "total", "fails"}, or {"error", "first_failure"} when there is no result."""
    for bad in BLOCKED:
        if bad in code:
            return {"error": "blocked: " + bad}
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "answer.py"), "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(tmp, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"entry": task["entry"], "tests": task["tests"]}, f)
        with open(os.path.join(tmp, "harness.py"), "w", encoding="utf-8") as f:
            f.write(HARNESS)
        try:
            p = _spawn_check("harness.py", tmp, timeout)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "first_failure": _timed_out(timeout)}
        except OSError as e:
            return {"error": "check did not start", "first_failure": f"the check could not start: {e}"}
        lines = p.stdout.strip().splitlines()
        if not lines:
            return {"error": f"check exited ({p.returncode})", "first_failure": _exit_reason(p)}
        try:
            return json.loads(lines[-1])
        except ValueError:
            return {"error": "bad output", "first_failure": _exit_reason(p)}


CUSTOM_FILE = "custom_check.py"
TESTS_HEADER = "\n\n# ---- the user's own tests ----\n"


def _first_failure(stderr, source, tests_start):
    """The failing assert line, or the last line of the error, at most 300 characters."""
    err_lines = [line for line in stderr.strip().splitlines() if line.strip()]
    last = err_lines[-1].strip() if err_lines else "the check failed with no error output"
    frames = [int(n) for f, n in re.findall(r'File "([^"]*)", line (\d+)', stderr)
              if os.path.basename(f) == CUSTOM_FILE]
    src = source.splitlines()

    def where(n):
        text = src[n - 1].strip() if 0 < n <= len(src) else ""
        if n >= tests_start:
            return f"tests line {n - tests_start + 1}: {text}"
        return f"answer line {n}: {text}"

    if frames and last.startswith("AssertionError"):
        msg = last[len("AssertionError"):].lstrip(": ").strip()
        text = where(frames[-1]) + (f" ({msg})" if msg else "")
    elif frames:
        tests = [n for n in frames if n >= tests_start]
        text = last + (f" (at {where(tests[0])})" if tests else f" (at {where(frames[-1])})")
    else:
        text = last
    return text[:300]


def run_custom_tests(code, tests_code, timeout=CHECK_TIMEOUT):
    """Check model code against the user's own assert-based tests_code.

    NOT a real sandbox: the code runs in a separate `python -I` process in a fresh temp
    directory with a time limit, and the model's code (never the user's tests) is screened with
    the same BLOCKED substring filter as run_tests. That stops accidents, not a determined attacker.
    Pass = exit code 0 AND a random end marker printed after the last test, so an answer that
    calls exit() before the tests run does not count as a pass.
    Returns {"passed", "error", "first_failure"}.
    """
    for bad in BLOCKED:
        if bad in code:
            return {"passed": False, "error": "blocked: " + bad,
                    "first_failure": f"the answer was rejected before running: it contains {bad!r}"}
    marker = "ECO_TESTS_DONE_" + os.urandom(8).hex()
    head = code.rstrip() + TESTS_HEADER
    tests_start = head.count("\n") + 1
    source = (head + tests_code.rstrip() + "\n\nimport sys as _eco_sys\n"
              f"_eco_sys.stdout.write('\\n{marker}\\n')\n")
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, CUSTOM_FILE), "w", encoding="utf-8") as f:
            f.write(source)
        try:
            p = _spawn_check(CUSTOM_FILE, tmp, timeout)
        except subprocess.TimeoutExpired:
            return {"passed": False, "error": "timeout", "first_failure": _timed_out(timeout)}
        except OSError as e:
            return {"passed": False, "error": "check did not start", "first_failure": f"the check could not start: {e}"}
    finished = p.stdout.rstrip().endswith(marker)
    if p.returncode == 0 and finished:
        return {"passed": True, "error": "", "first_failure": ""}
    if p.returncode == 0:
        return {"passed": False, "error": "tests did not finish",
                "first_failure": "the answer's code exited before the tests finished"}
    return {"passed": False, "error": "tests failed", "first_failure": _first_failure(p.stderr, source, tests_start)}


def main():
    backend = sys.argv[1] if len(sys.argv) > 1 else "pi"
    path = sys.argv[2] if len(sys.argv) > 2 else "data/coding_tasks_dev.py"
    if backend not in ("pi", "pc", "cloud"):
        raise SystemExit('Usage: python bench_coding.py [pi|pc|cloud] [tasks_file]')
    ask = {"pi": ask_pi, "pc": ask_pc, "cloud": ask_cloud}[backend]
    tasks = load_tasks(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    out_path = f"results/coding_{backend if backend in ('pi', 'pc') else 'cloud_' + os.environ.get('BENCH_EFFORT', router.CLOUD_EFFORT)}_{stem}.csv"
    rows = []
    for task in tasks:
        print(f"{task['id']} ...", end=" ", flush=True)
        res = ask(task["prompt"])
        code = extract_code(res["text"])
        t = run_tests(code, task)
        error = t.get("error", "")
        tp, tt = t.get("passed", 0), t.get("total", len(task["tests"]))
        ok = (not error) and tp == tt
        first = ""
        if t.get("fails"):
            args, exp, got = t["fails"][0]
            first = f"input={args} expected={exp} got={got}"
        rows.append({"id": task["id"], "passed": ok, "tests_passed": tp, "tests_total": tt,
                     "seconds": round(res["seconds"], 1), "in_tokens": res["in_tokens"],
                     "out_tokens": res["out_tokens"], "cost_usd": round(res["cost"], 6),
                     "error": error, "first_failure": first, "code": code})
        print(("PASS" if ok else "FAIL") + f" ({tp}/{tt} tests, {res['seconds']:.0f}s, "
              f"{res['out_tokens']} out tokens)" + (f" {error}" if error else ""), flush=True)
        if first:
            print("    first failure:", first)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    solved = sum(r["passed"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    print(f"\n{backend}: {solved}/{len(rows)} tasks passed all tests, cost ${total_cost:.4f}")
    print("saved", out_path)


if __name__ == "__main__":
    main()