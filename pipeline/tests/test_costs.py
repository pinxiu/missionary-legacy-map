"""Cost controls in mlm.llm and the stages that use it, with fake model clients (no network, no spend)."""

import json
from types import SimpleNamespace as NS

import pytest

from mlm import extract, translate
from mlm import llm as llm_mod
from mlm.llm import LLM, Cache, Req
from mlm.models import Extraction, PersonMention, SiteMention


def message(text="", tool=None, model="claude-haiku-5-5", stop="end_turn"):
    content = []
    if text:
        content.append(NS(type="text", text=text))
    if tool is not None:
        content.append(NS(type="tool_use", input=tool))
    return NS(content=content, model=model, stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeAnthropic:
    """messages.create answers with `answer(params)`; batches finish after `rounds` polls."""

    def __init__(self, answer, rounds=0):
        self.answer, self.rounds = answer, rounds
        self.created, self.batched, self.polls = [], [], 0
        self._batches = {}
        outer = self

        class Batches:
            def create(self, requests):
                bid = f"msgbatch_{len(outer._batches)}"
                outer._batches[bid] = requests
                outer.batched.append(requests)
                return NS(id=bid)

            def retrieve(self, bid):
                outer.polls += 1
                return NS(processing_status="ended" if outer.polls > outer.rounds else "in_progress")

            def results(self, bid):
                for r in outer._batches[bid]:
                    yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=outer.answer(r["params"])))

        class Messages:
            batches = Batches()

            def create(self, **params):
                outer.created.append(params)
                return outer.answer(params)

        self.messages = Messages()


@pytest.fixture
def make_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm_mod, "BATCH_POLL_S", 0)

    def make(answer, rounds=0, gemini_http=None):
        client = FakeAnthropic(answer, rounds)
        llm = LLM(store=False, anthropic_client=client, http=gemini_http)  # store=False: no table
        llm.cache = Cache(None, tmp_path / "cache")
        llm.gemini_ok = gemini_http is not None
        return llm, client
    return make


def test_cache_and_batch(make_llm):
    llm, client = make_llm(lambda p: message(text="ok"))
    reqs = [Req(model="claude-haiku-5-5", system="s", user=f"u{i}", purpose="t") for i in range(3)]
    first = llm.run(reqs)
    assert [r["text"] for r in first] == ["ok"] * 3
    assert len(client.batched) == 1 and len(client.batched[0]) == 3 and not client.created
    # same requests again: answered from cache, nothing sent
    llm.run(reqs)
    assert len(client.batched) == 1
    # a fresh LLM on the same cache directory also pays nothing
    llm2, client2 = make_llm(lambda p: message(text="other"))
    llm2.cache = llm.cache
    assert [r["text"] for r in llm2.run(reqs)] == ["ok"] * 3 and not client2.batched


