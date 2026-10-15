"""Every model call in the pipeline goes through here.

Cost controls, in the order they apply:
  1. Result cache. A request is keyed by a hash of everything that shapes the answer (model, system prompt, tools,
     user message, max_tokens). A hit costs nothing. The cache lives in .mlm-work (fast, per runner) and in the
     `llm_cache` table (durable, shared by every run), so a re-run, a lost Actions cache or a re-crawled but unchanged
     page never pays twice.
  2. Per-item cache (`per_item`). Checks that look at many small items in one request (claims, places, phrases to
     translate) cache each item's verdict on its own, so a batch that shifts by one new item doesn't re-pay for the rest.
  3. Message Batches. Uncached Anthropic requests go out as one batch at half price. A batch still running when the
     wait ends is left in flight; the next run collects its results instead of paying again.
  4. Gemini's free tier, for tasks it does well (translation), with an automatic switch to Claude when it is
     unavailable or out of quota.
  5. Prompt caching on the system prompt (and the tools before it) for anything that does go out.

Results are plain dicts: {"text", "tool_input", "stop_reason", "model", "usage"}. A request that could not be answered
in this run returns None; callers leave that work for the next run, as they already did for failed calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import ANTHROPIC_API_KEY, WORK_DIR, notice

CACHE_VERSION = 1
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or None
BATCH = os.environ.get("MLM_BATCH", "1") != "0"  # MLM_BATCH=0 sends requests one by one (local debugging)
BATCH_WAIT_MIN = float(os.environ.get("MLM_BATCH_WAIT_MIN", "150"))
BATCH_POLL_S = float(os.environ.get("MLM_BATCH_POLL_S", "30"))
CACHE_DIR = WORK_DIR / "llm-cache"

# USD per million tokens (input, output), standard tier, for the cost line printed after each stage. Batch halves both.
# Cache reads are billed at a fraction of input; usage reports them separately and they are priced at 10% here.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "gemini-free": (0.0, 0.0),
}


@dataclass
class Req:
    model: str
    system: str
    user: str
    max_tokens: int = 4000
    tools: list[dict] | None = None
    purpose: str = ""
    fallback: str | None = None  # Claude model to use when a Gemini request can't be served
    json_out: bool = False       # ask Gemini for a JSON response

    @property
    def key(self) -> str:
        blob = json.dumps([CACHE_VERSION, self.model, self.system, self.user, self.max_tokens, self.tools],
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def is_gemini(self) -> bool:
        return self.model.startswith("gemini")

    def anthropic_params(self) -> dict:
        p: dict = {
            "model": self.model, "max_tokens": self.max_tokens,
            # Tools come before the system prompt in the cached prefix, so this one marker covers both.
            "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": self.user}],
        }
        if self.tools:
            p["tools"] = self.tools
            p["tool_choice"] = {"type": "auto"}  # forced tool choice is not supported by every model
        return p


def item_key(namespace: str, payload) -> str:
    blob = json.dumps([CACHE_VERSION, namespace, payload], ensure_ascii=False, sort_keys=True)
    return "item:" + hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- cache

class Cache:
    """Local files plus the `llm_cache` table. Either side may be missing; the table is optional."""

    def __init__(self, store=None, local: Path = CACHE_DIR):
        self.local = local
        self.store = store

    def _path(self, key: str) -> Path:
        k = key.replace(":", "_")
        return self.local / k[-2:] / f"{k}.json"

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        missing = []
        for k in dict.fromkeys(keys):
            p = self._path(k)
            if p.exists():
                try:
                    out[k] = json.loads(p.read_text())
                    continue
                except ValueError:
                    pass
            missing.append(k)
        if missing and self.store is not None:
            for i in range(0, len(missing), 80):
                chunk = missing[i:i + 80]
                try:
                    rows = self.store.select("llm_cache", select="key,result", key=f"in.({','.join(chunk)})")
                except Exception as e:  # noqa: BLE001 - the table is an optimisation; carry on without it
                    notice(f"llm cache: table read failed ({type(e).__name__}); using local cache only", "warning")
                    self.store = None
                    break
                for r in rows:
                    out[r["key"]] = r["result"]
                    self._write_local(r["key"], r["result"])
        return out

    def _write_local(self, key: str, value: dict) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False))

    def put_many(self, items: dict[str, dict], model: str = "", purpose: str = "") -> None:
        if not items:
            return
        for k, v in items.items():
            self._write_local(k, v)
        if self.store is not None:
            rows = [{"key": k, "model": model or (v.get("model") if isinstance(v, dict) else None) or "",
                     "purpose": purpose, "result": v} for k, v in items.items()]
            try:
                for i in range(0, len(rows), 200):
                    self.store.upsert("llm_cache", rows[i:i + 200], on_conflict="key")
            except Exception as e:  # noqa: BLE001
                notice(f"llm cache: table write failed ({type(e).__name__}); kept locally", "warning")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        if self.store is not None:
            try:
                self.store.delete("llm_cache", {"key": key})
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- usage / cost log

@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    batched: int = 0
    in_tok: int = 0
    out_tok: int = 0
    cache_read: int = 0
    usd: float = 0.0
    models: set = field(default_factory=set)

    def add(self, result: dict, batched: bool) -> None:
        u = result.get("usage") or {}
        model = result.get("model") or ""
        i, o, cr = u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0)
        cw = u.get("cache_creation_input_tokens", 0)
        self.calls += 1
        self.batched += batched
        self.in_tok += i + cw
        self.out_tok += o
        self.cache_read += cr
        self.models.add(model)
        price = next((v for k, v in PRICES.items() if model.startswith(k)), None)
        if model.startswith("gemini"):
            price = PRICES["gemini-free"]
        if price:
            usd = (i * price[0] + cw * price[0] * 1.25 + cr * price[0] * 0.1 + o * price[1]) / 1e6
            self.usd += usd * (0.5 if batched else 1.0)


# ---------------------------------------------------------------- the client

def _now_iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


class LLM:
    def __init__(self, store=None, anthropic_client=None, http=None):
        if store is None:
            from .config import SUPABASE_SECRET_KEY, SUPABASE_URL
            if SUPABASE_URL and SUPABASE_SECRET_KEY:
                try:
                    from .store import Store
                    store = Store()
                except Exception:  # noqa: BLE001
                    store = None
        self.cache = Cache(store)
        self._client = anthropic_client
        self._http = http
        self.gemini_ok = bool(GEMINI_API_KEY)
        self.usage: dict[str, Usage] = defaultdict(Usage)
        self.in_flight: set[str] = set()
        self._collected = False

    # -- providers
    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        return self._client

    @property
    def http(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=120)
        return self._http

    @staticmethod
    def _from_message(msg) -> dict:
        content = list(getattr(msg, "content", []) or [])
        text = "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text")
        tool = next((b for b in content if getattr(b, "type", "") == "tool_use"), None)
        usage = getattr(msg, "usage", None)
        u = {}
        if usage is not None:
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                u[k] = getattr(usage, k, 0) or 0
        return {"text": text, "tool_input": dict(tool.input) if tool is not None else None,
                "stop_reason": getattr(msg, "stop_reason", None), "model": getattr(msg, "model", ""), "usage": u}

    def _anthropic_one(self, req: Req) -> dict:
        return self._from_message(self.client.messages.create(**req.anthropic_params()))

    def _gemini_one(self, req: Req) -> dict:
        body: dict = {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.user}]}],
            "generationConfig": {"maxOutputTokens": req.max_tokens, "temperature": 0},
        }
        if req.json_out:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{req.model}:generateContent"
        r = self.http.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY or ""})
        if r.status_code == 429:
            raise GeminiQuota(r.text[:300])
        r.raise_for_status()
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        if not text:
            raise RuntimeError(f"empty Gemini response ({cand.get('finishReason')})")
        um = data.get("usageMetadata") or {}
        return {"text": text, "tool_input": None, "stop_reason": cand.get("finishReason"), "model": req.model,
                "usage": {"input_tokens": um.get("promptTokenCount", 0), "output_tokens": um.get("candidatesTokenCount", 0)}}

    # -- batches
    def collect_pending(self) -> None:
        """Pick up batches an earlier run left in flight: finished ones fill the cache, running ones are skipped."""
        if self._collected or not ANTHROPIC_API_KEY:
            return
        self._collected = True
        markers: list[dict] = []
        if self.cache.store is not None:
            try:
                markers = self.cache.store.select("llm_cache", select="key,result", key="like.batch:*")
            except Exception:  # noqa: BLE001
                markers = []
        for p in sorted(self.cache.local.glob("*/batch_*.json")) if self.cache.local.exists() else []:
            try:
                markers.append({"key": p.stem.replace("_", ":", 1), "result": json.loads(p.read_text())})
            except ValueError:
                continue
        seen = set()
        for m in markers:
            if m["key"] in seen:
                continue
            seen.add(m["key"])
            bid = m["key"].split(":", 1)[1]
            try:
                b = self.client.messages.batches.retrieve(bid)
            except Exception as e:  # noqa: BLE001
                notice(f"llm: could not check batch {bid} ({type(e).__name__})", "warning")
                continue
            if b.processing_status != "ended":
                self.in_flight.update(m["result"].get("keys", []))
                continue
            got = self._read_batch(bid, m["result"].get("purpose", ""))
            notice(f"llm: collected {got} result(s) from an earlier batch {bid}")
            self.cache.delete(m["key"])

    def _read_batch(self, bid: str, purpose: str) -> int:
        out: dict[str, dict] = {}
        for item in self.client.messages.batches.results(bid):
            if getattr(item.result, "type", "") == "succeeded":
                res = self._from_message(item.result.message)
                out[item.custom_id] = res
                self.usage[purpose or "batch"].add(res, batched=True)
        self.cache.put_many(out, purpose=purpose)
        return len(out)

    def _run_batch(self, reqs: list[Req]) -> dict[str, dict]:
        purpose = reqs[0].purpose
        batch = self.client.messages.batches.create(
            requests=[{"custom_id": r.key, "params": r.anthropic_params()} for r in reqs])
        marker = f"batch:{batch.id}"
        self.cache.put_many({marker: {"keys": [r.key for r in reqs], "purpose": purpose, "submitted": _now_iso()}},
                            model="batch", purpose=purpose)
        notice(f"llm[{purpose}]: submitted {len(reqs)} request(s) as batch {batch.id}")
        deadline = time.time() + BATCH_WAIT_MIN * 60
        while True:
            b = self.client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            if time.time() > deadline:
                notice(f"llm[{purpose}]: batch {batch.id} still running after {BATCH_WAIT_MIN:.0f} min; "
                       "the next run collects it", "warning")
                self.in_flight.update(r.key for r in reqs)
                return {}
            time.sleep(BATCH_POLL_S)
        self._read_batch(batch.id, purpose)
        self.cache.delete(marker)
        return self.cache.get_many([r.key for r in reqs])

    # -- public API
    def run(self, reqs: list[Req]) -> list[dict | None]:
        """Answer every request: cache first, then Gemini (with fallback), then Claude as one batch."""
        if not reqs:
            return []
        self.collect_pending()
        purpose = reqs[0].purpose or "llm"
        use = self.usage[purpose]
        found = self.cache.get_many([r.key for r in reqs])
        use.cached += sum(1 for r in reqs if r.key in found)
        todo = list({r.key: r for r in reqs if r.key not in found and r.key not in self.in_flight}.values())

        claude: list[Req] = []
        for r in todo:
            if not r.is_gemini:
                claude.append(r)
                continue
            if self.gemini_ok:
                try:
                    res = self._gemini_one(r)
                    self.cache.put_many({r.key: res}, model=r.model, purpose=r.purpose)
                    found[r.key] = res
                    use.add(res, batched=False)
                    continue
                except GeminiQuota as e:
                    self.gemini_ok = False  # stop asking for the rest of this run
                    notice(f"llm[{purpose}]: Gemini free quota reached ({str(e)[:120]}); switching to Claude", "warning")
                except Exception as e:  # noqa: BLE001
                    notice(f"llm[{purpose}]: Gemini call failed ({type(e).__name__}: {str(e)[:150]}); using Claude", "warning")
            if r.fallback:
                claude.append(Req(model=r.fallback, system=r.system, user=r.user, max_tokens=r.max_tokens,
                                  tools=r.tools, purpose=r.purpose))
        if claude:
            alias = {r.key: r for r in claude}
            done = self.cache.get_many(list(alias))  # a fallback may already be cached
            use.cached += len(done)
            rest = [r for k, r in alias.items() if k not in done and k not in self.in_flight]
            if rest and ANTHROPIC_API_KEY:
                if BATCH and len(rest) > 1:
                    done |= self._run_batch(rest)
                else:
                    for r in rest:
                        try:
                            res = self._anthropic_one(r)
                        except Exception as e:  # noqa: BLE001 - leave it for the next run
                            notice(f"llm[{purpose}]: {type(e).__name__}: {str(e)[:200]}", "warning")
                            continue
                        self.cache.put_many({r.key: res}, model=r.model, purpose=r.purpose)
                        done[r.key] = res
                        use.add(res, batched=False)
            # map fallback answers back onto the original (Gemini) requests
            for orig in todo:
                if orig.key in found or not orig.is_gemini or not orig.fallback:
                    continue
                fb = Req(model=orig.fallback, system=orig.system, user=orig.user, max_tokens=orig.max_tokens,
                         tools=orig.tools, purpose=orig.purpose)
                if fb.key in done:
                    found[orig.key] = done[fb.key]
            found |= done
        return [found.get(r.key) for r in reqs]

    def one(self, req: Req) -> dict | None:
        return self.run([req])[0]

    def per_item(self, namespace: str, items: list, payload: Callable, chunk: int,
                 build: Callable[[list], Req], parse: Callable[[dict, list], dict[int, object]]) -> list:
        """Answer many small items with few requests, caching each item's answer on its own.

        payload(item) -> what identifies the item's answer (cache key material); build(chunk_items) -> Req;
        parse(result, chunk_items) -> {index_in_chunk: answer}. Returns answers aligned with items (None if missing)."""
        keys = [item_key(namespace, payload(it)) for it in items]
        known = self.cache.get_many(keys)
        self.usage[namespace].cached += sum(1 for k in keys if k in known)
        pending = [n for n, k in enumerate(keys) if k not in known]
        uniq: dict[str, int] = {}
        for n in pending:
            uniq.setdefault(keys[n], n)
        order = list(uniq.values())
        chunks = [order[i:i + chunk] for i in range(0, len(order), chunk)]
        results = self.run([build([items[n] for n in c]) for c in chunks])
        new: dict[str, dict] = {}
        for c, res in zip(chunks, results, strict=True):
            if res is None:
                continue
            try:
                parsed = parse(res, [items[n] for n in c])
            except Exception as e:  # noqa: BLE001 - unparseable answer: leave these items for the next run
                notice(f"llm[{namespace}]: could not parse an answer ({type(e).__name__}: {str(e)[:120]})", "warning")
                continue
            for j, n in enumerate(c):
                if j in parsed:
                    new[keys[n]] = {"v": parsed[j]}
        self.cache.put_many(new, model="item", purpose=namespace)
        known |= new
        return [known[k]["v"] if k in known else None for k in keys]

    def report(self) -> None:
        for purpose, u in self.usage.items():
            if not (u.calls or u.cached):
                continue
            notice(f"llm[{purpose}]: {u.calls} paid call(s) ({u.batched} batched), {u.cached} from cache, "
                   f"{u.in_tok:,} in / {u.out_tok:,} out tokens, {u.cache_read:,} cache-read, ~${u.usd:.2f} "
                   f"[{', '.join(sorted(m for m in u.models if m)) or '-'}]")


class GeminiQuota(Exception):
    pass


def parse_json(text: str):
    """The JSON object in a model answer (models sometimes wrap it in prose or code fences)."""
    s = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(s) if s else {}


_shared: LLM | None = None


def shared() -> LLM:
    """One LLM per process, so all stages of a run share the cache, in-flight batches and the cost log."""
    global _shared
    if _shared is None:
        _shared = LLM()
    return _shared
