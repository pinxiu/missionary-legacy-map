"""Load policy-filtered extractions into Supabase as *pending* records with cited claims.

Nothing loaded here is public until the review stage approves it.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from .config import WORK_DIR, notice
from .geocode import geocode
from .models import Extraction
from .policy import apply as apply_policy
from .resolve import same_entity, slugify
from .store import Store


def _names(rec: dict) -> list[str]:
    return [rec["name_en"], *rec.get("alt_names", [])]


def load_extractions(region: str, store: Store | None = None, use_network: bool = True) -> dict:
    store = store or Store()
    stats = {"documents": 0, "new_sites": 0, "matched_sites": 0, "events": 0, "persons": 0, "dropped": 0}

    existing_sites = store.select("sites", select="id,slug,name_en,name_zh,alt_names", region=f"eq.{region}")
    existing_people = store.select("persons", select="id,slug,name_en,name_zh,alt_names")

    for f in sorted((WORK_DIR / "extracted" / region).glob("*.json")):
        ex, dropped = apply_policy(Extraction.model_validate_json(f.read_text()))
        stats["dropped"] += len(dropped)
        stats["documents"] += 1
        host = urlparse(ex.document_url).netloc
        source = store.upsert("sources", [{"url": ex.document_url, "publisher": host}], "url")[0]

        person_ids: dict[str, str] = {}
        for p in ex.persons:
            match = next((e for e in existing_people
                          if same_entity(_names(e), [p.name_en, *p.alt_names], e.get("name_zh"), p.name_zh)), None)
            if not match:
                row = {"slug": slugify(p.name_en), "name_en": p.name_en, "name_zh": p.name_zh,
                       "alt_names": p.alt_names, "birth_year": p.birth_year, "death_year": p.death_year,
                       "mission_society": p.mission_society}
                match = store.upsert("persons", [row], "slug")[0]
                existing_people.append(match)
                stats["persons"] += 1
            person_ids[p.name_en] = match["id"]
            for ev in p.evidence:
                store.insert("claims", [{
                    "subject_kind": "person", "subject_id": match["id"], "field": "person",
                    "value": p.model_dump(exclude={"evidence"}), "source_id": source["id"],
                    "excerpt": ev.excerpt[:300], "confidence": ev.confidence, "extracted_by": "llm"}])

        for s in ex.sites:
            match = next((e for e in existing_sites
                          if same_entity(_names(e), [s.name_en, *s.alt_names], e.get("name_zh"), s.name_zh)), None)
            if match:
                stats["matched_sites"] += 1
            else:
                lat, lng, precision = geocode(region, s.name_en, s.place_text, s.modern_address, use_network)
                row = {"slug": slugify(s.name_en), "region": region, "name_en": s.name_en, "name_zh": s.name_zh,
                       "alt_names": s.alt_names, "site_type": s.site_type, "location": f"POINT({lng} {lat})",
                       "geo_precision": precision, "modern_address": s.modern_address,
                       "current_status": s.current_status, "summary": s.summary}
                match = store.upsert("sites", [row], "slug")[0]
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
                    "description": e.description}])[0]
                stats["events"] += 1
                for evd in e.evidence or s.evidence[:1]:
                    store.insert("claims", [{
                        "subject_kind": "event", "subject_id": event["id"], "field": "event",
                        "value": e.model_dump(exclude={"evidence"}), "source_id": source["id"],
                        "excerpt": evd.excerpt[:300], "confidence": evd.confidence, "extracted_by": "llm"}])
    notice(f"load[{region}]: {json.dumps(stats)}")
    return stats
