"""Thin Supabase (PostgREST) client using the service role key. Server-side only."""

from __future__ import annotations

import httpx

from .config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL


class Store:
    def __init__(self, url: str | None = SUPABASE_URL, key: str | None = SUPABASE_SERVICE_ROLE_KEY):
        if not url or not key:
            raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to load data.")
        self.http = httpx.Client(
            base_url=f"{url.rstrip('/')}/rest/v1",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=30,
        )

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> list[dict]:
        if not rows:
            return []
        r = self.http.post(
            f"/{table}", params={"on_conflict": on_conflict}, json=rows,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        r = self.http.post(f"/{table}", json=rows, headers={"Prefer": "return=representation"})
        r.raise_for_status()
        return r.json()

    def select(self, table: str, **params) -> list[dict]:
        r = self.http.get(f"/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def update(self, table: str, match: dict, values: dict) -> None:
        params = {k: f"eq.{v}" for k, v in match.items()}
        r = self.http.patch(f"/{table}", params=params, json=values)
        r.raise_for_status()
