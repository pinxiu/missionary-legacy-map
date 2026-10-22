from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SiteType = Literal[
    "church", "hospital", "school", "residence", "cemetery", "mission_station", "press", "orphanage", "other"
]
GeoPrecision = Literal["exact", "street", "district", "city", "province"]
ZhStatus = Literal["cited", "translated"]


class Evidence(BaseModel):
    source_url: str
    excerpt: str = Field(default="", max_length=300, description="Short supporting excerpt, under 300 chars")
    confidence: float = Field(default=0.7, ge=0, le=1)


class PersonMention(BaseModel):
    name_en: str
    name_zh: str | None = Field(default=None, description="Simplified Chinese name, ONLY if written in the source text")
    name_zh_status: ZhStatus | None = None  # set by the pipeline, not the extractor
    alt_names: list[str] = []
    birth_year: int | None = None
    death_year: int | None = None
    mission_society: str | None = None
    mission_society_zh: str | None = None
    evidence: list[Evidence] = []


class EventMention(BaseModel):
    event_type: str  # founded | built | rebuilt | moved | served | closed | buried | baptism | renamed
    year_start: int | None = None
    year_end: int | None = None
    person: str | None = None  # name_en of a PersonMention
    description: str = ""
    description_zh: str | None = None
    evidence: list[Evidence] = []


class SiteMention(BaseModel):
    name_en: str
    name_zh: str | None = Field(default=None, description="Simplified Chinese name, ONLY if written in the source text")
    name_zh_status: ZhStatus | None = None  # set by the pipeline, not the extractor
    alt_names: list[str] = []
    site_type: SiteType = "other"
    country: str | None = Field(
        default=None, description="ISO 3166-1 alpha-2 country code, lowercase, of where the site is today (e.g. 'cn', 'kr', 'in')")
    province: str | None = Field(
        default=None,
        description="First-level administrative division (province, state, prefecture) in lowercase romanization, e.g. 'fujian', "
                    "'gyeonggi', 'tamil nadu'; a municipality such as Shanghai is its own province")
    city: str = Field(default="other", description="City or town, lowercase romanization; 'other' only if the text names none")
    place_text: str = Field(default="", description="Location as written in the source, e.g. 'outside the Qiantang Gate'")
    modern_address: str | None = None
    current_status: str | None = None
    current_status_zh: str | None = None
    summary: str = ""
    summary_zh: str | None = None
    events: list[EventMention] = []
    evidence: list[Evidence] = []


class Extraction(BaseModel):
    """Everything extracted from one document."""

    document_url: str
    sites: list[SiteMention] = []
    persons: list[PersonMention] = []
    citations_to_follow: list[str] = Field(default=[], description="Books/archives the text cites that may hold more evidence")
    translated: bool = False  # set once the translate stage has filled missing Chinese fields


class ResolvedSite(SiteMention):
    slug: str
    region: str
    lat: float | None = None
    lng: float | None = None
    geo_precision: GeoPrecision = "city"
    status: Literal["pending", "approved", "rejected", "conflict"] = "pending"
