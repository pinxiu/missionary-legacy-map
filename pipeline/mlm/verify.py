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

from .config import ANTHROPIC_API_KEY, VERIFY_MODEL, cached_system, notice

SYSTEM = """You check whether short source excerpts support claims extracted from them.
For each item, answer "supported": true only if the excerpt itself states the claim (names, years, places, roles).
If the excerpt is about something else, is vaguer than the claim, or contradicts it, answer false and say why in a few words.
Return JSON only: {"results": [{"i": <index>, "supported": true|false, "why": "..."}]}"""

PLACE_SYSTEM = """You decide whether each record names a specific physical place someone could stand at: a particular
church, chapel, school, hospital, cemetery, residence, press, orphanage or mission compound (it may no longer exist).
Answer false for a city, county or region on its own, a person's work in an area, a meeting, conference, movement,
anniversary or other happening. Use only the name, type, summary and excerpt given.
Return JSON only: {"results": [{"i": <index>, "place": true|false, "why": "..."}]}"""

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
    from .decisions import rulings_text
    resp = client.messages.create(model=VERIFY_MODEL, max_tokens=4000, system=cached_system(SYSTEM + rulings_text()),
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


def place_check(items: list[dict], client) -> dict[int, tuple[bool, str]]:
    if not items:
        return {}
    from .decisions import rulings_text
    resp = client.messages.create(model=VERIFY_MODEL, max_tokens=4000, system=cached_system(PLACE_SYSTEM + rulings_text()),
                                  messages=[{"role": "user", "content": json.dumps(
                                      [{"i": n, **it} for n, it in enumerate(items)], ensure_ascii=False)}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    text = text[text.find("{"): text.rfind("}") + 1]
    return {int(r["i"]): (bool(r.get("place")), str(r.get("why", ""))[:200]) for r in json.loads(text).get("results", [])}


def check_places(store, client) -> dict:
    """Every verified site must be a specific physical place; others are rejected. Re-runnable: it only looks at
    sites currently `verified`, so earlier verifications are re-checked under this rule too."""
    sites = store.select("sites", select="id,name_en,site_type,summary,place_text:modern_address,review_note", status="eq.verified")
    stats = {"checked": 0, "not_a_place": 0}
    for i in range(0, len(sites), 25):
        batch = sites[i:i + 25]
        items = []
        for s in batch:
            ex = store.select("claims", select="excerpt", subject_id=f"eq.{s['id']}", status="eq.verified", limit="1")
            items.append({"name": s["name_en"], "type": s["site_type"], "summary": (s.get("summary") or "")[:300],
                          "address": s.get("place_text"), "excerpt": (ex[0]["excerpt"] if ex else "")})
        try:
            res = place_check(items, client)
        except Exception as e:  # noqa: BLE001
            notice(f"verify: place check failed ({type(e).__name__}); batch left verified-pending-recheck", "warning")
            continue
        for n, s in enumerate(batch):
            if n not in res:
                continue  # no answer for this one: stays verified and is re-checked next run
            ok, why = res[n]
            stats["checked"] += 1
            if not ok:
                stats["not_a_place"] += 1
                store.update("sites", {"id": s["id"], "status": "verified"},
                             {"status": "rejected", "review_note": f"not a specific physical place: {why}"[:500]})
    notice(f"verify places: {stats}")
    return stats


def settle(store) -> dict:
    """Resolve records left `pending` although every one of their claims has been checked (e.g. a run that stopped
    between checking claims and updating the record). Same rule as below: verified only if all claims passed."""
    stats = {"settled": 0}
    for table in ("sites", "events", "persons"):
        for rec in store.select_all(table, select="id", status="eq.pending"):
            claims = store.select("claims", select="status,verified_at,review_note", subject_id=f"eq.{rec['id']}")
            if not claims or any(c["verified_at"] is None for c in claims):
                continue
            ok = all(c["status"] in ("verified", "approved") for c in claims)
            notes = "; ".join(c["review_note"] for c in claims if c.get("review_note"))[:500] or None
            store.update(table, {"id": rec["id"], "status": "pending"},
                         {"status": "verified" if ok else "rejected", "review_note": None if ok else notes})
            stats["settled"] += 1
    if stats["settled"]:
        notice(f"verify settle: {stats}")
    return stats


def run(store, client=None, use_model: bool = True) -> dict:
    claims = store.select("claims", select="id,subject_kind,subject_id,value,source_id,excerpt,status",
                          extracted_by="eq.llm", verified_at="is.null", limit="2000")
    if not claims:
        notice("verify: no new claims")
        settle(store)
        if use_model and ANTHROPIC_API_KEY:
            check_places(store, client or _client())
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
            try:
                res = support_check([{"claim": describe(c), "excerpt": c["excerpt"]} for c in batch], client) if client else {}
            except Exception as e:  # noqa: BLE001 - leave this batch unchecked; it is retried next run
                notice(f"verify: support check failed ({type(e).__name__}: {str(e)[:200]}); batch left for next run", "warning")
                for c in batch:
                    kinds.pop(c["subject_id"], None)
                continue
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
    settle(store)
    if client:
        check_places(store, client)
    return stats
