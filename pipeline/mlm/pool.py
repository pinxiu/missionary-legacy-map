"""The resource pool: what has been fetched, and what has already been through extraction.

A page is identified by the hash of its normalised text (teasers stripped, whitespace collapsed, Traditional folded to
Simplified), so the same article found by a Fujian search and a Guangdong search, or re-crawled with a changed
"related links" block, is one entry. A page that is `extracted` or `loaded` is skipped by every later run: that is how
the agents widen coverage (more provinces, more cities) without paying to read the same document twice.

Statuses: new → extracted → loaded.  `failed` is retried.
"""

from __future__ import annotations

import hashlib
import re

from .config import notice

DONE = ("extracted", "loaded")


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
        self._status: dict[str, str] | None = None

    def statuses(self) -> dict[str, str]:
        if self._status is None:
            self._status = {r["hash"]: r["status"] for r in self.store.select_all("resource_pool", select="hash,status")}
        return self._status

    def known(self, h: str) -> bool:
        return self.statuses().get(h) in DONE

    def add(self, page: dict, found_for: str) -> tuple[str, bool]:
        """Record a fetched page. Returns (hash, needs_work): False when this text was already extracted or loaded."""
        h = resource_hash(page["text"])
        if self.known(h):
            return h, False
        if h not in self.statuses():
            self.store.upsert("resource_pool", [{
                "hash": h, "url": page["url"], "title": page.get("title"), "kind": page.get("kind"), "lang": page.get("lang"),
                "source_name": page.get("source_name"), "found_for": [found_for], "text": page["text"], "chars": len(page["text"]),
                "status": "new",
            }], "hash")
            self.statuses()[h] = "new"
        return h, True

    def mark(self, h: str, status: str, locales: list[str] | None = None) -> None:
        vals: dict = {"status": status}
        if status == "extracted":
            import datetime as dt
            vals["extracted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        if locales is not None:
            vals["locales"] = sorted(set(locales))
        self.store.update("resource_pool", {"hash": h}, vals)
        self.statuses()[h] = status

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for st in self.statuses().values():
            out[st] = out.get(st, 0) + 1
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
