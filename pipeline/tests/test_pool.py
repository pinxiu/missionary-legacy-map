"""Resource pool, per-city discovery, one-pass multi-province extraction, and unit auto-approval."""

import json

from mlm.models import EventMention, Evidence, Extraction, PersonMention, SiteMention


class MemStore:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.n = 0

    def _id(self):
        self.n += 1
        return f"00000000-0000-0000-0000-{self.n:012d}"

    def select(self, table, select="*", **params):
        rows = self.rows.get(table, [])
        for k, v in params.items():
            if k == "limit":
                continue
            rows = [r for r in rows if str(r.get(k)) == v.removeprefix("eq.")]
        return rows

    select_all = select

    def insert(self, table, rows):
        recs = [{"id": self._id(), **r} for r in rows]
        self.rows.setdefault(table, []).extend(recs)
        return recs

    def upsert(self, table, rows, on_conflict):
        out = []
        for r in rows:
            hit = next((x for x in self.rows.setdefault(table, []) if x.get(on_conflict) == r[on_conflict]), None)
            if hit:
                hit.update(r)
                out.append(hit)
            else:
                out.extend(self.insert(table, [r]))
        return out

    def update(self, table, match, values):
        for r in self.rows.get(table, []):
            if all(str(r.get(k)) == str(v) for k, v in match.items()):
                r.update(values)


def test_resource_hash_ignores_teasers_whitespace_and_script():
    from mlm.pool import resource_hash

    a = "金陵女子大学在南京开学。\n\n1915年。"
    b = "金陵女子大學在南京開學。 1915年。\nOther Stories\nsome random teaser"
    assert resource_hash(a) == resource_hash(b)
    assert resource_hash(a) != resource_hash("金陵女子大学在南京开学。1916年。")


def test_pool_skips_pages_already_extracted():
    from mlm.pool import Pool

    store = MemStore()
    pool = Pool(store)
    page = {"url": "https://x.org/a", "title": "A", "text": "Foochow mission hospital founded 1850 by the American Board."}
    h, needs = pool.add(page, "CN-ZJ")
    assert needs and store.rows["resource_pool"][0]["status"] == "new"
    pool.mark(h, "extracted", locales=["fujian", "fuzhou", "fujian"])
    # the same article found again by another unit's search, even at a different URL
    fresh = Pool(store)
    h2, needs2 = fresh.add({**page, "url": "https://mirror.org/a"}, "CN-FJ")
    assert h2 == h and not needs2
    assert store.rows["resource_pool"][0]["locales"] == ["fujian", "fuzhou"]
    assert len(store.rows["resource_pool"]) == 1 and fresh.summary() == {"extracted": 1}


def test_long_pages_are_chunked_and_merged():
    from mlm.extract import MAX_CHARS, chunks, merge

    text = "\n".join(f"line {i} " + "x" * 80 for i in range(2000))  # ~170k chars
    parts = chunks(text)
    assert len(parts) >= 3 and all(len(p) <= MAX_CHARS for p in parts)
    assert "line 0 " in parts[0] and "line 1999 " in parts[-1]
    joined = "\n".join(parts)
    assert all(f"line {i} " in joined for i in range(0, 2000, 97))  # nothing lost between pieces
    assert chunks("short") == ["short"]
    a = Extraction(document_url="u", sites=[SiteMention(name_en="A")], citations_to_follow=["b1"])
    b = Extraction(document_url="u", sites=[SiteMention(name_en="B")], persons=[PersonMention(name_en="P")], citations_to_follow=["b2"])
    m = merge([a, b])
    assert [s.name_en for s in m.sites] == ["A", "B"] and len(m.persons) == 1 and m.citations_to_follow == ["b1", "b2"]


def test_discovery_searches_province_and_each_city():
    from mlm.discover import discovered_sources, places_of

    unit = {"id": "CN-JS", "region": "jiangsu", "en": "Jiangsu", "zh": "江苏", "subregions": ["nanjing", "suzhou"]}
    places = places_of(unit)
    assert [p[0] for p in places][:3] == ["jiangsu", "nanjing", "suzhou"]

    class Client:
        def __init__(self):
            self.queries = []

        def get(self, url, params=None):
            self.queries.append((url.split("/")[2], params["srsearch"]))
            title = params["srsearch"].split()[0] + " church"

            class R:
                status_code = 200

                def json(self_):
                    return {"query": {"search": [{"title": title}]}}
            return R()

    c = Client()
    srcs = discovered_sources(unit, c)
    assert {q[0] for q in c.queries} == {"en.wikipedia.org", "zh.wikipedia.org"}
    assert any("南京" in q[1] for q in c.queries) and any(q[1].startswith("Suzhou") for q in c.queries)
    assert all(s["max_pages"] == len(s["start_urls"]) <= 60 for s in srcs) and len(srcs) == 14  # the province and its 13 cities


