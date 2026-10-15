"""Approximate GitHub Actions minutes for this repo over the last 30 days, per workflow (no model calls).

Uses run durations from the repository API (one request per 100 runs). Billing counts each job rounded up to whole
minutes, so this slightly underestimates multi-job runs; it is meant to show the pace against the account quota
(2,000 minutes a month on GitHub Free, shared by all of the owner's private repositories), not to reproduce billing.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import subprocess
from collections import Counter

from .config import notice

QUOTA = int(os.environ.get("ACTIONS_QUOTA_MIN", "2000"))


def _runs(repo: str, since: str) -> list[dict]:
    out, page = [], 1
    while True:
        r = subprocess.run(["gh", "api", f"repos/{repo}/actions/runs?per_page=100&page={page}&created=%3E%3D{since}"],
                           capture_output=True, text=True, check=True)
        batch = json.loads(r.stdout).get("workflow_runs", [])
        out += batch
        if len(batch) < 100 or page >= 50:
            return out
        page += 1


def report(repo: str | None = None, days: int = 30) -> dict:
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "pinxiu/missionary-legacy-map")
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    p = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    minutes: Counter = Counter()
    runs = _runs(repo, since)
    for r in runs:
        if r.get("conclusion") == "skipped" or not r.get("run_started_at") or r.get("status") != "completed":
            continue
        secs = (p(r["updated_at"]) - p(r["run_started_at"])).total_seconds()
        minutes[r["name"]] += max(1, math.ceil(secs / 60))
    first = min((p(r["created_at"]) for r in runs), default=now)
    span = max(1.0, (now - first).total_seconds() / 86400)
    total = sum(minutes.values())
    pace = round(total / span * 30)
    top = ", ".join(f"{k} {v}" for k, v in minutes.most_common(5))
    notice(f"actions minutes ({repo}, last {span:.0f} days): {total}; pace ~{pace}/30 days vs {QUOTA} quota "
           f"(shared by all private repos). Top: {top}", "warning" if pace > QUOTA / 2 else "notice")
    return {"total": total, "pace_30d": pace, "by_workflow": dict(minutes)}
