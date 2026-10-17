"""Per-province, per-city discovery of source pages (no model calls).

For a coverage unit this searches Wikipedia (English and Chinese) for the province and for each of its cities, using
the themes of coverage/catalog.yaml, and returns the hits as extra crawl sources. They go into the resource pool like
any other page: the text is hashed, a page already extracted is skipped, and the extractor keeps every place on it,
so a city's search also fills in neighbouring cities. The curated, reputable sources in sources.yaml are unchanged
and are still crawled first; this only widens the net, politely (Wikipedia's API, robots.txt checked by the crawler).
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

MAX_PER_QUERY = 5
MAX_PAGES_PER_UNIT = 60

QUERIES_EN = ["{place} Christianity mission church history", "{place} Christian hospital school missionary"]
QUERIES_ZH = ["{place} 基督教 教堂 传教士", "{place} 天主教 教堂 历史", "{place} 教会 医院 学校 教会学校"]


def places_of(unit: dict) -> list[tuple[str, str, str]]:
    """(slug, English name, Chinese name) for the unit's province and each of its cities."""
    from .geocode import CITIES

    out = [(unit["region"], unit["en"], unit["zh"])]
    for slug in unit.get("subregions", []):
        c = CITIES.get(slug)
        if c:
            out.append((slug, c["en"], c["zh"]))
    for c in CITIES.values():  # cities listed under the province in cities.yaml but not in the unit's subregions
        if c["province"] == unit["region"] and all(c["slug"] != p[0] for p in out):
            out.append((c["slug"], c["en"], c["zh"]))
    return out


def wiki_search(client: httpx.Client, lang: str, query: str, limit: int = MAX_PER_QUERY) -> list[str]:
    try:
        r = client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"})
        hits = r.json().get("query", {}).get("search", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []
    return [f"https://{lang}.wikipedia.org/wiki/{quote(h['title'].replace(' ', '_'))}" for h in hits]


def discovered_sources(unit: dict, client: httpx.Client) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    total = 0
    for _slug, en, zh in places_of(unit):
        urls: list[str] = []
        for lang, name, templates in (("en", en, QUERIES_EN), ("zh", zh, QUERIES_ZH)):
            for t in templates:
                for u in wiki_search(client, lang, t.format(place=name)):
                    if u not in seen and total + len(urls) < MAX_PAGES_PER_UNIT:
                        seen.add(u)
                        urls.append(u)
        if urls:
            total += len(urls)
            out.append({"name": f"Discovery · {en} (Wikipedia search)", "start_urls": urls, "follow": [], "max_pages": len(urls),
                        "language": "mixed", "kind": "web"})
    return out
