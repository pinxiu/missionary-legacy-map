"""Independent audit: re-read the source for a random sample of verified sites and compare.

The auditor never sees the extraction. It gets only the archived source text and the site's name, and lists what the
text says about that place. Agreement is decided by trusted code: same site type, and every event year in our record
appears in the auditor's list. Results go to the `audits` table and feed the unit's gate (>= 95% agreement).
"""

from __future__ import annotations

import random

from .config import AUDIT_MODEL, notice
from .llm import Req, parse_json, shared

SYSTEM = """You read a historical source and report what it says about one named place. Use only the text.
"specific_place" is true only if the text describes a particular building, compound, cemetery or similar spot, not just a
city or region, a person's work in an area, or an event such as a meeting.
Return JSON only: {"found": true|false, "specific_place": true|false,
"site_type": "church|hospital|school|residence|cemetery|mission_station|press|orphanage|other",
"years": [<every year the text ties to this place>], "notes": "<one line>"}"""


MAX_TEXT = 60000
WINDOW = 6000
GENERIC = {"mission", "station", "church", "chapel", "cathedral", "hospital", "school", "college", "catholic", "protestant",
           "residence", "the", "and", "of", "at", "in", "post", "south", "north", "east", "west", "old", "new", "home",
           "house", "society", "scottish", "swedish", "nestorian", "memorial", "cemetery"}


def audit_text(name: str, text: str, anchors: tuple[str, ...] = ()) -> str:
    """The part of the source the auditor reads. Short pages whole. Long pages (whole archive.org books) used to be cut
    at 60,000 characters, so a place described on page 300 was "not found" (Nov 2026: Haicheng, Chaoyang and Kweiyang
    all sat in the cut-off part). Now: the passages around each mention of the place's distinctive name words, and
    around the record's verified quote (`anchors`), in page order, up to the same length. The auditor still sees no
    extraction, only source text."""
    if len(text) <= MAX_TEXT:
        return text
    import re
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()
    words = [w for w in re.findall(r"[^\W\d_]{4,}", name.lower()) if w not in GENERIC]
    hits: list[int] = []
    for a in anchors:
        probe = re.sub(r"\s+", " ", a).strip().lower()[:80]
        if probe and (i := low.find(probe)) >= 0:
            hits.append(i)
    for w in words:
        hits += [m.start() for m in re.finditer(re.escape(w), low)][:20]
    spans: list[list[int]] = []
    for i in sorted(set(hits)):
        lo, hi = max(0, i - WINDOW), min(len(flat), i + WINDOW)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    out, used = [], 0
    for lo, hi in spans:
        if used >= MAX_TEXT:
            break
        piece = flat[lo:min(hi, lo + MAX_TEXT - used)]
        out.append(piece)
        used += len(piece)
    return "\n[…]\n".join(out) if out else text[:MAX_TEXT]


def request(name: str, text: str, anchors: tuple[str, ...] = ()) -> Req:
    from .decisions import rulings_text
    return Req(model=AUDIT_MODEL, max_tokens=1500, system=SYSTEM + rulings_text(), purpose="audit",
               user=f"Place: {name}\n\n<text>\n{audit_text(name, text, anchors)}\n</text>")


def ask(name: str, text: str, llm=None) -> dict:
    res = (llm or shared()).one(request(name, text))
    if res is None:
        raise RuntimeError("no answer from the auditor this run")
    return parse_json(res["text"])


def agrees(record: dict, events: list[dict], answer: dict) -> tuple[bool, str]:
    if not answer.get("found"):
        return False, "auditor did not find the place in the source"
    if answer.get("specific_place") is False:
        return False, "auditor: not a specific physical place"
    if answer.get("site_type") and answer["site_type"] != record["site_type"] and record["site_type"] != "other":
        return False, f"type {record['site_type']} vs auditor {answer['site_type']}"
    years = {int(y) for y in answer.get("years", []) if str(y).isdigit()}

    def confirmed(e: dict) -> bool:
        # a range ("seventh or eighth century" = 601-800) is confirmed by any year inside it, or by a text that gives
        # no exact year; it was failing as "601 not confirmed" (Nov 2026, Qocho)
        if e.get("year_end") and e["year_end"] != e["year_start"]:
            return not years or any(e["year_start"] <= y <= e["year_end"] for y in years)
        return e["year_start"] in years
    missing = [e["year_start"] for e in events if e.get("year_start") and not confirmed(e)]
    if missing:
        return False, f"years not confirmed: {missing}"
    return True, "ok"


def run(store, uid: str, regions: list[str], sample: int = 10, llm=None) -> dict:
    audited = {a["subject_id"] for a in store.select("audits", select="subject_id", unit=f"eq.{uid}")}
    sites = [s for s in store.select("sites", select="id,name_en,site_type,region,unit,status")
             if s["status"] == "verified" and (s.get("unit") == uid or s["region"] in regions) and s["id"] not in audited]
    if not sites:
        notice(f"audit[{uid}]: nothing new to audit")
        return {}
    llm = llm or shared()
    docs = {d["source_id"]: d.get("text") or "" for d in store.select("documents", select="source_id,text")}
    stats = {"audited": 0, "agree": 0}
    picked = []
    for s in random.sample(sites, min(sample, len(sites))):
        claims = store.select("claims", select="source_id,excerpt", subject_id=f"eq.{s['id']}", status="eq.verified")
        src = next((c["source_id"] for c in claims if docs.get(c["source_id"])), None)
        if src:
            picked.append((s, docs[src], tuple(c.get("excerpt") or "" for c in claims if c["source_id"] == src)))
    answers = llm.run([request(s["name_en"], text, anchors) for s, text, anchors in picked])  # one batch for the sample
    for (s, _text, _a), res in zip(picked, answers, strict=True):
        if res is None:
            continue  # not answered this run; the site stays unaudited and can be sampled again
        try:
            answer = parse_json(res["text"])
        except ValueError as e:
            notice(f"audit: {s['name_en']}: unreadable answer ({e})", "warning")
            continue
        events = store.select("events", select="year_start,year_end", site_id=f"eq.{s['id']}", status="eq.verified")
        ok, why = agrees(s, events, answer)
        store.insert("audits", [{"unit": uid, "subject_kind": "site", "subject_id": s["id"], "agree": ok,
                                 "detail": {"why": why, "answer": answer}, "model": AUDIT_MODEL}])
        stats["audited"] += 1
        stats["agree"] += ok
    notice(f"audit[{uid}]: {stats}")
    llm.report()
    return stats
