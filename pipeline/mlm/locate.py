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
import re
import time
from math import cos, pi, sin, sqrt
from pathlib import Path

import httpx

from .config import USER_AGENT, notice
from .geocode import PROVINCE_CENTROIDS, REGION_CENTROIDS, km_apart
from .verify import norm

MAX_KM = 40.0          # a hit must lie this close to the site's city centre...
MAX_KM_PROVINCE = 400.0  # ...or this close to the province capital when only the province is known
AGREE_M = 300.0
BOX = 0.35  # degrees around the city centre for the OSM search (x10 for a province)


def search_radius(region: str) -> tuple[float, float]:
    """(max km from the centre, OSM box in degrees) for a site known to city or only to province level."""
    from .geocode import CITIES
    province_level = region in PROVINCE_CENTROIDS and region not in CITIES
    return (MAX_KM_PROVINCE, BOX * 10) if province_level else (MAX_KM, BOX)
PUBLIC = ("verified", "approved", "conflict")
COARSE = ("city", "district", "province")


_PAREN = re.compile(r"[（(][^）)]*[）)]")
_CITY_WORDS = [
               "上海", "北京", "天津", "重庆", "南京", "杭州", "宁波", "温州", "苏州", "福州", "厦门", "广州", "济南", "青岛", "烟台",
               "汉口", "武汉", "成都", "保定", "石家庄", "开封", "郑州", "长沙", "南昌", "合肥", "昆明", "贵阳", "桂林", "太原", "西安",
               "香港", "澳门"]


def name_variants(site: dict) -> list[str]:
    """Chinese names a map might use for the site: as written, without parentheticals, without a leading city word, and the
    first of several joined by 及/和/与 (e.g. '教堂及小学'). Longest and most specific first."""
    out: list[str] = []
    for n in [site.get("name_zh"), *(site.get("alt_names") or [])]:
        if not n or not any("一" <= ch <= "鿿" for ch in n):
            continue
        base = _PAREN.sub("", n).strip()
        forms = [n, base]
        for c in _CITY_WORDS:
            if base.startswith(c) and len(base) - len(c) >= 3:
                forms.append(base[len(c):])
        for sep in ("及", "和", "与"):
            if sep in base:
                forms.append(base.split(sep)[0])
        for f in forms:
            f = f.strip(" ·、,，")
            if len(f) >= 3 and f not in out:
                out.append(f)
    return out


def _fuzzy(a: str, b: str) -> int:
    from rapidfuzz import fuzz
    return int(fuzz.ratio(norm(a), norm(b)))


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

    def ohm(self, name: str, centre: tuple[float, float], box: float = BOX) -> list[dict]:
        """OpenHistoricalMap (CC0) from the committed planet extract `data/ohm/china.geojsonl` (OHM's web services
        refuse automated clients). Nothing when the extract isn't there. Credit: OpenHistoricalMap contributors."""
        if not hasattr(self, "_ohm"):
            path = os.environ.get("MLM_OHM_INDEX") or str(Path(__file__).resolve().parents[2] / "data" / "ohm" / "china.geojsonl")
            try:
                from .ohm_planet import OhmIndex
                self._ohm = OhmIndex(path) if Path(path).exists() else None
            except (OSError, ValueError):
                self._ohm = None
        return self._ohm.osm(name, centre, box) if self._ohm else []

    def osm(self, name: str, centre: tuple[float, float], box: float = BOX) -> list[dict]:
        wait = 1.1 - (time.monotonic() - self._last)  # Nominatim policy: one request a second
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        lat, lng = centre
        hits = self._json("https://nominatim.openstreetmap.org/search", q=name, format="jsonv2", limit=5,
                          viewbox=f"{lng - box},{lat + box},{lng + box},{lat - box}", bounded=1, **{"accept-language": "zh"})
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

    def amap_geocode(self, address: str, city_zh: str) -> list[dict]:
        if not self.amap_key:
            return []
        data = self._json("https://restapi.amap.com/v3/geocode/geo", key=self.amap_key, address=address, city=city_zh)
        out = []
        for g in (data.get("geocodes") or []) if isinstance(data, dict) else []:
            try:
                glng, glat = (float(x) for x in g["location"].split(","))
            except (KeyError, ValueError):
                continue
            if g.get("level") in ("国家", "省", "市", "区县", "开发区", "未知"):  # a city or district centre is no better than what we have
                continue
            lat, lng = _gcj_to_wgs(glat, glng)
            out.append({"source": "geocode", "level": g.get("level", ""), "name": g.get("formatted_address", ""), "lat": lat, "lng": lng,
                        "url": f"https://www.amap.com/search?query={address}"})
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


def candidates(finder: Finder, site: dict, city_zh: str, centre: tuple[float, float], region: str = "",
               china: bool = True) -> list[dict]:
    names = name_variants(site)
    max_km, box = search_radius(region)
    seen, out = set(), []

    def add(hit: dict) -> None:
        key = (hit["source"], hit["url"])
        if key in seen:
            return
        if hit["source"] != "geocode" and not _same_name(names, hit["name"]) and not any(_fuzzy(n, hit["name"]) >= 85 for n in names):
            return
        seen.add(key)
        hit["km"] = km_apart(centre, (hit["lat"], hit["lng"]))
        if hit["km"] <= max_km:
            out.append(hit)

    for i, q in enumerate(names[:3]):
        ohm = finder.ohm(q, centre, box) if hasattr(finder, "ohm") else []
        wiki = finder.wikidata(q) if i == 0 else []
        amap = finder.amap(q, city_zh) if china else []
        for hit in [*finder.osm(q, centre, box), *ohm, *wiki, *amap]:
            add(hit)
    if china and site.get("modern_address"):  # a street address is the most exact thing a source can give us
        for hit in finder.amap_geocode(site["modern_address"], city_zh):
            hit["name"] = names[0] if names else site.get("name_en", "")
            add(hit)
    if not out and site.get("name_en"):  # last resort: the English name, which OSM often carries
        for hit in finder.osm(site["name_en"], centre, box):
            if _fuzzy(site["name_en"], hit["name"]) >= 85:
                hit["km"] = km_apart(centre, (hit["lat"], hit["lng"]))
                if hit["km"] <= max_km and (hit["source"], hit["url"]) not in seen:
                    seen.add((hit["source"], hit["url"]))
                    out.append(hit)
    return out


