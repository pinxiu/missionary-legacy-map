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

from .config import DATA_DIR, REPO_ROOT, USER_AGENT
from .resolve import normalize

REGION_CENTROIDS = {
    "hangzhou": (30.2741, 120.1551), "ningbo": (29.8683, 121.5440), "wenzhou": (28.0006, 120.6994),
    "shaoxing": (30.0303, 120.5800), "huzhou": (30.8930, 120.0868), "jiaxing": (30.7461, 120.7555),
    "taizhou": (28.6564, 121.4208), "jinhua": (29.0790, 119.6474), "lishui": (28.4517, 119.9229),
    "quzhou": (28.9701, 118.8593), "zhoushan": (29.9853, 122.2072),
    "zhejiang": (29.1832, 120.0934), "china": (35.8617, 104.1954),
}


# Province capitals: where a site known only by province is placed (precision 'province'), and the centre of the
# search area when looking for its real pin (mlm/locate.py).
PROVINCE_CENTROIDS = {
    "zhejiang": (29.1832, 120.0934), "jiangsu": (32.0603, 118.7969), "fujian": (26.0745, 119.2965),
    "guangdong": (23.1291, 113.2644), "shandong": (36.6512, 117.1201), "beijing": (39.9042, 116.4074),
    "tianjin": (39.3434, 117.3616), "hebei": (38.0428, 114.5149), "henan": (34.7466, 113.6254),
    "hubei": (30.5928, 114.3055), "hunan": (28.2282, 112.9388), "jiangxi": (28.6820, 115.8579),
    "anhui": (31.8206, 117.2272), "sichuan": (30.5728, 104.0668), "chongqing": (29.5630, 106.5516),
    "yunnan": (25.0389, 102.7183), "guizhou": (26.6477, 106.6302), "guangxi": (22.8170, 108.3665),
    "shanxi": (37.8706, 112.5489), "shaanxi": (34.3416, 108.9398), "gansu": (36.0611, 103.8343),
    "liaoning": (41.8057, 123.4315), "jilin": (43.8171, 125.3235), "heilongjiang": (45.8038, 126.5350),
    "inner-mongolia": (40.8426, 111.7492), "ningxia": (38.4872, 106.2309), "qinghai": (36.6171, 101.7782),
    "xinjiang": (43.8256, 87.6168), "tibet": (29.65, 91.1), "hainan": (20.0440, 110.1999),
    "hong-kong": (22.3193, 114.1694), "macau": (22.1987, 113.5439), "taiwan": (25.0330, 121.5654),
}
REGION_CENTROIDS.update({k: v for k, v in PROVINCE_CENTROIDS.items() if k not in REGION_CENTROIDS})


def _load_cities() -> dict[str, dict]:
    """Cities of the provinces added after Zhejiang (coverage/cities.yaml), by slug."""
    p = REPO_ROOT / "coverage" / "cities.yaml"
    out: dict[str, dict] = {}
    for province, cities in (yaml.safe_load(p.read_text()) if p.exists() else {}).items():
        for c in cities:
            out[c["slug"]] = {**c, "province": province}
    return out


CITIES = _load_cities()
REGION_CENTROIDS.update({slug: (c["lat"], c["lng"]) for slug, c in CITIES.items()})


def resolve_city(city: str | None, unit_id: str | None, subregions: list[str] | None, default: str) -> str:
    """Map the extractor's city (lowercase pinyin) to one of the unit's region slugs; `default` if it isn't one.
    A pinyin name shared with another province resolves to the suffixed slug (taizhou -> taizhou-js in CN-JS)."""
    subs = set(subregions or [])
    c = (city or "").strip().lower()
    if c in subs:
        return c
    alias = f"{c}-{unit_id.split('-')[-1].lower()}" if unit_id else ""
    return alias if alias in subs else default


def km_apart(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat, dlng = radians(b[0] - a[0]), radians(b[1] - a[1])
    h = sin(dlat / 2) ** 2 + cos(radians(a[0])) * cos(radians(b[0])) * sin(dlng / 2) ** 2
    return 12742 * asin(sqrt(h))


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
        centre = REGION_CENTROIDS.get(region)
        # A hit far from the city is a same-named street somewhere else (the Lanzhou problem): ignore it.
        if hit and (centre is None or region in ("china", "zhejiang") or km_apart(centre, hit[:2]) <= 150):
            return hit
    lat, lng = REGION_CENTROIDS.get(region, REGION_CENTROIDS["china"])
    return lat, lng, "province" if region in PROVINCE_CENTROIDS and region not in CITIES else "city"
