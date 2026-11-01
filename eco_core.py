"""Eco mode core: run a coding task up a ladder of models and stop at the first that passes its tests.

Cloud tiers are skipped unless ALLOW_CLOUD=1 (and, in the server, while the budget allows).
Every solve appends one JSON line to the cost log.
"""
import ast
import csv
import glob
import hashlib
import json
import keyword
import os
import statistics
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import bench_coding

LOG_PATH = "results/eco_log.jsonl"
MEMORY_PATH = "results/eco_memory.jsonl"
CLOUD_MINIMAL_GLOB = "results/coding_cloud_minimal_*.csv"
CLOUD_IN_PER_M = 1.25   # USD per million input tokens, same price as bench_coding.ask_cloud
CLOUD_OUT_PER_M = 4.25  # USD per million output tokens
LOCAL_PORTS = {11434: "Ollama", 8080: "llama.cpp", 8081: "llama.cpp", 1234: "LM Studio"}
CLOUD_OFF_REASON = "cloud disabled (ALLOW_CLOUD is not 1)"
NO_KEY_REASON = "No API key set yet"
CHOOSE_KEY_REASON = "Choose a key"
DEFAULT_KEY_NAME = "Default"  # a key with no name: MODEL_API_KEY, or the old {"key": ...} request
DEFAULT_META_HOST = "api.meta.ai"
META_PROVIDER = "Meta"
OTHER_PROVIDER = "Other"
KEY_PROVIDERS = (META_PROVIDER, "OpenAI", "Claude (Anthropic)", "Gemini (Google)", OTHER_PROVIDER)
META_KEY_REFUSAL = "This rung calls Meta, but that key is for "
# Only prefixes distinctive of one provider. Never a bare "sk-": Meta's key format is unknown and could start the same way.
KEY_PREFIXES = (("sk-ant-", "Claude (Anthropic)"), ("sk-proj-", "OpenAI"), ("AIza", "Gemini (Google)"), ("gsk_", "Groq"))
LOCAL_MAX_TOKENS = 500   # same as bench_coding.ask_pi / ask_pc
# openai_compatible cloud tiers only; Meta rungs have their own limit. Reasoning counts against it, and
# a cut-off answer is paid for and useless. 25,000 is what OpenAI's reasoning guide says to reserve.
CLOUD_MAX_TOKENS = 25_000
CUSTOM_PROMPT_MAX = 4_000   # characters
CUSTOM_TESTS_MAX = 10_000   # characters


@dataclass
class Tier:
    name: str
    kind: str  # "local" or "cloud"
    # Takes a prompt. Returns the model's text, or a dict like bench_coding's ask_* helpers
    # (text, in_tokens, out_tokens, cost) when token counts are known.
    ask: Callable
    cost_per_call: float = 0.0
    # Why this cloud tier can't run yet (no usable API key). It is skipped, not failed.
    wait_reason: str = ""


def default_tiers():
    """Pi at router.LOCAL_URL, PC at bench_coding.PC_URL, then cloud (off unless ALLOW_CLOUD=1)."""
    return [
        Tier("pi", "local", bench_coding.ask_pi),
        Tier("pc", "local", bench_coding.ask_pc),
        Tier("cloud", "cloud", bench_coding.ask_cloud),
    ]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never resend a request (and its Authorization header) somewhere else


_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
_CLOUD_OPENER = urllib.request.build_opener(_NoRedirect)


