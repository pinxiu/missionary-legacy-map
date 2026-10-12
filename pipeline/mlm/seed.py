"""Load editorial seed files (data/seed/*.json) as approved, cited, bilingual records.

`mlm seed --sql` prints idempotent SQL for every seed file (or `--region <file>` for one).
Sources and persons are shared across files, so a site in one file may cite a source defined in another.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR

SEED_DIR = Path(DATA_DIR) / "seed"


def _q(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "array[" + ",".join(_q(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
    return "'" + str(v).replace("'", "''") + "'"


def load_seed(region: str) -> dict:
    return json.loads((SEED_DIR / f"{region}.json").read_text())


def load_all(only: str | None = None) -> dict:
    files = [SEED_DIR / f"{only}.json"] if only else sorted(SEED_DIR.glob("*.json"))
    all_sources, all_persons, sites = {}, {}, []
    for f in sorted(SEED_DIR.glob("*.json")):  # sources/persons always come from every file
        d = json.loads(f.read_text())
        for s in d["sources"]:
            all_sources[s["key"]] = s
        for p in d["persons"]:
            all_persons[p["slug"]] = p
    for f in files:
        d = json.loads(f.read_text())
        for s in d["sites"]:
            sites.append({**s, "region": s.get("region", d["region"])})
    return {"sources": all_sources, "persons": all_persons, "sites": sites}


def site_status(site: dict) -> str:
    return "conflict" if site.get("disputed") or any(e.get("conflict_with") for e in site["events"]) else "approved"


def _claim(kind: str, subject_sql: str, value: dict, url: str, status: str) -> str:
    return (
        "insert into claims (subject_kind, subject_id, field, value, source_id, confidence, status, extracted_by) "
        f"select '{kind}', ({subject_sql}), '{kind}', {_q(json.dumps(value, ensure_ascii=False))}::jsonb, s.id, 0.9, "
        f"'{status}', 'seed' from sources s where s.url = {_q(url)} "
        f"and not exists (select 1 from claims c where c.subject_id = ({subject_sql}) and c.source_id = s.id);"
    )


def to_sql(only: str | None = None) -> str:
    seed = load_all(only)
    src = seed["sources"]
    out = ["begin;"]
    for s in src.values():
        out.append(
            "insert into sources (url, title, title_zh, publisher, publisher_zh, language) values "
            f"({_q(s['url'])}, {_q(s['title'])}, {_q(s.get('title_zh'))}, {_q(s.get('publisher'))}, "
            f"{_q(s.get('publisher_zh'))}, {_q(s.get('language'))}) on conflict (url) do update set "
            "title = excluded.title, title_zh = excluded.title_zh, publisher = excluded.publisher, publisher_zh = excluded.publisher_zh;"
        )
    for p in seed["persons"].values():
        out.append(
            "insert into persons (slug, name_en, name_zh, name_zh_status, birth_year, death_year, mission_society, "
            "mission_society_zh, status) values "
            f"({_q(p['slug'])}, {_q(p['name_en'])}, {_q(p.get('name_zh'))}, {_q(p.get('name_zh_status'))}, "
            f"{_q(p.get('birth_year'))}, {_q(p.get('death_year'))}, {_q(p.get('mission_society'))}, "
            f"{_q(p.get('mission_society_zh'))}, 'approved') on conflict (slug) do update set "
            "name_en = excluded.name_en, name_zh = excluded.name_zh, name_zh_status = excluded.name_zh_status, "
            "birth_year = excluded.birth_year, death_year = excluded.death_year, mission_society = excluded.mission_society, "
            "mission_society_zh = excluded.mission_society_zh, status = 'approved';"
        )
        for k in p.get("sources", []):
            out.append(_claim("person", f"select id from persons where slug = {_q(p['slug'])}",
                              {"name_en": p["name_en"], "name_zh": p.get("name_zh")}, src[k]["url"], "approved"))
    for site in seed["sites"]:
        status = site_status(site)
        sid = f"select id from sites where slug = {_q(site['slug'])}"
        out.append(
            "insert into sites (slug, region, name_en, name_zh, name_zh_status, alt_names, site_type, location, geo_precision, "
            "modern_address, current_status, current_status_zh, summary, summary_zh, status) values "
            f"({_q(site['slug'])}, {_q(site['region'])}, {_q(site['name_en'])}, {_q(site.get('name_zh'))}, "
            f"{_q(site.get('name_zh_status'))}, {_q(site.get('alt_names', []))}, {_q(site['site_type'])}, "
            f"extensions.st_makepoint({site['lng']}, {site['lat']})::extensions.geography, {_q(site['geo_precision'])}, "
            f"{_q(site.get('modern_address'))}, {_q(site.get('current_status'))}, {_q(site.get('current_status_zh'))}, "
            f"{_q(site.get('summary'))}, {_q(site.get('summary_zh'))}, '{status}') on conflict (slug) do update set "
            "region = excluded.region, name_en = excluded.name_en, name_zh = excluded.name_zh, "
            "name_zh_status = excluded.name_zh_status, alt_names = excluded.alt_names, site_type = excluded.site_type, "
            "location = excluded.location, geo_precision = excluded.geo_precision, modern_address = excluded.modern_address, "
            "current_status = excluded.current_status, current_status_zh = excluded.current_status_zh, "
            "summary = excluded.summary, summary_zh = excluded.summary_zh, status = excluded.status;"
        )
        out.append(f"delete from events where site_id = ({sid});")
        for k in sorted({k for e in site["events"] for k in e.get("sources", [])}):
            out.append(_claim("site", sid, {"name_en": site["name_en"], "name_zh": site.get("name_zh")}, src[k]["url"], "approved"))
        for e in site["events"]:
            disputed_event = e.get("conflict_with") or e["type"] in ("founded", "built")
            ev_status = "conflict" if (status == "conflict" and disputed_event) else "approved"
            person = f"(select id from persons where slug = {_q(e['person'])})" if e.get("person") else "null"
            urls = ", ".join(_q(src[k]["url"]) for k in e["sources"])
            value = {k: e.get(k) for k in ("type", "year_start", "year_end", "description", "description_zh") if e.get(k) is not None}
            out.append(
                "with ev as (insert into events (site_id, person_id, event_type, year_start, year_end, description, "
                "description_zh, status) "
                f"select ({sid}), {person}, {_q(e['type'])}, {_q(e.get('year_start'))}, {_q(e.get('year_end'))}, "
                f"{_q(e.get('description'))}, {_q(e.get('description_zh'))}, '{ev_status}' returning id) "
                "insert into claims (subject_kind, subject_id, field, value, source_id, confidence, status, extracted_by) "
                f"select 'event', ev.id, 'event', {_q(json.dumps(value, ensure_ascii=False))}::jsonb, s.id, 0.9, '{ev_status}', 'seed' "
                f"from ev, sources s where s.url in ({urls});"
            )
    out.append("delete from claims c where c.subject_kind = 'event' and not exists (select 1 from events e where e.id = c.subject_id);")
    out.append("commit;")
    return "\n".join(out)


def load_via_api(store, only: str | None = None) -> dict:
    """Upsert the seed through PostgREST with the secret key (used by the seed-sync workflow)."""
    seed = load_all(only)
    src = seed["sources"]
    src_rows = store.upsert("sources", [
        {k: s.get(k) for k in ("url", "title", "title_zh", "publisher", "publisher_zh", "language")} for s in src.values()
    ], "url")
    src_id = {r["url"]: r["id"] for r in src_rows}

    person_rows = store.upsert("persons", [
        {**{k: p.get(k) for k in ("slug", "name_en", "name_zh", "name_zh_status", "birth_year", "death_year",
                                  "mission_society", "mission_society_zh")}, "status": "approved"}
        for p in seed["persons"].values()
    ], "slug")
    person_id = {r["slug"]: r["id"] for r in person_rows}

    def claims_for(kind: str, subject_id: str, keys: list[str], value: dict, status: str) -> list[dict]:
        return [{"subject_kind": kind, "subject_id": subject_id, "field": kind, "value": value,
                 "source_id": src_id[src[k]["url"]], "confidence": 0.9, "status": status, "extracted_by": "seed"} for k in keys]

    ids = list(person_id.values())
    claims: list[dict] = []
    for p in seed["persons"].values():
        claims += claims_for("person", person_id[p["slug"]], p.get("sources", []),
                             {"name_en": p["name_en"], "name_zh": p.get("name_zh")}, "approved")

    stats = {"sources": len(src_rows), "persons": len(person_rows), "sites": 0, "events": 0}
    for site in seed["sites"]:
        status = site_status(site)
        row = {k: site.get(k) for k in ("slug", "region", "name_en", "name_zh", "name_zh_status", "site_type", "geo_precision",
                                         "modern_address", "current_status", "current_status_zh", "summary", "summary_zh")}
        row |= {"alt_names": site.get("alt_names", []), "status": status,
                "location": f"SRID=4326;POINT({site['lng']} {site['lat']})"}
        site_row = store.upsert("sites", [row], "slug")[0]
        stats["sites"] += 1
        old_events = [r["id"] for r in store.select("events", select="id", site_id=f"eq.{site_row['id']}")]
        ids += old_events  # their seed claims are removed below with the rest
        store.delete("events", {"site_id": site_row["id"]})
        keys = sorted({k for e in site["events"] for k in e.get("sources", [])})
        claims += claims_for("site", site_row["id"], keys, {"name_en": site["name_en"], "name_zh": site.get("name_zh")}, "approved")
        ids.append(site_row["id"])
        if "photos" in site:  # openly licensed historical images curated in the seed
            store.delete("photos", {"site_id": site_row["id"], "kind": "historical"})
            store.insert("photos", [{
                "site_id": site_row["id"], "kind": "historical", "status": "approved", "image_url": p["image_url"],
                **{k: p.get(k) for k in ("caption", "caption_zh", "credit", "license", "source_url", "year_taken")},
            } for p in site["photos"]])
        for e in site["events"]:
            disputed_event = e.get("conflict_with") or e["type"] in ("founded", "built")
            ev_status = "conflict" if (status == "conflict" and disputed_event) else "approved"
            ev = store.insert("events", [{
                "site_id": site_row["id"], "person_id": person_id.get(e.get("person") or ""), "event_type": e["type"],
                "year_start": e.get("year_start"), "year_end": e.get("year_end"), "description": e.get("description"),
                "description_zh": e.get("description_zh"), "status": ev_status}])[0]
            stats["events"] += 1
            value = {k: e.get(k) for k in ("type", "year_start", "year_end", "description", "description_zh") if e.get(k) is not None}
            claims += claims_for("event", ev["id"], e["sources"], value, ev_status)
            ids.append(ev["id"])

    # Replace seed claims for these subjects so re-runs stay idempotent.
    for i in range(0, len(ids), 50):
        store.delete_in("claims", "subject_id", ids[i:i + 50], extra={"extracted_by": "seed"})
    store.insert("claims", claims)
    stats["claims"] = len(claims)
    return stats
