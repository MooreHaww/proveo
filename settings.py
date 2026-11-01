"""Eco mode settings: the ordered tier list and the cloud budget, in one JSON file.

The file is <ECO_DATA_DIR>/settings.json (default ./user_data/settings.json). Until something is
saved, load() returns the defaults, which reproduce the original Pi -> PC -> cloud ladder.
The API key is never part of the settings: unknown fields are rejected, and save() refuses to
write a file that contains a secret.
"""
import json
import os
import re
from urllib.parse import urlsplit

import bench_coding
import router

MAX_TIERS = 8
KINDS = ("local", "cloud")
PROVIDERS = ("meta", "openai_compatible")  # openai_compatible cloud is UNTESTED
PERIODS = ("month",)
TIER_FIELDS = {"id", "name", "kind", "provider", "base_url", "model", "flags", "enabled",
               "price_in_per_m", "price_out_per_m"}
FLAG_FIELDS = {"disable_thinking", "effort"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
EFFORT_RE = re.compile(r"^[a-z]{1,16}$")


def data_dir():
    return os.environ.get("ECO_DATA_DIR") or os.path.join(".", "user_data")


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def _base_url(chat_url):
    """router.LOCAL_URL and bench_coding.PC_URL end in /chat/completions; tiers store the /v1 base."""
    suffix = "/chat/completions"
    return chat_url[:-len(suffix)] if chat_url.endswith(suffix) else chat_url


def default_settings():
    """Today's ladder: Pi, PC (thinking off), then Meta Muse Spark at minimal effort. No budget limit."""
    return {
        "tiers": [
            {"id": "pi", "name": "Raspberry Pi 5", "kind": "local", "base_url": _base_url(router.LOCAL_URL),
             "model": "Llama-3.2-3B-Instruct-Q4_K_M", "flags": {}, "enabled": True},
            {"id": "pc", "name": "PC GPU", "kind": "local", "base_url": _base_url(bench_coding.PC_URL),
             "model": "Qwen3-14B-Q4_K_M", "flags": {"disable_thinking": True}, "enabled": True},
            {"id": "cloud", "name": "Meta Muse Spark 1.3", "kind": "cloud", "provider": "meta",
             "base_url": router.CLOUD_URL.rsplit("/", 1)[0], "model": router.CLOUD_MODEL,
             "flags": {"effort": "minimal"}, "enabled": True},
        ],
        "budget": {"limit_usd": None, "period": "month"},
    }


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _tier(raw, i, errors):
    where = f"tier {i + 1}"
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object")
        return None
    unknown = set(raw) - TIER_FIELDS
    if unknown:
        errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    tier = {}
    tid = raw.get("id")
    if not isinstance(tid, str) or not ID_RE.match(tid) or tid == "memory":
        errors.append(f"{where}: id must be 1-32 lowercase letters, digits, - or _ (not 'memory')")
    tier["id"] = tid
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        errors.append(f"{where}: name must be non-empty (at most 60 characters)")
    tier["name"] = name.strip() if isinstance(name, str) else name
    kind = raw.get("kind")
    if kind not in KINDS:
        errors.append(f"{where}: kind must be 'local' or 'cloud'")
    tier["kind"] = kind
    provider = raw.get("provider", "openai_compatible" if kind == "local" else None)
    if provider not in PROVIDERS or (kind == "local" and provider != "openai_compatible"):
        errors.append(f"{where}: provider must be 'meta' or 'openai_compatible' (local tiers: openai_compatible)")
    if kind == "cloud":
        tier["provider"] = provider
    url = raw.get("base_url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname
            or parts.query or parts.fragment or parts.username or parts.password
            or not url.rstrip("/").endswith("/v1")):
        errors.append(f"{where}: base_url must be an http(s) URL ending in /v1, without credentials")
    tier["base_url"] = url.rstrip("/") if isinstance(url, str) else url
    model = raw.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        errors.append(f"{where}: model must be non-empty")
    tier["model"] = model.strip() if isinstance(model, str) else model
    flags = raw.get("flags", {})
    if not isinstance(flags, dict):
        errors.append(f"{where}: flags must be an object")
        flags = {}
    if set(flags) - FLAG_FIELDS:
        errors.append(f"{where}: unknown flag(s) {', '.join(sorted(set(flags) - FLAG_FIELDS))}")
    if "disable_thinking" in flags and not isinstance(flags["disable_thinking"], bool):
        errors.append(f"{where}: disable_thinking must be true or false")
    if "effort" in flags and (provider != "meta" or not isinstance(flags["effort"], str)
                              or not EFFORT_RE.match(flags["effort"])):
        errors.append(f"{where}: effort is a short word and only applies to the meta provider")
    tier["flags"] = {k: flags[k] for k in sorted(flags) if k in FLAG_FIELDS}
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"{where}: enabled must be true or false")
    tier["enabled"] = enabled
    if kind == "cloud" and provider == "openai_compatible":
        # Without prices the budget guard could not count this tier's spend, so they are required.
        for field in ("price_in_per_m", "price_out_per_m"):
            if not _number(raw.get(field)) or raw[field] < 0:
                errors.append(f"{where}: {field} (USD per million tokens, >= 0) is required for "
                              f"an openai_compatible cloud tier")
            tier[field] = raw.get(field)
    elif "price_in_per_m" in raw or "price_out_per_m" in raw:
        errors.append(f"{where}: prices only apply to openai_compatible cloud tiers")
    return tier


