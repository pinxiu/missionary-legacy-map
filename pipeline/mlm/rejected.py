"""Owner review of rejected records, so a record the verifier turned down can be looked at again (no model calls).

`mlm decisions rejected [--unit X]`  keeps one `rejected-review` issue per unit up to date: every rejected site, and
                                     every rejected event on a site that was kept, with the reason and the source quote.
                                     Each line has a checkbox.
`mlm decisions reconsider --issue N` runs when the owner ticks boxes: each ticked record gets its own `needs-decision`
                                     issue showing every claim, its quote and why it failed. The owner then adds one
                                     decision label, applied by `mlm decide` (decisions.py):
    decision:keep-record    restore it as `verified` (it still goes through the audit and the unit approval)
    decision:edit-record    edit the JSON first (e.g. drop the details no source states), then restore it
    decision:reject-record  confirm the rejection

Restoring never invents a citation: claims whose quote is in the archived source but that the support check turned
down are accepted on the owner's word; claims whose quote is not in the source stay rejected. A record left with no
accepted claim can't be restored here (add it through the app with a source instead).
"""

from __future__ import annotations

import datetime as dt
import json
import re

from .config import notice
from .decisions import FIXABLE, gh

LABEL = ("rejected-review", "8a8a8a")
EVENT_FIXABLE = {"event_type", "year_start", "year_end", "description", "description_zh"}
NOT_FOUND = "excerpt not found"
MARK = re.compile(r"<!-- r:(site|event):([0-9a-f-]{36}) -->")
BODY_LIMIT = 62000


