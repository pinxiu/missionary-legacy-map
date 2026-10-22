"""The parse stage: turn a pool page into structured, evidence-backed mentions of EVERY place on it.

Uses Claude tool-use with the Extraction JSON schema so output is always parseable. Parsing is independent of finding
(which unit's search fetched the page) and of entity resolution (which site a mention is, and which unit it belongs to):
the prompt names no region, so a page's result is the same whoever asked, and one reading serves the whole world.
Pages showing no sign of Christian history are skipped before any API call.
"""

from __future__ import annotations

import os

from .config import (
    ANTHROPIC_API_KEY,
    EXTRACT_ESCALATE_MODEL,
    EXTRACT_MIN_EXCERPT_RATE,
    EXTRACT_MODEL,
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
- Extract every qualifying site the text describes, wherever in the world it is: each page is read once, for everyone, so
  nothing may be left out because it is far from the page's main subject. For each site give `country` (ISO 3166-1 alpha-2
  code, lowercase, of where it is today: "cn", "kr", "in"), `province` (the first-level division today, lowercase
  romanization: "zhejiang", "fujian", "gyeonggi"; a municipality such as Shanghai is its own province) and `city` (the
  city or town, lowercase romanization, e.g. "hangzhou"). Use historical spellings only to work out the modern place
  ("Hangchow" is hangzhou, "Foochow" is fuzhou); use "other" for the city only if the text names none.
- Historical scope only: skip anything after 1952 and anything about present-day religious workers or activity.
  A neutral note on current use ("now a university campus") is allowed in current_status.
