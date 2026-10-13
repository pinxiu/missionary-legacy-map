"""One-off cleanup: remove exact duplicate events and claims left by pages that were loaded twice (no model calls).

Events are duplicates when site, type, start year and description match; claims when subject, source and quote match.
Of each group the copy furthest along in review is kept (approved > conflict > verified > pending > rejected), then the
oldest. Claims of removed events are removed with them.
"""

from __future__ import annotations

from collections import defaultdict

from .config import notice

RANK = {"approved": 0, "conflict": 1, "verified": 2, "pending": 3, "rejected": 4}


def _losers(rows: list[dict], key) -> list[str]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for g in groups.values():
        g.sort(key=lambda r: (RANK.get(r["status"], 9), r["created_at"]))
        out += [r["id"] for r in g[1:]]
    return out


def _delete(store, table: str, ids: list[str]) -> None:
    for i in range(0, len(ids), 100):
        store.http.delete(f"/{table}", params={"id": f"in.({','.join(ids[i:i + 100])})"}).raise_for_status()


def run(store, dry_run: bool = False) -> dict:
    events = store.select_all("events", select="id,site_id,event_type,year_start,description,status,created_at")
    dup_events = _losers(events, lambda e: (e["site_id"], e["event_type"], e["year_start"], e["description"] or ""))
    claims = store.select_all("claims", select="id,subject_id,source_id,excerpt,status,created_at")
    gone = set(dup_events)
    orphan_claims = [c["id"] for c in claims if c["subject_id"] in gone]
    rest = [c for c in claims if c["subject_id"] not in gone]
    dup_claims = _losers(rest, lambda c: (c["subject_id"], c["source_id"], c["excerpt"] or ""))
    stats = {"events": len(events), "claims": len(claims), "duplicate_events": len(dup_events),
             "claims_of_duplicate_events": len(orphan_claims), "duplicate_claims": len(dup_claims)}
    if not dry_run:
        _delete(store, "claims", orphan_claims + dup_claims)
        _delete(store, "events", dup_events)
    notice(f"dedupe{' (dry run)' if dry_run else ''}: {stats}")
    return stats
