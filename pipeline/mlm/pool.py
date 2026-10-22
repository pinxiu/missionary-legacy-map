"""The resource pool: what has been found, what the parser has read, and what the parser found on each page.

Three stages meet only here, so each can run (and be fixed) without the others:
  find     fetches pages for a unit's search and adds them to the pool (`resource_pool`)
  parse    reads every pool page once per PARSER_VERSION and records EVERY place on it, whatever unit it belongs to
           (`extractions`); it knows nothing about the unit being worked on
  resolve  matches the parsed places to known sites, files each under its country/region's unit, and pins it

A page is identified by the hash of its normalised text (teasers stripped, whitespace collapsed, Traditional folded to
Simplified), so the same article found by a Fujian search and a Guangdong search, or re-crawled with a changed
"related links" block, is one entry. A page parsed with the current PARSER_VERSION is never read by a model again; one
parsed by an older, narrower parser (e.g. scoped to the unit being worked on) is parsed again in full.

Statuses: new → extracted → loaded.  `failed` is retried.
"""

from __future__ import annotations

import hashlib
import re

from .config import notice

# Bump when the parser prompt or schema changes in a way that should re-read pages.
# 1 = prompt scoped to the unit being worked on; 2 = every place on the page, worldwide.
PARSER_VERSION = 2


def normalize_text(text: str) -> str:
    from .crawl import strip_teasers
    from .verify import _t2s

    s = _t2s()(strip_teasers(text or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def resource_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


class Pool:
    def __init__(self, store):
        self.store = store
        self._meta: dict[str, dict] | None = None

    def _rows(self) -> dict[str, dict]:
        if self._meta is None:
            self._meta = {r["hash"]: r for r in self.store.select_all(
                "resource_pool", order="hash", select="hash,status,parsed_version,found_for,chars,kind,first_seen")}
        return self._meta

    def statuses(self) -> dict[str, str]:
        return {h: r["status"] for h, r in self._rows().items()}

    def parsed(self, h: str) -> bool:
        """Read in full by the current parser (an older parser's reading doesn't count)."""
        row = self._rows().get(h)
        return bool(row) and (row.get("parsed_version") or 0) >= PARSER_VERSION and row["status"] != "failed"

    known = parsed

    def add(self, page: dict, found_for: str) -> tuple[str, bool]:
        """Record a fetched page. Returns (hash, needs_parse): False when this text is already parsed."""
        h = resource_hash(page["text"])
        if self.parsed(h):
            return h, False
        if h not in self._rows():
            row = {"hash": h, "url": page["url"], "title": page.get("title"), "kind": page.get("kind"), "lang": page.get("lang"),
                   "source_name": page.get("source_name"), "found_for": [found_for], "text": page["text"],
                   "chars": len(page["text"]), "status": "new"}
            self.store.upsert("resource_pool", [row], "hash")
            self._rows()[h] = {k: row[k] for k in ("hash", "status", "found_for", "chars", "kind")} | {"parsed_version": None}
        return h, True

    def mark(self, h: str, status: str, locales: list[str] | None = None) -> None:
        vals: dict = {"status": status}
        if status == "extracted":
            import datetime as dt
            vals["extracted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            vals["parsed_version"] = PARSER_VERSION
        if locales is not None:
            vals["locales"] = sorted(set(locales))
        self.store.update("resource_pool", {"hash": h}, vals)
        self._rows().setdefault(h, {"hash": h})
        self._rows()[h].update(vals)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._rows().values():
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    # ---- parse stage: which pages still need reading, and where the result goes --------------------------------------

    def pending_parse(self, limit: int, prefer: str | None = None) -> list[dict]:
        """Up to `limit` pool pages not yet read by the current parser, with their text. Pages found for `prefer` (the unit
        being worked on) come first, then the rest oldest first, so a backlog is worked off a little each run."""
        todo = [r for h, r in self._rows().items() if not self.parsed(h)]
        todo.sort(key=lambda r: (prefer not in (r.get("found_for") or []), str(r.get("first_seen") or ""), r["hash"]))
        pages: list[dict] = []
        chosen = [r["hash"] for r in todo[:limit]]
        for i in range(0, len(chosen), 40):
            pages += self.store.select("resource_pool", select="hash,url,title,kind,text", hash=f"in.({','.join(chosen[i:i + 40])})")
        return sorted(pages, key=lambda p: chosen.index(p["hash"]))

    def save_extraction(self, h: str, ex) -> None:
        """Store what the parser found on a page and mark the page parsed. The places wait for resolution."""
        import json
        self.store.upsert("extractions", [{"hash": h, "version": PARSER_VERSION, "data": json.loads(ex.model_dump_json()),
                                            "resolved_at": None, "unplaced": [], "unplaced_count": 0}], "hash")
        self.mark(h, "extracted", locales=[x for s in ex.sites for x in (s.province, s.city) if x and x != "other"])

    # ---- resolve stage ---------------------------------------------------------------------------------------------------

    def pending_resolution(self, placeable=lambda key: False) -> list[dict]:
        """Extractions not yet resolved, plus those with places that had no unit but `placeable(key)` now says have one."""
        rows = self.store.select("extractions", select="hash,version,data,unplaced", resolved_at="is.null")
        waiting = self.store.select("extractions", select="hash,version,data,unplaced", unplaced_count="gt.0")
        seen = {r["hash"] for r in rows}
        rows += [r for r in waiting if r["hash"] not in seen and any(placeable(k) for k in r.get("unplaced") or [])]
        return rows

    def update_extraction(self, h: str, ex) -> None:
        import json
        self.store.update("extractions", {"hash": h}, {"data": json.loads(ex.model_dump_json())})

    def mark_resolved(self, h: str, unplaced: list[str]) -> None:
        import datetime as dt
        self.store.update("extractions", {"hash": h}, {
            "resolved_at": dt.datetime.now(dt.timezone.utc).isoformat(), "unplaced": sorted(set(unplaced)),
            "unplaced_count": len(set(unplaced))})
        self.mark(h, "loaded")

    def text_of(self, hashes: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(hashes), 40):
            for r in self.store.select("resource_pool", select="hash,text", hash=f"in.({','.join(hashes[i:i + 40])})"):
                out[r["hash"]] = r.get("text") or ""
        return out


def pool_or_none():
    """The pool in the configured database, or None when the database isn't configured (local runs, tests)."""
    from .config import SUPABASE_SECRET_KEY, SUPABASE_URL
    if not (SUPABASE_URL and SUPABASE_SECRET_KEY):
        return None
    try:
        from .store import Store
        pool = Pool(Store())
        pool.statuses()
        return pool
    except Exception as e:  # noqa: BLE001 - e.g. migration not applied yet: run without it rather than fail the unit
        notice(f"pool: unavailable ({type(e).__name__}: {str(e)[:120]}); continuing without it", "warning")
        return None


def backfill(store) -> dict:
    """Enter every document already loaded into the database as `loaded`, so earlier work is never repeated."""
    pool = Pool(store)
    stats = {"documents": 0, "added": 0, "already": 0}
    for d in store.select_all("documents", select="url,text"):
        stats["documents"] += 1
        text = d.get("text") or ""
        if not text:
            continue
        h = resource_hash(text)
        if h in pool.statuses():
            stats["already"] += 1
            continue
        store.upsert("resource_pool", [{"hash": h, "url": d["url"], "text": text, "chars": len(text), "status": "loaded",
                                        "found_for": ["backfill"]}], "hash")
        pool.statuses()[h] = "loaded"
        stats["added"] += 1
    notice(f"pool backfill: {stats}")
    return stats
