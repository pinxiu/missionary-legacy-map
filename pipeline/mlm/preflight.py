"""Credential preflight for the scheduled runners (no model call, costs nothing).

From Nov 11 to Nov 20 2026 every coverage run failed because ANTHROPIC_API_KEY had been revoked (401 invalid x-api-key),
yet the status job kept committing STATUS.md, so nothing looked wrong. This check runs first: a rejected credential stops
the run before any work and opens (or refreshes) one `runner-blocked` issue that says which secret to replace. The issue
closes itself on the first run whose credentials all pass.
"""

from __future__ import annotations

import os
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


def problems() -> list[str]:
    out = [check_anthropic(os.environ.get("ANTHROPIC_API_KEY")),
           check_supabase(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY"))]
    return [p for p in out if p]


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def _open_issue() -> str | None:
    r = _gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "number", "--jq", ".[0].number")
    return r.stdout.strip() or None if r.returncode == 0 else None


def report(found: list[str]) -> None:
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
    body = ("The scheduled coverage runners stopped before doing any work because a credential was rejected:\n\n"
            + "\n".join(f"- {p}" for p in found)
            + "\n\nFix: replace the secret under Settings → Secrets and variables → Actions, then run the "
            "**Coverage agents** workflow by hand (Actions → Coverage agents → Run workflow). "
            f"This issue closes itself on the next run whose credentials pass.\n\nLast failing run: {run}")
    if number:
        _gh("issue", "comment", number, "--body", body)
    else:
        _gh("label", "create", LABEL, "--color", "b60205", "--description", "Scheduled runners cannot run", "--force")
        _gh("issue", "create", "--title", ISSUE_TITLE, "--label", LABEL, "--body", body)


def run() -> int:
    found = problems()
    for p in found:
        print(f"::error::{p}")
    report(found)
    if not found:
        print("preflight: credentials ok")
    return 1 if found else 0
