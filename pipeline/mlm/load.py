"""Load policy-filtered extractions into Supabase as *pending* records with cited claims.

Nothing loaded here is public until the review stage approves it.
Idempotent: a document already loaded with the same content hash is skipped, and model output never
overwrites an existing (e.g. hand-curated seed) site or person; it only adds claims and events to it.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

from .config import WORK_DIR, notice
from .geocode import REGION_CENTROIDS, geocode
from .models import Extraction
from .policy import apply as apply_policy
from .resolve import same_entity, slugify
from .store import Store


def _names(rec: dict) -> list[str]:
    return [rec["name_en"], *(rec.get("alt_names") or [])]


def _unique_slug(base: str, taken: set[str]) -> str:
    slug, n = base or "site", 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def load_extractions(region: str, store: Store | None = None, use_network: bool = True) -> dict:
    store = store or Store()
    stats = {"documents": 0, "skipped": 0, "new_sites": 0, "matched_sites": 0, "events": 0, "persons": 0, "dropped": 0}

    existing_sites = store.select("sites", select="id,slug,name_en,name_zh,alt_names")
    existing_people = store.select("persons", select="id,slug,name_en,name_zh,alt_names")
    site_slugs = {s["slug"] for s in existing_sites}
    person_slugs = {p["slug"] for p in existing_people}
    loaded = {d["url"]: d["content_hash"] for d in store.select("documents", select="url,content_hash")}

    for f in sorted((WORK_DIR / "extracted" / region).glob("*.json")):
        raw = f.read_text()
        content_hash = hashlib.sha256(raw.encode()).hexdigest()
        ex, dropped = apply_policy(Extraction.model_validate_json(raw))
        if loaded.get(ex.document_url) == content_hash:
            stats["skipped"] += 1
            continue
        stats["dropped"] += len(dropped)
        stats["documents"] += 1
        host = urlparse(ex.document_url).netloc
        source = store.upsert("sources", [{"url": ex.document_url, "publisher": host}], "url")[0]

        person_ids: dict[str, str] = {}
        for p in ex.persons:
            match = next((e for e in existing_people
                          if same_entity(_names(e), [p.name_en, *p.alt_names], e.get("name_zh"), p.name_zh)), None)
            if not match:
                row = {"slug": _unique_slug(slugify(p.name_en), person_slugs), "name_en": p.name_en, "name_zh": p.name_zh,
                       "name_zh_status": p.name_zh_status if p.name_zh else None, "alt_names": p.alt_names,
                       "birth_year": p.birth_year, "death_year": p.death_year, "mission_society": p.mission_society,
                       "mission_society_zh": p.mission_society_zh}
                match = store.insert("persons", [row])[0]
                existing_people.append(match)
                stats["persons"] += 1
            person_ids[p.name_en] = match["id"]
            for ev in p.evidence:
                store.insert("claims", [{
                    "subject_kind": "person", "subject_id": match["id"], "field": "person",
                    "value": p.model_dump(exclude={"evidence"}), "source_id": source["id"],
                    "excerpt": ev.excerpt[:300], "confidence": ev.confidence, "extracted_by": "llm"}])

        for s in ex.sites:
            city = s.city if s.city in REGION_CENTROIDS else region
            if city == "other":
                continue  # outside the region being mapped
            match = next((e for e in existing_sites
                          if same_entity(_names(e), [s.name_en, *s.alt_names], e.get("name_zh"), s.name_zh)), None)
            if match:
                stats["matched_sites"] += 1
            else:
                lat, lng, precision = geocode(city, s.name_en, s.place_text, s.modern_address, use_network)
                row = {"slug": _unique_slug(slugify(s.name_en), site_slugs), "region": city, "name_en": s.name_en,
                       "name_zh": s.name_zh, "name_zh_status": s.name_zh_status if s.name_zh else None,
                       "alt_names": s.alt_names, "site_type": s.site_type, "location": f"SRID=4326;POINT({lng} {lat})",
                       "geo_precision": precision, "modern_address": s.modern_address,
                       "current_status": s.current_status, "current_status_zh": s.current_status_zh,
                       "summary": s.summary, "summary_zh": s.summary_zh}
                match = store.insert("sites", [row])[0]
                existing_sites.append(match)
                stats["new_sites"] += 1
            for ev in s.evidence:
                store.insert("claims", [{
                    "subject_kind": "site", "subject_id": match["id"], "field": "site",
                    "value": s.model_dump(exclude={"events", "evidence"}), "source_id": source["id"],
                    "excerpt": ev.excerpt[:300], "confidence": ev.confidence, "extracted_by": "llm"}])
            for e in s.events:
                event = store.insert("events", [{
                    "site_id": match["id"], "person_id": person_ids.get(e.person or ""),
                    "event_type": e.event_type, "year_start": e.year_start, "year_end": e.year_end,
                    "description": e.description, "description_zh": e.description_zh}])[0]
                stats["events"] += 1
                for evd in e.evidence or s.evidence[:1]:
                    store.insert("claims", [{
                        "subject_kind": "event", "subject_id": event["id"], "field": "event",
                        "value": e.model_dump(exclude={"evidence"}), "source_id": source["id"],
                        "excerpt": evd.excerpt[:300], "confidence": evd.confidence, "extracted_by": "llm"}])
        store.upsert("documents", [{"url": ex.document_url, "source_id": source["id"], "content_hash": content_hash}], "url")
    notice(f"load[{region}]: {json.dumps(stats)}")
    return stats
