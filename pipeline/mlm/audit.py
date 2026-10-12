"""Independent audit: re-read the source for a random sample of verified sites and compare.

The auditor never sees the extraction. It gets only the archived source text and the site's name, and lists what the
text says about that place. Agreement is decided by trusted code: same site type, and every event year in our record
appears in the auditor's list. Results go to the `audits` table and feed the unit's gate (>= 95% agreement).
"""

from __future__ import annotations

import json
import random

from .config import ANTHROPIC_API_KEY, AUDIT_MODEL, notice

SYSTEM = """You read a historical source and report what it says about one named place. Use only the text.
Return JSON only: {"found": true|false, "site_type": "church|hospital|school|residence|cemetery|mission_station|press|orphanage|other",
"years": [<every year the text ties to this place>], "notes": "<one line>"}"""


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def ask(name: str, text: str, client) -> dict:
    resp = client.messages.create(model=AUDIT_MODEL, max_tokens=1500, system=SYSTEM, messages=[{
        "role": "user", "content": f"Place: {name}\n\n<text>\n{text[:60000]}\n</text>"}])
    out = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(out[out.find("{"): out.rfind("}") + 1])


def agrees(record: dict, events: list[dict], answer: dict) -> tuple[bool, str]:
    if not answer.get("found"):
        return False, "auditor did not find the place in the source"
    if answer.get("site_type") and answer["site_type"] != record["site_type"] and record["site_type"] != "other":
        return False, f"type {record['site_type']} vs auditor {answer['site_type']}"
    years = {int(y) for y in answer.get("years", []) if str(y).isdigit()}
    missing = [e["year_start"] for e in events if e.get("year_start") and e["year_start"] not in years]
    if missing:
        return False, f"years not confirmed: {missing}"
    return True, "ok"


def run(store, uid: str, regions: list[str], sample: int = 10, client=None) -> dict:
    audited = {a["subject_id"] for a in store.select("audits", select="subject_id", unit=f"eq.{uid}")}
    sites = [s for s in store.select("sites", select="id,name_en,site_type,region,unit,status")
             if s["status"] == "verified" and (s.get("unit") == uid or s["region"] in regions) and s["id"] not in audited]
    if not sites:
        notice(f"audit[{uid}]: nothing new to audit")
        return {}
    client = client or _client()
    docs = {d["source_id"]: d.get("text") or "" for d in store.select("documents", select="source_id,text")}
    stats = {"audited": 0, "agree": 0}
    for s in random.sample(sites, min(sample, len(sites))):
        claims = store.select("claims", select="source_id", subject_id=f"eq.{s['id']}", status="eq.verified")
        text = next((docs[c["source_id"]] for c in claims if docs.get(c["source_id"])), "")
        if not text:
            continue
        events = store.select("events", select="year_start", site_id=f"eq.{s['id']}", status="eq.verified")
        try:
            answer = ask(s["name_en"], text, client)
        except Exception as e:  # noqa: BLE001
            notice(f"audit: {s['name_en']}: {e}", "warning")
            continue
        ok, why = agrees(s, events, answer)
        store.insert("audits", [{"unit": uid, "subject_kind": "site", "subject_id": s["id"], "agree": ok,
                                 "detail": {"why": why, "answer": answer}, "model": AUDIT_MODEL}])
        stats["audited"] += 1
        stats["agree"] += ok
    notice(f"audit[{uid}]: {stats}")
    return stats