def validate(obj):
    """Return a cleaned copy of obj, or raise ValueError listing every problem."""
    errors = []
    if not isinstance(obj, dict):
        raise ValueError("settings must be an object")
    unknown = set(obj) - {"tiers", "budget"}
    if unknown:
        errors.append(f"unknown field(s) {', '.join(sorted(unknown))}")
    raw_tiers = obj.get("tiers")
    tiers = []
    if not isinstance(raw_tiers, list) or not raw_tiers:
        errors.append("tiers must be a non-empty list")
    elif len(raw_tiers) > MAX_TIERS:
        errors.append(f"at most {MAX_TIERS} tiers")
    else:
        tiers = [_tier(t, i, errors) for i, t in enumerate(raw_tiers)]
        ids = [t["id"] for t in tiers if t]
        if len(ids) != len(set(ids)):
            errors.append("tier ids must be unique")
        if not any(t and t.get("enabled") is True for t in tiers):
            errors.append("at least one tier must be enabled")
    raw_budget = obj.get("budget", {"limit_usd": None, "period": "month"})
    budget = {}
    if not isinstance(raw_budget, dict) or set(raw_budget) - {"limit_usd", "period"}:
        errors.append("budget must be an object with limit_usd and period")
    else:
        limit = raw_budget.get("limit_usd")
        if limit is not None and (not _number(limit) or limit < 0):
            errors.append("budget.limit_usd must be null (no limit) or a number >= 0")
        period = raw_budget.get("period", "month")
        if period not in PERIODS:
            errors.append("budget.period must be 'month'")
        budget = {"limit_usd": limit, "period": period}
    if errors:
        raise ValueError("; ".join(errors))
    return {"tiers": tiers, "budget": budget}


def load():
    """Saved settings, or the defaults if nothing has been saved. Raises ValueError if the file is invalid."""
    path = settings_path()
    if not os.path.exists(path):
        return default_settings()
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))


def save(obj, secrets=()):
    """Validate and write atomically. Refuses (ValueError) if any secret appears in the output."""
    clean = validate(obj)
    text = json.dumps(clean, indent=2)
    for secret in [*secrets, os.environ.get("MODEL_API_KEY", "")]:
        if secret and len(secret) >= 8 and (secret in text or json.dumps(secret)[1:-1] in text):
            raise ValueError("settings must not contain the API key")
    os.makedirs(data_dir(), exist_ok=True)
    tmp = settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, settings_path())
    return clean
