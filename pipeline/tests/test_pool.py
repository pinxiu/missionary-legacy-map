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
            if k in ("limit", "offset", "order"):
                continue
            if v == "is.null":
                rows = [r for r in rows if r.get(k) is None]
            elif v.startswith("gt."):
                rows = [r for r in rows if (r.get(k) or 0) > float(v[3:])]
            elif v.startswith("in."):
                rows = [r for r in rows if str(r.get(k)) in v[4:-1].split(",")]
            else:
                rows = [r for r in rows if str(r.get(k)) == v.removeprefix("eq.")]
        return rows

    def select_all(self, table, page=1000, order="id", **params):
        return self.select(table, **params)

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
    assert by["Foochow hospital"]["unit"] == "CN-FJ" and by["Foochow hospital"]["region"] == "fuzhou"
    assert by["Foochow hospital"]["geo_precision"] == "city"  # Fuzhou is in the gazetteer: pinned to the city, not the province
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
    monkeypatch.setattr(cov, "publish", lambda store, uid, agreed_only=True: {"sites": 4, "events": 9})
    monkeypatch.setattr(cov, "held_back", lambda store, uid: {"x"})
    monkeypatch.setattr("mlm.site.refresh", lambda store: None)
    u = cov.unit("CN-FJ")
    cov.auto_publish(MemStore(), u, {"number": 5}, {"agreement": "85%", "audited": 12}, "Gates met")
    body = next(c for c in calls if c[1] == "comment")[4]
    assert "published 4 sites and 9 events" in body and "1 record(s)" in body
    edit = next(c for c in calls if c[1] == "edit")
    assert "--add-label" in edit and "done" in edit and any(c[1] == "close" for c in calls)
    json.dumps(calls)


def test_select_pages_past_the_postgrest_cap():
    from mlm.store import Store

    rows = [{"id": f"{i:05d}"} for i in range(2350)]
    calls = []

    class S(Store):
        def __init__(self): pass
        def _get(self, table, params):
            calls.append(params)
            lo, n = int(params.get("offset", 0)), int(params["limit"])
            return rows[lo:lo + n]

    s = S()
    assert len(s.select("sites", select="id")) == 2350
    assert calls[0]["order"] == "id"
    assert s.select("sites", limit="1") == rows[:1]


