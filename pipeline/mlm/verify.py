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
from functools import lru_cache

from .config import ANTHROPIC_API_KEY, VERIFY_MODEL, notice
from .llm import Req, parse_json, shared

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


ELLIPSIS = re.compile(r"\s*(?:…+|\.{3,}|⋯+|\[\s*\.\.\.\s*\])\s*")
MIN_FRAGMENT = 4        # each piece of an elided quote must still say something
MAX_ELIDED_SPAN = 1500  # the pieces must come from one passage, not be stitched across the page


@lru_cache(maxsize=1)
def _t2s():
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert
    except Exception:  # noqa: BLE001 - converter missing: compare characters as they are
        return lambda x: x


def norm(s: str) -> str:
    """Comparable form: Simplified script, lower case, no punctuation, single spaces. A source page and a quote from it
    can legitimately differ in all of these (e.g. zh.wikipedia serves either script)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _t2s()(s)
    s = re.sub(r"[\s　]+", " ", s)
    return re.sub(r"[“”\"'‘’`·•,，。．.;；:：!！?？()（）\[\]【】—–-]", "", s).strip()


@lru_cache(maxsize=256)
def _norm_text(text: str) -> str:
    return norm(text)


def excerpt_found(excerpt: str, text: str) -> bool:
    """The excerpt is the source's own words. A quote that skips text with an ellipsis ("A … B") counts only if every
    piece appears verbatim, in order, within one passage of the page."""
    t = _norm_text(text or "")
    parts = [norm(x) for x in ELLIPSIS.split(excerpt or "")]
    parts = [x for x in parts if x]
    if not parts or sum(len(x) for x in parts) < MIN_EXCERPT:
        return False
    if len(parts) == 1:
        return parts[0] in t
    if any(len(x) < MIN_FRAGMENT for x in parts):
        return False
    start = t.find(parts[0])
    while start != -1:
        pos, ok = start + len(parts[0]), True
        for x in parts[1:]:
            pos = t.find(x, pos)
            if pos == -1:
                ok = False
                break
            pos += len(x)
        if ok and pos - start <= MAX_ELIDED_SPAN:
            return True
        start = t.find(parts[0], start + 1)
    return False


def _support_req(items: list[dict], system: str) -> Req:
    payload = [{"i": n, "claim": it["claim"], "excerpt": it["excerpt"]} for n, it in enumerate(items)]
    return Req(model=VERIFY_MODEL, max_tokens=4000, system=system, purpose="verify",
               user=json.dumps(payload, ensure_ascii=False))


def _parse_results(field: str):
    def parse(res: dict, items: list) -> dict[int, list]:
        out = {}
        for r in parse_json(res["text"]).get("results", []):
            i = int(r["i"])
            if 0 <= i < len(items):
                out[i] = [bool(r.get(field)), str(r.get("why", ""))[:200]]
        return out
    return parse


def support_check(items: list[dict], llm=None) -> dict[int, tuple[bool, str]]:
    """Does each excerpt support its claim? Each (claim, excerpt) pair is answered once and cached on its own."""
    if not items:
        return {}
    from .decisions import rulings_text
    llm = llm or shared()
    system = SYSTEM + rulings_text()
    answers = llm.per_item("support", items, lambda it: [system, it["claim"], it["excerpt"]], 25,
                           lambda chunk: _support_req(chunk, system), _parse_results("supported"))
    return {n: (a[0], a[1]) for n, a in enumerate(answers) if a is not None}


def describe(claim: dict) -> str:
    v = claim.get("value") or {}
    if claim["subject_kind"] == "event":
        return f"Event: {v.get('event_type') or v.get('type')} {v.get('year_start') or ''}–{v.get('year_end') or ''}: {v.get('description', '')}"
    if claim["subject_kind"] == "person":
        return f"Person: {v.get('name_en')} ({v.get('birth_year') or '?'}–{v.get('death_year') or '?'}), {v.get('mission_society') or ''}"
    return f"Site: {v.get('name_en')} ({v.get('site_type')}) in {v.get('city') or v.get('place_text') or '?'}: {v.get('summary', '')[:200]}"


def place_check(items: list[dict], llm=None) -> dict[int, tuple[bool, str]]:
    """Is each record a specific physical place? Cached per record, so unchanged sites are never asked again."""
    if not items:
        return {}
    from .decisions import rulings_text
    llm = llm or shared()
    system = PLACE_SYSTEM + rulings_text()

    def build(chunk: list[dict]) -> Req:
        return Req(model=VERIFY_MODEL, max_tokens=4000, system=system, purpose="verify-places",
                   user=json.dumps([{"i": n, **it} for n, it in enumerate(chunk)], ensure_ascii=False))

    answers = llm.per_item("place", items, lambda it: [system, it], 25, build, _parse_results("place"))
    return {n: (a[0], a[1]) for n, a in enumerate(answers) if a is not None}


def check_places(store, llm=None) -> dict:
    """Every verified site must be a specific physical place; others are rejected. Re-runnable: it only looks at
    sites currently `verified`, so earlier verifications are re-checked under this rule too (cached answers are free)."""
    sites = store.select("sites", select="id,name_en,site_type,summary,place_text:modern_address,review_note", status="eq.verified")
    stats = {"checked": 0, "not_a_place": 0}
    items = []
    for s in sites:
        ex = store.select("claims", select="excerpt", subject_id=f"eq.{s['id']}", status="eq.verified", limit="1")
        items.append({"name": s["name_en"], "type": s["site_type"], "summary": (s.get("summary") or "")[:300],
                      "address": s.get("place_text"), "excerpt": (ex[0]["excerpt"] if ex else "")})
    try:
        res = place_check(items, llm)
    except Exception as e:  # noqa: BLE001
        notice(f"verify: place check failed ({type(e).__name__}); sites stay verified and are re-checked next run", "warning")
        res = {}
    for n, s in enumerate(sites):
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


def run(store, llm=None, use_model: bool = True) -> dict:
    claims = store.select("claims", select="id,subject_kind,subject_id,value,source_id,excerpt,status",
                          extracted_by="eq.llm", verified_at="is.null", limit="2000")
    if not claims:
        notice("verify: no new claims")
        settle(store)
        if use_model and ANTHROPIC_API_KEY:
            llm = llm or shared()
            check_places(store, llm)
            llm.report()
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

    llm = llm or (shared() if use_model and ANTHROPIC_API_KEY else None)
    pending = [c for group in by_source.values() for c in group]
    try:
        res = support_check([{"claim": describe(c), "excerpt": c["excerpt"]} for c in pending], llm) if llm else {}
    except Exception as e:  # noqa: BLE001 - leave these claims unchecked; they are retried next run
        notice(f"verify: support check failed ({type(e).__name__}: {str(e)[:200]}); claims left for next run", "warning")
        res = {}
    for n, c in enumerate(pending):
        if n not in res:
            kinds.pop(c["subject_id"], None)  # no answer yet (e.g. batch still running): retried next run
            continue
        ok, why = res[n]
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
        # Decide on every claim the record has, not only those checked in this run (a record can hold claims from
        # several documents, checked in different runs). Unchecked ones keep it pending.
        try:
            others = store.select("claims", select="status,verified_at", subject_id=f"eq.{sid}")
        except Exception:  # noqa: BLE001
            others = []
        if any(c.get("verified_at") is None and c.get("status") != "approved" for c in others):
            continue
        status = "verified" if oks and all(oks) and all(c["status"] in ("verified", "approved") for c in others) \
            else "rejected"
        try:
            store.update(table[kinds[sid]], {"id": sid, "status": "pending"},
                         {"status": status, "review_note": "; ".join(notes[sid])[:500] or None})
        except Exception as e:  # noqa: BLE001 - subject may have been removed
            notice(f"verify: could not update {kinds[sid]} {sid}: {e}", "warning")
    notice(f"verify: {stats}")
    settle(store)
    if llm:
        check_places(store, llm)
        llm.report()
    return stats


def recheck_excerpts(store, dry_run: bool = False, llm=None) -> dict:
    """Give claims rejected only because their excerpt wasn't found another look under the current matcher (elided
    quotes, Traditional/Simplified script). Claims whose excerpt is now found go back to unchecked, their records back
    to pending, and the normal verifier (model support check included) decides them again."""
    rejected = store.select_all("claims", select="id,subject_id,subject_kind,source_id,excerpt",
                                review_note="eq.excerpt not found in source", status="eq.rejected")
    docs = {d["source_id"]: d.get("text") or "" for d in store.select("documents", select="source_id,text")}
    found = [c for c in rejected if docs.get(c["source_id"]) and excerpt_found(c.get("excerpt") or "", docs[c["source_id"]])]
    stats = {"rejected_for_excerpt": len(rejected), "now_found": len(found)}
    if dry_run or not found:
        notice(f"verify recheck: {stats}{' (dry run)' if dry_run else ''}")
        return stats
    table = {"site": "sites", "event": "events", "person": "persons"}
    for c in found:
        store.update("claims", {"id": c["id"]}, {"status": "pending", "review_note": None, "verified_at": None})
    for sid, kind in {c["subject_id"]: c["subject_kind"] for c in found}.items():
        try:
            store.update(table[kind], {"id": sid, "status": "rejected"}, {"status": "pending", "review_note": None})
        except Exception as e:  # noqa: BLE001
            notice(f"verify recheck: could not reopen {kind} {sid}: {e}", "warning")
    notice(f"verify recheck: {stats}; re-verifying")
    stats["verify"] = run(store, llm)
    return stats