- Use confidence 0.9 for explicit statements, 0.6 for implied or approximate ones, 0.4 for vague ones.
- If the text cites books, archives or articles with more detail, list them in citations_to_follow.
- Always respond by calling the record_extraction tool exactly once, with empty lists if nothing qualifies."""

MAX_CHARS = 60_000
CHUNK_OVERLAP = 1_500


def chunks(text: str, size: int = MAX_CHARS - 2_000, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """A long page (e.g. a whole book) as overlapping pieces that each fit the model, split at line breaks where possible."""
    if len(text) <= MAX_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            end = cut if cut > 0 else end
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def merge(parts: list[Extraction]) -> Extraction:
    """One extraction from the pieces of a long page. Later pieces repeating a site or person are merged on load."""
    first = parts[0].model_copy(deep=True)
    for p in parts[1:]:
        first.sites += p.sites
        first.persons += p.persons
        first.citations_to_follow = sorted(set(first.citations_to_follow) | set(p.citations_to_follow))
    return first


# A page is skipped before any model call only if it shows no sign of Christian history anywhere in the world.
CHRISTIAN_TERMS = ["church", "chapel", "cathedral", "mission", "missionary", "monastery", "seminary", "parish", "diocese",
                   "bishop", "christian", "catholic", "protestant", "jesuit", "nestorian", "gospel", "baptis",
                   "教堂", "教会", "教會", "礼拜堂", "禮拜堂", "传教", "傳教", "宣教", "天主", "基督", "福音", "神父", "主教",
                   "差会", "差會",
                   "景教", "修道", "神学", "神學", "牧师", "牧師", "教士"]
# Many hits for these with an empty result mean the cheap model probably missed something.
PLACE_TERMS = ["church", "chapel", "cathedral", "hospital", "seminary", "school", "cemetery", "monastery",
               "教堂", "礼拜堂", "禮拜堂", "医院", "醫院", "神学院", "神學院", "学校", "學校", "墓地", "修道院"]


def relevant(text: str, keywords: list[str] = CHRISTIAN_TERMS) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def keyword_hits(text: str, keywords: list[str]) -> int:
    lower = text.lower()
    return sum(lower.count(k.lower()) for k in keywords if k)


def place_hints() -> str:
    """Historical spellings and city notes for every registered region (not just the unit being worked on), so a place
    anywhere on a page is named correctly. Grows as the scouts add regions; it is not part of the parser version."""
    notes = [f"- {cfg.get('name', slug)}: {' '.join(str(cfg['extract_hint']).split())}"
             for slug, cfg in sorted(load_registry()["regions"].items()) if cfg.get("extract_hint")]
    return "\n\nHistorical spellings and city notes, by region:\n" + "\n".join(notes) if notes else ""


def system_prompt() -> str:
    from .decisions import rulings_text
    return SYSTEM + place_hints() + rulings_text()


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
        ex = Extraction.model_validate(data)
    except Exception:  # noqa: BLE001
        return None
    snap_excerpts(ex, page.get("text", "")[:MAX_CHARS])
    return ex


SNAP_MIN_SCORE = 92


def snap_excerpts(ex: Extraction, text: str) -> int:
    """Replace a quoted excerpt that is almost, but not exactly, in the page (changed punctuation, case or spacing, a
    dropped word) with the page's own text for that passage. The verifier only accepts verbatim excerpts and then asks
    whether the excerpt supports the claim, so this changes no verdict on its own: it stops good claims being thrown
    away (and pages being re-extracted with the expensive model) over transcription slips. Returns how many it fixed."""
    from rapidfuzz import fuzz

    from .verify import MIN_EXCERPT, excerpt_found
    evs = [e for p in ex.persons for e in p.evidence] + [e for s in ex.sites for e in s.evidence] + \
          [e for s in ex.sites for ev in s.events for e in ev.evidence]
    fixed = 0
    for e in evs:
        if not e.excerpt or excerpt_found(e.excerpt, text):
            continue
        r = fuzz.partial_ratio_alignment(e.excerpt, text, score_cutoff=SNAP_MIN_SCORE)
        if r is None:
            continue
        span = text[r.dest_start:r.dest_end].strip()
        if abs(len(span) - len(e.excerpt)) > 0.2 * len(e.excerpt) or not (MIN_EXCERPT <= len(span) <= 300):
            continue
        if excerpt_found(span, text):
            e.excerpt = span
            fixed += 1
    return fixed


def excerpt_rate(ex: Extraction, text: str) -> tuple[int, int]:
    """(excerpts found verbatim in the page, excerpts given). The same check the trusted verifier applies first."""
    from .verify import excerpt_found
    evs = [e for p in ex.persons for e in p.evidence] + [e for s in ex.sites for e in s.evidence] + \
          [e for s in ex.sites for ev in s.events for e in ev.evidence]
    return sum(excerpt_found(e.excerpt, text) for e in evs), len(evs)


def needs_escalation(result: dict | None, ex: Extraction | None, page: dict, keywords: list[str] = PLACE_TERMS) -> str:
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
    if not (ex.sites or ex.persons) and keyword_hits(page["text"][:MAX_CHARS], keywords) >= 5:
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


def run(pool, limit: int | None = None, prefer: str | None = None, llm=None) -> dict:
    """The parse stage. Reads up to `limit` pool pages that the current parser hasn't read, each in full, and stores every
    place found on them. It knows nothing about units: `prefer` (the unit being worked on) only decides which pages come
    first when there is a backlog. Pages found by any unit's search are served by one pass."""
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set; skipping parsing.")
    limit = limit or int(os.environ.get("MLM_PARSE_MAX_PAGES", "100"))
    llm = llm or shared()
    system = system_prompt()
    stats = {"pending": 0, "irrelevant": 0, "parsed": 0, "escalated": 0, "unanswered": 0, "chunks": 0, "backlog": 0}
    pages = pool.pending_parse(limit, prefer)
    stats["pending"] = len(pages)
    stats["backlog"] = sum(1 for h in pool._rows() if not pool.parsed(h)) - len(pages)
    files: list[dict] = []
    for page in pages:
        if page.get("kind") == "bibliography" or not relevant(page.get("text") or ""):
            stats["irrelevant"] += 1
            pool.mark(page["hash"], "extracted", locales=[])  # read, nothing to find: not read again
            continue
        files.append(page)

    # A long page (a book) goes in as several overlapping pieces; their results are merged back into one extraction.
    todo: list[tuple[int, dict]] = [(i, {**page, "text": piece}) for i, page in enumerate(files) for piece in chunks(page["text"])]
    stats["chunks"] = len(todo)

    # Pass 1: every piece through the cheap model, as one batch (cached results cost nothing).
    first = llm.run([request(page, system) for _, page in todo])
    results: list[Extraction | None] = []
    redo: list[int] = []
    for n, ((_, page), res) in enumerate(zip(todo, first, strict=True)):
        ex = parse(res, page)
        why = needs_escalation(res, ex, page) if EXTRACT_ESCALATE_MODEL != EXTRACT_MODEL else ""
        if why:
            redo.append(n)
            if len(redo) <= 5:
                print(f"[parse] escalating ({why}): {page['url']}")
        results.append(ex if res is not None else None)

    # Pass 2: the pieces that failed the checks, through the stronger model, as one batch.
    if redo:
        second = llm.run([request(todo[n][1], system, EXTRACT_ESCALATE_MODEL) for n in redo])
        for n, res in zip(redo, second, strict=True):
            ex = parse(res, todo[n][1])
            if ex is not None:
                results[n] = ex
                stats["escalated"] += 1
            elif res is None:
                results[n] = None  # not answered yet: retried next run rather than keeping the weaker result

    by_file: dict[int, list[Extraction | None]] = {}
    for (i, _), ex in zip(todo, results, strict=True):
        by_file.setdefault(i, []).append(ex)
    for i, page in enumerate(files):
        parts = by_file.get(i, [])
        if not parts or any(p is None for p in parts):
            stats["unanswered"] += 1
            continue  # a piece is pending (batch still running) or malformed twice: the whole page is retried next run
        pool.save_extraction(page["hash"], merge(parts))
        stats["parsed"] += 1
    notice(f"parse: {stats}")
    llm.report()
    return stats
