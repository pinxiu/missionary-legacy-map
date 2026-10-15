"""Find real pins for sites that only sit at a city centre (no model calls).

For each public site pinned at city/district/province precision, search free place databases for the site's own
Chinese name inside its city:
  osm       OpenStreetMap (Nominatim), bounded to a box around the city
  wikidata  Wikidata items with a coordinate (P625) whose label or alias matches
  amap      Amap (高德) POI search, only when AMAP_KEY is set; GCJ-02 results are converted to WGS-84

A hit counts only when its name matches the site's name (Traditional = Simplified) and lies within MAX_KM of the
city centre. Two sources within 300 m of each other agree. Dry run by default: the report lists every candidate so
the owner can look before `--apply`, which moves the pin to `street` precision and records a `location` claim
naming the source (rule 1: every fact cited, and rule 4: honest about location).

A match means "a place with this name exists here today". It may be a rebuilt or renamed building, so the map's
precision label stays `street` (never `exact`) and the claim's excerpt says where the pin came from.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from math import cos, pi, sin, sqrt

import httpx

from .config import USER_AGENT, notice
from .geocode import REGION_CENTROIDS, km_apart
from .verify import norm

MAX_KM = 40.0
AGREE_M = 300.0
BOX = 0.35  # degrees around the city centre for the OSM search
PUBLIC = ("verified", "approved", "conflict")
COARSE = ("city", "district", "province")


def _same_name(site_names: list[str], hit_name: str) -> bool:
    h = norm(hit_name)
    return len(h) >= 3 and any(len(n) >= 3 and (n == h or n in h or h in n) for n in map(norm, site_names) if n)


def _gcj_to_wgs(lat: float, lng: float) -> tuple[float, float]:
    """Amap/GCJ-02 → WGS-84 (the standard approximate inverse, good to a few metres)."""
    a, ee = 6378245.0, 0.00669342162296594323

    def tlat(x, y):
        r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * sqrt(abs(x))
        r += (20 * sin(6 * x * pi) + 20 * sin(2 * x * pi)) * 2 / 3
        r += (20 * sin(y * pi) + 40 * sin(y / 3 * pi)) * 2 / 3
        return r + (160 * sin(y / 12 * pi) + 320 * sin(y * pi / 30)) * 2 / 3

    def tlng(x, y):
        r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * sqrt(abs(x))
        r += (20 * sin(6 * x * pi) + 20 * sin(2 * x * pi)) * 2 / 3
        r += (20 * sin(x * pi) + 40 * sin(x / 3 * pi)) * 2 / 3
        return r + (150 * sin(x / 12 * pi) + 300 * sin(x / 30 * pi)) * 2 / 3

    dlat, dlng = tlat(lng - 105, lat - 35), tlng(lng - 105, lat - 35)
    rad = lat / 180 * pi
    magic = 1 - ee * sin(rad) ** 2
    sq = sqrt(magic)
    dlat = dlat * 180 / ((a * (1 - ee)) / (magic * sq) * pi)
    dlng = dlng * 180 / (a / sq * cos(rad) * pi)
    return lat - dlat, lng - dlng


class Finder:
    def __init__(self, client: httpx.Client | None = None, amap_key: str | None = None):
        self.c = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=25)
        self.amap_key = amap_key if amap_key is not None else os.environ.get("AMAP_KEY")
        self._last = 0.0

    def _json(self, url: str, **params) -> dict | list:
        try:
            r = self.c.get(url, params=params)
            return r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    def osm(self, name: str, centre: tuple[float, float]) -> list[dict]:
        wait = 1.1 - (time.monotonic() - self._last)  # Nominatim policy: one request a second
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        lat, lng = centre
        hits = self._json("https://nominatim.openstreetmap.org/search", q=name, format="jsonv2", limit=5,
                          viewbox=f"{lng - BOX},{lat + BOX},{lng + BOX},{lat - BOX}", bounded=1, **{"accept-language": "zh"})
        return [{"source": "osm", "name": h.get("name") or h.get("display_name", "").split(",")[0],
                 "lat": float(h["lat"]), "lng": float(h["lon"]),
                 "url": f"https://www.openstreetmap.org/{h.get('osm_type')}/{h.get('osm_id')}"}
                for h in (hits if isinstance(hits, list) else [])]

    def wikidata(self, name: str) -> list[dict]:
        found = self._json("https://www.wikidata.org/w/api.php", action="wbsearchentities", search=name, language="zh",
                           uselang="zh", format="json", limit=5).get("search", [])
        out = []
        for f in found if isinstance(found, list) else []:
            ent = self._json("https://www.wikidata.org/w/api.php", action="wbgetentities", ids=f["id"],
                             props="claims|labels", format="json").get("entities", {}).get(f["id"], {})
            for c in ent.get("claims", {}).get("P625", [])[:1]:
                v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                if v:
                    out.append({"source": "wikidata", "name": f.get("label") or name, "lat": v["latitude"],
                                "lng": v["longitude"], "url": f"https://www.wikidata.org/wiki/{f['id']}"})
        return out

    def amap(self, name: str, city_zh: str) -> list[dict]:
        if not self.amap_key:
            return []
        data = self._json("https://restapi.amap.com/v3/place/text", key=self.amap_key, keywords=name, city=city_zh,
                          citylimit="true", offset=5, extensions="base")
        out = []
        for p in (data.get("pois") or []) if isinstance(data, dict) else []:
            try:
                glng, glat = (float(x) for x in p["location"].split(","))
            except (KeyError, ValueError):
                continue
            lat, lng = _gcj_to_wgs(glat, glng)
            out.append({"source": "amap", "name": p.get("name", ""), "lat": lat, "lng": lng,
                        "url": f"https://www.amap.com/place/{p.get('id', '')}"})
        return out


def candidates(finder: Finder, site: dict, city_zh: str, centre: tuple[float, float]) -> list[dict]:
    names = [n for n in [site.get("name_zh"), *(site.get("alt_names") or [])] if n and any("一" <= ch <= "鿿" for ch in n)]
    seen, out = set(), []
    for q in names[:2]:
        for hit in [*finder.osm(q, centre), *finder.wikidata(q), *finder.amap(q, city_zh)]:
            key = (hit["source"], hit["url"])
            if key in seen or not _same_name(names, hit["name"]):
                continue
            seen.add(key)
            hit["km"] = km_apart(centre, (hit["lat"], hit["lng"]))
            if hit["km"] <= MAX_KM:
                out.append(hit)
    return out


def best(hits: list[dict]) -> tuple[dict | None, bool]:
    """The preferred hit and whether a second source agrees within AGREE_M."""
    if not hits:
        return None, False
    rank = {"osm": 0, "amap": 1, "wikidata": 2}
    pick = sorted(hits, key=lambda h: (rank[h["source"]], h["km"]))[0]
    agree = any(h["source"] != pick["source"] and km_apart((pick["lat"], pick["lng"]), (h["lat"], h["lng"])) * 1000 <= AGREE_M
                for h in hits)
    return pick, agree


def run(store, apply: bool = False, finder: Finder | None = None, limit: int | None = None) -> dict:
    finder = finder or Finder()
    regions = {r["slug"]: r for r in store.select("regions", select="slug,name_zh")}
    sites = [s for s in store.select_all("sites", select="id,slug,region,name_en,name_zh,alt_names,geo_precision,status,unit")
             if s["status"] in PUBLIC and s["geo_precision"] in COARSE and s.get("name_zh")]
    if limit:
        sites = sites[:limit]
    stats = {"checked": 0, "found": 0, "agreed": 0, "applied": 0, "none": 0}
    lines = ["| Site | City | Source | Matched name | km from centre | Agreed | Link |", "|---|---|---|---|---|---|---|"]
    for s in sites:
        stats["checked"] += 1
        centre = REGION_CENTROIDS.get(s["region"])
        if not centre:
            stats["none"] += 1
            continue
        hits = candidates(finder, s, (regions.get(s["region"]) or {}).get("name_zh") or "", centre)
        pick, agree = best(hits)
        if not pick:
            stats["none"] += 1
            continue
        stats["found"] += 1
        stats["agreed"] += agree
        lines.append(f"| {s['name_en']} {s['name_zh']} | {s['region']} | {pick['source']} | {pick['name']} | {pick['km']:.1f} | "
                     f"{'yes' if agree else 'no'} | {pick['url']} |")
        if apply:
            src = store.upsert("sources", [{"url": pick["url"], "publisher": pick["source"]}], "url")[0]
            store.update("sites", {"id": s["id"]}, {"location": f"SRID=4326;POINT({pick['lng']} {pick['lat']})",
                                                   "geo_precision": "street"})
            store.insert("claims", [{
                "subject_kind": "site", "subject_id": s["id"], "field": "location",
                "value": {"lat": pick["lat"], "lng": pick["lng"], "source": pick["source"], "agreed": agree},
                "source_id": src["id"], "excerpt": f"{pick['source']}: {pick['name']}"[:300], "confidence": 0.8 if agree else 0.6,
                "status": "verified", "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(), "extracted_by": "geocoder"}])
            stats["applied"] += 1
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"### Locate ({'applied' if apply else 'dry run'}): {stats}\n\n{report}\n")
    notice(f"locate: {stats}")
    return stats
