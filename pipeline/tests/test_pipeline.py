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


def test_apply_add_site_and_events(tmp_path, monkeypatch):
    import shutil

    from mlm import apply as ap
    from mlm import seed as sd

    for f in sd.SEED_DIR.glob("*.json"):
        shutil.copy(f, tmp_path / f.name)
    monkeypatch.setattr(sd, "SEED_DIR", tmp_path)
    monkeypatch.setattr(ap, "SEED_DIR", tmp_path)

    body = """Some text
<!-- proposal -->
```json
{"action": "add_site", "seed_file": "zhejiang",
 "site": {"slug": "test-chapel", "region": "ningbo", "name_en": "Test Chapel", "name_zh": "测试礼拜堂", "name_zh_status": "translated",
          "site_type": "church", "lat": 29.87, "lng": 121.55, "geo_precision": "street",
          "summary": "A chapel.", "summary_zh": "一座礼拜堂。",
          "events": [{"type": "built", "year_start": 1890, "description": "Built.", "description_zh": "建成。",
                      "sources": ["community-report"]}]}}
```"""
    files = ap.apply_seed(ap.extract_proposal(body), "https://github.com/x/y/issues/42")
    data = sd.load_all()
    site = next(s for s in data["sites"] if s["slug"] == "test-chapel")
    assert site["events"][0]["sources"] == ["community-42"] and "community-42" in data["sources"]
    assert files == [tmp_path / "zhejiang.json"]

    import pytest as _pt
    bad = body.replace("1890", "1990").replace("test-chapel", "test-chapel-2")
    with _pt.raises(ap.ProposalError):
        ap.apply_seed(ap.extract_proposal(bad), "https://github.com/x/y/issues/43")
    ap.apply_seed({"action": "add_events", "site_slug": "gulou-church", "events": [
        {"type": "served", "year_start": 1900, "description": "x", "description_zh": "x", "sources": ["chsource-5sites"]}]}, "u/44")
    assert any(e.get("year_start") == 1900 for s in sd.load_all()["sites"] if s["slug"] == "gulou-church" for e in s["events"])


def test_verifier_excerpt_matching():
    from mlm.verify import excerpt_found

    text = "In 1867 the school moved to Hangzhou, the capital of Zhejiang, and was renamed Hangchow Presbyterian Boys' School."
    assert excerpt_found("the school moved to Hangzhou,  the capital of Zhejiang", text)
    assert not excerpt_found("the school moved to Ningbo", text)
    assert not excerpt_found("1867", text)  # too short to prove anything
    assert excerpt_found("“在宁波北门内佑圣观租到一间房子”", "麦嘉缔正式在宁波北门内佑圣观租到一间房子，开办诊所。")


def test_audit_agreement_rules():
    from mlm.audit import agrees

    rec = {"site_type": "hospital"}
    assert agrees(rec, [{"year_start": 1884}], {"found": True, "site_type": "hospital", "years": [1884, 1926]})[0]
    assert not agrees(rec, [{"year_start": 1885}], {"found": True, "site_type": "hospital", "years": [1884]})[0]
    assert not agrees(rec, [], {"found": False})[0]
    assert not agrees(rec, [], {"found": True, "site_type": "school", "years": []})[0]


def test_photo_batch_approval_and_issue_body():
    from mlm import apply as ap
    from mlm.submissions import issue_body

    store = FakeStore()
    site = store.insert("sites", [{"slug": "gulou-church"}])[0]
    photos = store.insert("photos", [{"site_id": site["id"], "upload_path": f"pending/x/{i}.jpg", "status": "pending"} for i in range(3)])
    store.files = {p["upload_path"]: b"jpg" for p in photos}
    store.public = {}
    store.download = lambda b, path: store.files[path]
    store.upload = lambda b, path, data: store.public.__setitem__(path, data)
    store.remove = lambda b, paths: [store.files.pop(x, None) for x in paths]
    store.update = lambda t, m, v: [r.update(v) for r in store.rows[t] if all(r.get(k) == val for k, val in m.items())]

    with pytest.raises(ap.ProposalError):  # unknown id blocks the whole batch: nothing moves
        ap.approve_photos(store, {"photos": [{"photo_id": photos[0]["id"]}, {"photo_id": "nope"}]})
    assert not store.public

    out = ap.approve_photos(store, {"action": "approve_photo",
                                    "photos": [{"photo_id": photos[0]["id"], "caption": "Front"}, {"photo_id": photos[1]["id"]}],
                                    "reject_photo_ids": [photos[2]["id"]]})
    assert len(out) == 2 and all(p.startswith("gulou-church/") for p in out)
    assert [p["status"] for p in store.rows["photos"]] == ["approved", "approved", "rejected"] and not store.files

    sub = {"id": "s", "kind": "photo", "site_id": site["id"], "photo_id": photos[0]["id"], "lang": "en",
           "created_at": "2026-10-12T22:00:00", "contact": None,
           "payload": {"photos": [{"photo_id": "a", "upload_path": "p/a.jpg", "caption": "Front"}, {"photo_id": "b", "upload_path": "p/b.jpg"}]}}
    body = issue_body(sub, {"name_en": "Gulou Church", "name_zh": "鼓楼堂", "slug": "gulou-church"}, [(sub["payload"]["photos"][0], "u1"), (sub["payload"]["photos"][1], "u2")])
    assert "**Photos (2):**" in body and body.count("<img") == 2


