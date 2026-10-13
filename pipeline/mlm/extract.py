"""LLM extraction: turn a crawled page into structured, evidence-backed mentions.

Uses Claude tool-use with the Extraction JSON schema so output is always parseable.
Pages with no region keyword are skipped before any API call.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ANTHROPIC_API_KEY, EXTRACT_MODEL, WORK_DIR, notice
from .crawl import load_registry
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


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def relevant(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def system_prompt(region_cfg: dict) -> str:
    from .decisions import rulings_text
    note = region_cfg.get("extract_hint", "")
    region = f"\n\nRegion being mapped: {region_cfg.get('name', '')}\n{note}" if note or region_cfg.get("name") else ""
    return SYSTEM + region + rulings_text()


def extract_page(page: dict, client=None, system: str = SYSTEM) -> Extraction:
    client = client or _client()
    schema = Extraction.model_json_schema()
    resp = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=8000,
        system=system,
        tools=[{"name": "record_extraction", "description": "Record the extracted records.", "input_schema": schema}],
        tool_choice={"type": "auto"},  # forced tool choice is not supported by every model
        messages=[{
            "role": "user",
            "content": f"Source URL: {page['url']}\nTitle: {page['title']}\n\n<text>\n{page['text'][:MAX_CHARS]}\n</text>",
        }],
    )
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:  # the model found nothing to record
        return Extraction(document_url=page["url"])
    data = dict(block.input)
    data["document_url"] = page["url"]
    return Extraction.model_validate(data)


def same_page(old: str | None, new: str, threshold: float = 0.9) -> bool:
    """True when a re-crawled page is essentially the text already loaded. Some sites change a few lines on every
    visit (related links, counters), so an exact match would re-extract (and re-pay for) the same page each run."""
    return bool(old) and overlap(old, new) >= threshold


def overlap(old: str, new: str) -> float:
    a = {ln.strip() for ln in old.splitlines() if ln.strip()}
    b = {ln.strip() for ln in new.splitlines() if ln.strip()}
    return len(a & b) / len(a | b) if a and b else 0.0


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
    client = _client()
    stats = {"pages": 0, "cached": 0, "already_loaded": 0, "irrelevant": 0, "extracted": 0, "failed": 0}
    archived = _archived_texts()
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
        print(f"[extract] {page['url']}")
        try:
            ex = extract_page(page, client, system)
        except Exception as e:  # noqa: BLE001 - keep the batch going; failures are re-tried next run
            stats["failed"] += 1
            if stats["failed"] <= 3:
                notice(f"extract failed for {page['url']}: {type(e).__name__}: {str(e)[:300]}", "warning")
            continue
        stats["extracted"] += 1
        target.write_text(ex.model_dump_json(indent=2))
    notice(f"extract[{region}]: {stats}")
    return out
