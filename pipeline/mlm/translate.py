"""Fill missing Simplified Chinese fields, as cheaply as correctness allows.

Chinese names that came from the source text are marked 'cited' and left alone. Everything this stage writes is
marked 'translated', and the map shows those names with a 译 badge. Sources, in order:
  1. Glossary: names already in the hand-curated seed and in records whose Chinese name a source wrote (free).
  2. Wikidata, for people only, when exactly one person with the same name, birth year and death year has a
     Simplified Chinese label (free; established names such as 戴德生 instead of a phonetic guess).
  3. A model, for everything else: Gemini's free tier when GEMINI_API_KEY is set, Claude Haiku otherwise. Requests
     cover many documents at once and every phrase is cached, so a society or description is translated only once.
A machine-translation API is not used: it turns historical names into phonetic guesses, which rule 5 forbids presenting
as names.
"""

from __future__ import annotations

import json
import os

from .config import DATA_DIR, TRANSLATE_FALLBACK_MODEL, TRANSLATE_MODEL, WORK_DIR, notice
from .llm import Req, item_key, parse_json, shared
from .models import Extraction
from .resolve import normalize

SYSTEM = """Translate English text from historical records into Simplified Chinese (简体中文).
Rules:
- For people and institutions with an established historical Chinese name you are confident of (e.g. Hudson Taylor = 戴德生),
  use it. Otherwise give a plain phonetic transliteration (e.g. 乔治·史密斯). Never invent a "historical" Chinese name.
- Use standard terms for mission societies (e.g. China Inland Mission = 中国内地会, Church Missionary Society = 英国圣公会).
- Translate descriptions faithfully; do not add facts. The context field is only there to pick the right reading.
Input: a JSON list of {"i", "kind", "text", "context"}.
Return JSON only: {"results": [{"i": <index>, "zh": "<translation>"}]}"""

WIKIDATA = os.environ.get("MLM_WIKIDATA", "1") != "0"
UA = os.environ.get("MLM_USER_AGENT", "MissionaryLegacyMapBot/0.1 (+https://github.com/pinxiu/missionary-legacy-map)")


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


def context_of(ex: Extraction, key: str) -> tuple[str, str]:
    """(kind, context) for a field key from missing_fields."""
    head, _, tail = key.partition(".")
    idx = int(head[1:])
    if head[0] == "p":
        p = ex.persons[idx]
        years = f"{p.birth_year or '?'}–{p.death_year or '?'}"
        return ("person name" if tail == "name" else "mission society", f"{p.name_en} ({years}) {p.mission_society or ''}".strip())
    s = ex.sites[idx]
    kind = {"name": "place name", "summary": "summary", "status": "current use"}.get(tail, "event description")
    return kind, f"{s.name_en} ({s.site_type}, {s.city})"


# ---------------------------------------------------------------- 1. glossary

def glossary(store=None) -> dict[str, list[dict]]:
    """normalized English name -> [{"zh", "birth", "death"}] from the seed and source-cited records."""
    out: dict[str, list[dict]] = {}

    def add(name: str | None, zh: str | None, birth=None, death=None, city=None) -> None:
        if name and zh:
            out.setdefault(normalize(name), []).append({"zh": zh, "birth": birth, "death": death, "city": city})

    for f in sorted((DATA_DIR / "seed").glob("*.json")):
        d = json.loads(f.read_text())
        for p in d.get("persons", []):
            add(p.get("name_en"), p.get("name_zh"), p.get("birth_year"), p.get("death_year"))
            add(p.get("mission_society"), p.get("mission_society_zh"))
        for s in d.get("sites", []):
            for n in [s.get("name_en"), *[a for a in s.get("alt_names", []) if a.isascii()]]:
                add(n, s.get("name_zh"), city=s.get("region"))
    if store is not None:
        try:
            for p in store.select_all("persons", select="name_en,name_zh,birth_year,death_year",
                                      name_zh_status="eq.cited", status="in.(verified,approved)"):
                add(p["name_en"], p["name_zh"], p.get("birth_year"), p.get("death_year"))
            for s in store.select_all("sites", select="name_en,name_zh,region", name_zh_status="eq.cited",
                                      status="in.(verified,approved)"):
                add(s["name_en"], s["name_zh"], city=s.get("region"))
        except Exception as e:  # noqa: BLE001
            notice(f"translate: could not read cited names ({type(e).__name__}); using the seed only", "warning")
    return out


def from_glossary(gloss: dict, name: str, birth=None, death=None, city: str | None = None) -> str | None:
    """A name's Chinese form if the glossary holds exactly one reading that doesn't contradict the years. Place names
    repeat across cities ("Trinity Church"), so for a place (city given) only entries from the same city count."""
    cands = {c["zh"] for c in gloss.get(normalize(name), [])
             if not (birth and c["birth"] and birth != c["birth"]) and not (death and c["death"] and death != c["death"])
             and (city is None or (c["city"] and c["city"] == city))}
    return cands.pop() if len(cands) == 1 else None


# ---------------------------------------------------------------- 2. Wikidata (people)

def _year(claims: dict, prop: str) -> int | None:
    for c in claims.get(prop, []):
        t = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time", "")
        if len(t) > 5 and t[1:5].isdigit():
            return int(t[1:5]) * (-1 if t[0] == "-" else 1)
    return None


