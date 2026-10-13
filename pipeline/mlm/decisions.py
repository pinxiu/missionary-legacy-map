"""Owner decisions on audit disagreements, resolved through GitHub issues.

`mlm decisions sync`  (no model): one `needs-decision` issue per unresolved audit disagreement, showing our record,
                      what the independent auditor read, and the source quote.
`mlm decide --issue N` (no model): runs when the owner adds one decision label:
    decision:ours     our record is right; the audit counts as agreeing (override noted)
    decision:fix      apply the edited JSON block in the issue to the record, then count as agreeing
    decision:reject   the record is wrong or unverifiable; reject it (it stays private)
A line `ruling: <text>` anywhere in the issue body or a comment by the owner is appended to coverage/rulings.md,
which every extraction, verification and audit prompt includes, so the same question doesn't come back.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess

from .config import REPO_ROOT, notice

RULINGS = REPO_ROOT / "coverage" / "rulings.md"
LABELS = {"needs-decision": "d93f0b", "decision:ours": "0e8a16", "decision:fix": "fbca04", "decision:reject": "b60205"}
FIXABLE = {"name_en", "name_zh", "name_zh_status", "site_type", "geo_precision", "summary", "summary_zh",
           "modern_address", "current_status", "current_status_zh", "lat", "lng"}


def rulings_text() -> str:
    """The rulings to include in model prompts (empty string if none yet)."""
    if not RULINGS.exists():
        return ""
    lines = [ln for ln in RULINGS.read_text().splitlines() if ln.startswith("- ")]
    return ("\n\nOwner rulings (always follow these):\n" + "\n".join(lines)) if lines else ""


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500])
    return r.stdout.strip()


def issue_body(audit: dict, site: dict, events: list[dict], excerpt: str, source_url: str) -> str:
    answer = (audit.get("detail") or {}).get("answer", {})
    why = (audit.get("detail") or {}).get("why", "")
    ours = {k: site.get(k) for k in ("name_en", "name_zh", "site_type", "geo_precision", "summary")}
    fix = {k: site.get(k) for k in ("site_type", "name_en", "name_zh", "summary")}
    if answer.get("site_type") and answer["site_type"] != site.get("site_type"):
        fix["site_type"] = answer["site_type"]
    years = ", ".join(str(e["year_start"]) for e in events if e.get("year_start")) or "none"
    return f"""**Unit:** {audit['unit']}  ·  **Record:** {site['name_en']} {site.get('name_zh') or ''} (`{site['slug']}`)

**Disagreement:** {why}

| | Our record | Independent auditor |
|---|---|---|
| Type | {site.get('site_type')} | {answer.get('site_type', '—')} |
| Specific place? | yes | {answer.get('specific_place', '—')} |
| Years | {years} | {', '.join(map(str, answer.get('years', []))) or '—'} |
| Auditor's note | | {answer.get('notes', '')} |

**Source:** {source_url}
> {excerpt or '(no excerpt)'}

<details><summary>Full record</summary>

```json
{json.dumps(ours, ensure_ascii=False, indent=2)}
```
</details>

### Decide (add ONE label)
- `decision:ours` — our record is right; count the audit as agreeing.
- `decision:fix` — edit the JSON below first, then add the label; those fields are updated and the audit counts as agreeing.
- `decision:reject` — the record is wrong or can't be verified; it stays off the map.

Optional: add a line starting with `ruling:` (here or in a comment) to set a general rule for all future records,
e.g. `ruling: A leprosy settlement counts as a hospital.` It is saved to coverage/rulings.md and every agent follows it.

<!-- fix -->
```json
{json.dumps(fix, ensure_ascii=False, indent=2)}
```

<!-- audit:{audit['id']} -->
"""


def sync(store) -> int:
    for name, color in LABELS.items():
        gh("label", "create", name, "--color", color, "--force", check=False)
    pending = store.select("audits", select="*", agree="eq.false", decision_issue="is.null", resolved_at="is.null")
    made = 0
    for a in pending:
        site = (store.select("sites", select="*", id=f"eq.{a['subject_id']}") or [None])[0]
        if not site or site["status"] == "rejected":
            continue
        events = store.select("events", select="year_start", site_id=f"eq.{site['id']}", status="eq.verified")
        claim = (store.select("claims", select="excerpt,source_id", subject_id=f"eq.{site['id']}", limit="1") or [{}])[0]
        src = (store.select("sources", select="url", id=f"eq.{claim.get('source_id')}") or [{}])[0] if claim else {}
        url = gh("issue", "create", "--title", f"Decide: {site['name_en']} {site.get('name_zh') or ''} ({a['unit']})"[:200],
                 "--body", issue_body(a, site, events, claim.get("excerpt", ""), src.get("url", "")),
                 "--label", "needs-decision")
        store.update("audits", {"id": a["id"]}, {"decision_issue": int(url.rstrip("/").split("/")[-1])})
        made += 1
    notice(f"decisions: opened {made} issue(s)")
    return made


def extract_rulings(texts: list[str]) -> list[str]:
    out = []
    for t in texts:
        for m in re.finditer(r"(?im)^\s*ruling:\s*(.+?)\s*$", t or ""):
            out.append(m.group(1))
    return out


def add_rulings(rulings: list[str], issue: int) -> list[str]:
    existing = RULINGS.read_text() if RULINGS.exists() else ""
    added = [r for r in rulings if r not in existing]
    if added:
        with RULINGS.open("a") as f:
            for r in added:
                f.write(f"- {r} _(#{issue}, {dt.date.today():%Y-%m-%d})_\n")
    return added


def decide(store, issue: int) -> str:
    data = json.loads(gh("issue", "view", str(issue), "--json", "body,labels,comments"))
    labels = {lb["name"] for lb in data["labels"]}
    choice = next((lb.split(":", 1)[1] for lb in labels if lb.startswith("decision:")), None)
    m = re.search(r"<!-- audit:([0-9a-f-]{36}) -->", data["body"])
    if not choice or not m:
        raise SystemExit("Issue has no decision label or audit marker.")
    audit = store.select("audits", select="*", id=f"eq.{m.group(1)}")[0]
    site_id = audit["subject_id"]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "pinxiu")
    owner_comments = [c["body"] for c in data.get("comments", []) if c["author"]["login"] == owner]
    if choice == "fix":
        fm = re.search(r"<!-- fix -->\s*```(?:json)?\s*(\{.*?\})\s*```", data["body"], re.S)
        if not fm:
            raise SystemExit("decision:fix needs the JSON block after <!-- fix -->.")
        changes = {k: v for k, v in json.loads(fm.group(1)).items() if k in FIXABLE}
        store.update("sites", {"id": site_id}, changes)
        summary = f"updated {', '.join(sorted(changes))}"
    elif choice == "reject":
        store.update("sites", {"id": site_id}, {"status": "rejected", "review_note": f"rejected by owner (#{issue})"})
        summary = "record rejected"
    else:
        summary = "kept our record"
    detail = {**(audit.get("detail") or {}), "override": choice, "decided_in": issue}
    store.update("audits", {"id": audit["id"]}, {
        "agree": choice in ("ours", "fix"), "resolution": choice, "resolved_at": now, "detail": detail})
    added = add_rulings(extract_rulings([data["body"], *owner_comments]), issue)
    if added:
        summary += f"; ruling saved: {'; '.join(added)}"
    notice(f"decide #{issue}: {summary}")
    return summary
