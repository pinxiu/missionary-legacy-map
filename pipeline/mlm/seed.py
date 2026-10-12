"""Load an editorial seed file (data/seed/<region>.json) as approved, cited records.

`mlm seed --region hangzhou --sql` prints idempotent SQL instead of calling the API,
which is how the initial Supabase data was loaded.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR


def _q(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "array[" + ",".join(_q(x) for x in v) + "]::text[]" if v else "'{}'::text[]"
    return "'" + str(v).replace("'", "''") + "'"


def load_seed(region: str) -> dict:
    return json.loads((Path(DATA_DIR) / "seed" / f"{region}.json").read_text())


def to_sql(region: str) -> str:
    seed = load_seed(region)
    src = {s["key"]: s for s in seed["sources"]}
    out = ["begin;"]
    for s in seed["sources"]:
        out.append(
            f"insert into sources (url, title, publisher, language) values ({_q(s['url'])}, {_q(s['title'])}, "
            f"{_q(s['publisher'])}, {_q(s.get('language'))}) on conflict (url) do update set title = excluded.title;"
        )
    for p in seed["persons"]:
        out.append(
            "insert into persons (slug, name_en, name_zh, birth_year, death_year, mission_society, status) values "
            f"({_q(p['slug'])}, {_q(p['name_en'])}, {_q(p.get('name_zh'))}, {_q(p.get('birth_year'))}, "
            f"{_q(p.get('death_year'))}, {_q(p.get('mission_society'))}, 'approved') "
            "on conflict (slug) do update set name_en = excluded.name_en, name_zh = excluded.name_zh, "
            "birth_year = excluded.birth_year, death_year = excluded.death_year, "
            "mission_society = excluded.mission_society, status = 'approved';"
        )
        for k in p.get("sources", []):
            out.append(
                "insert into claims (subject_kind, subject_id, field, value, source_id, confidence, status, extracted_by) "
                f"select 'person', p.id, 'person', {_q(json.dumps({'name_en': p['name_en']}))}::jsonb, s.id, 0.9, "
                f"'approved', 'seed' from persons p, sources s where p.slug = {_q(p['slug'])} and s.url = {_q(src[k]['url'])} "
                "and not exists (select 1 from claims c where c.subject_id = p.id and c.source_id = s.id);"
            )
    for site in seed["sites"]:
        has_conflict = any(e.get("conflict_with") for e in site["events"])
        status = "conflict" if has_conflict else "approved"
        out.append(
            "insert into sites (slug, region, name_en, name_zh, alt_names, site_type, location, geo_precision, "
            "modern_address, current_status, summary, status) values "
            f"({_q(site['slug'])}, {_q(region)}, {_q(site['name_en'])}, {_q(site.get('name_zh'))}, "
            f"{_q(site.get('alt_names', []))}, {_q(site['site_type'])}, "
            f"extensions.st_makepoint({site['lng']}, {site['lat']})::extensions.geography, {_q(site['geo_precision'])}, "
            f"{_q(site.get('modern_address'))}, {_q(site.get('current_status'))}, {_q(site.get('summary'))}, '{status}') "
            "on conflict (slug) do update set name_en = excluded.name_en, name_zh = excluded.name_zh, "
            "alt_names = excluded.alt_names, location = excluded.location, geo_precision = excluded.geo_precision, "
            "modern_address = excluded.modern_address, current_status = excluded.current_status, "
            "summary = excluded.summary, status = excluded.status;"
        )
        out.append(f"delete from events where site_id = (select id from sites where slug = {_q(site['slug'])});")
        site_keys = {k for e in site["events"] for k in e.get("sources", [])}
        for k in sorted(site_keys):
            out.append(
                "insert into claims (subject_kind, subject_id, field, value, source_id, confidence, status, extracted_by) "
                f"select 'site', st.id, 'site', {_q(json.dumps({'name_en': site['name_en']}))}::jsonb, s.id, 0.9, "
                f"'approved', 'seed' from sites st, sources s where st.slug = {_q(site['slug'])} and s.url = {_q(src[k]['url'])} "
                "and not exists (select 1 from claims c where c.subject_id = st.id and c.source_id = s.id);"
            )
        for e in site["events"]:
            ev_status = "conflict" if (e["type"] == "founded" and has_conflict) else "approved"
            person = f"(select id from persons where slug = {_q(e['person'])})" if e.get("person") else "null"
            out.append(
                "with ev as (insert into events (site_id, person_id, event_type, year_start, year_end, description, status) "
                f"select id, {person}, {_q(e['type'])}, {_q(e.get('year_start'))}, {_q(e.get('year_end'))}, "
                f"{_q(e.get('description'))}, '{ev_status}' from sites where slug = {_q(site['slug'])} returning id) "
                "insert into claims (subject_kind, subject_id, field, value, source_id, confidence, status, extracted_by) "
                f"select 'event', ev.id, 'event', {_q(json.dumps(e, ensure_ascii=False))}::jsonb, s.id, 0.9, '{ev_status}', 'seed' "
                f"from ev, sources s where s.url in ({', '.join(_q(src[k]['url']) for k in e['sources'])});"
            )
    out.append("delete from claims c where c.subject_kind = 'event' and not exists (select 1 from events e where e.id = c.subject_id);")
    out.append("commit;")
    return "\n".join(out)
