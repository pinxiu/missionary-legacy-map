"""Apply an owner-approved proposal from a GitHub issue to production data.

The AI first pass writes a fenced JSON block after the marker `<!-- proposal -->` in the issue body;
the owner may edit it, then adds the `approved` label. This module validates the proposal against the
same rules as the seed (citations, bilingual fields, nothing after 1952) and then:
  * edits data/seed/<file>.json (the workflow commits it and runs `mlm seed` to publish), and/or
  * publishes an uploaded photo (copies it to the public bucket and marks it approved).
Anything invalid is rejected with an issue comment; nothing is partially applied.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import HISTORICAL_CUTOFF_YEAR, notice
from .seed import SEED_DIR, load_all

ACTIONS = {"add_site", "update_site", "add_events", "approve_photo", "none"}
SITE_TYPES = {"church", "hospital", "school", "residence", "cemetery", "mission_station", "press", "orphanage", "other"}
PRECISIONS = {"exact", "street", "district", "city", "province"}


class ProposalError(ValueError):
    pass


def extract_proposal(body: str) -> dict:
    m = re.search(r"<!--\s*proposal\s*-->\s*```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        raise ProposalError("No proposal block found (expected `<!-- proposal -->` followed by a ```json block).")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ProposalError(f"Proposal JSON is invalid: {e}") from e


def _check_event(e: dict, source_keys: set[str], person_slugs: set[str]) -> None:
    if not e.get("type") or not e.get("description") or not e.get("description_zh"):
        raise ProposalError(f"Event needs type, description and description_zh: {e}")
    if not e.get("sources") or not set(e["sources"]) <= source_keys:
        raise ProposalError(f"Event cites unknown or no sources: {e.get('sources')}")
    if e.get("year_start") is not None and int(e["year_start"]) > HISTORICAL_CUTOFF_YEAR:
        raise ProposalError(f"Event year {e['year_start']} is after {HISTORICAL_CUTOFF_YEAR}; historical records only.")
    if e.get("person") and e["person"] not in person_slugs:
        raise ProposalError(f"Event names unknown person '{e['person']}' (add it under persons).")


def _check_site_fields(s: dict, partial: bool) -> None:
    required = [] if partial else [
        "slug", "name_en", "name_zh", "name_zh_status", "site_type", "lat", "lng", "geo_precision", "summary", "summary_zh"]
    missing = [k for k in required if s.get(k) in (None, "")]
    if missing:
        raise ProposalError(f"Site is missing {missing}")
    if "site_type" in s and s["site_type"] not in SITE_TYPES:
        raise ProposalError(f"Unknown site_type {s['site_type']}")
    if "geo_precision" in s and s["geo_precision"] not in PRECISIONS:
        raise ProposalError(f"Unknown geo_precision {s['geo_precision']}")
    if "name_zh_status" in s and s["name_zh_status"] not in ("cited", "translated"):
        raise ProposalError("name_zh_status must be 'cited' or 'translated'")
    for k in ("lat", "lng"):
        if k in s and not isinstance(s[k], (int, float)):
            raise ProposalError(f"{k} must be a number")


def _find_site(slug: str) -> tuple[Path, dict, dict]:
    for f in sorted(SEED_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        for s in d["sites"]:
            if s["slug"] == slug:
                return f, d, s
    raise ProposalError(f"No seed site with slug '{slug}'.")


def apply_seed(proposal: dict, issue_url: str) -> list[Path]:
    """Validate and write seed changes. Returns the files written."""
    action = proposal.get("action")
    if action not in ACTIONS:
        raise ProposalError(f"Unknown action {action!r}; expected one of {sorted(ACTIONS)}")
    if action in ("none", "approve_photo"):
        return []
    current = load_all()
    # The visitor's own report becomes a citable source once the owner approves it. The agent cites it
    # as "community-report"; here it is renamed to a per-issue key.
    community_key = f"community-{issue_url.rstrip('/').split('/')[-1]}"
    proposal = json.loads(json.dumps(proposal).replace('"community-report"', f'"{community_key}"'))
    new_sources = {s["key"]: s for s in proposal.get("sources", [])}
    for k, s in new_sources.items():
        if not s.get("url") or not s.get("title"):
            raise ProposalError(f"Source {k} needs url and title")
    new_sources.setdefault(community_key, {"key": community_key, "url": issue_url, "title": "Community report",
                                           "title_zh": "用户提交", "publisher": "Legacy Map contributors",
                                           "publisher_zh": "宣教遗迹地图贡献者", "language": "en"})
    source_keys = set(current["sources"]) | set(new_sources)
    new_persons = {p["slug"]: p for p in proposal.get("persons", [])}
    for p in new_persons.values():
        if not p.get("name_en") or not p.get("name_zh") or p.get("name_zh_status") not in ("cited", "translated"):
            raise ProposalError(f"Person {p.get('slug')} needs name_en, name_zh and name_zh_status")
        if not set(p.get("sources", [])) <= source_keys:
            raise ProposalError(f"Person {p['slug']} cites unknown sources")
    person_slugs = set(current["persons"]) | set(new_persons)

    if action == "add_site":
        site = proposal.get("site") or {}
        _check_site_fields(site, partial=False)
        if site["slug"] in {s["slug"] for s in current["sites"]}:
            raise ProposalError(f"Slug {site['slug']} already exists; use update_site.")
        events = site.get("events") or proposal.get("events") or []
        if not events:
            raise ProposalError("A new site needs at least one cited event.")
        for e in events:
            _check_event(e, source_keys, person_slugs)
        target = SEED_DIR / f"{proposal.get('seed_file', 'zhejiang')}.json"
        if not target.exists():
            raise ProposalError(f"Seed file {target.name} does not exist.")
        d = json.loads(target.read_text())
        d["sites"].append({**site, "events": events})
    else:
        slug = proposal.get("site_slug") or (proposal.get("site") or {}).get("slug")
        if not slug:
            raise ProposalError("site_slug is required.")
        target, d, site = _find_site(slug)
        if action == "update_site":
            changes = {k: v for k, v in (proposal.get("site") or {}).items() if k not in ("slug", "events")}
            _check_site_fields(changes, partial=True)
            site.update(changes)
        for e in proposal.get("events", []):
            _check_event(e, source_keys, person_slugs)
            site["events"].append(e)
        if action == "add_events" and not proposal.get("events"):
            raise ProposalError("add_events needs events.")

    # New sources/persons are stored in the file being edited (the loader merges all files).
    used = {k for st in d["sites"] for e in st["events"] for k in e.get("sources", [])}
    used |= {k for p in proposal.get("persons", []) for k in p.get("sources", [])}
    d["sources"] += [src for k, src in new_sources.items() if k in used and k not in current["sources"]]
    slugs_in_file = {p["slug"] for p in d["persons"]}
    d["persons"] += [p for slug, p in new_persons.items() if slug not in slugs_in_file and slug not in current["persons"]]
    target.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    load_all()  # re-parse every file to make sure the result is loadable
    return [target]


def photo_entries(proposal: dict) -> list[dict]:
    """`photos` (a batch) or the older single `photo` form."""
    return list(proposal.get("photos") or ([proposal["photo"]] if proposal.get("photo") else []))


def approve_photos(store, proposal: dict) -> list[str]:
    """Publish every listed photo and reject any in `reject_photo_ids`. All ids are checked before anything moves."""
    entries = photo_entries(proposal)
    reject_ids = list(proposal.get("reject_photo_ids") or [])
    if not entries and not reject_ids:
        raise ProposalError("approve_photo needs photos[].photo_id (or reject_photo_ids)")
    rows = {}
    for pid in [e.get("photo_id") for e in entries] + reject_ids:
        if not pid:
            raise ProposalError("every photo entry needs photo_id")
        row = (store.select("photos", select="id,site_id,upload_path,status", id=f"eq.{pid}") or [None])[0]
        if not row or not row.get("upload_path"):
            raise ProposalError(f"Photo {pid} not found or has no upload.")
        rows[pid] = row
    published = []
    for e in entries:
        row = rows[e["photo_id"]]
        site = store.select("sites", select="slug", id=f"eq.{row['site_id']}")[0]
        public_path = f"{site['slug']}/{row['id']}.jpg"
        store.upload("site-photos", public_path, store.download("photo-uploads", row["upload_path"]))
        store.update("photos", {"id": row["id"]}, {
            "status": "approved", "public_path": public_path,
            "caption": e.get("caption"), "caption_zh": e.get("caption_zh"), "credit": e.get("credit"),
            "year_taken": e.get("year_taken"), "moderation_note": "approved by owner",
        })
        store.remove("photo-uploads", [row["upload_path"]])
        published.append(public_path)
    for pid in reject_ids:
        store.update("photos", {"id": pid}, {"status": "rejected", "moderation_note": "not approved by owner"})
        store.remove("photo-uploads", [rows[pid]["upload_path"]])
    return published


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()


def run(issue: int) -> None:
    data = json.loads(gh("issue", "view", str(issue), "--json", "body,url,labels"))
    try:
        proposal = extract_proposal(data["body"])
        files = apply_seed(proposal, data["url"])
        photos: list[str] = []
        if proposal.get("action") == "approve_photo" or photo_entries(proposal) or proposal.get("reject_photo_ids"):
            from .store import Store
            photos = approve_photos(Store(), proposal)
    except ProposalError as e:
        gh("issue", "comment", str(issue), "--body", f"⚠️ Not applied: {e}\n\nEdit the proposal block and re-add `approved`.")
        gh("issue", "edit", str(issue), "--remove-label", "approved", "--add-label", "needs-approval")
        notice(f"apply #{issue}: rejected: {e}", "warning")
        raise SystemExit(1) from e
    summary = ", ".join([f"updated {f.name}" for f in files] + ([f"published {len(photos)} photo(s)"] if photos else [])) or "no data change"
    Path("/tmp/apply-summary.txt").write_text(summary)
    notice(f"apply #{issue}: {summary}")
