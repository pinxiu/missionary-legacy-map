"""Does OpenHistoricalMap (OHM) help this map? A report-only probe, run from GitHub Actions (the dev sandbox can't reach OHM).

It measures, on our own published sites rather than on guesses:
  tiles     do OHM's style, TileJSON and vector tiles respond, and are the tiles under our sites non-empty (a coverage proxy)?
  names     does OHM's search find our sites by name near their pins, and how far from the pin is the hit (could it fix
            city-centre pins, or confirm street ones)?
  oldnames  does OHM resolve the old romanizations our sources use (Hangchow, Foochow, Tientsin ...) to the right modern city?
  features  how many dated places of worship does OHM hold in China at all?
Nothing is written to the database. OHM data is CC0; credit "OpenHistoricalMap contributors" on any map that shows it.
"""

from __future__ import annotations

import os
import random
import time
from math import cos, log, pi, radians, tan

import httpx

from .config import USER_AGENT, notice
from .geocode import km_apart
from .locate import _same_name

NOMINATIM = "https://nominatim.openhistoricalmap.org/search"
TILEJSON = "https://vtiles.openhistoricalmap.org/maps/ohm.json"
STYLE = "https://www.openhistoricalmap.org/map-styles/main/main.json"
TILE = "https://vtiles.openhistoricalmap.org/maps/ohm/{z}/{x}/{y}.pbf"
OVERPASS = "https://overpass-api.openhistoricalmap.org/api/interpreter"

# Old romanizations in our sources, with the modern city OHM ought to resolve them to.
OLD_NAMES = [("Hangchow", 30.27, 120.16), ("Foochow", 26.07, 119.30), ("Tientsin", 39.13, 117.20), ("Paotingfu", 38.87, 115.46),
             ("Amoy", 24.48, 118.09), ("Canton", 23.13, 113.26), ("Peking", 39.90, 116.40), ("Ningpo", 29.87, 121.54),
             ("Chefoo", 37.54, 121.39), ("Nanking", 32.06, 118.80), ("Soochow", 31.30, 120.62), ("Kulangsu", 24.45, 118.07)]


def tile_xy(lat: float, lng: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    return int((lng + 180) / 360 * n), int((1 - log(tan(radians(lat)) + 1 / cos(radians(lat))) / pi) / 2 * n)


class Probe:
    def __init__(self):
        self.c = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=40, follow_redirects=True)
        self._last = 0.0
        self.seen: set[str] = set()

    def get(self, url: str, **params):
        """One request; the first of each endpoint is announced (status, size, start of the body) so a failing endpoint is
        visible in the job annotations instead of reading as 'OHM has nothing'."""
        host = url.split("/")[2]
        try:
            r = self.c.get(url, params=params)
        except httpx.HTTPError as e:
            if host not in self.seen:
                self.seen.add(host)
                notice(f"ohm-probe: {host}: {type(e).__name__}: {str(e)[:150]}", "warning")
            return None
        if host not in self.seen:
            self.seen.add(host)
            notice(f"ohm-probe: {host}: HTTP {r.status_code}, {len(r.content)} bytes: {r.text[:120]!r}")
        return r

    def search(self, q: str, box: tuple[float, float, float, float] | None = None) -> list[dict]:
        wait = 1.1 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        extra = {"viewbox": f"{box[0]},{box[1]},{box[2]},{box[3]}", "bounded": 1} if box else {}
        r = self.get(NOMINATIM, q=q, format="jsonv2", limit=5, **{"accept-language": "zh,en"}, **extra)
        try:
            return r.json() if r is not None and r.status_code == 200 else []
        except ValueError:
            return []