def test_one_extraction_fills_every_province_it_mentions(tmp_path, monkeypatch):
    from mlm import load

    monkeypatch.setattr(load, "WORK_DIR", tmp_path)
    ev = [Evidence(source_url="u", excerpt="the Foochow hospital opened in 1850")]
    ex = Extraction(document_url="https://x.org/p", sites=[
        SiteMention(name_en="Hangchow church", province="zhejiang", city="hangzhou", evidence=ev),
        SiteMention(name_en="Foochow hospital", province="fujian", city="fuzhou", evidence=ev,
                    events=[EventMention(event_type="founded", year_start=1850, description="Opened.", evidence=ev)]),
        SiteMention(name_en="Mission house, Seoul", province="korea", city="seoul", evidence=ev),
        SiteMention(name_en="Somewhere", province=None, city="other", evidence=ev),
    ])
    d = tmp_path / "extracted" / "zhejiang"
    d.mkdir(parents=True)
    (d / "p.json").write_text(ex.model_dump_json())
    store = MemStore({"regions": [{"slug": "zhejiang"}, {"slug": "hangzhou"}]})
    stats = load.load_extractions("zhejiang", store=store, use_network=False, unit="CN-ZJ", subregions=["hangzhou", "ningbo"])
    by = {s["name_en"]: s for s in store.rows["sites"]}
    assert set(by) == {"Hangchow church", "Foochow hospital"}
    assert by["Hangchow church"]["unit"] == "CN-ZJ" and by["Hangchow church"]["region"] == "hangzhou"
    assert by["Foochow hospital"]["unit"] == "CN-FJ" and by["Foochow hospital"]["region"] == "fujian"
    assert by["Foochow hospital"]["geo_precision"] == "province"
    assert any(r["slug"] == "fujian" for r in store.rows["regions"])
    assert stats["other_units"] == 1 and stats["unmapped"] == 1 and len(store.rows["events"]) == 1


def test_auto_approve_gate_and_held_back_records(monkeypatch):
    from mlm import coverage as cov

    assert cov.auto_approve() and cov.catalog()["done_when"]["audit_agreement"] == 0.8
    base = {"verified": 20, "pending": 0, "audited": 10, "sources": 5, "agreement_value": 0.8, "open_decisions": 3}
    assert cov.gates_met(base)  # open disagreements no longer block the unit
    assert not cov.gates_met({**base, "agreement_value": 0.79})
    assert not cov.gates_met({**base, "pending": 1}) and not cov.gates_met({**base, "audited": 2})

    store = MemStore({
        "sites": [{"id": "s1", "status": "verified", "region": "fujian", "unit": "CN-FJ"},
                  {"id": "s2", "status": "verified", "region": "fujian", "unit": "CN-FJ"}],
        "audits": [{"unit": "CN-FJ", "subject_id": "s1", "agree": False, "resolved_at": None},
                   {"unit": "CN-FJ", "subject_id": "s2", "agree": True, "resolved_at": None}],
        "persons": [],
    })
    assert cov.held_back(store, "CN-FJ") == {"s1"}

    class Http:
        def patch(self, *a, **k):
            class R:
                def raise_for_status(self_):
                    pass
            return R()
    store.http = Http()
    out = cov.publish(store, "CN-FJ")
    status = {s["id"]: s["status"] for s in store.rows["sites"]}
    assert out["sites"] == 1 and status == {"s1": "verified", "s2": "approved"}


def test_auto_publish_closes_the_unit_issue(monkeypatch):
    from mlm import coverage as cov

    calls = []
    monkeypatch.setattr(cov, "gh", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(cov, "publish", lambda store, uid: {"sites": 4, "events": 9})
    monkeypatch.setattr(cov, "held_back", lambda store, uid: {"x"})
    monkeypatch.setattr("mlm.site.refresh", lambda store: None)
    u = cov.unit("CN-FJ")
    cov.auto_publish(MemStore(), u, {"number": 5}, {"agreement": "85%", "audited": 12}, "Gates met")
    body = next(c for c in calls if c[1] == "comment")[4]
    assert "published 4 sites and 9 events" in body and "1 record(s)" in body
    edit = next(c for c in calls if c[1] == "edit")
    assert "--add-label" in edit and "done" in edit and any(c[1] == "close" for c in calls)
    json.dumps(calls)
