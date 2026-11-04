"""Cities that are not in the built-in gazetteer: find a real centre for them (OpenStreetMap) and remember it.

A site whose source names its city ("taian", "wuhan") used to fall back to the province centre when that city wasn't in
coverage/cities.yaml. `ensure_city` looks the city up once, adds it to `regions` with its centre (lat/lng) and registers it
for the rest of the process, so geocoding, pin search and later runs all use the same city centre.
"""

from __future__ import annotations

import time

import httpx

from .config import USER_AGENT, notice
from .geocode import CITIES, PROVINCE_CENTROIDS, REGION_CENTROIDS, km_apart

PLACE_TYPES = {"city", "town", "county", "municipality", "state_district", "village", "suburb", "city_district", "borough",
               "administrative", "district", "region", "hamlet"}
_misses: set[str] = set()
_last = 0.0


def has_cjk(s: str | None) -> bool:
    return bool(s) and any("一" <= ch <= "鿿" for ch in s)


def load_known(store) -> int:
    """Register the centres of regions created by earlier runs."""
    n = 0
    for r in store.select("regions", select="slug,name_en,name_zh,lat,lng"):
        if r.get("lat") is not None and r["slug"] not in REGION_CENTROIDS:
            REGION_CENTROIDS[r["slug"]] = (r["lat"], r["lng"])
            CITIES.setdefault(r["slug"], {"en": r["name_en"], "zh": r.get("name_zh") or r["name_en"], "lat": r["lat"], "lng": r["lng"]})
            n += 1
    return n


def lookup(city: str, province_en: str, province_slug: str, client: httpx.Client | None = None) -> dict | None:
    """The OSM place for a city named in lowercase pinyin, inside its province (a hit far from the province is another place)."""
    global _last
    wait = 1.1 - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    c = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20)
    try:
        r = c.get("https://nominatim.openstreetmap.org/search", params={
            "q": f"{city}, {province_en}, China", "format": "jsonv2", "limit": 5, "accept-language": "zh,en"})
        hits = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    centre = PROVINCE_CENTROIDS.get(province_slug)
    for h in hits:
        if h.get("addresstype") not in PLACE_TYPES:
            continue
        lat, lng = float(h["lat"]), float(h["lon"])
        if centre and km_apart(centre, (lat, lng)) > 400:
            continue
        return {"lat": lat, "lng": lng, "name_zh": h.get("name") if has_cjk(h.get("name")) else None}
    return None


def ensure_city(store, city: str | None, unit: dict, client: httpx.Client | None = None) -> str | None:
    """The region slug for a city of `unit` (created with a real centre if new), or None when it can't be placed."""
    c = (city or "").strip().lower()
    if not c or c == "other":
        return None
    suffix = unit["id"].split("-")[-1].lower()
    for slug in (c, f"{c}-{suffix}"):  # a pinyin name shared with another province gets a suffixed slug
        if slug in CITIES:
            return slug if CITIES[slug].get("province", unit["region"]) == unit["region"] or slug == f"{c}-{suffix}" else None
    key = f"{unit['region']}:{c}"
    if key in _misses:
        return None
    hit = lookup(c, unit["en"], unit["region"], client)
    if not hit:
        _misses.add(key)
        return None
    row = {"slug": c, "name_en": c.title(), "name_zh": hit["name_zh"] or c.title(), "parent": unit["region"],
           "lat": hit["lat"], "lng": hit["lng"]}
    store.upsert("regions", [row], "slug")
    REGION_CENTROIDS[c] = (hit["lat"], hit["lng"])
    CITIES[c] = {"en": row["name_en"], "zh": row["name_zh"], "lat": hit["lat"], "lng": hit["lng"], "province": unit["region"]}
    notice(f"places: new city {c} ({row['name_zh']}) in {unit['id']} at {hit['lat']:.3f},{hit['lng']:.3f}")
    return c