class FakeLLM:
    """Stands in for llm.shared(): answers every request with `answer(req)` as a tool result."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def run(self, reqs):
        self.seen += reqs
        return [{"tool_input": self.answer(r), "stop_reason": "end_turn"} for r in reqs]

    def report(self):
        pass


def _mention(name, province, city, text, country="cn"):
    return {"name_en": name, "site_type": "church", "country": country, "province": province, "city": city,
            "evidence": [{"source_url": "u", "excerpt": text}]}


def test_pages_read_by_an_older_unit_scoped_parser_are_parsed_again():
    from mlm.pool import PARSER_VERSION, Pool

    page = {"url": "https://x.org/a", "title": "A", "text": "The Foochow church and the Hangchow church, mission stations."}
    store = MemStore()
    pool = Pool(store)
    h, _ = pool.add(page, "CN-ZJ")
    # what the first design did: marked "loaded" after parsing only the Zhejiang places on the page
    store.rows["resource_pool"][0].update(status="loaded", parsed_version=1)
    again = Pool(store)
    assert again.add(page, "CN-FJ") == (h, True) and not again.parsed(h)
    assert [p["hash"] for p in again.pending_parse(10)] == [h]
    again.mark(h, "extracted")
    assert Pool(store).parsed(h) and store.rows["resource_pool"][0]["parsed_version"] == PARSER_VERSION
    assert Pool(store).add(page, "CN-GD")[1] is False


def test_parser_reads_every_place_on_a_page_whoever_asked(monkeypatch):
    from mlm import extract
    from mlm.pool import Pool

    monkeypatch.setattr(extract, "ANTHROPIC_API_KEY", "test")
    text = "The mission built a church at Hangchow, a hospital church at Foochow and a chapel at Seoul in 1890."
    store = MemStore()
    pool = Pool(store)
    h, _ = pool.add({"url": "https://x.org/a", "title": "A", "text": text, "kind": "web"}, "CN-ZJ")
    answer = {"sites": [_mention("Hangchow church", "zhejiang", "hangzhou", text),
                        _mention("Foochow hospital church", "fujian", "fuzhou", text),
                        _mention("Seoul chapel", "seoul", "seoul", text, country="kr")]}
    llm = FakeLLM(lambda r: answer)
    stats = extract.run(pool, limit=10, prefer="zhejiang", llm=llm)
    assert stats["parsed"] == 1
    assert "Region being mapped" not in llm.seen[0].system  # no unit in the prompt: the result is the same whoever asked
    row = store.rows["extractions"][0]
    assert row["hash"] == h and row["resolved_at"] is None
    assert {s["country"] for s in row["data"]["sites"]} == {"cn", "kr"}
    assert extract.run(pool, limit=10, llm=llm)["pending"] == 0  # read once: not read again


def test_resolution_files_places_by_unit_and_keeps_the_rest_for_later(monkeypatch):
    from mlm import load
    from mlm.pool import Pool

    text = "A church at Hangchow and a chapel at Seoul."
    store = MemStore({"regions": [{"slug": "zhejiang"}, {"slug": "hangzhou"}]})
    pool = Pool(store)
    h, _ = pool.add({"url": "https://x.org/a", "title": "A", "text": text}, "CN-ZJ")
    ex = Extraction(document_url="https://x.org/a", sites=[
        SiteMention(name_en="Hangchow church", country="cn", province="zhejiang", city="hangzhou",
                    evidence=[Evidence(source_url="u", excerpt="A church at Hangchow")]),
        SiteMention(name_en="Seoul chapel", country="kr", province="seoul", city="seoul",
                    evidence=[Evidence(source_url="u", excerpt="a chapel at Seoul")])])
    pool.save_extraction(h, ex)

    stats = load.resolve_pending(store=store, use_network=False)
    assert [s["name_en"] for s in store.rows["sites"]] == ["Hangchow church"]
    assert store.rows["sites"][0]["unit"] == "CN-ZJ"
    row = store.rows["extractions"][0]
    assert row["resolved_at"] and row["unplaced"] == ["kr|seoul"] and stats["pages_waiting_for_a_unit"] == 1
    # nothing changed: the waiting page is not touched again
    assert Pool(store).pending_resolution(placeable=load.key_is_placeable) == []

    # a Korea unit appears: the same page is resolved again, with no new parse, and only the new place is added
    monkeypatch.setattr(load, "unit_for_place", lambda c, p: {"id": "KR", "region": "seoul", "subregions": [], "zh": "首尔", "en": "Seoul"}
                        if c == "kr" else None)
    monkeypatch.setattr(load, "ensure_region", lambda *a, **k: None)
    monkeypatch.setattr("mlm.coverage.unit", lambda u: {"id": u, "zh": "首尔", "en": "Seoul", "region": "seoul"})
    assert [r["hash"] for r in Pool(store).pending_resolution(placeable=load.key_is_placeable)] == [h]


def test_every_workflow_file_is_valid_yaml():
    """A comment line that lost its `#` once made every coverage run fail at parse time, before any job started."""
    from pathlib import Path

    import yaml

    for f in sorted((Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.yml")):
        assert isinstance(yaml.safe_load(f.read_text()), dict), f.name


def test_paging_uses_each_tables_own_unique_column():
    """Tables with no `id` column (checked against the live schema) would 400 on order=id."""
    from mlm.store import Store

    assert Store.PAGE_KEY == {"resource_pool": "hash", "extractions": "hash", "llm_cache": "key",
                              "app_settings": "key", "regions": "slug"}


def test_only_records_the_auditor_agreed_with_are_published(monkeypatch):
    from mlm import audit
    from mlm import coverage as cov

    store = MemStore({
        "sites": [{"id": f"s{i}", "status": "verified", "region": "fujian", "unit": "CN-FJ"} for i in range(4)],
        "audits": [{"unit": "CN-FJ", "subject_id": "s0", "agree": True}, {"unit": "CN-FJ", "subject_id": "s1", "agree": True},
                   {"unit": "CN-FJ", "subject_id": "s2", "agree": False}],   # s3 is not audited yet
    })
    assert cov.agreed_ids(store, "CN-FJ") == {"s0", "s1"}
    store.rows["audits"][2]["resolved_at"] = "2026-11-04"  # the owner ruled on s2: it is no longer held (but still not agreed)
    assert cov.agreed_ids(store, "CN-FJ") == {"s0", "s1"}

    published = []
    monkeypatch.setattr(cov, "publish", lambda st, uid, agreed_only=True: published.append((uid, agreed_only)) or {"sites": 2, "events": 1})
    monkeypatch.setattr(cov, "units", lambda: [{"id": "CN-FJ", "region": "fujian", "subregions": []}])
    monkeypatch.setattr(audit, "run", lambda *a, **k: {})
    monkeypatch.setattr("mlm.site.refresh", lambda st: None)
    out = cov.sweep_done(store)
    assert published == [("CN-FJ", True)] and out["CN-FJ"] == {"verified": 4, "agreed": 2, "published": 2}
    for a in store.rows["audits"]:
        a["agree"] = False  # nobody agreed: nothing is published
    published.clear()
    assert cov.sweep_done(store)["CN-FJ"]["published"] == 0 and published == []


def test_ohm_probe_reports_without_writing(monkeypatch):
    import httpx

    from mlm import ohm_probe

    assert ohm_probe.tile_xy(30.27, 120.16, 14) == (13660, 6745)

    def handler(req: httpx.Request) -> httpx.Response:
        if "nominatim" in req.url.host:
            q = req.url.params["q"]
            if q in ("Hangchow", "杭州天主堂"):
                return httpx.Response(200, json=[{"name": q, "display_name": "Hangzhou", "lat": "30.27", "lon": "120.16",
                                                  "osm_type": "node", "osm_id": 1}])
            return httpx.Response(200, json=[])
        if "overpass" in req.url.host:
            return httpx.Response(200, json={"elements": [{"tags": {"total": "42"}}]})
        return httpx.Response(200, content=b"x" * 10)

    monkeypatch.setattr(ohm_probe.time, "sleep", lambda s: None)
    real_client = httpx.Client
    monkeypatch.setattr(ohm_probe.httpx, "Client", lambda **k: real_client(transport=httpx.MockTransport(handler)))
    store = MemStore({"public_sites": [
        {"id": "a", "name_en": "Bell Tower", "name_zh": "杭州天主堂", "alt_names": [], "region": "hangzhou", "lat": 30.2701, "lng": 120.1601,
         "geo_precision": "city"},
        {"id": "b", "name_en": "Nowhere Chapel", "name_zh": None, "alt_names": [], "region": "hangzhou", "lat": 30.3, "lng": 120.2,
         "geo_precision": "street"}]})
    out = ohm_probe.run(store, sample=10)
    assert out["name_hits"] == 1 and out["within_300m"] == 1 and out["coarse_hits"] == 1
    assert out["old_names_resolved"].startswith("1/") and out["china_places_of_worship"] == "42"
    assert "public_sites" in store.rows and "sites" not in store.rows


def test_locate_only_moves_confident_pins():
    from mlm.locate import confident

    site = {"name_zh": "天主堂", "alt_names": ["圣母无原罪堂"]}
    assert not confident(site, {"name": "天主堂"}, False)            # generic name: matches every church in town
    assert confident(site, {"name": "天主堂"}, True)                 # but two sources agree
    assert confident(site, {"name": "圣母无原罪堂"}, False)         # exact, specific name
    assert not confident(site, {"name": "圣母无原罪堂天主教会"}, False)  # only a partial match


def test_name_variants_and_fuzzy_match():
    from mlm.locate import confident, name_variants

    v = name_variants({"name_zh": "上海圣保罗堂", "alt_names": []})
    assert "圣保罗堂" in v
    v = name_variants({"name_zh": "朱泾布道站（Shangyuantang）", "alt_names": []})
    assert "朱泾布道站" in v
    v = name_variants({"name_zh": "上海路中华基督教会教堂及尚德小学", "alt_names": []})
    assert "上海路中华基督教会教堂" in v
    site = {"name_zh": "上海圣保罗堂", "alt_names": []}
    assert confident(site, {"name": "圣保罗堂", "source": "amap"}, False)
    assert not confident(site, {"name": "圣保罗医院", "source": "amap"}, False)
    assert confident({"name_zh": None}, {"name": "x", "source": "geocode"}, False)


def test_ohm_planet_index_matches_a_site_by_old_name(tmp_path):
    import json as _json

    from mlm.ohm_planet import OhmIndex

    f = tmp_path / "x.geojsonseq"
    feats = [{"type": "Feature", "id": "w42", "properties": {"name": "St Paul's", "old_name:zh": "圣保罗堂", "start_date": "1900"},
              "geometry": {"type": "Point", "coordinates": [121.47, 31.23]}},
             {"type": "Feature", "id": "n7", "properties": {"name": "圣保罗医院"}, "geometry": {"type": "Point", "coordinates": [121.4, 31.2]}}]
    f.write_text("\n".join("\x1e" + _json.dumps(x) for x in feats))
    idx = OhmIndex(str(f))
    hits = idx.osm("圣保罗堂", (31.23, 121.47))
    assert [h["url"] for h in hits] == ["https://www.openhistoricalmap.org/way/42"] and hits[0]["source"] == "ohm"
    assert idx.osm("圣保罗堂", (39.9, 116.4)) == []


def test_unknown_city_gets_a_real_centre_and_a_region(monkeypatch):
    from mlm import places
    from mlm.geocode import CITIES, REGION_CENTROIDS

    monkeypatch.setattr(places, "lookup", lambda c, en, slug, client=None: {"lat": 36.2, "lng": 117.1, "name_zh": "泰安市"})
    store = MemStore()
    unit = {"id": "CN-SD", "region": "shandong", "en": "Shandong"}
    try:
        assert places.ensure_city(store, "taian", unit) == "taian"
        assert REGION_CENTROIDS["taian"] == (36.2, 117.1) and CITIES["taian"]["zh"] == "泰安市"
        row = store.rows["regions"][0]
        assert row["parent"] == "shandong" and row["lat"] == 36.2 and row["name_zh"] == "泰安市"
        assert places.ensure_city(store, "other", unit) is None
        monkeypatch.setattr(places, "lookup", lambda *a, **k: None)
        assert places.ensure_city(store, "nowhere", unit) is None
    finally:
        CITIES.pop("taian", None)
        REGION_CENTROIDS.pop("taian", None)


def test_replace_classifies_amap_hits_by_how_well_they_match_the_lead():
    from mlm.replace import classify

    assert classify("解放公园路12号", {"source": "geocode", "level": "门牌号", "name": "x"}) == "street"
    assert classify("通伸村", {"source": "geocode", "level": "村庄", "name": "x"}) == "district"
    assert classify("通伸村", {"source": "geocode", "level": "市", "name": "x"}) is None
    assert classify("武汉大学医院", {"source": "amap", "name": "武汉大学医院"}) == "street"
    assert classify("武汉大学医院", {"source": "amap", "name": "武汉大学校医院北门"}) == "district"
    assert classify("武汉大学医院", {"source": "amap", "name": "星巴克"}) is None


def test_locate_approx_needs_a_single_place():
    from mlm.locate import unambiguous

    pick = {"lat": 30.0, "lng": 120.0}
    assert unambiguous([pick, {"lat": 30.001, "lng": 120.001}], pick)
    assert not unambiguous([pick, {"lat": 30.05, "lng": 120.05}], pick)


def test_finder_reads_ohm_extract(tmp_path, monkeypatch):
    import json

    from mlm.locate import Finder
    f = tmp_path / "ohm.geojsonl"
    f.write_text(json.dumps({"id": "w1", "properties": {"name": "圣三一堂"}, "geometry": {"type": "Point", "coordinates": [120.17, 30.25]}},
                            ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setenv("MLM_OHM_INDEX", str(f))
    hits = Finder(amap_key="").ohm("圣三一堂", (30.25, 120.16))
    assert hits and hits[0]["source"] == "ohm" and "openhistoricalmap.org/way/1" in hits[0]["url"]
    monkeypatch.setenv("MLM_OHM_INDEX", str(tmp_path / "missing"))
    assert Finder(amap_key="").ohm("圣三一堂", (30.25, 120.16)) == []


def test_upcoming_lists_in_progress_first(monkeypatch):
    from mlm import coverage
    monkeypatch.setattr(coverage, "unit_issues", lambda: {
        "CN-ZJ": {"state": "CLOSED", "labels": ["done"]}, "CN-JS": {"state": "OPEN", "labels": ["todo"]},
        "CN-SH": {"state": "OPEN", "labels": ["in-progress"]}, "CN-FJ": {"state": "OPEN", "labels": ["todo"]}})
    assert [u["id"] for u in coverage.upcoming(3)] == ["CN-SH", "CN-JS", "CN-FJ"]


def test_photo_licences_and_dates():
    from mlm import photos as ph
    assert all(ph.license_ok(x) for x in ("CC BY-SA 4.0", "CC BY 3.0", "CC0", "Public domain", "PD-old-70"))
    assert not any(ph.license_ok(x) for x in ("CC BY-NC 2.0", "CC BY-ND 4.0", "Non-free", "Fair use", "", None, "All rights reserved"))
    assert ph.year_of("circa 1923") == 1923 and ph.year_of("2019-05-04 14:00:00") == 2019 and ph.year_of("1920-1925") == 1920
    assert ph.year_of("no date") is None
    assert [ph.kind_of(y) for y in (1900, 1952, 1953, 2004, 2005, 2020, None)] == [
        "historical", "historical", None, None, "current", "current", None]


def _photo_site(**kw):
    return {"id": "s1", "slug": "gulou", "name_en": "Gulou Church", "name_zh": "鼓楼堂", "alt_names": [], "region": "hangzhou",
            "unit": "CN-ZJ", "geo_precision": "street", "lat": 30.25, "lng": 120.17, **kw}


class FakeCommons:
    def __init__(self, ents=None, files=None, geo=(), pages=None):
        self.ents, self.files, self.geo, self.pages = ents or {}, files or {}, list(geo), pages or {}

    def wikidata_search(self, name, lang):
        return list(self.ents)

    def wikidata_items(self, ids):
        return {i: self.ents[i] for i in ids if i in self.ents}

    def page_images(self, lang, title):
        return self.pages.get(title, [])

    def geosearch(self, lat, lng, radius=250):
        return self.geo

    def file_info(self, titles):
        return {t: self.files[t] for t in titles if t in self.files}


def _info(title, year="1920", lic="Public domain", desc="Gulou Church in 1920", **kw):
    return {"title": title, "page": "https://commons.wikimedia.org/wiki/" + title, "thumb": "https://upload.wikimedia.org/x.jpg",
            "license": lic, "nonfree": False, "restricted": False, "artist": "A. Photographer", "date": year, "description": desc, **kw}


def _ent(label, lat, lng, image):
    return {"labels": {"zh": {"value": label}}, "aliases": {}, "claims": {
        "P625": [{"mainsnak": {"datavalue": {"value": {"latitude": lat, "longitude": lng}}}}],
        "P18": [{"mainsnak": {"datavalue": {"value": image}}}]}}


def test_photo_high_confidence_needs_same_name_and_place():
    from mlm import photos as ph
    site = _photo_site()
    good = FakeCommons({"Q1": _ent("鼓楼堂", 30.251, 120.171, "Gulou 1920.jpg")}, {"File:Gulou 1920.jpg": _info("File:Gulou 1920.jpg")})
    c = ph.candidates(good, site, [])
    assert [(x["file"], x["strength"]) for x in c] == [("File:Gulou 1920.jpg", "high")]
    v = ph.judge(c[0], good.file_info(["File:Gulou 1920.jpg"])["File:Gulou 1920.jpg"])
    assert v["add"]["kind"] == "historical" and v["add"]["year_taken"] == 1920 and v["add"]["license"] == "Public domain"
    far = FakeCommons({"Q1": _ent("鼓楼堂", 31.5, 121.4, "Other.jpg")})  # same name, 150 km away: only an option
    assert ph.candidates(far, site, [])[0]["strength"] == "option"
    other = FakeCommons({"Q1": _ent("天水堂", 30.251, 120.171, "Other.jpg")})  # a different church nearby
    assert ph.candidates(other, site, []) == []


def test_photo_judge_drops_unfree_and_holds_back_doubtful():
    from mlm import photos as ph
    high = {"file": "File:A.jpg", "strength": "high", "why": "x"}
    assert ph.judge(high, _info("File:A.jpg", lic="CC BY-NC 4.0")) is None
    assert ph.judge(high, None) is None
    assert "option" in ph.judge(high, _info("File:A.jpg", year="1990"))            # neither historical nor current
    assert "option" in ph.judge(high, _info("File:A.jpg", year="", desc="old"))    # undated
    assert "option" in ph.judge(high, _info("File:A.jpg", year="2019", desc="Sunday worship service"))
    assert "add" in ph.judge(high, _info("File:A.jpg", year="2019", desc="Exterior of the church"))
    assert ph.judge({**high, "strength": "option"}, _info("File:A.jpg"))["option"] == "x"


def test_photo_run_adds_once_and_lists_options(monkeypatch):
    from mlm import photos as ph
    store = MemStore({
        "public_sites": [{"id": "s1", "lat": 30.25, "lng": 120.17}],
        "sites": [{**_photo_site(), "status": "approved"}], "photos": [], "claims": [], "sources": []})
    store.select_all = store.select
    api = FakeCommons({"Q1": _ent("鼓楼堂", 30.251, 120.171, "Gulou 1920.jpg")}, {"File:Gulou 1920.jpg": _info("File:Gulou 1920.jpg")})
    written = []
    monkeypatch.setattr(ph, "write_issues", lambda opts: written.append(opts))
    stats = ph.run(store, apply=True, api=api)
    assert stats["added"] == 1 and stats["historical"] == 1 and len(store.rows["photos"]) == 1
    assert store.rows["photos"][0]["status"] == "approved" and store.rows["photos"][0]["kind"] == "historical"
    again = ph.run(store, apply=True, api=api)
    assert again["added"] == 0 and len(store.rows["photos"]) == 1  # never twice


def test_status_promotes_a_todo_unit_that_already_has_records(monkeypatch):
    from mlm import coverage
    calls = []
    monkeypatch.setattr(coverage, "unit_issues", lambda: {
        "CN-JS": {"state": "OPEN", "number": 3, "labels": ["todo"]}, "CN-SH": {"state": "OPEN", "number": 4, "labels": ["todo"]}})
    monkeypatch.setattr(coverage, "units", lambda: [
        {"id": "CN-JS", "region": "jiangsu", "en": "Jiangsu", "zh": "江苏", "phase": 1},
        {"id": "CN-SH", "region": "shanghai", "en": "Shanghai", "zh": "上海", "phase": 1}])
    monkeypatch.setattr(coverage, "metrics", lambda store, u: {"verified": 2, "published": 5} if u["id"] == "CN-JS" else {})
    monkeypatch.setattr(coverage, "gates_met", lambda m: False)
    monkeypatch.setattr(coverage, "issue_body", lambda u, m: "body")
    monkeypatch.setattr(coverage, "gh", lambda *a, **k: calls.append(a))
    text = coverage.status(object(), write=False)
    assert any("--add-label" in c and "in-progress" in c and c[2] == "3" for c in calls)
    assert not any(c[2:3] == ("4",) for c in calls if "--add-label" in c)
    assert "| CN-JS |" in text and "in-progress" in text


def test_gates_count_published_records_too(monkeypatch):
    from mlm import coverage
    monkeypatch.setattr(coverage, "catalog", lambda: {"done_when": {"min_audit_sample": 10, "min_sources": 3, "audit_agreement": 0.8}})
    base = {"verified": 0, "pending": 0, "sources": 4, "audited": 3, "agreement_value": 1.0}
    assert coverage.gates_met({**base, "published": 3})        # everything already published and the audit agrees
    assert not coverage.gates_met({**base, "published": 0})    # nothing at all
    assert not coverage.gates_met({**base, "published": 3, "agreement_value": 0.7})
    assert not coverage.gates_met({**base, "published": 3, "sources": 2})