def wikidata_person(name: str, birth: int | None, death: int | None, http=None) -> str | None:
    """Simplified Chinese label of the one human on Wikidata with this name, birth year and death year; else None."""
    if not (birth and death):
        return None  # without both years a name match is too weak to trust
    import httpx
    c = http or httpx.Client(headers={"User-Agent": UA}, timeout=20)
    api = "https://www.wikidata.org/w/api.php"
    try:
        hits = c.get(api, params={"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                                  "limit": 7, "format": "json"}).json().get("search", [])
        if not hits:
            return None
        ents = c.get(api, params={"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits), "format": "json",
                                  "props": "labels|claims", "languages": "zh-hans|zh-cn"}).json().get("entities", {})
    except (httpx.HTTPError, ValueError):
        return None
    found = set()
    for e in ents.values():
        claims = e.get("claims", {})
        human = any(((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id") == "Q5"
                    for c in claims.get("P31", []))
        if not human or _year(claims, "P569") != birth or _year(claims, "P570") != death:
            continue
        labels = e.get("labels", {})
        zh = (labels.get("zh-hans") or labels.get("zh-cn") or {}).get("value")
        if zh:
            found.add(zh)
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------- 3. model

def _request(chunk: list[dict]) -> Req:
    payload = [{"i": n, "kind": it["kind"], "text": it["text"], "context": it["context"]} for n, it in enumerate(chunk)]
    return Req(model=TRANSLATE_MODEL, fallback=TRANSLATE_FALLBACK_MODEL, system=SYSTEM, max_tokens=8000,
               purpose="translate", json_out=True, user=json.dumps(payload, ensure_ascii=False))


def _parse(res: dict, chunk: list) -> dict[int, str]:
    out = {}
    for r in parse_json(res["text"]).get("results", []):
        i, zh = int(r["i"]), str(r.get("zh") or "").strip()
        if 0 <= i < len(chunk) and zh:
            out[i] = zh
    return out


def translate_all(exs: list[Extraction], llm=None, gloss: dict | None = None, wikidata=None) -> dict[str, int]:
    """Fill every document's missing Chinese fields. A document is marked translated only when all its fields are."""
    llm = llm or shared()
    gloss = gloss if gloss is not None else glossary(llm.cache.store)
    lookup = wikidata if wikidata is not None else (wikidata_person if WIKIDATA else None)
    stats = {"glossary": 0, "wikidata": 0, "model": 0, "unanswered": 0}
    wanted: list[tuple[int, str, dict]] = []  # (doc index, field key, item)
    filled: list[dict[str, str]] = [{} for _ in exs]
    for d, ex in enumerate(exs):
        for key, text in missing_fields(ex).items():
            kind, ctx = context_of(ex, key)
            person = ex.persons[int(key.split(".")[0][1:])] if key.startswith("p") and key.endswith(".name") else None
            if kind in ("person name", "place name", "mission society"):
                site = ex.sites[int(key.split(".")[0][1:])] if kind == "place name" else None
                if site is not None and site.city in ("", "other"):
                    site = None
                    hit = None
                else:
                    hit = from_glossary(gloss, text, person.birth_year if person else None,
                                        person.death_year if person else None, site.city if site else None)
                if hit:
                    filled[d][key] = hit
                    stats["glossary"] += 1
                    continue
            if person is not None and lookup is not None:
                wk = item_key("wikidata", [text, person.birth_year, person.death_year])
                known = llm.cache.get_many([wk]).get(wk)
                zh = known["v"] if known is not None else lookup(text, person.birth_year, person.death_year)
                if known is None:
                    llm.cache.put_many({wk: {"v": zh}}, model="wikidata", purpose="translate")
                if zh:
                    filled[d][key] = zh
                    stats["wikidata"] += 1
                    continue
            wanted.append((d, key, {"kind": kind, "text": text, "context": ctx}))
    answers = llm.per_item("translate", [w[2] for w in wanted], lambda it: [SYSTEM, it], 60, _request, _parse)
    for (d, key, _it), zh in zip(wanted, answers, strict=True):
        if zh:
            filled[d][key] = zh
            stats["model"] += 1
        else:
            stats["unanswered"] += 1
    for d, ex in enumerate(exs):
        complete = len(filled[d]) == len(missing_fields(ex))
        apply_translations(ex, filled[d])
        ex.translated = complete  # incomplete documents are retried next run; answered phrases come from the cache
    return stats


def run(region: str) -> None:
    llm = shared()
    stats = {"documents": 0, "translated": 0, "skipped": 0, "incomplete": 0}
    files, exs = [], []
    for f in sorted((WORK_DIR / "extracted" / region).glob("*.json")):
        stats["documents"] += 1
        ex = Extraction.model_validate_json(f.read_text())
        if ex.translated or not (ex.sites or ex.persons):
            stats["skipped"] += 1
            continue
        files.append(f)
        exs.append(ex)
    how = translate_all(exs, llm) if exs else {}
    for f, ex in zip(files, exs, strict=True):
        stats["translated" if ex.translated else "incomplete"] += 1
        f.write_text(ex.model_dump_json(indent=2))
    notice(f"translate[{region}]: {stats} {how}")
    llm.report()