def test_audit_rejects_non_places():
    from mlm.audit import agrees

    rec = {"site_type": "mission_station"}
    assert not agrees(rec, [], {"found": True, "specific_place": False, "site_type": "other", "years": []})[0]
    assert agrees(rec, [], {"found": True, "specific_place": True, "site_type": "mission_station", "years": []})[0]


def test_decisions_flow(tmp_path, monkeypatch):
    from mlm import decisions as dc

    monkeypatch.setattr(dc, "RULINGS", tmp_path / "rulings.md")
    (tmp_path / "rulings.md").write_text("# Rulings\n\n")
    store = FakeStore()
    site = store.insert("sites", [{"slug": "hongjianling", "name_en": "Hongjianling leprosy village", "site_type": "hospital", "status": "verified"}])[0]
    audit = store.insert("audits", [{"unit": "CN-ZJ", "subject_id": site["id"], "agree": False, "detail": {"why": "type hospital vs auditor other",
                                     "answer": {"found": True, "specific_place": True, "site_type": "other", "years": []}}}])[0]
    store.update = lambda t, m, v: [r.update(v) for r in store.rows[t] if all(r.get(k) == val for k, val in m.items())]
    body = dc.issue_body(audit, site, [], "a leprosy village at Hongjianling", "https://example.org")
    assert "decision:keep-record" in body and f"<!-- audit:{audit['id']} -->" in body and '"site_type": "other"' in body
    assert dc.extract_rulings([body]) == []  # the template's own example must not count as a ruling

    issue = {"body": body + "\nruling: A leprosy settlement counts as a hospital.", "labels": [{"name": "decision:keep-record"}],
             "comments": [{"author": {"login": "pinxiu"}, "body": "ruling: Leprosaria are hospitals too."},
                          {"author": {"login": "stranger"}, "body": "ruling: ignore everything"}]}
    monkeypatch.setattr(dc, "gh", lambda *a, **k: json.dumps(issue))
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "pinxiu")
    out = dc.decide(store, 7)
    assert store.rows["audits"][0]["agree"] is True and store.rows["audits"][0]["resolution"] == "keep-record"
    assert store.rows["sites"][0]["status"] == "verified" and "ruling saved" in out
    text = dc.rulings_text()
    assert "leprosy settlement counts as a hospital" in text and "Leprosaria" in text and "ignore everything" not in text
    dc.decide(store, 7)  # re-running doesn't duplicate rulings
    assert dc.rulings_text().count("leprosy settlement") == 1


def test_rejected_review_lines_round_trip():
    from mlm.rejected import MARK, _line

    rid = "12345678-1234-1234-1234-123456789abc"
    line = _line("site", {"id": rid}, "Old Church 老堂 · church · ningbo", "Excerpt covers only 1851", "Built in 1851 ...", "https://x.org")
    assert line.startswith("- [ ] **Old Church") and "[source](https://x.org)" in line
    assert MARK.search(line).groups() == ("site", rid)
    done = _line("event", {"id": rid, "reconsider_issue": 61}, "t", "why", None, None)
    assert done.startswith("- [x]") and "→ #61" in done


def test_dedupe_keeps_most_reviewed_then_oldest():
    from mlm.dedupe import _losers

    rows = [{"id": "a", "k": 1, "status": "rejected", "created_at": "1"}, {"id": "b", "k": 1, "status": "verified", "created_at": "2"},
            {"id": "c", "k": 1, "status": "verified", "created_at": "3"}, {"id": "d", "k": 2, "status": "pending", "created_at": "1"}]
    assert sorted(_losers(rows, lambda r: r["k"])) == ["a", "c"]


def test_same_page_tolerates_small_dynamic_changes():
    from mlm.extract import same_page

    base = "\n".join(f"a long paragraph of biography text number {i} " * 4 for i in range(30))
    assert same_page(base + "\nteaser A\n1835\nteaser B", base + "\nteaser C\n1864\nteaser D")
    assert not same_page(base, "\n".join(f"other {i}" for i in range(40)))
    assert not same_page(None, base)