def best(hits: list[dict]) -> tuple[dict | None, bool]:
    """The preferred hit and whether a second source agrees within AGREE_M."""
    if not hits:
        return None, False
    rank = {"geocode": 0, "osm": 1, "amap": 2, "ohm": 1, "wikidata": 3}
    pick = sorted(hits, key=lambda h: (rank[h["source"]], h["km"]))[0]
    agree = any(h["source"] != pick["source"] and km_apart((pick["lat"], pick["lng"]), (h["lat"], h["lng"])) * 1000 <= AGREE_M
                for h in hits)
    return pick, agree


GENERIC = {"天主堂", "教堂", "福音堂", "礼拜堂", "基督教堂", "医院", "学校", "小学", "中学", "大学", "墓地", "公墓", "神学院", "修道院"}


def confident(site: dict, pick: dict, agree: bool) -> bool:
    """Safe to move a pin automatically: two sources agree, or the hit's name equals one of the site's own specific names
    (a generic name such as 天主堂 matches every church in the city, so it never counts)."""
    if agree:
        return True
    if pick.get("source") == "geocode":
        return True  # a street address resolved to a street-level point
    h = norm(pick["name"])
    generic = {norm(g) for g in GENERIC}
    names = [norm(n) for n in name_variants(site)]
    return any(n not in generic and len(n) >= 4 and (n == h or _fuzzy(n, h) >= 92) for n in names)


def unambiguous(hits: list[dict], pick: dict) -> bool:
    """Every candidate lies within 500 m of the pick: one place, not several same-named ones in the city."""
    return all(km_apart((pick["lat"], pick["lng"]), (h["lat"], h["lng"])) <= 0.5 for h in hits)


def apply_pick(store, site_id: str, pick: dict, agree: bool, precision: str = "street", note: str = "") -> None:
    """Move a site's pin to a found place and record a `location` claim naming where the pin came from."""
    src = store.upsert("sources", [{"url": pick["url"], "publisher": pick["source"]}], "url")[0]
    store.update("sites", {"id": site_id}, {"location": f"SRID=4326;POINT({pick['lng']} {pick['lat']})",
                                           "geo_precision": precision})
    store.insert("claims", [{
        "subject_kind": "site", "subject_id": site_id, "field": "location",
        "value": {"lat": pick["lat"], "lng": pick["lng"], "source": pick["source"], "agreed": agree, "precision": precision},
        "source_id": src["id"], "excerpt": f"{pick['source']}: {pick['name']}{note}"[:300],
        "confidence": 0.8 if agree else (0.6 if precision == "street" else 0.4),
        "status": "verified", "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(), "extracted_by": "geocoder"}])


def refine(store, site: dict, city_zh: str, finder: Finder, china: bool = True) -> dict | None:
    """Look a freshly loaded, city-pinned site up by its Chinese name; moves the pin when a match is found.
    Returns the pick, or None. Used by the load stage so new sites are pinned as precisely as the free data allows."""
    centre = REGION_CENTROIDS.get(site["region"])
    if not centre:
        return None
    pick, agree = best(candidates(finder, site, city_zh, centre, site["region"], china))
    if pick:
        apply_pick(store, site["id"], pick, agree)
    return pick


def run(store, apply: bool = False, finder: Finder | None = None, limit: int | None = None, strict: bool = True,
        approx: bool = False) -> dict:
    finder = finder or Finder()
    from .places import load_known
    load_known(store)
    regions = {r["slug"]: r for r in store.select("regions", select="slug,name_zh")}
    cols = "id,slug,region,name_en,name_zh,alt_names,modern_address,geo_precision,status,unit"
    sites = [s for s in store.select_all("sites", select=cols)
             if s["status"] in PUBLIC and s["geo_precision"] in COARSE]
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
        hits = candidates(finder, s, (regions.get(s["region"]) or {}).get("name_zh") or "", centre, s["region"])
        pick, agree = best(hits)
        if not pick:
            stats["none"] += 1
            continue
        stats["found"] += 1
        stats["agreed"] += agree
        lines.append(f"| {s['name_en']} {s['name_zh']} | {s['region']} | {pick['source']} | {pick['name']} | {pick['km']:.1f} | "
                     f"{'yes' if agree else 'no'} | {pick['url']} |")
        sure = confident(s, pick, agree)
        stats["confident"] = stats.get("confident", 0) + sure
        if apply and (sure or not strict):
            apply_pick(store, s["id"], pick, agree)
            stats["applied"] += 1
        elif apply and approx and unambiguous(hits, pick):
            # a partial name match inside the right city: shown as "approximate (district)", never as a street-level pin
            apply_pick(store, s["id"], pick, agree, precision="district", note=" (approximate: partial name match)")
            stats["approx"] = stats.get("approx", 0) + 1
    stats["amap_hits"] = sum(1 for ln in lines if "| amap |" in ln)
    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"### Locate ({'applied' if apply else 'dry run'}): {stats}\n\n{report}\n")
    notice(f"locate: {stats}")
    return stats