def ask_openai_chat(base_url, model, prompt, disable_thinking=False, key=None, max_tokens=LOCAL_MAX_TOKENS,
                    price_in_per_m=0.0, price_out_per_m=0.0, local=True, timeout=600,
                    omit_temperature=False, max_tokens_field="max_tokens"):
    """POST {base_url}/chat/completions on any OpenAI-compatible server (llama.cpp, Ollama, LM Studio).

    Same body, timeout and bookkeeping as bench_coding.ask_pi/ask_pc, plus the model name.
    disable_thinking sends chat_template_kwargs enable_thinking=false, exactly like ask_pc.
    omit_temperature and max_tokens_field are for cloud services that reject temperature 0 or
    the max_tokens field. If the limit cut the answer off, the reply carries a note saying so.
    """
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], max_tokens_field: max_tokens}
    if not omit_temperature:
        body["temperature"] = 0
    if disable_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    start = time.time()
    with (_LOCAL_OPENER if local else _CLOUD_OPENER).open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    usage = data.get("usage") or {}
    tin, tout = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    choice = data["choices"][0]
    reply = {"text": choice["message"].get("content") or "",  # a model can spend its whole limit thinking
             "seconds": time.time() - start,
             "in_tokens": tin,
             "out_tokens": tout,
             "cost": tin * price_in_per_m / 1e6 + tout * price_out_per_m / 1e6}
    if choice.get("finish_reason") == "length":
        reply["note"] = f"the answer was cut off at the {max_tokens}-token limit"
    return reply


def may_use_only_key(cfg):
    """True only for a Meta rung on Meta's own HTTPS endpoint. Every other host must name its key."""
    if cfg.get("provider") != "meta":
        return False
    try:
        url = urlsplit(cfg.get("base_url", ""))
        return url.scheme == "https" and url.hostname == DEFAULT_META_HOST and url.port in (None, 443)
    except ValueError:
        return False


def key_provider(name, providers=None):
    """The provider label of a key. A key without one: Default is Meta, the only provider it could
    have been for when it was stored, and any other name is Other, so it is never used automatically."""
    label = (providers or {}).get(name)
    if label:
        return label
    return META_PROVIDER if name.casefold() == DEFAULT_KEY_NAME.casefold() else OTHER_PROVIDER


def key_label_problem(key, label):
    """Why this key can't have this label, or "". Other always passes, and so does a key with no known
    prefix. The message names providers only, never any part of the key."""
    looks_like = next((provider for prefix, provider in KEY_PREFIXES if key.startswith(prefix)), None)
    if not looks_like or label in (looks_like, OTHER_PROVIDER):
        return ""
    article = "an" if looks_like == "OpenAI" else "a"
    problem = f"This key looks like {article} {looks_like} key, but it is labelled {label}. Choose the right provider."
    return problem if looks_like in KEY_PROVIDERS else f"{problem} {looks_like} is not on the list, so choose Other."


def stored_key_label(key, label):
    """The label for a key that was not just chosen by someone: a label its prefix contradicts becomes Other."""
    return OTHER_PROVIDER if key_label_problem(key, label) else label


def calls_meta_host(cfg):
    """True for any rung on the Meta host, whatever its provider, scheme or port."""
    try:
        return (urlsplit(cfg.get("base_url", "")).hostname or "").rstrip(".") == DEFAULT_META_HOST
    except ValueError:
        return False


def meta_key_refusal(cfg, name, providers=None):
    """Why this rung may not use the named key, or "". The Meta host only ever gets a Meta key."""
    provider = key_provider(name, providers)
    return META_KEY_REFUSAL + provider if calls_meta_host(cfg) and provider != META_PROVIDER else ""


def is_key_wait(reason):
    """True if a skipped rung is only waiting for a usable key."""
    return reason in (NO_KEY_REASON, CHOOSE_KEY_REASON) or reason.startswith(META_KEY_REFUSAL)


def find_key_name(wanted, keys):
    """The stored name matching wanted, ignoring case, or None."""
    return next((n for n in keys if n.casefold() == wanted.casefold()), None)


