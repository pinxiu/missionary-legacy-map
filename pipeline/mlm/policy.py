"""Publication policy.

This product maps *historical* legacy. To protect people, the pipeline never publishes:
  * events dated after the historical cutoff (default 1952),
  * persons who may be living (born after 1900 with no recorded death year),
  * descriptions of present-day religious activity at a site beyond a neutral current-use note
    (e.g. "now a hospital", "active church").
Everything removed here is dropped, not stored, so it cannot leak through the database.
"""

from __future__ import annotations

import re

from .config import HISTORICAL_CUTOFF_YEAR
from .models import Extraction, PersonMention

_PRESENT_ACTIVITY = re.compile(
    r"\b(currently|today|now)\b.{0,40}\b(serv|minist|evangel|missionar|underground|house church)",
    re.IGNORECASE,
)


def person_allowed(p: PersonMention) -> bool:
    if p.death_year is not None:
        return True
    if p.birth_year is not None and p.birth_year > 1900:
        return False  # possibly living
    return True


def _clean_text(text: str | None) -> str | None:
    if text and _PRESENT_ACTIVITY.search(text):
        return None
    return text


def apply(extraction: Extraction) -> tuple[Extraction, list[str]]:
    """Return a policy-filtered copy and a list of human-readable drop reasons."""
    dropped: list[str] = []
    ex = extraction.model_copy(deep=True)

    allowed_people = []
    for p in ex.persons:
        if person_allowed(p):
            allowed_people.append(p)
        else:
            dropped.append(f"person '{p.name_en}': possibly living")
    ex.persons = allowed_people
    allowed_names = {p.name_en for p in allowed_people}

    for site in ex.sites:
        kept = []
        for ev in site.events:
            if ev.year_start is not None and ev.year_start > HISTORICAL_CUTOFF_YEAR:
                dropped.append(f"event at '{site.name_en}' ({ev.year_start}): after cutoff")
                continue
            if ev.person and ev.person not in allowed_names:
                ev.person = None
            desc = _clean_text(ev.description)
            if desc is None:
                dropped.append(f"event at '{site.name_en}': describes present-day activity")
                continue
            ev.description = desc
            kept.append(ev)
        site.events = kept
        site.summary = _clean_text(site.summary) or ""
    return ex, dropped
