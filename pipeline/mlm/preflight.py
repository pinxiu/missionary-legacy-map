"""Credential preflight for the scheduled runners (no model call, costs nothing).

From Nov 11 to Nov 20 2026 every coverage run failed because ANTHROPIC_API_KEY had been revoked (401 invalid x-api-key),
yet the status job kept committing STATUS.md, so nothing looked wrong. This check runs first: a rejected credential stops
the run before any work and opens (or refreshes) one `runner-blocked` issue that says which secret to replace. The issue
closes itself on the first run whose credentials all pass.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess

import httpx

ISSUE_TITLE = "Runner blocked: a credential was rejected"
LABEL = "runner-blocked"


def check_anthropic(key: str | None) -> str | None:
    """None when the key works. Listing models is free and needs a valid key."""
    if not key:
        return "ANTHROPIC_API_KEY is not set"
    try:
        r = httpx.get("https://api.anthropic.com/v1/models", params={"limit": 1}, timeout=20,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except httpx.HTTPError as e:  # a network blip is not a dead key: let the run try
        print(f"::warning::Anthropic preflight could not connect ({e}); continuing")
        return None
    if r.status_code in (401, 403):
        return f"ANTHROPIC_API_KEY was rejected by the Anthropic API ({r.status_code}: {r.text[:160]})"
    return None


def check_supabase(url: str | None, key: str | None) -> str | None:
    if not url or not key:
        return "SUPABASE_URL / SUPABASE_SECRET_KEY is not set"
    from .store import auth_headers
    try:
        headers = auth_headers(url, key)
    except SystemExit as e:
        return str(e)
    try:
        r = httpx.get(f"{url.rstrip('/')}/rest/v1/sites", params={"select": "id", "limit": 1}, headers=headers, timeout=20)
    except httpx.HTTPError as e:
        print(f"::warning::Supabase preflight could not connect ({e}); continuing")
        return None
    if r.status_code in (401, 403):
        return f"SUPABASE_SECRET_KEY was rejected by Supabase ({r.status_code}: {r.text[:160]})"
    return None


BLOCKED_UNTIL = re.compile(r"<!-- blocked-until: (\S+) -->")


def spend_limit_until(message: str) -> str | None:
    """'You will regain access on 2026-12-01 at 00:00 UTC' -> '2026-12-01T00:00:00Z' (None if not a spend-limit error)."""
    if "usage limit" not in message.lower():
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?: at (\d{2}:\d{2}))?", message)
    return f"{m.group(1)}T{m.group(2) or '00:00'}:00Z" if m else "unknown"


def check_spend_block(now: dt.datetime | None = None) -> str | None:
    """A spend limit is only visible on a paid call, so the first stage that hits one records its reset time in the open
    runner-blocked issue (`report_spend_limit`). Until then every run would fail at each model stage; this stops it here."""
    if not os.environ.get("GH_TOKEN"):
        return None
    r = _gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "body", "--jq", ".[0].body")
    m = BLOCKED_UNTIL.search(r.stdout or "") if r.returncode == 0 else None
    if not m:
        return None
    until = m.group(1)
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        if dt.datetime.fromisoformat(until.replace("Z", "+00:00")) <= now:
            return None  # the limit has reset: let the run try (a working run closes the issue)
    except ValueError:
        pass
    return f"the Anthropic account's API spend limit is reached until {until} (close the runner-blocked issue after raising it)"


def report_spend_limit(message: str) -> bool:
    """Called when a model call fails on the spend limit: open/refresh the runner-blocked issue with the reset time."""
    until = spend_limit_until(message)
    if until is None:
        return False
    print(f"::error::Anthropic API spend limit reached; access returns {until}. Raise the limit in the Anthropic Console "
          "(Settings → Limits) and close the runner-blocked issue to resume sooner.")
    report([f"ANTHROPIC_API_KEY's account reached its API spend limit: {message[:200]}"], blocked_until=until)
    return True


def problems() -> list[str]:
    out = [check_spend_block(), check_anthropic(os.environ.get("ANTHROPIC_API_KEY")),
           check_supabase(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY"))]
    return [p for p in out if p]


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _open_issue() -> str | None:
    r = _gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "--jq", ".[0].number")
    return r.stdout.strip() or None if r.returncode == 0 else None


def report(found: list[str], blocked_until: str | None = None) -> None:
    """Open/refresh the runner-blocked issue, or close it when everything passes. GitHub errors never fail the check."""
    if not os.environ.get("GH_TOKEN"):
        return
    number = _open_issue()
    run = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}" \
          f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    if not found:
        if number:
            _gh("issue", "close", number, "--comment", f"Credentials pass again ({run}); the runners are working.")
        return
    if blocked_until:
        body = ("The model stages stopped because the Anthropic account reached its API spend limit:\n\n"
                + "\n".join(f"- {p}" for p in found)
                + f"\n\nRuns are skipped until **{blocked_until}**. To resume sooner, raise the limit in the Anthropic "
                "Console (Settings → Limits), then close this issue and run **Coverage agents** by hand."
                f"\n\nLast failing run: {run}\n\n<!-- blocked-until: {blocked_until} -->")
    else:
        body = ("The scheduled coverage runners stopped before doing any work because a credential was rejected:\n\n"
                + "\n".join(f"- {p}" for p in found)
                + "\n\nFix: replace the secret under Settings → Secrets and variables → Actions, then run the "
                "**Coverage agents** workflow by hand (Actions → Coverage agents → Run workflow). "
                f"This issue closes itself on the next run whose credentials pass.\n\nLast failing run: {run}")
    if number and blocked_until:
        _gh("issue", "edit", number, "--body", body)  # the body holds the marker the preflight reads; no comment spam
    elif number and found == problems_seen(number):
        return  # already reported this exact problem
    elif number:
        _gh("issue", "comment", number, "--body", body)
    else:
        _gh("label", "create", LABEL, "--color", "b60205", "--description", "Scheduled runners cannot run", "--force")
        _gh("issue", "create", "--title", ISSUE_TITLE, "--label", LABEL, "--body", body)


def problems_seen(number: str) -> list[str]:
    r = _gh("issue", "view", number, "--json", "body", "--jq", ".body")
    return [p for p in re.findall(r"^- (.+)$", r.stdout or "", re.M)] if r.returncode == 0 else []


def run() -> int:
    found = problems()
    for p in found:
        print(f"::error::{p}")
    report(found)
    if not found:
        print("preflight: credentials ok")
    return 1 if found else 0
