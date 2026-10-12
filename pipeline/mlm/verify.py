"""Trusted verifier. Agents never grade their own work: this runs after extraction, in trusted code.

For every model-extracted claim not yet checked:
  1. Deterministic: its excerpt must appear in the archived text of its source document
     (after whitespace/punctuation normalisation). No excerpt, or no match: the claim fails.
  2. Model check: does the excerpt actually support the claimed value? One call per document, answers in JSON.
A site/event/person becomes `verified` only if it has at least one passing claim and none that fail the support
check; otherwise it is `rejected` with a review note. Verified records stay unpublished until the owner approves the unit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from collections import defaultdict

from .config import ANTHROPIC_API_KEY, VERIFY_MODEL, notice

SYSTEM = """You check whether short source excerpts support claims extracted from them.
For each item, answer "supported": true only if the excerpt itself states the claim (names, years, places, roles).
If the excerpt is about something else, is vaguer than the claim, or contradicts it, answer false and say why in a few words.
Return JSON only: {"results": [{"i": <index>, "supported": true|false, "why": "..."}]}"""

MIN_EXCERPT = 12


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\s　]+", " ", s)
    return re.sub(r"[“”\"'‘’`·•,，。．.;；:：!！?？()（）\[\]【】—–-]", "", s).strip()


def excerpt_found(excerpt: str, text: str) -> bool:
    e = norm(excerpt)
    return len(e) >= MIN_EXCERPT and e in norm(text)


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def support_check(items: list[dict], client=None) -> dict[int, tuple[bool, str]]:
    if not items:
        return {}
    client = client or _client()
    payload = [{"i": n, "claim": it["claim"], "excerpt": it["excerpt"]} for n, it in enumerate(items)]
    resp = client.messages.create(model=VERIFY_MODEL, max_tokens=4000, system=SYSTEM,
                                  messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = text[text.find("{"): text.rfind("}") + 1]
    out = {}
    for r in json.loads(text).get("results", []):
        out[int(r["i"])] = (bool(r.get("supported")), str(r.get("why", ""))[:200])
    return out


def describe(claim: dict) -> str:
    v = claim.get("value") or {}
    if claim["subject_kind"] == "event":
        return f"Event: {v.get('event_type') or v.get('type')} {v.get('year_start') or ''}–{v.get('year_end') or ''}: {v.get('description', '')}"
    if claim["subject_kind"] == "person":
        return f"Person: {v.get('name_en')} ({v.get('birth_year') or '?'}–{v.get('death_year') or '?'}), {v.get('mission_society') or ''}"
    return f"Site: {v.get('name_en')} ({v.get('site_type')}) in {v.get('city') or v.get('place_text') or '?'}: {v.get('summary', '')[:200]}"


def run(store, client=None, use_model: bool = True) -> dict:
    claims = store.select("claims", select="id,subject_kind,subject_id,value,source_id,excerpt,status",
                          extracted_by="eq.llm", verified_at="is.null", limit="2000")
    if not claims:
        notice("verify: nothing to check")
        return {}
    docs = {d["source_id"]: d.get("text") or "" for d in store.select("documents", select="source_id,text")}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    verdict: dict[str, list[bool]] = defaultdict(list)
    notes: dict[str, list[str]] = defaultdict(list)
    kinds: dict[str, str] = {}
    by_source: dict[str, list[dict]] = defaultdict(list)
    stats = {"claims": len(claims), "excerpt_missing": 0, "unsupported": 0, "supported": 0}

    for c in claims:
        kinds[c["subject_id"]] = c["subject_kind"]
        if not docs.get(c["source_id"]):
            stats.setdefault("no_archived_text", 0)
            stats["no_archived_text"] += 1  # left unchecked until the source is re-crawled and archived
            kinds.pop(c["subject_id"], None)
            continue
        if not excerpt_found(c.get("excerpt") or "", docs.get(c["source_id"], "")):
            stats["excerpt_missing"] += 1
            verdict[c["subject_id"]].append(False)
            notes[c["subject_id"]].append("excerpt not found in source text")
            store.update("claims", {"id": c["id"]}, {"status": "rejected", "review_note": "excerpt not found in source", "verified_at": now})
        else:
            by_source[c["source_id"]].append(c)

    client = client or (_client() if use_model and ANTHROPIC_API_KEY else None)
    for group in by_source.values():
        for i in range(0, len(group), 25):
            batch = group[i:i + 25]
            res = support_check([{"claim": describe(c), "excerpt": c["excerpt"]} for c in batch], client) if client else {}
            for n, c in enumerate(batch):
                ok, why = res.get(n, (False, "support check unavailable"))
                verdict[c["subject_id"]].append(ok)
                stats["supported" if ok else "unsupported"] += 1
                if not ok:
                    notes[c["subject_id"]].append(why)
                store.update("claims", {"id": c["id"]}, {"status": "verified" if ok else "rejected",
                                                         "review_note": None if ok else why, "verified_at": now})

    table = {"site": "sites", "event": "events", "person": "persons"}
    for sid, oks in verdict.items():
        if sid not in kinds:
            continue
        status = "verified" if oks and all(oks) else "rejected"
        try:
            store.update(table[kinds[sid]], {"id": sid, "status": "pending"},
                         {"status": status, "review_note": "; ".join(notes[sid])[:500] or None})
        except Exception as e:  # noqa: BLE001 - subject may have been removed
            notice(f"verify: could not update {kinds[sid]} {sid}: {e}", "warning")
    notice(f"verify: {stats}")
    return stats
