"""Coverage tracking: catalog of units, one GitHub issue per unit, status scoreboard, unit publishing.

Unit lifecycle (labels on the unit issue):
  todo -> in-progress (agent workers) -> needs-approval (verifier + audit gates met) -> approved (owner) -> done
Nothing a worker produces is public until the owner approves the unit; `publish` then flips verified -> approved.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import yaml

from .config import REPO_ROOT, notice

CATALOG = REPO_ROOT / "coverage" / "catalog.yaml"
STATUS_MD = REPO_ROOT / "STATUS.md"
UNIT_LABELS = {"unit": "0052cc", "todo": "ededed", "in-progress": "fbca04", "needs-approval": "d93f0b",
               "approved": "0e8a16", "done": "5319e7", "needs-human": "b60205", "phase:1": "c2e0c6", "phase:2": "c2e0c6"}


def catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())


def units() -> list[dict]:
    return sorted(catalog()["units"], key=lambda u: u["order"])


def unit(uid: str) -> dict:
    for u in units():
        if u["id"] == uid:
            return u
    raise SystemExit(f"Unknown unit {uid}")


def regions_of(u: dict) -> list[str]:
    return [u["region"], *u.get("subregions", [])]


def title_of(u: dict) -> str:
    return f"{u['id']} · {u['en']} {u['zh']}"


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500])
    return r.stdout.strip()


def unit_issues() -> dict[str, dict]:
    raw = gh("issue", "list", "--label", "unit", "--state", "all", "--limit", "500", "--json", "number,title,labels,state")
    out = {}
    for i in json.loads(raw or "[]"):
        out[i["title"].split(" ·")[0]] = {**i, "labels": [lb["name"] for lb in i["labels"]]}
    return out


def issue_body(u: dict, metrics: dict | None = None) -> str:
    cat = catalog()
    themes = "\n".join(f"- [ ] {t['en']} · {t['zh']}" for t in cat["themes"])
    gates = cat["done_when"]
    m = metrics or {}
    return f"""**Unit:** {u['en']} {u['zh']}  ·  **Phase:** {u['phase']}  ·  **Region key:** `{u['region']}`

### Themes to research
{themes}

### Done when
- ≥ {gates['min_sources']} registered sources (at least one in Chinese for China units)
- every record to publish passed the verifier (excerpt found in the archived source + support check)
- independent audit of ≥ {gates['min_audit_sample']} records agrees ≥ {int(gates['audit_agreement'] * 100)}%
- the owner approves this unit's batch (add the `approved` label here)

### Status (updated by the status job)
| verified | pending | rejected | published | sources | audited | audit agreement |
|---|---|---|---|---|---|---|
| {m.get('verified', 0)} | {m.get('pending', 0)} | {m.get('rejected', 0)} | {m.get('published', 0)} | {m.get('sources', 0)} | {m.get('audited', 0)} | {m.get('agreement', '—')} |

_Agent workers post run notes as comments. Correctness over speed: rejected records stay out._
"""


def sync_issues() -> None:
    for name, color in UNIT_LABELS.items():
        gh("label", "create", name, "--color", color, "--force", check=False)
    have = unit_issues()
    made = 0
    for u in units():
        if u["id"] in have:
            continue
        gh("issue", "create", "--title", title_of(u), "--body", issue_body(u),
           "--label", "unit", "--label", f"phase:{u['phase']}", "--label", "todo")
        made += 1
    notice(f"coverage: created {made} unit issue(s)")


def active_unit() -> dict | None:
    """The unit workers should work on: the in-progress one, else the next todo in phase order."""
    issues = unit_issues()
    for u in units():
        i = issues.get(u["id"])
        if i and i["state"] == "OPEN" and "in-progress" in i["labels"]:
            return u
    for u in units():
        i = issues.get(u["id"])
        if i and i["state"] == "OPEN" and "todo" in i["labels"]:
            gh("issue", "edit", str(i["number"]), "--remove-label", "todo", "--add-label", "in-progress")
            return u
    return None


def metrics(store, u: dict) -> dict:
    regs = regions_of(u)
    sites = [s for s in store.select("sites", select="id,status,region,unit") if s.get("unit") == u["id"] or s["region"] in regs]
    count = lambda st: sum(1 for s in sites if s["status"] == st)  # noqa: E731
    audits = store.select("audits", select="agree", unit=f"eq.{u['id']}")
    from .crawl import load_registry
    reg = load_registry()["regions"].get(u["region"], {})
    n_sources = sum(len(s.get("start_urls", [])) for s in reg.get("sources", []))
    agree = sum(1 for a in audits if a["agree"])
    return {
        "verified": count("verified"), "pending": count("pending"), "rejected": count("rejected"),
        "published": count("approved") + count("conflict"), "sources": n_sources, "audited": len(audits),
        "agreement": f"{agree / len(audits):.0%}" if audits else "—", "agreement_value": agree / len(audits) if audits else None,
    }


def gates_met(m: dict) -> bool:
    g = catalog()["done_when"]
    sample_ok = m["audited"] >= min(g["min_audit_sample"], max(m["verified"], 1))
    return (m["verified"] > 0 and m["pending"] == 0 and m["sources"] >= g["min_sources"] and sample_ok
            and (m["agreement_value"] or 0) >= g["audit_agreement"])


def status(store, write: bool = True) -> str:
    issues = unit_issues()
    rows, totals = [], {"verified": 0, "published": 0, "audited": 0}
    for u in units():
        i = issues.get(u["id"])
        labels = i["labels"] if i else []
        state = next((x for x in ("done", "approved", "needs-approval", "in-progress", "needs-human", "todo") if x in labels), "—")
        m = metrics(store, u) if state not in ("todo", "—") else {}
        for k in totals:
            totals[k] += m.get(k, 0)
        if i and m:
            gh("issue", "edit", str(i["number"]), "--body", issue_body(u, m), check=False)
            if state == "in-progress" and gates_met(m):
                gh("issue", "edit", str(i["number"]), "--remove-label", "in-progress", "--add-label", "needs-approval", check=False)
                sheet = report(store, u["id"]).read_text()
                if len(sheet) > 60000:
                    sheet = sheet[:60000] + "\n\n…(truncated; full list in the run artifact)"
                gh("issue", "comment", str(i["number"]), "--body",
                   f"✅ Gates met: {m['verified']} verified records, audit agreement {m['agreement']} on {m['audited']}. "
                   "Review the records below and add `approved` to publish this unit.\n\n" + sheet, check=False)
                state = "needs-approval"
        link = f"[#{i['number']}](../../issues/{i['number']})" if i else ""
        rows.append(f"| {u['phase']} | {u['id']} | {u['en']} {u['zh']} | {state} | {m.get('published', '')} | {m.get('verified', '')} "
                    f"| {m.get('pending', '')} | {m.get('rejected', '')} | {m.get('agreement', '')} | {link} |")
    done = sum(1 for r in rows if "| done |" in r)
    text = f"""# Coverage status

