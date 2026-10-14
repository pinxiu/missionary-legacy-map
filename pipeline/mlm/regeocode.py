"""Re-place a unit's sites that were pinned only to a region centre, using the city the extractor recorded (no model).

Early Jiangsu loads had no city list, so every site fell back to the centre of China (near Lanzhou). Each site's claim
still holds the extractor's `city`, `place_text` and `modern_address`: the site is moved to its city's region and
pinned there (at 'city' precision unless a nearby address match is found). Pins made any other way are left alone.
"""

from __future__ import annotations

from .config import notice
from .geocode import geocode, resolve_city


def run(store, uid: str, use_network: bool = True) -> dict:
    from .coverage import unit
    from .load import ensure_region

    u = unit(uid)
    ensure_region(store, u["region"], uid)
    subs = u.get("subregions", [])
    stats = {"checked": 0, "moved": 0, "no_city": 0}
    for s in store.select_all("sites", select="id,region,geo_precision", unit=f"eq.{uid}"):
        if s["geo_precision"] not in ("city", "province"):
            continue
        stats["checked"] += 1
        claim = (store.select("claims", select="value", subject_id=f"eq.{s['id']}", subject_kind="eq.site", limit="1") or [{}])[0]
        v = claim.get("value") or {}
        city = resolve_city(v.get("city"), uid, subs, default=u["region"])
        if city == u["region"] and not subs:
            stats["no_city"] += 1
        lat, lng, precision = geocode(city, v.get("name_en") or "", v.get("place_text") or "", v.get("modern_address"), use_network)
        store.update("sites", {"id": s["id"]}, {"region": city, "location": f"SRID=4326;POINT({lng} {lat})", "geo_precision": precision})
        stats["moved"] += 1
    notice(f"regeocode[{uid}]: {stats}")
    return stats
