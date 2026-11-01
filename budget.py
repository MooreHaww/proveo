"""Cloud budget guard. Spend is read from the Eco log, never estimated.

Spend = sum of cost_usd over attempts of kind "cloud" in results/eco_log.jsonl whose timestamp
falls in the current calendar month (local time). Log lines written before attempts had a
"kind" field count an attempt as cloud when its tier is "cloud".
Levels: ok below 80% of the limit, warn from 80%, blocked from 100%. No limit: always ok.
"""
import json
import os
from datetime import datetime

import eco_core
import settings

WARN_AT = 0.80
BLOCKED_REASON = "budget limit reached"


def _is_cloud(attempt):
    kind = attempt.get("kind")
    return kind == "cloud" if kind is not None else attempt.get("tier") == "cloud"


def spent(log_path=eco_core.LOG_PATH, now=None):
    """Actual cloud cost (USD) logged in the calendar month of now (local time)."""
    now = (now or datetime.now()).astimezone()
    total = 0.0
    if not os.path.exists(log_path):
        return 0.0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                when = datetime.fromisoformat(entry["timestamp"]).astimezone()
            except (ValueError, KeyError, TypeError):
                continue
            if (when.year, when.month) != (now.year, now.month):
                continue
            for attempt in entry.get("attempts") or []:
                if isinstance(attempt, dict) and _is_cloud(attempt):
                    total += float(attempt.get("cost_usd") or 0.0)
    return total


def status(budget=None, log_path=eco_core.LOG_PATH, now=None, pending=0.0):
    """{limit, spent, remaining, percent, level, period}. pending adds cost not yet in the log."""
    budget = settings.load()["budget"] if budget is None else budget
    limit = budget.get("limit_usd")
    used = round(spent(log_path, now) + pending, 9)
    out = {"limit": limit, "spent": round(used, 6), "remaining": None, "percent": None,
           "level": "ok", "period": budget.get("period", "month")}
    if limit is None:
        return out
    out["remaining"] = round(max(limit - used, 0.0), 6)
    out["percent"] = round(used / limit * 100, 2) if limit > 0 else 100.0
    if used >= limit:  # a limit of 0 blocks the cloud entirely
        out["level"] = "blocked"
    elif used >= round(WARN_AT * limit, 9):
        out["level"] = "warn"
    return out


def block_reason(state):
    """The skip reason for a blocked status, else ""."""
    if state["level"] != "blocked":
        return ""
    return (f"{BLOCKED_REASON}: ${state['spent']:.6f} spent of ${state['limit']:.2f} this month. "
            f"Raise or remove the limit to use the cloud tier again.")
