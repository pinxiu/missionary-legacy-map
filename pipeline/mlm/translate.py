"""Fill missing Simplified Chinese fields with one cheap model call per document.

Chinese names that came from the source text are marked 'cited' and left alone. Anything this stage
writes is marked 'translated', and the map shows those names with a 译 badge.
"""

from __future__ import annotations

import json

from .config import ANTHROPIC_API_KEY, TRANSLATE_MODEL, WORK_DIR, notice
from .models import Extraction

SYSTEM = """Translate English fields from a historical record into Simplified Chinese (简体中文).
Rules:
- For people and institutions with an established historical Chinese name you are confident of (e.g. Hudson Taylor = 戴德生),
  use it. Otherwise give a plain phonetic transliteration (e.g. 乔治·史密斯). Never invent a "historical" Chinese name.
- Use standard terms for mission societies (e.g. China Inland Mission = 中国内地会, Church Missionary Society = 英国圣公会).
- Translate descriptions faithfully; do not add facts.
- Return JSON only: an object mapping each input key to its translation."""


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def missing_fields(ex: Extraction) -> dict[str, str]:
    """Collect every English field whose Chinese counterpart is empty, keyed by a stable path."""
    out: dict[str, str] = {}
    for i, p in enumerate(ex.persons):
        if not p.name_zh:
            out[f"p{i}.name"] = p.name_en
        if p.mission_society and not p.mission_society_zh:
            out[f"p{i}.society"] = p.mission_society
    for i, s in enumerate(ex.sites):
        if not s.name_zh:
            out[f"s{i}.name"] = s.name_en
        if s.summary and not s.summary_zh:
            out[f"s{i}.summary"] = s.summary
        if s.current_status and not s.current_status_zh:
            out[f"s{i}.status"] = s.current_status
        for j, e in enumerate(s.events):
            if e.description and not e.description_zh:
                out[f"s{i}.e{j}"] = e.description
    return out


def apply_translations(ex: Extraction, tr: dict[str, str]) -> Extraction:
    for p_i, p in enumerate(ex.persons):
        if p.name_zh:
            p.name_zh_status = p.name_zh_status or "cited"
        elif tr.get(f"p{p_i}.name"):
            p.name_zh, p.name_zh_status = tr[f"p{p_i}.name"], "translated"
        p.mission_society_zh = p.mission_society_zh or tr.get(f"p{p_i}.society")
    for s_i, s in enumerate(ex.sites):
        if s.name_zh:
            s.name_zh_status = s.name_zh_status or "cited"
        elif tr.get(f"s{s_i}.name"):
            s.name_zh, s.name_zh_status = tr[f"s{s_i}.name"], "translated"
        s.summary_zh = s.summary_zh or tr.get(f"s{s_i}.summary")
        s.current_status_zh = s.current_status_zh or tr.get(f"s{s_i}.status")
        for e_j, e in enumerate(s.events):
            e.description_zh = e.description_zh or tr.get(f"s{s_i}.e{e_j}")
    ex.translated = True
    return ex


def translate(ex: Extraction, client=None) -> Extraction:
    fields = missing_fields(ex)
    if not fields:
        return apply_translations(ex, {})
    client = client or _client()
    resp = client.messages.create(
        model=TRANSLATE_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(fields, ensure_ascii=False)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = text[text.find("{"): text.rfind("}") + 1]
    return apply_translations(ex, json.loads(text) if text else {})


def run(region: str) -> None:
    if not ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set; skipping translation.")
    client = _client()
    stats = {"documents": 0, "translated": 0, "skipped": 0, "failed": 0}
    for f in sorted((WORK_DIR / "extracted" / region).glob("*.json")):
        stats["documents"] += 1
        ex = Extraction.model_validate_json(f.read_text())
        if ex.translated or not (ex.sites or ex.persons):
            stats["skipped"] += 1
            continue
        try:
            ex = translate(ex, client)
        except Exception as e:  # noqa: BLE001 - retried next run
            stats["failed"] += 1
            if stats["failed"] <= 3:
                notice(f"translate failed for {ex.document_url}: {type(e).__name__}: {str(e)[:200]}", "warning")
            continue
        stats["translated"] += 1
        f.write_text(ex.model_dump_json(indent=2))
    notice(f"translate[{region}]: {stats}")
