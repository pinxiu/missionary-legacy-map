from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SiteType = Literal[
    "church", "hospital", "school", "residence", "cemetery", "mission_station", "press", "orphanage", "other"
]
GeoPrecision = Literal["exact", "street", "district", "city", "province"]


class Evidence(BaseModel):
    source_url: str
    excerpt: str = Field(default="", max_length=300, description="Short supporting excerpt, under 300 chars")
    confidence: float = Field(default=0.7, ge=0, le=1)


class PersonMention(BaseModel):
    name_en: str
    name_zh: str | None = None
    alt_names: list[str] = []
    birth_year: int | None = None
    death_year: int | None = None
    mission_society: str | None = None
    evidence: list[Evidence] = []


class EventMention(BaseModel):
    event_type: str  # founded | built | rebuilt | moved | served | closed | buried | baptism | renamed
    year_start: int | None = None
    year_end: int | None = None
    person: str | None = None  # name_en of a PersonMention
    description: str = ""
    evidence: list[Evidence] = []


class SiteMention(BaseModel):
    name_en: str
    name_zh: str | None = None
    alt_names: list[str] = []
    site_type: SiteType = "other"
    place_text: str = Field(default="", description="Location as written in the source, e.g. 'outside the Qiantang Gate'")
    modern_address: str | None = None
    current_status: str | None = None
    summary: str = ""
    events: list[EventMention] = []
    evidence: list[Evidence] = []


class Extraction(BaseModel):
    """Everything extracted from one document."""

    document_url: str
    sites: list[SiteMention] = []
    persons: list[PersonMention] = []
    citations_to_follow: list[str] = Field(default=[], description="Books/archives the text cites that may hold more evidence")


class ResolvedSite(SiteMention):
    slug: str
    region: str
    lat: float | None = None
    lng: float | None = None
    geo_precision: GeoPrecision = "city"
    status: Literal["pending", "approved", "rejected", "conflict"] = "pending"
