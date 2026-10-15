"""LLM extraction: turn a crawled page into structured, evidence-backed mentions.

Uses Claude tool-use with the Extraction JSON schema so output is always parseable.
Pages with no region keyword are skipped before any API call.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import (
    ANTHROPIC_API_KEY,
    EXTRACT_ESCALATE_MODEL,
    EXTRACT_MIN_EXCERPT_RATE,
    EXTRACT_MODEL,
    WORK_DIR,
    notice,
)
from .crawl import load_registry
from .llm import Req, shared
from .models import Extraction

SYSTEM = """You extract historical records of Christian history from source texts for a cited historical map: missions of every
tradition (Church of the East, Catholic, Protestant, Orthodox), indigenous and independent churches and movements,
revivals and church leaders, schools, hospitals, presses, cemeteries and memorials.

Rules:
- Extract only what the text states. Never add facts from your own knowledge.
- Every site, person and event needs evidence: the source URL and a short excerpt (under 300 characters) copied from the text.
- A site must be a specific physical place someone could stand at: a named church, chapel, school, hospital, cemetery,
  residence, press, orphanage or mission compound. Never make a site out of a city or region ("Taizhou"), a person's work
  in an area, a meeting, a conference, a movement or an anniversary. Record those as events on the specific place where they
  happened, or leave them out if the text names no such place.
- Record places as written (place_text), including historical romanizations such as "Hangchow" or "Kwang-Chi".
- Sources may be English or Chinese. Fill name_zh (and summary_zh / description_zh) only with Simplified Chinese
  actually written in the text (convert Traditional characters to Simplified). Leave them empty otherwise.
- Set `city` to the prefecture-level city the site is in, in lowercase pinyin (e.g. "hangzhou", "ningbo"), using the
  region notes below for historical spellings. Use "other" if the site is outside the region being mapped.
- Historical scope only: skip anything after 1952 and anything about present-day religious workers or activity.
  A neutral note on current use ("now a university campus") is allowed in current_status.
- Use confidence 0.9 for explicit statements, 0.6 for implied or approximate ones, 0.4 for vague ones.
- If the text cites books, archives or articles with more detail, list them in citations_to_follow.
- Always respond by calling the record_extraction tool exactly once, with empty lists if nothing qualifies."""

MAX_CHARS = 60_000


def relevant(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def keyword_hits(text: str, keywords: list[str]) -> int:
    lower = text.lower()
    return sum(lower.count(k.lower()) for k in keywords if k)


def system_prompt(region_cfg: dict) -> str:
    from .decisions import rulings_text
    note = region_cfg.get("extract_hint", "")
    region = f"\n\nRegion being mapped: {region_cfg.get('name', '')}\n{note}" if note or region_cfg.get("name") else ""
    return SYSTEM + region + rulings_text()


def request(page: dict, system: str = SYSTEM, model: str = EXTRACT_MODEL) -> Req:
    schema = Extraction.model_json_schema()
    return Req(model=model, system=system, max_tokens=8000, purpose="extract",
               tools=[{"name": "record_extraction", "description": "Record the extracted records.", "input_schema": schema}],
               user=f"Source URL: {page['url']}\nTitle: {page['title']}\n\n<text>\n{page['text'][:MAX_CHARS]}\n</text>")


def parse(result: dict | None, page: dict) -> Extraction | None:
    """The extraction in a model result; None if the result is missing or malformed."""
    if result is None:
        return None
    if result.get("tool_input") is None:  # the model found nothing to record
        return Extraction(document_url=page["url"])
    try:
        data = dict(result["tool_input"])
        data["document_url"] = page["url"]
        return Extraction.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def excerpt_rate(ex: Extraction, text: str) -> tuple[int, int]:
    """(excerpts found verbatim in the page, excerpts given). The same check the trusted verifier applies first."""
    from .verify import excerpt_found
    evs = [e for p in ex.persons for e in p.evidence] + [e for s in ex.sites for e in s.evidence] + \
          [e for s in ex.sites for ev in s.events for e in ev.evidence]
    return sum(excerpt_found(e.excerpt, text) for e in evs), len(evs)


def needs_escalation(result: dict | None, ex: Extraction | None, page: dict, keywords: list[str]) -> str:
    """Why the cheap model's result should be redone with the stronger one ('' = keep it)."""
    if result is None:
        return ""  # not answered this run (e.g. batch still running): retried next run, not escalated
    if ex is None:
        return "malformed output"
    if result.get("stop_reason") == "max_tokens":
        return "output cut off"
    found, total = excerpt_rate(ex, page["text"][:MAX_CHARS])
    if total and found / total < EXTRACT_MIN_EXCERPT_RATE:
        return f"only {found}/{total} excerpts found in the page"
    if not (ex.sites or ex.persons) and keyword_hits(page["text"][:MAX_CHARS], keywords) >= 3:
        return "nothing found on a clearly relevant page"
    return ""