def test_prompt_caching_marker():
    p = Req(model="m", system="SYS", user="u", tools=[{"name": "t"}]).anthropic_params()
    assert p["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]


def test_batch_left_running_is_collected_next_run(make_llm, monkeypatch):
    monkeypatch.setattr(llm_mod, "BATCH_WAIT_MIN", 0)
    llm, client = make_llm(lambda p: message(text="late"), rounds=10)
    reqs = [Req(model="claude-haiku-5-5", system="s", user=f"u{i}", purpose="t") for i in range(2)]
    assert llm.run(reqs) == [None, None]          # still running: nothing answered, nothing lost
    assert llm.run(reqs) == [None, None]          # and not submitted a second time
    assert len(client.batched) == 1
    nxt, _ = make_llm(lambda p: message(text="never sent"))
    nxt.cache, nxt._client = llm.cache, client
    client.rounds = 0                              # the batch has finished by the next run
    assert [r["text"] for r in nxt.run(reqs)] == ["late", "late"]
    assert len(client.batched) == 1


def test_per_item_caches_each_item(make_llm):
    seen = []

    def answer(p):
        items = json.loads(p["messages"][0]["content"])
        seen.append(len(items))
        return message(text=json.dumps({"results": [{"i": it["i"], "v": it["x"] * 2} for it in items]}))

    llm, _ = make_llm(answer)
    build = lambda chunk: Req(model="claude-haiku-5-5", system="s", purpose="pi",  # noqa: E731
                              user=json.dumps([{"i": n, **it} for n, it in enumerate(chunk)]))
    parse = lambda res, chunk: {r["i"]: r["v"] for r in json.loads(res["text"])["results"]}  # noqa: E731
    items = [{"x": i} for i in range(5)]
    assert llm.per_item("ns", items, lambda it: it, 2, build, parse) == [0, 2, 4, 6, 8]
    # one new item among the old ones: only the new one is sent
    seen.clear()
    assert llm.per_item("ns", items + [{"x": 9}], lambda it: it, 2, build, parse) == [0, 2, 4, 6, 8, 18]
    assert seen == [1]


class FakeHttp:
    def __init__(self, status=200, text='{"results": [{"i": 0, "zh": "戴德生"}]}'):
        self.status, self.text, self.calls = status, text, 0

    def post(self, url, json=None, headers=None):
        self.calls += 1
        body = {"candidates": [{"content": {"parts": [{"text": self.text}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
        return NS(status_code=self.status, text="quota", json=lambda: body,
                  raise_for_status=lambda: None)


def test_gemini_used_then_falls_back_to_claude_on_quota(make_llm):
    llm, client = make_llm(lambda p: message(text="claude"), gemini_http=FakeHttp())
    r = Req(model="gemini-3.8-flash", fallback="claude-haiku-5-5", system="s", user="a", purpose="tr")
    assert llm.one(r)["model"] == "gemini-3.8-flash" and not client.created
    llm2, client2 = make_llm(lambda p: message(text="claude"), gemini_http=FakeHttp(status=429))
    r2 = Req(model="gemini-3.8-flash", fallback="claude-haiku-5-5", system="s", user="b", purpose="tr")
    assert llm2.one(r2)["text"] == "claude" and not llm2.gemini_ok and len(client2.created) == 1


PAGE = {"url": "https://example.org/a", "title": "A",
        "text": "Hangchow mission. The Tianshui Church was built in 1876 by the mission at Hangchow. Hangchow again."}


def extraction(excerpt):
    return {"sites": [{"name_en": "Tianshui Church", "site_type": "church", "city": "hangzhou",
                       "evidence": [{"source_url": PAGE["url"], "excerpt": excerpt}]}], "persons": []}


def test_extraction_escalates_when_excerpts_are_not_in_the_page(make_llm):
    def answer(p):
        good = p["model"] == "claude-sonnet-5-5"
        ex = extraction("The Tianshui Church was built in 1876" if good else "a sentence the page does not contain")
        return message(tool=ex, model=p["model"])

    llm, client = make_llm(answer)
    llm.cache = llm.cache
    req = extract.request(PAGE)
    res = llm.one(req)
    ex = extract.parse(res, PAGE)
    assert extract.needs_escalation(res, ex, PAGE, ["hangchow"]).startswith("only 0/1")
    final = extract.extract_page(PAGE, llm)
    assert final.sites[0].evidence[0].excerpt.startswith("The Tianshui Church")


def test_extraction_keeps_a_good_cheap_result(make_llm):
    llm, client = make_llm(lambda p: message(tool=extraction("The Tianshui Church was built in 1876"), model=p["model"]))
    ex = extract.extract_page(PAGE, llm)
    assert ex.sites and all(c["model"] == extract.EXTRACT_MODEL for c in client.created)


def test_translation_prefers_glossary_and_wikidata(make_llm):
    asked = []

    def answer(p):
        items = json.loads(p["messages"][0]["content"])
        asked.extend(it["text"] for it in items)
        return message(text=json.dumps({"results": [{"i": it["i"], "zh": "译" + it["text"][:3]} for it in items]}))

    llm, _ = make_llm(answer)
    gloss = {"tianshui church": [{"zh": "天水堂", "birth": None, "death": None, "city": "hangzhou"}],
             "trinity church": [{"zh": "三一堂", "birth": None, "death": None, "city": "ningbo"}]}
    ex = Extraction(document_url="u",
                    sites=[SiteMention(name_en="Tianshui Church", city="hangzhou"),
                           SiteMention(name_en="Trinity Church", city="shanghai")],
                    persons=[PersonMention(name_en="Hudson Taylor", birth_year=1832, death_year=1905),
                             PersonMention(name_en="John Doe", birth_year=1850, death_year=1900)])
    wd = lambda name, b, d: "戴德生" if name == "Hudson Taylor" else None  # noqa: E731
    stats = translate.translate_all([ex], llm, gloss, wd)
    assert ex.sites[0].name_zh == "天水堂"            # glossary, same city
    assert ex.sites[1].name_zh != "三一堂"            # same name in another city: not reused
    assert ex.persons[0].name_zh == "戴德生"          # Wikidata with matching years
    assert "Hudson Taylor" not in asked and "Tianshui Church" not in asked
    assert stats["glossary"] == 1 and stats["wikidata"] == 1
    assert all(x.name_zh_status == "translated" for x in [*ex.sites, *ex.persons])
    assert ex.translated


def test_translation_left_incomplete_is_retried(make_llm):
    llm, _ = make_llm(lambda p: message(text="not json"))
    ex = Extraction(document_url="u", persons=[PersonMention(name_en="Jane Roe")])
    translate.translate_all([ex], llm, {}, lambda *a: None)
    assert not ex.translated and ex.persons[0].name_zh is None


def test_wikidata_requires_unique_match_on_both_years():
    class H:
        def __init__(self, ents):
            self.ents = ents

        def get(self, url, params):
            if params["action"] == "wbsearchentities":
                return NS(json=lambda: {"search": [{"id": k} for k in self.ents]})
            return NS(json=lambda: {"entities": self.ents})

    def person(zh, b, d):
        t = lambda y: [{"mainsnak": {"datavalue": {"value": {"time": f"+{y}-01-01T00:00:00Z"}}}}]  # noqa: E731
        return {"labels": {"zh-hans": {"value": zh}},
                "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}], "P569": t(b), "P570": t(d)}}

    assert translate.wikidata_person("Hudson Taylor", 1832, 1905, H({"Q1": person("戴德生", 1832, 1905)})) == "戴德生"
    assert translate.wikidata_person("Hudson Taylor", 1832, 1906, H({"Q1": person("戴德生", 1832, 1905)})) is None
    assert translate.wikidata_person("Hudson Taylor", None, 1905, H({"Q1": person("戴德生", 1832, 1905)})) is None
    two = H({"Q1": person("甲", 1832, 1905), "Q2": person("乙", 1832, 1905)})
    assert translate.wikidata_person("John Smith", 1832, 1905, two) is None


def test_excerpt_matcher_accepts_elisions_and_script_but_not_stitching():
    from mlm.verify import excerpt_found
    text = ("稻惟德医师是英国内地会医疗传教士，生于1848年。1874年来华，作为中国内地会传教士，起初在浙江绍兴宣教。"
            + "x" * 3000 + "后于1880年应邀前往温州。")
    assert excerpt_found("稻惟德医师是英国内地会医疗传教士……起初在浙江绍兴宣教", text)
    assert excerpt_found("1915年，金陵女子大學在南京開學", "1915年，金陵女子大学在南京开学。")
    assert not excerpt_found("起初在浙江绍兴宣教……生于1848年", text)        # wrong order
    assert not excerpt_found("生于1848年……后于1880年应邀前往温州", text)      # pieces too far apart
    assert not excerpt_found("生于1848年……他发明了电话", text)                 # a piece not in the source
    assert not excerpt_found("生于1848年……绍", text)                          # a piece too short to mean anything


def test_snap_fixes_a_dropped_word_only_when_close():
    from mlm.extract import snap_excerpts
    text = "In 1876 the new Tianshui Church was built by the American Presbyterian mission outside the gate."
    ex = Extraction.model_validate({"document_url": "u", "sites": [{"name_en": "x", "evidence": [
        {"source_url": "u", "excerpt": "In 1876 the Tianshui Church was built by the American Presbyterian mission"},
        {"source_url": "u", "excerpt": "In 1890 the Gulou Church was destroyed by a great fire in the city"}]}]})
    assert snap_excerpts(ex, text) == 1
    ev = ex.sites[0].evidence
    assert ev[0].excerpt in text and ev[1].excerpt.startswith("In 1890")


def test_bad_answer_is_not_cached_and_items_are_retried_smaller(make_llm):
    sizes = []

    def answer(p):
        items = json.loads(p["messages"][0]["content"])
        sizes.append(len(items))
        if len(items) > 2:  # a long answer comes back broken
            return message(text='{"results": [{"i": 0, "v": 1} {"i": 1}]}')
        return message(text=json.dumps({"results": [{"i": it["i"], "v": it["x"] * 2} for it in items]}))

    llm, _ = make_llm(answer)
    build = lambda chunk: Req(model="claude-haiku-5-5", system="s", purpose="pi",  # noqa: E731
                              user=json.dumps([{"i": n, **it} for n, it in enumerate(chunk)]))
    parse = lambda res, chunk: {r["i"]: r["v"] for r in json.loads(res["text"])["results"]}  # noqa: E731
    items = [{"x": i} for i in range(8)]
    assert llm.per_item("ns", items, lambda it: it, 8, build, parse) == [i * 2 for i in range(8)]
    assert sizes[0] == 8 and max(sizes[1:]) <= 2
    # the broken answer was dropped from the cache, so it can't be replayed by a later run
    assert not llm.cache.get_many([build(items).key])


def test_store_retries_only_repeatable_requests():
    import httpx

    from mlm.store import RetryTransport
    calls = []

    class Flaky(RetryTransport):
        def __init__(self):
            super().__init__(tries=3, backoff_s=0)

    def fake(self, request):
        calls.append(request.method)
        return httpx.Response(502 if len(calls) < 3 else 200, request=request)

    t = Flaky()
    httpx.HTTPTransport.handle_request, orig = fake, httpx.HTTPTransport.handle_request
    try:
        c = httpx.Client(base_url="https://x.supabase.co/rest/v1", transport=t)
        assert c.patch("/claims", params={"id": "eq.1"}, json={}).status_code == 200 and calls == ["PATCH"] * 3
        calls.clear()
        assert c.post("/claims", json=[{}]).status_code == 502 and calls == ["POST"]          # plain insert: once
        calls.clear()
        assert c.post("/llm_cache", params={"on_conflict": "key"}, json=[{}]).status_code == 200 and len(calls) == 3
    finally:
        httpx.HTTPTransport.handle_request = orig
