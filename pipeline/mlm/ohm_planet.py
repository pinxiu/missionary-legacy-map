"""Pins from the OpenHistoricalMap planet file (CC0). The OHM web services refuse automated clients, but its planet dumps
are public, so the runner downloads one, filters it with osmium to places that could be ours, and this module matches our
city-pinned sites against the named features (name, name:zh, old_name, alt_name...) with the same confidence gate as
`locate`. Source label `ohm`; credit "OpenHistoricalMap contributors" wherever a pin from it is shown.

Input: a GeoJSON-sequence file from `osmium export` (one feature per line)."""

from __future__ import annotations

import json
from collections import defaultdict

from .locate import run
from .verify import norm

NAME_KEYS = ("name", "name:zh", "name:zh-Hans", "name:zh-Hant", "name:en", "old_name", "old_name:zh", "alt_name", "alt_name:zh",
             "official_name", "short_name")


def _center(geom: dict) -> tuple[float, float] | None:
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Point":
        return c[1], c[0]
    ring = c[0] if t == "Polygon" else (c[0][0] if t == "MultiPolygon" else None)
    if not ring:
        return None
    return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)


class OhmIndex:
    """Quacks like locate.Finder: `osm()` answers from the planet file; the other sources return nothing."""

    def __init__(self, path: str):
        self.cells: dict[tuple[int, int], list[dict]] = defaultdict(list)
        self.count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    ft = json.loads(line)
                except ValueError:
                    continue
                props, ctr = ft.get("properties") or {}, _center(ft.get("geometry") or {})
                names = [str(props[k]) for k in NAME_KEYS if props.get(k)]
                if not names or not ctr:
                    continue
                fid = str(ft.get("id") or props.get("@id") or "")
                kind = {"n": "node", "w": "way", "r": "relation"}.get(fid[:1], "way")
                rec = {"names": names, "lat": ctr[0], "lng": ctr[1], "url": f"https://www.openhistoricalmap.org/{kind}/{fid[1:] or fid}",
                       "start": props.get("start_date", "")}
                self.cells[(int(ctr[0] * 10), int(ctr[1] * 10))].append(rec)
                self.count += 1

    def osm(self, name: str, centre: tuple[float, float], box: float = 0.35) -> list[dict]:
        q = norm(name)
        out = []
        for cx in range(int((centre[0] - box) * 10) - 1, int((centre[0] + box) * 10) + 2):
            for cy in range(int((centre[1] - box) * 10) - 1, int((centre[1] + box) * 10) + 2):
                for r in self.cells.get((cx, cy), ()):
                    if abs(r["lat"] - centre[0]) > box or abs(r["lng"] - centre[1]) > box:
                        continue
                    for n in r["names"]:
                        nn = norm(n)
                        if len(nn) >= 3 and (q == nn or q in nn or nn in q):
                            out.append({"source": "ohm", "name": n, "lat": r["lat"], "lng": r["lng"], "url": r["url"]})
                            break
        return out

    def wikidata(self, name: str) -> list[dict]:
        return []

    def amap(self, name: str, city_zh: str) -> list[dict]:
        return []

    def amap_geocode(self, address: str, city_zh: str) -> list[dict]:
        return []


def match(store, path: str, apply: bool = False, limit: int | None = None) -> dict:
    idx = OhmIndex(path)
    from .config import notice
    notice(f"ohm-planet: {idx.count} named features indexed")
    stats = run(store, apply=apply, finder=idx, limit=limit)
    stats["features"] = idx.count
    return stats