def extract_page(page: dict, llm=None, system: str = SYSTEM) -> Extraction:
    """One page through the cheap model, escalating once if its result fails the local checks (used for single pages)."""
    llm = llm or shared()
    res = llm.one(request(page, system))
    ex = parse(res, page)
    if needs_escalation(res, ex, page, []) and EXTRACT_ESCALATE_MODEL != EXTRACT_MODEL:
        res = llm.one(request(page, system, EXTRACT_ESCALATE_MODEL))
        ex = parse(res, page)
    if ex is None:
        raise RuntimeError("no extraction returned")
    return ex


def same_page(old: str | None, new: str, threshold: float = 0.9) -> bool:
    """True when a re-crawled page is essentially the text already loaded. Some sites change a few lines on every
    visit (related links, counters), so an exact match would re-extract (and re-pay for) the same page each run."""
    from .crawl import strip_teasers
    return bool(old) and overlap(strip_teasers(old), strip_teasers(new)) >= threshold


def overlap(old: str, new: str) -> float:
    """Share of the text (by characters, so long paragraphs outweigh short link lines) present in both versions.
    E.g. bdcconline pages end with three random "other biographies" teasers that change on every visit."""
    a = {ln.strip() for ln in old.splitlines() if ln.strip()}
    b = {ln.strip() for ln in new.splitlines() if ln.strip()}
    union = sum(len(x) for x in a | b)
    return sum(len(x) for x in a & b) / union if union else 0.0


def _archived_texts() -> dict[str, str]:
    """Page text already loaded into the database, by URL. Empty when the database isn't configured."""
    from .config import SUPABASE_SECRET_KEY, SUPABASE_URL
    if not (SUPABASE_URL and SUPABASE_SECRET_KEY):
        return {}
    try:
        from .store import Store
        return {d["url"]: d.get("text") or "" for d in Store().select("documents", select="url,text")}
    except Exception as e:  # noqa: BLE001 - fall back to the local cache only
        notice(f"extract: could not read loaded documents ({type(e).__name__}); relying on the local cache", "warning")
        return {}


def run(region: str) -> Path:
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set; skipping extraction.")
    cfg = load_registry()["regions"][region]
    keywords, system = cfg["keywords"], system_prompt(cfg)
    raw = WORK_DIR / "raw" / region
    out = WORK_DIR / "extracted" / region
    out.mkdir(parents=True, exist_ok=True)
    llm = shared()
    stats = {"pages": 0, "cached": 0, "already_loaded": 0, "irrelevant": 0, "extracted": 0, "escalated": 0,
             "failed": 0, "pending": 0}
    archived = _archived_texts()
    todo: list[tuple[Path, dict]] = []
    for f in sorted(raw.glob("*.json")):
        stats["pages"] += 1
        target = out / f.name
        if target.exists():
            stats["cached"] += 1
            continue  # content-hash keyed: unchanged pages are not re-extracted
        page = json.loads(f.read_text())
        if page.get("text") and page["url"] in archived and not same_page(archived[page["url"]], page["text"]):
            stats["changed_since_load"] = stats.get("changed_since_load", 0) + 1
            if stats["changed_since_load"] <= 3:
                print(f"[extract] changed since load ({overlap(archived[page['url']], page['text']):.0%} lines shared): {page['url']}")
        if page.get("text") and same_page(archived.get(page["url"]), page["text"]):
            stats["already_loaded"] += 1
            continue  # same text already extracted and loaded in an earlier run (work cache was lost); don't pay twice
        if page.get("kind") == "bibliography" or not relevant(page["text"], keywords):
            stats["irrelevant"] += 1
            continue
        todo.append((out / f.name, page))

    # Pass 1: every page through the cheap model, as one batch (cached results cost nothing).
    first = llm.run([request(page, system) for _, page in todo])
    results: list[Extraction | None] = []
    redo: list[int] = []
    for n, ((_, page), res) in enumerate(zip(todo, first, strict=True)):
        ex = parse(res, page)
        why = needs_escalation(res, ex, page, keywords) if EXTRACT_ESCALATE_MODEL != EXTRACT_MODEL else ""
        if why:
            redo.append(n)
            if len(redo) <= 5:
                print(f"[extract] escalating ({why}): {page['url']}")
        results.append(ex if res is not None else None)
        if res is None:
            stats["pending"] += 1

    # Pass 2: the pages that failed the checks, through the stronger model, as one batch.
    if redo:
        second = llm.run([request(todo[n][1], system, EXTRACT_ESCALATE_MODEL) for n in redo])
        for n, res in zip(redo, second, strict=True):
            ex = parse(res, todo[n][1])
            if ex is not None:
                results[n] = ex
                stats["escalated"] += 1
            elif res is None:
                results[n] = None  # not answered yet: retried next run rather than keeping the weaker result
                stats["pending"] += 1

    for (target, _page), ex in zip(todo, results, strict=True):
        if ex is None:
            continue  # pending (answered in a later run) or malformed twice (retried next run)
        stats["extracted"] += 1
        target.write_text(ex.model_dump_json(indent=2))
    stats["failed"] = len(todo) - stats["extracted"] - stats["pending"]
    notice(f"extract[{region}]: {stats}")
    llm.report()
    return out
