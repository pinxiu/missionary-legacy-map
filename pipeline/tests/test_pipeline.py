import json

from mlm import policy, review, seed
from mlm.crawl import html_to_page
from mlm.models import EventMention, Extraction, PersonMention, SiteMention
from mlm.resolve import normalize, same_entity, slugify


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


def test_seed_is_cited_and_sql_builds():
    data = seed.load_seed("hangzhou")
    keys = {s["key"] for s in data["sources"]}
    for site in data["sites"]:
        for e in site["events"]:
            assert e["sources"] and set(e["sources"]) <= keys, site["slug"]
            assert e.get("year_start") is None or e["year_start"] <= 1952
    sql = seed.to_sql("hangzhou")
    assert sql.startswith("begin;") and sql.rstrip().endswith("commit;")


def test_html_to_page_strips_chrome():
    html = "<html lang='en'><title>T</title><nav>menu</nav><main><p>Hangchow 1867</p><a href='/x'>x</a></main></html>"
    page = html_to_page("https://ex.org/a", html, "test")
    assert "menu" not in page.text and "Hangchow 1867" in page.text
    assert page.links == ["https://ex.org/x"] and page.lang == "en"
    json.dumps(page.__dict__)
