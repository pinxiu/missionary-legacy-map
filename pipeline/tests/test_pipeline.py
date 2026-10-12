import json

import pytest

from mlm import policy, review, seed
from mlm.crawl import html_to_page
from mlm.models import EventMention, Extraction, PersonMention, SiteMention
from mlm.resolve import normalize, same_entity, slugify
from mlm.store import auth_headers


def test_romanization_normalizes():
    assert normalize("Hangchow Presbyterian College") == "hangzhou presbyterian college"
    assert slugify("Kwang-Chi Hospital") == "guangji-hospital"


def test_same_entity_across_spellings():
    assert same_entity(["Hangchow Christian College"], ["Hangzhou Christian College"])
    assert not same_entity(["Gulou Church"], ["Tianshui Church"])
    assert same_entity(["X"], ["Y"], "天水堂", "天水堂")


def test_policy_drops_modern_and_living():
    ex = Extraction(
        document_url="https://example.org",
        persons=[PersonMention(name_en="Old Missionary", death_year=1930),
                 PersonMention(name_en="Possibly Living", birth_year=1975)],
        sites=[SiteMention(name_en="A Church", events=[
            EventMention(event_type="founded", year_start=1880, person="Old Missionary"),
            EventMention(event_type="served", year_start=1999, person="Possibly Living"),
            EventMention(event_type="served", description="Currently serves as an underground evangelist"),
        ])],
    )
    clean, dropped = policy.apply(ex)
    assert [p.name_en for p in clean.persons] == ["Old Missionary"]
    assert [e.year_start for e in clean.sites[0].events] == [1880]
    assert len(dropped) == 3


def test_review_decision():
    one = [{"source_id": "a", "confidence": 0.9}]
    two = one + [{"source_id": "b", "confidence": 0.9}]
    assert review.decide(one) == "pending"
    assert review.decide(two) == "approved"


def test_founding_conflicts():
    events = [
        {"site_id": "s1", "event_type": "founded", "year_start": 1860, "status": "approved"},
        {"site_id": "s1", "event_type": "founded", "year_start": 1867, "status": "pending"},
        {"site_id": "s2", "event_type": "founded", "year_start": 1884, "status": "approved"},
    ]
    assert review.founding_conflicts(events) == {"s1": {1860, 1867}}


def test_seed_is_cited_bilingual_and_sql_builds():
    data = seed.load_all()
    keys = set(data["sources"])
    for p in data["persons"].values():
        assert set(p.get("sources", [])) <= keys, p["slug"]
        assert p.get("name_zh") and p.get("name_zh_status") in ("cited", "translated"), p["slug"]
    for site in data["sites"]:
        assert site.get("name_zh") and site.get("name_zh_status") in ("cited", "translated"), site["slug"]
        assert site.get("summary_zh"), site["slug"]
        for e in site["events"]:
            assert e["sources"] and set(e["sources"]) <= keys, site["slug"]
            assert e.get("description_zh"), site["slug"]
            assert e.get("year_start") is None or e["year_start"] <= 1952
            assert e.get("person") is None or e["person"] in data["persons"], (site["slug"], e["person"])
    sql = seed.to_sql()
    assert sql.startswith("begin;") and sql.rstrip().endswith("commit;")


def test_html_to_page_strips_chrome():
    html = "<html lang='en'><title>T</title><nav>menu</nav><main><p>Hangchow 1867</p><a href='/x'>x</a></main></html>"
    page = html_to_page("https://ex.org/a", html, "test")
    assert "menu" not in page.text and "Hangchow 1867" in page.text
    assert page.links == ["https://ex.org/x"] and page.lang == "en"
    json.dumps(page.__dict__)


def test_store_rejects_publishable_and_accepts_secret_keys():
    with pytest.raises(SystemExit):
        auth_headers("https://x.supabase.co", "sb_publishable_abc")
    h = auth_headers("https://x.supabase.co", "sb_secret_abc")
    assert h["apikey"] == "sb_secret_abc" and "Authorization" not in h
    assert auth_headers("https://x.supabase.co", "eyJlegacy")["Authorization"] == "Bearer eyJlegacy"


class FakeStore:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.n = 0

    def _id(self):
        self.n += 1
        return f"00000000-0000-0000-0000-{self.n:012d}"

    def upsert(self, table, rows, on_conflict):
        out = []
        for r in rows:
            existing = next((x for x in self.rows.setdefault(table, []) if x[on_conflict] == r[on_conflict]), None)
            if existing:
                existing.update(r)
                out.append(existing)
            else:
                rec = {"id": self._id(), **r}
                self.rows[table].append(rec)
                out.append(rec)
        return out

    def insert(self, table, rows):
        recs = [{"id": self._id(), **r} for r in rows]
        self.rows.setdefault(table, []).extend(recs)
        return recs

    def select(self, table, select="*", **params):
        rows = self.rows.get(table, [])
        for k, v in params.items():
            rows = [r for r in rows if str(r.get(k)) == v.removeprefix("eq.")]
        return rows

    def delete(self, table, match):
        self.rows[table] = [r for r in self.rows.get(table, []) if any(r.get(k) != v for k, v in match.items())]

    def delete_in(self, table, column, values, extra=None):
        extra = extra or {}
        self.rows[table] = [r for r in self.rows.get(table, [])
                            if not (r.get(column) in values and all(r.get(k) == v for k, v in extra.items()))]


def test_seed_api_load_is_idempotent():
    store = FakeStore()
    first = seed.load_via_api(store)
    counts = {t: len(r) for t, r in store.rows.items()}
    seed.load_via_api(store)
    assert {t: len(r) for t, r in store.rows.items()} == counts
    assert first["sites"] == len(seed.load_all()["sites"])
    assert all(c["source_id"] for c in store.rows["claims"])
    assert any(s["status"] == "conflict" for s in store.rows["sites"])


def test_translate_marks_status_and_keeps_cited_names():
    from mlm.translate import apply_translations, missing_fields

    ex = Extraction(document_url="u", sites=[
        SiteMention(name_en="Blyth Hospital", summary="A hospital.", events=[EventMention(event_type="founded", description="Opened.")]),
        SiteMention(name_en="Huayuan Lane Church", name_zh="花园巷教堂"),
    ], persons=[PersonMention(name_en="George Stott", name_zh="曹雅直")])
    fields = missing_fields(ex)
    assert set(fields) == {"s0.name", "s0.summary", "s0.e0"}
    out = apply_translations(ex, {"s0.name": "白累德医院", "s0.summary": "一所医院。", "s0.e0": "开办。"})
    assert out.sites[0].name_zh_status == "translated" and out.sites[1].name_zh_status == "cited"
    assert out.persons[0].name_zh_status == "cited" and out.translated