def run(store, sample: int = 100, seed: int = 7) -> dict:
    p = Probe()
    lines: list[str] = []
    out: dict = {}

    # --- tiles ---------------------------------------------------------------------------------------------------------
    rows = ["| Resource | Status | Bytes |", "|---|---|---|"]
    for name, url in (("Historic style", STYLE), ("TileJSON", TILEJSON)):
        r = p.get(url)
        rows.append(f"| {name} | {r.status_code if r is not None else 'unreachable'} | {len(r.content) if r is not None else 0} |")
    sites = [s for s in store.select_all("public_sites", select="id,slug,name_en,name_zh,alt_names,region,lat,lng,geo_precision")
             if s.get("lat") is not None]
    random.Random(seed).shuffle(sites)
    pick = sites[:sample]
    nonempty = 0
    for s in pick[:40]:
        x, y = tile_xy(s["lat"], s["lng"], 14)
        r = p.get(TILE.format(z=14, x=x, y=y))
        nonempty += bool(r is not None and r.status_code == 200 and len(r.content) > 0)
    out["tiles_nonempty_z14"] = f"{nonempty}/{min(40, len(pick))}"
    lines += ["### Tiles", *rows, "", f"Vector tiles under our sites at zoom 14 that hold any data: **{out['tiles_nonempty_z14']}**", ""]

    # --- names near our pins ------------------------------------------------------------------------------------------------
    hits, near, coarse_hits, coarse_total, examples = 0, 0, 0, 0, []
    for s in pick:
        coarse = s["geo_precision"] in ("city", "district", "province")
        coarse_total += coarse
        d = 0.2 if not coarse else 0.4
        box = (s["lng"] - d, s["lat"] + d, s["lng"] + d, s["lat"] - d)
        names = [n for n in [s.get("name_zh"), *(s.get("alt_names") or []), s.get("name_en")] if n]
        found = None
        for q in names[:2]:
            for h in p.search(q, box):
                nm = h.get("name") or h.get("display_name", "").split(",")[0]
                if _same_name(names, nm):
                    found = (nm, float(h["lat"]), float(h["lon"]), h.get("osm_type", "") + "/" + str(h.get("osm_id", "")))
                    break
            if found:
                break
        if found:
            hits += 1
            km = km_apart((s["lat"], s["lng"]), (found[1], found[2]))
            near += km <= 0.3
            coarse_hits += coarse
            if len(examples) < 12:
                link = f"https://www.openhistoricalmap.org/{found[3]}"
                examples.append(f"| {s['name_en']} | {s['geo_precision']} | {found[0]} | {km:.2f} | {link} |")
    out |= {"sampled": len(pick), "name_hits": hits, "within_300m": near,
            "coarse_sampled": coarse_total, "coarse_hits": coarse_hits}
    lines += ["### Search by name near our pin", "",
              f"Sampled **{len(pick)}** published sites: OHM found **{hits}** by name; **{near}** of those within 300 m of our pin; "
              f"of **{coarse_total}** city-centre pins, OHM found **{coarse_hits}**.", ""]
    if examples:
        lines += ["| Site | Our precision | OHM name | km from our pin | OHM link |", "|---|---|---|---|---|", *examples, ""]

    # --- old romanizations ---------------------------------------------------------------------------------------------------
    rows, good = ["| Old name | Top OHM result | km from the modern city |", "|---|---|---|"], 0
    for name, lat, lng in OLD_NAMES:
        res = p.search(name)
        if res:
            top = res[0]
            km = km_apart((lat, lng), (float(top["lat"]), float(top["lon"])))
            good += km <= 40
            rows.append(f"| {name} | {top.get('display_name', '')[:70]} | {km:.0f} |")
        else:
            rows.append(f"| {name} | (nothing) | |")
    out["old_names_resolved"] = f"{good}/{len(OLD_NAMES)}"
    lines += ["### Old romanizations", f"Resolved to within 40 km of the right city: **{out['old_names_resolved']}**", "", *rows, ""]

    # --- how much dated Christian-heritage data exists in China at all ---------------------------------------------------------
    q = '[out:json][timeout:120];(nwr["amenity"="place_of_worship"](18,73,54,135););out count;'
    r = p.get(OVERPASS, data=q)
    count = None
    try:
        if r is not None and r.status_code == 200:
            count = r.json()["elements"][0]["tags"]["total"]
    except (ValueError, KeyError, IndexError):
        pass
    out["china_places_of_worship"] = count
    lines += ["### Features", f"OHM places of worship in China (any date): **{count if count is not None else 'query failed'}**", ""]

    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write("## OpenHistoricalMap probe\n\n" + report + "\n")
    notice(f"ohm-probe: {out}")
    return out