_Updated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC by the status job. Correctness over speed: records are published only after
the verifier, an independent audit, and the owner's approval of the unit._

**Units done:** {done} / {len(rows)}  ·  **Published records:** {totals['published']}  ·  **Verified, awaiting approval:** {totals['verified']}  ·  **Audited:** {totals['audited']}

| Phase | Unit | Name | State | Published | Verified | Pending | Rejected | Audit agreement | Issue |
|---|---|---|---|---|---|---|---|---|---|
""" + "\n".join(rows) + "\n"
    if write:
        STATUS_MD.write_text(text)
    return text


def publish(store, uid: str) -> dict:
    """Owner approved the unit: verified -> approved, founding-year disagreements -> conflict (visible, labeled)."""
    from .review import founding_conflicts

    u = unit(uid)
    regs = regions_of(u)
    sites = [s for s in store.select("sites", select="id,status,region,unit") if s.get("unit") == uid or s["region"] in regs]
    site_ids = [s["id"] for s in sites if s["status"] == "verified"]
    stats = {"sites": 0, "events": 0}
    subject_ids = list(site_ids)
    # Verified events can also attach to sites that are already public (e.g. curated seed sites).
    for sid in [s["id"] for s in sites if s["status"] in ("verified", "approved", "conflict")]:
        if sid in site_ids:
            store.update("sites", {"id": sid}, {"status": "approved"})
            stats["sites"] += 1
        events = store.select("events", select="id,site_id,event_type,year_start,status", site_id=f"eq.{sid}")
        for e in events:
            if e["status"] == "verified":
                store.update("events", {"id": e["id"]}, {"status": "approved"})
                subject_ids.append(e["id"])
                stats["events"] += 1
        for site_id in founding_conflicts([{**e, "status": "approved" if e["status"] == "verified" else e["status"]} for e in events]):
            store.update("sites", {"id": site_id}, {"status": "conflict"})
            for e in events:
                if e["event_type"] == "founded":
                    store.update("events", {"id": e["id"]}, {"status": "conflict"})
    persons = store.select("persons", select="id", status="eq.verified")
    subject_ids += [p["id"] for p in persons]
    for i in range(0, len(subject_ids), 50):
        store.http.patch("/claims", params={"subject_id": f"in.({','.join(subject_ids[i:i + 50])})", "status": "eq.verified"},
                         json={"status": "approved"}).raise_for_status()
    for p in persons:
        store.update("persons", {"id": p["id"]}, {"status": "approved"})
    notice(f"publish[{uid}]: {stats}, persons {len(persons)}")
    return stats


def report(store, uid: str) -> Path:
    """Markdown review sheet of a unit's verified records, attached to the unit issue for the owner."""
    u = unit(uid)
    regs = regions_of(u)
    sites = [s for s in store.select("sites", select="id,slug,name_en,name_zh,name_zh_status,region,site_type,geo_precision,status,unit")
             if (s.get("unit") == uid or s["region"] in regs) and s["status"] == "verified"]
    lines = [f"## Records awaiting approval: {u['en']} {u['zh']}", ""]
    for s in sites:
        lines.append(f"- **{s['name_en']}** {s.get('name_zh') or ''}{' (译)' if s.get('name_zh_status') == 'translated' else ''} "
                     f"· {s['site_type']} · {s['region']} · location: {s['geo_precision']}")
        for e in store.select("events", select="event_type,year_start,year_end,description", site_id=f"eq.{s['id']}"):
            lines.append(f"  - {e.get('year_start') or '—'} {e['event_type']}: {e.get('description') or ''}")
    path = REPO_ROOT / ".mlm-work" / f"report-{uid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