def key_name_for_tier(cfg, keys, providers=None):
    """(the name of the key this cloud tier uses or None, why it has none or "").

    keys is {name: key}, providers is {name: provider label} (see key_provider). A tier with a
    key_name only ever uses that key, ignoring case: if it was removed the tier waits, it never falls
    back to another. A tier with no key_name gets no key, except that the default Meta rung uses the
    only Meta key if there is exactly one. A rung on the Meta host never gets a key for anyone else.
    """
    wanted = cfg.get("key_name")
    if wanted:
        found = find_key_name(wanted, keys)
        if not found:
            return None, NO_KEY_REASON
        refusal = meta_key_refusal(cfg, found, providers)
        return (None, refusal) if refusal else (found, "")
    meta_keys = [n for n in keys if key_provider(n, providers) == META_PROVIDER]
    if len(meta_keys) == 1 and may_use_only_key(cfg):
        return meta_keys[0], ""
    return None, CHOOSE_KEY_REASON if keys else NO_KEY_REASON


def tier_from_config(cfg, key="", allow_cloud=None, wait_reason=""):
    """One Tier from a settings entry (see settings.py). Tier.name is the settings id."""
    flags = cfg.get("flags", {})
    if cfg["kind"] == "local":
        # Local and LAN servers never receive the cloud key.
        def ask(prompt):
            return ask_openai_chat(cfg["base_url"], cfg["model"], prompt,
                                   disable_thinking=flags.get("disable_thinking", False))
    elif cfg.get("provider") == "meta":
        def ask(prompt):
            return bench_coding.ask_cloud(prompt, effort=flags.get("effort"), key=key, allowed=allow_cloud,
                                          url=cfg["base_url"] + "/responses", model=cfg["model"])
    else:
        # UNTESTED: provider "openai_compatible" for a cloud endpoint. Never called against a real
        # service during development; only fake servers in the tests.
        def ask(prompt):
            allowed = os.environ.get("ALLOW_CLOUD") == "1" if allow_cloud is None else allow_cloud
            if not key or not allowed:
                raise SystemExit("Cloud is off. Set an API key and ALLOW_CLOUD=1 first.")
            return ask_openai_chat(cfg["base_url"], cfg["model"], prompt,
                                   disable_thinking=flags.get("disable_thinking", False), key=key,
                                   max_tokens=CLOUD_MAX_TOKENS, price_in_per_m=cfg["price_in_per_m"],
                                   price_out_per_m=cfg["price_out_per_m"], local=False,
                                   omit_temperature=flags.get("omit_temperature", False),
                                   max_tokens_field=flags.get("max_tokens_field", "max_tokens"))
    return Tier(cfg["id"], cfg["kind"], ask, wait_reason=wait_reason)


def build_tiers(config, key="", allow_cloud=None, keys=None, providers=None):
    """The enabled tiers of a settings dict, in order.

    keys ({name: key}) gives each cloud tier only its own key, and a local tier none. A plain key
    counts as the only key, named Default. providers is {name: provider label}.
    """
    if keys is None and key:
        keys = {DEFAULT_KEY_NAME: key}
        providers = {DEFAULT_KEY_NAME: stored_key_label(key, META_PROVIDER)}
    tiers = []
    for t in config["tiers"]:
        if not t.get("enabled", True):
            continue
        if keys is None or t["kind"] != "cloud":
            tiers.append(tier_from_config(t, "", allow_cloud))
        else:
            name, reason = key_name_for_tier(t, keys, providers)
            tiers.append(tier_from_config(t, keys[name] if name else "", allow_cloud, reason))
    return tiers


def check(code, task):
    """Run the task's tests on code. Returns (passed_all, error)."""
    result = bench_coding.run_tests(code, task)
    error = result.get("error", "")
    return (not error and result.get("passed", 0) == result.get("total", len(task["tests"]))), error


def check_detail(code, task):
    """(passed_all, error, first_failure) for a benchmark task or a custom task (has tests_code)."""
    if task.get("tests_code") is not None:
        r = bench_coding.run_custom_tests(code, task["tests_code"])
        return r["passed"], r["error"], r["first_failure"]
    result = bench_coding.run_tests(code, task)
    error = result.get("error", "")
    ok = not error and result.get("passed", 0) == result.get("total", len(task["tests"]))
    first = result.get("first_failure", "")
    if not ok and not first and result.get("fails"):
        args, expected, got = result["fails"][0]
        first = f"{result.get('passed', 0)} of {result.get('total', 0)} tests passed; input={args} expected={expected} got={got}"[:300]
    return ok, error, first


