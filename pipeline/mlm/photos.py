"""Photo backfill: historical and present-day photos of sites, from openly licensed Wikimedia material only.

Correctness over quantity. No model is involved; the matching is deterministic and strict:
  * HIGH confidence (added, status approved): the image comes from a Wikipedia article or Wikidata item that IS the site
    (name equal to one of ours) and sits where our pin is (or in the site's city, when our pin is only the city centre),
    its licence is open (public domain, CC0, CC BY, CC BY-SA), and nothing in its title or description hints at people
    at worship. A photo's date decides its kind: taken up to 1952 = historical, 2005 or later = current ("what it looks
    like now"). Undated or 1953-2004 photos are only options.
  * Anything else that looks plausible (partial name match, farther away, undated, possible worship scene) is NOT added.
    It is written to a "Photo options" issue per coverage unit for the owner to look at; `mlm photos --accept` adds one.
Every photo keeps its credit, licence and the Commons file page it came from (shown on the map)."""

from __future__ import annotations

import html
import re
import time
from urllib.parse import quote, unquote, urlparse

import httpx

from .config import USER_AGENT, notice
from .geocode import REGION_CENTROIDS, km_apart
from .locate import _fuzzy, _same_name, name_variants
from .verify import norm

HISTORICAL_UNTIL = 1952
CURRENT_FROM = 2005
MAX_PER_KIND = 2
PRECISE = ("exact", "street", "district")
WORSHIP = re.compile(
    r"service|worship|congregation|baptis|\bmass\b|sunday|prayer|choir|preach|wedding|funeral|"
    r"礼拜|弥撒|聚会|洗礼|信徒|婚礼|葬礼", re.I)
SKIP_FILES = re.compile(
    r"icon|flag|logo|symbol|map|locator|plan|floor|diagram|layout|poster|brochure|screenshot|scan|sketch"
    r"|平面|地图|地圖|示意|规划|規劃|commons-|wiki|\.svg$|\.gif$|\.ogg|\.webm|\.pdf|\.djvu",
    re.I,
)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|tiff?)$", re.I)


def license_ok(short: str | None) -> bool:
    s = (short or "").lower().strip()
    if not s or re.search(r"(^|[\s-])(nc|nd)([\s-]|$)|non-?free|fair use|copyrighted|all rights", s):
        return False
    return s.startswith(("cc by", "cc0", "public domain", "pd")) or "public domain" in s


def year_of(text: str | None) -> int | None:
    years = [int(y) for y in re.findall(r"(?<!\d)(1[89]\d\d|20[0-2]\d)(?!\d)", text or "")]
    return min(years) if years else None


def kind_of(year: int | None) -> str | None:
    if year is None:
        return None
    if year <= HISTORICAL_UNTIL:
        return "historical"
    return "current" if year >= CURRENT_FROM else None


def clean(text: str | None, limit: int = 250) -> str:
    t = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def caption_for(title: str, description: str) -> tuple[str | None, str | None]:
    """The file's own description, never our wording. Chinese goes to the Chinese caption."""
    stem = re.sub(r"\.[A-Za-z]+$", "", title.split(":", 1)[-1]).replace("_", " ").strip()
    text = description if description and not description.rstrip().endswith(("...", "…")) else stem
    if any("一" <= ch <= "鿿" for ch in text):
        return (stem if not any("一" <= ch <= "鿿" for ch in stem) else None), text
    return text, None


