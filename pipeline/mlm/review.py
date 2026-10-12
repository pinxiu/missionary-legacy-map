"""Review agent.

Rules (no human needed for the clear cases):
  * Approve a pending site/person/event when >= AUTO_APPROVE_MIN_SOURCES independent sources support it
    with mean confidence >= AUTO_APPROVE_MIN_CONFIDENCE.
  * Mark 'conflict' when sources disagree on a site's founding year. Both claims stay visible, labeled.
  * Everything else stays pending and goes into a Markdown report that CI posts as a GitHub issue.
"""

from __future__ import annotations

from collections import defaultdict

from .config import AUTO_APPROVE_MIN_CONFIDENCE, AUTO_APPROVE_MIN_SOURCES, WORK_DIR
from .store import Store


def decide(claims: list[dict]) -> str:
    sources = {c["source_id"] for c in claims}
    if not claims:
        return "pending"
    mean_conf = sum(float(c["confidence"] or 0) for c in claims) / len(claims)
    if len(sources) >= AUTO_APPROVE_MIN_SOURCES and mean_conf >= AUTO_APPROVE_MIN_CONFIDENCE:
        return "approved"
    return "pending"


def founding_conflicts(events: list[dict]) -> dict[str, set[int]]:
    years: dict[str, set[int]] = defaultdict(set)
    for e in events:
        if e["event_type"] == "founded" and e.get("year_start") and e["status"] != "rejected":
            years[e["site_id"]].add(e["year_start"])
    return {k: v for k, v in years.items() if len(v) > 1}


def run(store: Store | None = None) -> str:
    store = store or Store()
    claims = store.select("claims", select="subject_kind,subject_id,source_id,confidence")
    by_subject: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in claims:
        by_subject[(c["subject_kind"], c["subject_id"])].append(c)

    report = ["# Review queue", ""]
    counts = defaultdict(int)
    for table, kind in (("sites", "site"), ("persons", "person"), ("events", "event")):
        cols = "id,name_en,slug" if table != "events" else "id,site_id,event_type,year_start,description"
        pending = store.select(table, select=cols, status="eq.pending")
        needs_human = []
        for row in pending:
            verdict = decide(by_subject.get((kind, row["id"]), []))
            if verdict == "approved":
                store.update(table, {"id": row["id"]}, {"status": "approved"})
                counts[f"{table}_approved"] += 1
            else:
                needs_human.append(row)
        if needs_human:
            report += [f"## {table.title()} needing a second source ({len(needs_human)})", ""]
            for row in needs_human[:100]:
                label = row.get("name_en") or f"{row['event_type']} {row.get('year_start') or ''}: {row['description']}"
                report.append(f"- [ ] `{row['id'][:8]}` {label}")
            report.append("")

    events = store.select("events", select="id,site_id,event_type,year_start,status")
    conflicts = founding_conflicts(events)
    if conflicts:
        sites = {s["id"]: s for s in store.select("sites", select="id,name_en")}
        report += ["## Conflicting founding years", ""]
        for site_id, years in conflicts.items():
            report.append(f"- {sites.get(site_id, {}).get('name_en', site_id)}: {sorted(years)}")
            for e in events:
                if e["site_id"] == site_id and e["event_type"] == "founded" and e["status"] != "rejected":
                    store.update("events", {"id": e["id"]}, {"status": "conflict"})
        report.append("")

    summary = ", ".join(f"{k}={v}" for k, v in counts.items()) or "no automatic approvals"
    report.insert(2, f"Auto-review: {summary}.\n")
    text = "\n".join(report)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / "review-report.md").write_text(text)
    print(text)
    return text