def custom_prompt(prompt):
    """The user's prompt plus the benchmark's ending, so models are asked the same way."""
    text = prompt.strip()
    return text if text.endswith(bench_coding.CODE_SUFFIX.strip()) else text + bench_coding.CODE_SUFFIX


def custom_task_id(prompt, entry, tests_code):
    """'custom:' plus the first 8 hex characters of the memory key (prompt as sent to the models)."""
    return "custom:" + memory_key(prompt, None, entry, tests_code)[:8]


def make_custom_task(prompt, entry, tests_code):
    """Validate a user's own task. Returns {id, prompt, entry, tests_code}; raises ValueError in plain words.

    tests_code is the user's own Python (plain assert statements, any imports allowed). It is
    only parsed here, never run; it runs later next to each answer, in run_custom_tests.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is empty: describe what the function should do")
    if len(prompt) > CUSTOM_PROMPT_MAX:
        raise ValueError(f"prompt is too long: {len(prompt):,} characters (the limit is {CUSTOM_PROMPT_MAX:,})")
    if not isinstance(entry, str) or not entry.isidentifier() or keyword.iskeyword(entry):
        raise ValueError("entry must be the name of the function to test, like is_palindrome "
                         "(letters, digits and _, not starting with a digit)")
    if not isinstance(tests_code, str) or not tests_code.strip():
        raise ValueError(f"tests_code is empty: add assert lines such as assert {entry}(...) == ...")
    if len(tests_code) > CUSTOM_TESTS_MAX:
        raise ValueError(f"tests_code is too long: {len(tests_code):,} characters "
                         f"(the limit is {CUSTOM_TESTS_MAX:,})")
    try:
        tree = ast.parse(tests_code)
    except SyntaxError as e:
        raise ValueError(f"tests_code is not valid Python (line {e.lineno}: {e.msg})")
    nodes = list(ast.walk(tree))
    if not any(isinstance(n, ast.Assert) for n in nodes):
        raise ValueError(f"tests_code has no assert statement: add lines such as assert {entry}(...) == ...")
    if not any(isinstance(n, ast.Name) and n.id == entry for n in nodes):
        raise ValueError(f"tests_code never calls {entry}, so it cannot test it")
    sent = custom_prompt(prompt)
    return {"id": custom_task_id(sent, entry, tests_code), "prompt": sent, "entry": entry,
            "tests_code": tests_code}


def solve(prompt, tests, entry, tiers, task_id=None, log_path=LOG_PATH,
          allow_cloud=None, on_start=None, on_attempt=None, memory_path=None,
          cloud_check=None, secrets=(), tests_code=None, on_answer=None):
    """Try each tier in order and stop at the first answer that passes all tests.

    Benchmark format: tests is a list of (args, expected). Custom format: tests=None and
    tests_code is the user's assert-based test code (see make_custom_task); attempts then
    also carry first_failure, and task_id defaults to custom:<8 hex of the memory key>.
    on_answer(tier, code, passed, first_failure) gets each checked answer. Answers never go in
    the attempts or the result, because those are written to the log. first_failure stays in
    the returned result but not in the log (see write_log).
    allow_cloud: None reads ALLOW_CLOUD from the environment. on_start(tier_name) and
    on_attempt(attempt) are optional callbacks so a caller can show progress live.
    memory_path: a verified-memory JSONL file, or None to run without memory.
    cloud_check(pending_cloud_cost) runs before each cloud tier and returns "" to allow it or
    the reason it is skipped (the budget guard). secrets are redacted from the log and memory.
    """
    if allow_cloud is None:
        allow_cloud = os.environ.get("ALLOW_CLOUD") == "1"
    custom = tests_code is not None
    task = {"entry": entry, "tests": tests, "tests_code": tests_code}
    if custom and task_id is None:
        task_id = custom_task_id(prompt, entry, tests_code)
    attempts, skipped = [], []
    skip_reason = ""
    skip_reasons = {}
    solved_by = None
    passing_code = None

    key = memory_key(prompt, tests, entry, tests_code) if memory_path else None
    if key:
        if on_start:
            on_start("memory")
        start = time.time()
        stored = memory_lookup(key, memory_path)
        attempt = {"tier": "memory", "kind": "memory", "memory": "miss", "passed": False, "seconds": 0.0, "in_tokens": 0,
                   "out_tokens": 0, "cost_usd": 0.0, "error": ""}
        if stored is not None:
            # Re-verify on every reuse: a stored answer is trusted only if it passes now.
            ok, error, first = check_detail(stored["code"], task)
            if ok:
                attempt.update(memory="hit", passed=True, stored_tier=stored.get("tier"))
                if on_answer:
                    on_answer("memory", stored["code"], True, "")
            else:
                attempt["error"] = "stored answer failed the re-check" + (f": {error}" if error else "")
                if custom or first:
                    attempt["first_failure"] = first
        attempt["seconds"] = round(time.time() - start, 2)
        attempts.append(attempt)
        if on_attempt:
            on_attempt(attempt)
        if attempt["passed"]:
            solved_by = "memory"

    for tier in tiers if solved_by is None else []:
        if tier.kind == "cloud":
            pending = sum(a["cost_usd"] for a in attempts if a.get("kind") == "cloud")
            reason = CLOUD_OFF_REASON if not allow_cloud else (
                tier.wait_reason or (cloud_check(pending) if cloud_check else ""))
            if reason:
                skipped.append(tier.name)
                skip_reason = skip_reason or reason
                skip_reasons[tier.name] = tier.wait_reason or reason
                continue
        if on_start:
            on_start(tier.name)
        start = time.time()
        attempt = {"tier": tier.name, "kind": tier.kind, "passed": False, "seconds": 0.0, "in_tokens": 0,
                   "out_tokens": 0, "cost_usd": 0.0, "error": ""}
        try:
            reply = tier.ask(prompt)
        except (Exception, SystemExit) as e:  # an unreachable or broken tier counts as a failed attempt
            attempt["seconds"] = round(time.time() - start, 2)
            attempt["error"] = "ask: " + repr(e)
            attempts.append(attempt)
            if on_attempt:
                on_attempt(attempt)
            continue
        if isinstance(reply, dict):
            text = reply["text"]
            attempt["in_tokens"] = reply.get("in_tokens", 0)
            attempt["out_tokens"] = reply.get("out_tokens", 0)
            attempt["cost_usd"] = reply.get("cost", tier.cost_per_call)
        else:
            text = reply
            attempt["cost_usd"] = tier.cost_per_call
        code = bench_coding.extract_code(text)
        attempt["passed"], attempt["error"], first = check_detail(code, task)
        if not attempt["passed"] and isinstance(reply, dict) and reply.get("note"):
            first = reply["note"]
        if custom or first:
            attempt["first_failure"] = first
        attempt["seconds"] = round(time.time() - start, 2)
        attempts.append(attempt)
        if on_answer:
            on_answer(tier.name, code, attempt["passed"], first or attempt["error"])
        if on_attempt:
            on_attempt(attempt)
        if attempt["passed"]:
            solved_by = tier.name
            passing_code = code
            break

    if key and passing_code is not None:
        memory_store(key, passing_code, solved_by, attempts[-1]["seconds"], memory_path, secrets)

    solved_tier = next((t for t in tiers if t.name == solved_by), None)
    local_solve = solved_by == "memory" or (solved_tier is not None and solved_tier.kind == "local")
    avoided = estimate_cloud_cost(prompt) if local_solve else 0.0
    out = {
        "task_id": task_id,
        "solved_by": solved_by,
        "attempts": attempts,
        "skipped": skipped,
        "cost_usd": round(sum(a["cost_usd"] for a in attempts), 6),
        "seconds": round(sum(a["seconds"] for a in attempts), 2),
        "estimated_cloud_cost_avoided_usd": round(avoided, 6),
    }
    if skipped:
        out["skip_reason"] = skip_reason
        out["skip_reasons"] = skip_reasons
    write_log(out, log_path, secrets)
    return out


def _secrets(extra=()):
    found = {s for s in (*extra, os.environ.get("MODEL_API_KEY", "")) if s and len(s) >= 8}
    return sorted(found, key=len, reverse=True)


def redact(text, secrets=()):
    """Replace each secret (raw or JSON-escaped) with [redacted]. MODEL_API_KEY is always included."""
    for secret in _secrets(secrets):
        for form in {secret, json.dumps(secret)[1:-1]}:
            text = text.replace(form, "[redacted]")
    return text


def write_log(result, log_path=LOG_PATH, secrets=()):
    # first_failure can quote the user's checks, so it never reaches the log.
    attempts = [{k: v for k, v in a.items() if k != "first_failure"} for a in result["attempts"]]
    line = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result, "attempts": attempts}
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(redact(json.dumps(line), secrets) + "\n")


def memory_key(prompt, tests, entry, tests_code=None):
    """SHA-256 of the exact prompt, tests, entry point and (custom tasks) tests_code.

    Any change is a different key. tests_code is left out when None, so benchmark keys
    stored before custom tasks existed stay the same.
    """
    fields = {"prompt": prompt, "tests": tests, "entry": entry}
    if tests_code is not None:
        fields["tests_code"] = tests_code
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def memory_lookup(key, path=MEMORY_PATH):
    """Latest stored entry for key, or None. Later lines replace earlier ones."""
    found = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    if entry.get("key") == key:
                        found = entry
    return found


def memory_store(key, code, tier, seconds, path=MEMORY_PATH, secrets=()):
    """Append a verified solution. Callers store only code that passed all its tests.

    Returns False and stores nothing if the code contains the cloud API key.
    """
    if redact(code, secrets) != code:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    line = {"key": key, "code": code, "tier": tier, "seconds": seconds,
            "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return True


def memory_count(path=MEMORY_PATH):
    """Number of distinct tasks with a stored solution."""
    keys = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            keys = {json.loads(line).get("key") for line in f if line.strip()}
    return len(keys)


def clear_memory(path=MEMORY_PATH):
    if os.path.exists(path):
        os.remove(path)


def median_cloud_minimal_out_tokens(pattern=CLOUD_MINIMAL_GLOB):
    values = []
    for path in sorted(glob.glob(pattern)):
        with open(path, newline="", encoding="utf-8") as f:
            values += [int(r["out_tokens"]) for r in csv.DictReader(f) if r.get("out_tokens")]
    return statistics.median(values) if values else None


def estimate_cloud_cost(prompt, pattern=CLOUD_MINIMAL_GLOB):
    """ESTIMATE of what always-cloud (minimal) would have cost for this prompt.

    Input tokens are approximated as characters / 4; output tokens are the median
    out_tokens of the saved minimal-effort cloud results. Not a measured cost.
    """
    median_out = median_cloud_minimal_out_tokens(pattern)
    if median_out is None:
        return 0.0
    return len(prompt) / 4 * CLOUD_IN_PER_M / 1e6 + median_out * CLOUD_OUT_PER_M / 1e6


def discover_local(ports=LOCAL_PORTS, host="localhost", timeout=1.0):
    """Ask each usual local port for its OpenAI-compatible /v1/models list. No benchmarking."""
    found = []
    for port, kind in ports.items():
        url = f"http://{host}:{port}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        models = [m.get("id", "?") for m in data.get("data", [])] if isinstance(data, dict) else []
        found.append({"port": port, "kind": kind, "url": f"http://{host}:{port}/v1/chat/completions",
                      "models": models})
    return found
