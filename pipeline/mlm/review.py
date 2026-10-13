"""Helpers shared by the trusted verifier and unit publishing.

The old auto-approve review (2+ sources -> public) is retired: records now publish only after the verifier,
an independent audit, and the owner's approval (see coverage.py).
"""

from __future__ import annotations

from collections import defaultdict

from .config import AUTO_APPROVE_MIN_CONFIDENCE, AUTO_APPROVE_MIN_SOURCES


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