def _short(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _unit_sites(store, uid: str) -> list[dict]:
    from .coverage import regions_of, unit

    regs = regions_of(unit(uid))
    sites = store.select("sites", select="id,slug,name_en,name_zh,site_type,region,unit,status,review_note,reconsider_issue")
    return [s for s in sites if s.get("unit") == uid or s["region"] in regs]


def _claims(store, subject_id: str) -> list[dict]:
    return store.select("claims", select="id,status,excerpt,review_note,source_id,value", subject_id=f"eq.{subject_id}")


def _source_urls(store, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = store.select("sources", select="id,url", id=f"in.({','.join(ids)})")
    return {r["id"]: r["url"] for r in rows}


def _line(kind: str, rec: dict, title: str, why: str, quote: str | None, url: str | None) -> str:
    done = rec.get("reconsider_issue")
    box = "[x]" if done else "[ ]"
    link = f" · [source]({url})" if url else ""
    tail = f" → #{done}" if done else ""
    out = f"- {box} **{title}** — {_short(why, 220) or 'no reason recorded'}{link}{tail} <!-- r:{kind}:{rec['id']} -->"
    if quote:
        out += f"\n  > {_short(quote, 160)}"
    return out


def render(store, uid: str) -> tuple[str, int]:
    from .coverage import unit

    u = unit(uid)
    sites = _unit_sites(store, uid)
    names = {s["id"]: s for s in sites}
    partly, not_place, unsupported, events = [], [], [], []
    for s in [s for s in sites if s["status"] == "rejected"]:
        claims = _claims(store, s["id"])
        ok = [c for c in claims if c["status"] in ("verified", "approved")]
        urls = _source_urls(store, {c["source_id"] for c in claims if c.get("source_id")})
        best = (ok or [c for c in claims if NOT_FOUND not in (c.get("review_note") or "")] or claims or [{}])[0]
        title = f"{s['name_en']} {s.get('name_zh') or ''}".strip() + f" · {s['site_type']} · {s['region']}"
        line = _line("site", s, title, s.get("review_note") or "", best.get("excerpt"), urls.get(best.get("source_id")))
        note = s.get("review_note") or ""
        (not_place if note.startswith("not a specific physical place") else partly if ok else unsupported).append(line)
    kept = [s["id"] for s in sites if s["status"] != "rejected"]
    for i in range(0, len(kept), 50):
        for e in store.select("events", select="id,site_id,event_type,year_start,year_end,description,review_note,reconsider_issue",
                              site_id=f"in.({','.join(kept[i:i + 50])})", status="eq.rejected"):
            claims = _claims(store, e["id"])
            urls = _source_urls(store, {c["source_id"] for c in claims if c.get("source_id")})
            best = ([c for c in claims if NOT_FOUND not in (c.get("review_note") or "")] or claims or [{}])[0]
            yrs = f"{e.get('year_start') or '—'}" + (f"–{e['year_end']}" if e.get("year_end") else "")
            title = f"{names[e['site_id']]['name_en']}: {yrs} {e['event_type']} — {_short(e.get('description'), 90)}"
            events.append(_line("event", e, title, e.get("review_note") or "", best.get("excerpt"), urls.get(best.get("source_id"))))

    def section(head: str, help_: str, rows: list[str]) -> str:
        return f"### {head} ({len(rows)})\n_{help_}_\n\n" + ("\n".join(rows) if rows else "_None._") + "\n"

    body = f"""**Unit:** {u['en']} {u['zh']} · rejected records, refreshed by the pipeline. Updated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC.

**To look at one again, tick its box.** Each ticked record gets its own `needs-decision` issue with every quote and why
it failed, where you choose keep / edit / reject. Nothing is published from here: a restored record still goes through
the audit and your approval of the unit.

{section("Partly supported", "Some of its quotes check out; other details in the record don't appear in the source. Often the place is real and the summary says too much: edit-record lets you trim it.", partly)}
{section("Not supported by the quoted source", "The quote is missing from the source or doesn't say what the record claims.", unsupported)}
{section("Not a specific place", "Judged to be a city, a person's work, an event or a movement rather than a place you could stand at.", not_place)}
{section("Rejected details on kept sites", "Events (founding years, people, closures) the source didn't support, on sites that passed.", events)}
<!-- rejected-review:{uid} -->
"""
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n…(list truncated; the rest appears as earlier ones are decided)\n<!-- rejected-review:" + uid + " -->\n"
    return body, len(partly) + len(unsupported) + len(not_place) + len(events)


def _review_issue(uid: str) -> int | None:
    raw = gh("issue", "list", "--label", LABEL[0], "--state", "open", "--limit", "100", "--json", "number,body")
    for i in json.loads(raw or "[]"):
        if f"<!-- rejected-review:{uid} -->" in (i.get("body") or ""):
            return i["number"]
    return None


def refresh(store, uid: str) -> int:
    gh("label", "create", *LABEL[:1], "--color", LABEL[1], "--force", check=False)
    body, n = render(store, uid)
    num = _review_issue(uid)
    if num:
        gh("issue", "edit", str(num), "--body", body)
    elif n:
        url = gh("issue", "create", "--title", f"Rejected records · {uid}", "--body", body, "--label", LABEL[0])
        num = int(url.rstrip("/").split("/")[-1])
    notice(f"rejected[{uid}]: {n} record(s) listed in #{num}")
    return n


def refresh_active(store) -> None:
    from .coverage import unit_issues, units

    issues = unit_issues()
    for u in units():
        i = issues.get(u["id"])
        if i and any(x in i["labels"] for x in ("in-progress", "needs-approval", "approved", "done")):
            refresh(store, u["id"])


def decision_body(store, uid: str, kind: str, rec: dict) -> str:
    claims = _claims(store, rec["id"])
    urls = _source_urls(store, {c["source_id"] for c in claims if c.get("source_id")})
    rows = []
    for c in claims:
        mark = "✅" if c["status"] in ("verified", "approved") else "❌"
        rows.append(f"- {mark} {_short(c.get('review_note') or 'supported', 200)} · [source]({urls.get(c.get('source_id'), '')})\n"
                    f"  > {_short(c.get('excerpt'), 400) or '(no quote)'}")
    fields = FIXABLE if kind == "site" else EVENT_FIXABLE
    fix = {k: rec.get(k) for k in sorted(fields) if k in rec and k not in ("lat", "lng")}
    name = f"{rec['name_en']} {rec.get('name_zh') or ''}" if kind == "site" else f"{rec['event_type']} {rec.get('year_start') or ''}"
    return f"""**Unit:** {uid} · **Rejected {kind}:** {name}

**Why the verifier rejected it:** {rec.get('review_note') or '—'}

### Claims and quotes
{chr(10).join(rows) or '_No claims._'}

### Decide (add ONE label)
- `decision:keep-record` — restore it as it is. Quotes that are in the source are accepted on your word; it still goes
  through the audit and your approval of the unit.
- `decision:edit-record` — edit the JSON below first (e.g. remove details no quote states), then add the label.
- `decision:reject-record` — the rejection stands.

Optional: a line starting with `ruling:` sets a general rule every agent follows from now on.

<!-- fix -->
```json
{json.dumps(fix, ensure_ascii=False, indent=2)}
```

<!-- reconsider:{kind}:{rec['id']}:{uid} -->
"""


def reconsider(store, issue: int) -> int:
    """Open a needs-decision issue for each record ticked in a rejected-review issue."""
    body = json.loads(gh("issue", "view", str(issue), "--json", "body"))["body"]
    m = re.search(r"<!-- rejected-review:([\w-]+) -->", body)
    if not m:
        raise SystemExit("Not a rejected-review issue.")
    uid = m.group(1)
    made = 0
    for line in body.splitlines():
        if not line.lstrip().startswith("- [x]"):
            continue
        mk = MARK.search(line)
        if not mk:
            continue
        kind, rid = mk.groups()
        table = "sites" if kind == "site" else "events"
        rec = (store.select(table, select="*", id=f"eq.{rid}") or [None])[0]
        if not rec or rec["status"] != "rejected" or rec.get("reconsider_issue"):
            continue
        if kind == "event":
            site = store.select("sites", select="name_en", id=f"eq.{rec['site_id']}")[0]
            title = f"Reconsider: {site['name_en']} — {rec['event_type']} {rec.get('year_start') or ''} ({uid})"
        else:
            title = f"Reconsider: {rec['name_en']} {rec.get('name_zh') or ''} ({uid})"
        url = gh("issue", "create", "--title", title[:200], "--body", decision_body(store, uid, kind, rec), "--label", "needs-decision")
        store.update(table, {"id": rid}, {"reconsider_issue": int(url.rstrip("/").split("/")[-1])})
        made += 1
    refresh(store, uid)
    notice(f"reconsider #{issue}: opened {made} decision issue(s)")
    return made


def decide(store, issue: int, kind: str, rid: str, choice: str, body: str) -> str:
    """Apply the owner's label on a reconsider issue (called from decisions.decide)."""
    table = "sites" if kind == "site" else "events"
    if choice == "reject-record":
        store.update(table, {"id": rid}, {"review_note": f"rejection confirmed by owner (#{issue})"})
        return "rejection confirmed"
    claims = _claims(store, rid)
    found = [c for c in claims if NOT_FOUND not in (c.get("review_note") or "")]
    if not found:
        raise SystemExit("Can't restore: none of its quotes were found in the archived source. Add it through the app "
                         "with a source instead.")
    changes = {}
    if choice == "edit-record":
        fm = re.search(r"<!-- fix -->\s*```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
        if not fm:
            raise SystemExit("decision:edit-record needs the JSON block after <!-- fix -->.")
        allowed = FIXABLE if kind == "site" else EVENT_FIXABLE
        changes = {k: v for k, v in json.loads(fm.group(1)).items() if k in allowed}
        if kind == "event" and (changes.get("year_start") or 0) > 1952:
            raise SystemExit("Events after 1952 can't be published.")
    for c in found:
        if c["status"] == "rejected":
            store.update("claims", {"id": c["id"]}, {"status": "verified", "review_note": f"accepted by owner (#{issue})"})
    if kind == "site":
        from .decisions import site_changes
        changes = site_changes(store, changes)
    store.update(table, {"id": rid}, {**changes, "status": "verified", "review_note": f"restored by owner (#{issue})"})
    return f"restored as verified{' with ' + ', '.join(sorted(changes)) + ' updated' if changes else ''}"
