"""Re-place sites that are still pinned at a city or province centre, in three steps.

1. City centres (no model): a site whose source names a city that isn't in the gazetteer (Taian, Wuhan, Changsha...) is
   moved from the province centre to that city's real centre (OpenStreetMap, recorded in `regions`).
2. Place leads (Haiku, cached, via llm.py): from the site's own description and quotes, up to three modern Simplified Chinese
   queries (a street address, today's name of the building or institution, a village or town). A lead is only a search
   term: nothing is pinned unless Amap returns a place that matches it, inside the city.
3. Approximate pins: a match that is only partial, or only a village/town, is pinned at `district` precision ("approximate
   location") with a cited `location` claim. Street-level precision needs an address or a close name match.
"""

from __future__ import annotations

import os

from .config import EXTRACT_MODEL, notice
from .geocode import CITIES, REGION_CENTROIDS, km_apart
from .llm import Req, parse_json, shared
from .locate import COARSE, PUBLIC, Finder, _fuzzy, apply_pick, search_radius
from .places import ensure_city, has_cjk, load_known
from .verify import norm

SYSTEM = """You turn a historical source's description of ONE place into search queries for a modern Chinese map (Amap).
Use only what the text says: a street address, a modern name the text gives ("now ...", "今..."), the institution or
building that still stands, or a village or town named in it. Do not add facts from your own memory. Write the queries in
Simplified Chinese, most specific first, at most 3. If the text gives nothing a map could find, return an empty list.
Return JSON only: {"queries": ["..."], "level": "street" | "area"} ("street" when a street address or exact building name is
given, "area" when it only names a village, town or district)."""

STREET_LEVELS = {"门牌号", "兴趣点", "单元号", "道路交叉路口"}
AREA_LEVELS = {"乡镇", "村庄", "热点商圈", "道路"}


def lead_request(site: dict, claim: dict, quotes: list[str]) -> Req:
    v = claim.get("value") or {}
    text = "\n".join(f"{k}: {v[k]}" for k in ("name_en", "name_zh", "city", "place_text", "modern_address", "current_status", "summary")
                     if v.get(k))
    text += "\nQuotes:\n" + "\n".join(f"- {q}" for q in quotes[:3])
    return Req(model=EXTRACT_MODEL, system=SYSTEM, max_tokens=500, purpose="places", user=text[:3000])


def claim_of(store, site_id: str) -> tuple[dict, list[str]]:
    rows = store.select("claims", select="value,excerpt,field", subject_id=f"eq.{site_id}", subject_kind="eq.site")
    first = next((r for r in rows if r.get("field") == "site"), rows[0] if rows else {})
    return first, [r["excerpt"] for r in rows if r.get("excerpt")]


def step_cities(store, sites: list[dict], units: dict, apply: bool, stats: dict) -> None:
    """Step 1: province-centre (or unknown-city) pins move to their real city centre."""
    for s in sites:
        claim, _ = claim_of(store, s["id"])
        city = ((claim.get("value") or {}).get("city") or "").strip().lower()
        u = units.get(s.get("unit"))
        if not u or not city or city == "other" or (s["region"] in CITIES and s["geo_precision"] != "province"):
            continue
        if s["region"] == city and s["region"] in CITIES:
            continue
        slug = ensure_city(store, city, u)
        if not slug or slug == s["region"]:
            continue
        lat, lng = REGION_CENTROIDS[slug]
        stats["city_centres"] += 1
        if apply:
            store.update("sites", {"id": s["id"]}, {"region": slug, "location": f"SRID=4326;POINT({lng} {lat})", "geo_precision": "city"})
            s.update(region=slug, geo_precision="city")


def classify(lead: str, hit: dict) -> str | None:
    """'street', 'district' or None for an Amap hit against one lead query."""
    if hit["source"] == "geocode":
        lvl = hit.get("level", "")
        return "street" if lvl in STREET_LEVELS else ("district" if lvl in AREA_LEVELS else None)
    nm, q = norm(hit["name"]), norm(lead)
    if len(nm) < 3 or len(q) < 3:
        return None
    if nm == q or _fuzzy(nm, q) >= 90:
        return "street"
    if (q in nm or nm in q) and min(len(q), len(nm)) >= 4:
        return "district"
    return "district" if _fuzzy(nm, q) >= 75 else None


def run(store, apply: bool = False, limit: int | None = None, llm=None, finder: Finder | None = None) -> dict:
    from .coverage import units as all_units
    load_known(store)
    units = {u["id"]: u for u in all_units()}
    cols = "id,slug,region,name_en,name_zh,alt_names,modern_address,geo_precision,status,unit"
    sites = [s for s in store.select_all("sites", select=cols)
             if s["status"] in (*PUBLIC, "pending") and s["geo_precision"] in COARSE]
    if limit:
        sites = sites[:limit]
    stats = {"coarse": len(sites), "city_centres": 0, "leads": 0, "street": 0, "district": 0, "none": 0}

    step_cities(store, sites, units, apply, stats)

    # Step 2: leads from the source text (one cached Haiku batch).
    pending = [s for s in sites if s["geo_precision"] in COARSE]
    claims = {s["id"]: claim_of(store, s["id"]) for s in pending}
    llm = llm or shared()
    answers = llm.run([lead_request(s, *claims[s["id"]]) for s in pending])
    finder = finder or Finder()
    regions = {r["slug"]: r for r in store.select("regions", select="slug,name_zh")}
    lines = ["| Site | City | Lead | Amap place | Precision | km from centre |", "|---|---|---|---|---|---|"]

    for s, res in zip(pending, answers, strict=True):
        centre = REGION_CENTROIDS.get(s["region"])
        if res is None or not centre:
            stats["none"] += 1
            continue
        try:
            leads = [q for q in parse_json(res["text"]).get("queries", []) if isinstance(q, str) and has_cjk(q)][:3]
        except ValueError:
            leads = []
        stats["leads"] += bool(leads)
        city_zh = (regions.get(s["region"]) or {}).get("name_zh") or ""
        max_km, _ = search_radius(s["region"])
        best_pick, best_prec = None, None
        for q in leads:
            hits = [*finder.amap_geocode(q, city_zh), *finder.amap(q, city_zh)]
            for h in hits:
                prec = classify(q, h)
                h["km"] = km_apart(centre, (h["lat"], h["lng"]))
                if prec and h["km"] <= max_km and (best_prec is None or (prec == "street" and best_prec == "district")):
                    best_pick, best_prec, h["lead"] = h, prec, q
            if best_prec == "street":
                break
        if not best_pick:
            stats["none"] += 1
            continue
        stats[best_prec] += 1
        lines.append(f"| {s['name_en']} | {s['region']} | {best_pick['lead']} | {best_pick['name']} | {best_prec} | "
                     f"{best_pick['km']:.1f} |")
        if apply:
            apply_pick(store, s["id"], best_pick, False, precision=best_prec,
                       note=f" (lead from the source text: {best_pick['lead']})")
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"### Replace ({'applied' if apply else 'dry run'}): {stats}\n\n{report}\n")
    notice(f"replace: {stats}")
    return stats