class Commons:
    """Wikidata / Wikipedia / Commons reads, politely (one request at a time, with a pause)."""

    def __init__(self, client: httpx.Client | None = None, pause: float = 0.4):
        self.c = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30)
        self.pause = pause

    def _json(self, url: str, **params) -> dict:
        time.sleep(self.pause)
        try:
            r = self.c.get(url, params={"format": "json", **params})
            return r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    def wikidata_search(self, name: str, lang: str) -> list[str]:
        d = self._json("https://www.wikidata.org/w/api.php", action="wbsearchentities", search=name, language=lang, uselang=lang, limit=5)
        return [x["id"] for x in d.get("search", [])]

    def wikidata_items(self, ids: list[str]) -> dict:
        if not ids:
            return {}
        return self._json("https://www.wikidata.org/w/api.php", action="wbgetentities", ids="|".join(ids),
                          props="labels|aliases|claims", languages="zh|zh-hans|zh-cn|en").get("entities", {})

    def page_images(self, lang: str, title: str) -> list[str]:
        """File titles used by a Wikipedia article: its lead image first, then the others."""
        d = self._json(f"https://{lang}.wikipedia.org/w/api.php", action="query", titles=title, prop="pageimages|images",
                       piprop="name", imlimit=60, redirects=1)
        out: list[str] = []
        for p in (d.get("query", {}).get("pages") or {}).values():
            if p.get("pageimage"):
                out.append("File:" + p["pageimage"].replace("_", " "))
            out += [i["title"] for i in p.get("images", [])]
        return list(dict.fromkeys(out))

    def geosearch(self, lat: float, lng: float, radius: int = 250) -> list[str]:
        d = self._json("https://commons.wikimedia.org/w/api.php", action="query", list="geosearch", gscoord=f"{lat}|{lng}",
                       gsradius=radius, gsnamespace=6, gslimit=30)
        return [x["title"] for x in d.get("query", {}).get("geosearch", [])]

    def file_info(self, titles: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(titles), 20):
            d = self._json("https://commons.wikimedia.org/w/api.php", action="query", titles="|".join(titles[i:i + 20]), prop="imageinfo",
                           iiprop="url|extmetadata", iiurlwidth=1024,
                           iiextmetadatafilter="LicenseShortName|Artist|Credit|DateTimeOriginal|DateTime|ImageDescription|NonFree|Restrictions")
            for p in (d.get("query", {}).get("pages") or {}).values():
                ii = (p.get("imageinfo") or [None])[0]
                if not ii:
                    continue
                m = {k: v.get("value") for k, v in (ii.get("extmetadata") or {}).items()}
                out[p["title"]] = {
                    "title": p["title"], "page": ii.get("descriptionurl"), "thumb": ii.get("thumburl") or ii.get("url"),
                    "license": clean(m.get("LicenseShortName"), 100), "nonfree": str(m.get("NonFree", "")).lower() in ("1", "true", "yes"),
                    "restricted": bool(m.get("Restrictions")), "artist": clean(m.get("Artist") or m.get("Credit"), 200),
                    "date": clean(m.get("DateTimeOriginal") or m.get("DateTime"), 80), "description": clean(m.get("ImageDescription")),
                }
        return out


def wiki_title(url: str) -> tuple[str, str] | None:
    u = urlparse(url)
    m = re.match(r"^(\w+)\.wikipedia\.org$", u.netloc)
    if m and u.path.startswith("/wiki/"):
        return m.group(1), unquote(u.path[6:]).replace("_", " ")
    return None


def _entity_names(ent: dict) -> list[str]:
    names = [v["value"] for v in (ent.get("labels") or {}).values()]
    for al in (ent.get("aliases") or {}).values():
        names += [a["value"] for a in al]
    return names


