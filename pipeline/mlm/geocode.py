"""Geocoding historical place text.

Order: curated gazetteer (historical names, hand-checked) -> OpenStreetMap Nominatim (modern address)
-> region centroid at 'city' precision. Nominatim policy: 1 request/second, identifying User-Agent.
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

from .config import DATA_DIR, USER_AGENT
from .resolve import normalize

REGION_CENTROIDS = {"hangzhou": (30.2741, 120.1551), "zhejiang": (29.1832, 120.0934), "china": (35.8617, 104.1954)}


@lru_cache
def gazetteer(region: str) -> list[dict]:
    p = Path(DATA_DIR) / "gazetteer" / f"{region}.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else []


def from_gazetteer(region: str, *texts: str) -> tuple[float, float, str] | None:
    for entry in gazetteer(region):
        names = [normalize(n) for n in entry["names"]]
        for t in texts:
            nt = normalize(t or "")
            if nt and any(n in nt or nt in n for n in names):
                return entry["lat"], entry["lng"], entry.get("precision", "district")
    return None


_last = 0.0


def from_nominatim(query: str, client: httpx.Client | None = None) -> tuple[float, float, str] | None:
    global _last
    wait = 1.1 - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    c = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20)
    try:
        r = c.get("https://nominatim.openstreetmap.org/search", params={"q": query, "format": "jsonv2", "limit": 1})
        hits = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    if not hits:
        return None
    h = hits[0]
    precision = "street" if h.get("addresstype") in {"building", "amenity", "road", "house"} else "district"
    return float(h["lat"]), float(h["lon"]), precision


def geocode(region: str, name: str, place_text: str = "", modern_address: str | None = None,
            use_network: bool = True) -> tuple[float, float, str]:
    hit = from_gazetteer(region, name, place_text, modern_address or "")
    if hit:
        return hit
    if use_network and modern_address:
        hit = from_nominatim(modern_address)
        if hit:
            return hit
    lat, lng = REGION_CENTROIDS.get(region, REGION_CENTROIDS["china"])
    return lat, lng, "city"