def _coord(ent: dict) -> tuple[float, float] | None:
    for c in (ent.get("claims") or {}).get("P625", []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if v:
            return v["latitude"], v["longitude"]
    return None


def _exact(names: list[str], other: list[str]) -> bool:
    a = {norm(n) for n in names if len(norm(n)) >= 3}
    return any(norm(o) in a for o in other)


def where_ok(site: dict, spot: tuple[float, float] | None) -> bool:
    """Is a candidate at the site? Precise pin: within 1.5 km. City-level pin: within 40 km of the city centre."""
    if not spot:
        return False
    if site["geo_precision"] in PRECISE:
        return km_apart((site["lat"], site["lng"]), spot) <= 1.5
    centre = REGION_CENTROIDS.get(site["region"])
    return bool(centre) and km_apart(centre, spot) <= 40


def candidates(api: Commons, site: dict, article_titles: list[tuple[str, str]]) -> list[dict]:
    """[{file, strength: 'high'|'option', why}] for one site."""
    zh = name_variants(site)
    names = [*zh, site.get("name_en") or "", *(site.get("alt_names") or [])]
    names = [n for n in names if n]
    out: dict[str, dict] = {}

    def add(f: str, strength: str, why: str) -> None:
        if SKIP_FILES.search(f) or not IMAGE_EXT.search(f):
            return
        if f not in out or (strength == "high" and out[f]["strength"] != "high"):
            out[f] = {"file": f, "strength": strength, "why": why}

    # 1. Wikipedia articles we already cite for this site, when the article is about the site itself.
    for lang, title in article_titles:
        if _exact(names, [title]) or any(_fuzzy(n, title) >= 90 for n in names):
            for i, f in enumerate(api.page_images(lang, title)):
                if i == 0:
                    add(f, "high", f"lead image of the cited article {lang}.wikipedia.org/wiki/{title}")
                elif _same_name(names, f.split(":", 1)[-1]):
                    add(f, "high", f"image named for the site in the cited article {title}")

    # 2. A Wikidata item with the same name, at the site (or in its city).
    ids: list[str] = []
    for n in zh[:2]:
        ids += api.wikidata_search(n, "zh")
    if site.get("name_en"):
        ids += api.wikidata_search(site["name_en"], "en")
    ents = api.wikidata_items(list(dict.fromkeys(ids))[:20])
    for qid, ent in ents.items():
        enames = _entity_names(ent)
        if not _same_name(names, " ".join(enames[:1])) and not _exact(names, enames):
            continue
        located = where_ok(site, _coord(ent))
        exact = _exact(names, enames)
        for c in (ent.get("claims") or {}).get("P18", []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if v:
                add("File:" + v, "high" if (exact and located) else "option",
                    f"Wikidata {qid}: " + ("same name and place" if exact and located else
                                          "similar name" if not exact else "name matches but the place could not be confirmed"))

    # 3. Commons files geotagged within 250 m of a precise pin whose title names the site.
    if site["geo_precision"] in PRECISE:
        for f in api.geosearch(site["lat"], site["lng"]):
            stem = f.split(":", 1)[-1]
            if any(len(n) >= 4 and norm(n) in norm(stem) for n in names):
                add(f, "high", "geotagged within 250 m and named for the site")
            elif any(_fuzzy(n, stem) >= 70 for n in names):
                add(f, "option", "geotagged within 250 m, title only partly matches")
    return list(out.values())


def judge(cand: dict, info: dict | None) -> dict | None:
    """Final call on one candidate file: {'add': row fields} or {'option': reason} or None (drop)."""
    if not info or info["nonfree"] or not license_ok(info["license"]):
        return None
    year = year_of(info["date"])
    kind = kind_of(year)
    text = f"{info['title']} {info['description']}"
    row = {"kind": kind, "year_taken": year, "image_url": info["thumb"], "source_url": info["page"], "license": info["license"][:100],
           "credit": (info["artist"] or "Wikimedia Commons contributor")[:200]}
    row["caption"], row["caption_zh"] = caption_for(info["title"], info["description"])
    row = {**row, "caption": (row["caption"] or "")[:500] or None, "caption_zh": (row["caption_zh"] or "")[:500] or None}
    if cand["strength"] != "high":
        return {"option": cand["why"], "row": row}
    if WORSHIP.search(text):
        return {"option": "may show people at worship (not added automatically)", "row": row}
    if kind is None:
        return {"option": "undated, or taken between 1953 and 2004: neither historical nor current", "row": row}
    if info["restricted"]:
        return {"option": "carries a usage restriction on Commons", "row": row}
    return {"add": row}


def cited_articles(store, site_id: str) -> list[tuple[str, str]]:
    out = []
    for c in store.select("claims", select="source_id", subject_id=f"eq.{site_id}", field="eq.site"):
        if not c.get("source_id"):
            continue
        for s in store.select("sources", select="url", id=f"eq.{c['source_id']}"):
            t = wiki_title(s["url"])
            if t and t not in out:
                out.append(t)
    return out


def run(store, apply: bool = False, limit: int | None = None, api: Commons | None = None, unit: str | None = None) -> dict:
    api = api or Commons()
    pts = {p["id"]: p for p in store.select_all("public_sites", select="id,lat,lng")}
    sites = [s for s in store.select_all("sites", select="id,slug,name_en,name_zh,alt_names,region,unit,geo_precision,status,site_type")
             if s["status"] in ("approved", "conflict") and s["id"] in pts and (not unit or s.get("unit") == unit)]
    have: dict[str, list[dict]] = {}
    for p in store.select_all("photos", select="site_id,kind,source_url,status"):
        have.setdefault(p["site_id"], []).append(p)
    # sites with precise pins first: their matches are the most reliable
    sites.sort(key=lambda s: (s["geo_precision"] not in PRECISE, s["slug"]))
    if limit:
        sites = sites[:limit]
    stats = {"sites": len(sites), "added": 0, "historical": 0, "current": 0, "options": 0, "sites_with_photos": 0}
    options: dict[str, list[dict]] = {}
    for s in sites:
        s = {**s, "lat": pts[s["id"]]["lat"], "lng": pts[s["id"]]["lng"]}
        mine = have.get(s["id"], [])
        known = {p["source_url"] for p in mine if p.get("source_url")}
        counts = {k: sum(1 for p in mine if p["kind"] == k) for k in ("historical", "current")}
        try:
            cands = candidates(api, s, cited_articles(store, s["id"]))
            infos = api.file_info([c["file"] for c in cands]) if cands else {}
        except Exception as e:  # noqa: BLE001 - one site's lookup must not stop the backfill
            notice(f"photos: {s['slug']}: {type(e).__name__}", "warning")
            continue
        added_here = 0
        for c in cands:
            info = infos.get(c["file"]) or infos.get(c["file"].replace("_", " "))
            if info and info["page"] in known:
                continue
            verdict = judge(c, info)
            if not verdict:
                continue
            if "option" in verdict:
                options.setdefault(s.get("unit") or "other", []).append({"site": s, "info": info, "why": verdict["option"]})
                stats["options"] += 1
                continue
            row = verdict["add"]
            if counts[row["kind"]] >= MAX_PER_KIND:
                continue
            counts[row["kind"]] += 1
            known.add(info["page"])
            added_here += 1
            stats["added"] += 1
            stats[row["kind"]] += 1
            if apply:
                store.insert("photos", [{**row, "site_id": s["id"], "status": "approved",
                                         "moderation_note": f"Backfilled from Wikimedia: {c['why']}"[:500]}])
        stats["sites_with_photos"] += bool(added_here)
    if apply:
        write_issues(options)
    notice(f"photos: {stats}")
    return stats


def write_issues(options: dict[str, list[dict]]) -> None:
    """One issue per coverage unit listing the photos that looked plausible but were not added, and why."""
    from .coverage import unit as unit_cfg
    from .decisions import gh

    gh("label", "create", "photo-options", "--color", "5319e7", "--description", "Photos found but not added: need a human look",
       "--force", check=False)
    for uid, rows in options.items():
        try:
            u = unit_cfg(uid)
            title = f"Photo options: {u['en']} {u['zh']} ({uid})"
        except SystemExit:
            title = f"Photo options: {uid}"
        lines = [
            "Openly licensed Wikimedia photos that might show these sites but were **not added**, because the match was not certain "
            "enough (partial name, place unconfirmed, undated, or possibly people at worship). Look at each; to add one, run the "
            "**Photos** workflow with `accept` = `site-slug | File:Name.jpg`. Updated automatically.", "",
            "| Site | Photo | Licence | Date | Why it is only an option |", "|---|---|---|---|---|"]
        seen = set()
        for r in rows:
            i = r["info"]
            if not i or (r["site"]["slug"], i["page"]) in seen:
                continue
            seen.add((r["site"]["slug"], i["page"]))
            name = f"{r['site']['name_en']} {r['site'].get('name_zh') or ''}".strip()
            lines.append(f"| `{r['site']['slug']}` {name} | [![]({quote(i['thumb'], safe=':/%?=&')})]({i['page']}) {i['title'][5:60]} "
                         f"| {i['license']} | {i['date'] or '—'} | {r['why']} |")
        body = "\n".join(lines)[:60000]
        found = gh("issue", "list", "--label", "photo-options", "--search", f'"{title}" in:title', "--state", "all",
                   "--json", "number", "--jq", ".[0].number", check=False)
        if found:
            gh("issue", "edit", found, "--body", body, check=False)
        else:
            gh("issue", "create", "--title", title, "--label", "photo-options", "--body", body, check=False)


def accept(store, spec: str, api: Commons | None = None) -> bool:
    """Owner-approved: add `File:Name.jpg` to a site (`slug | File:Name.jpg`). The licence must still be open."""
    api = api or Commons()
    slug, _, f = [x.strip() for x in spec.partition("|")]
    site = (store.select("sites", select="id,slug", slug=f"eq.{slug}") or [None])[0]
    info = api.file_info([f]).get(f) if f else None
    if not site or not info or info["nonfree"] or not license_ok(info["license"]):
        notice(f"photos accept: {spec!r}: unknown site, file or licence", "warning")
        return False
    year = year_of(info["date"])
    cap, cap_zh = caption_for(info["title"], info["description"])
    store.insert("photos", [{
        "site_id": site["id"], "kind": kind_of(year) or ("historical" if (year or 9999) <= HISTORICAL_UNTIL else "current"),
        "year_taken": year, "image_url": info["thumb"], "source_url": info["page"], "license": info["license"][:100],
        "credit": (info["artist"] or "Wikimedia Commons contributor")[:200], "caption": cap, "caption_zh": cap_zh,
        "status": "approved", "moderation_note": "Accepted by the owner from the Photo options issue"}])
    return True
