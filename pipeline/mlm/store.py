"""Thin Supabase (PostgREST) client using a secret key (sb_secret_...). Server-side only."""

from __future__ import annotations

import httpx

from .config import SUPABASE_SECRET_KEY, SUPABASE_URL


def auth_headers(url: str | None, key: str | None) -> dict[str, str]:
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY must be set to load data.")
    if key.startswith("sb_publishable_"):
        # Publishable keys ship in the public web bundle and run under RLS as `anon`; they must never write.
        raise SystemExit("SUPABASE_SECRET_KEY is a publishable key. Use the sb_secret_ key from Supabase -> API keys.")
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # legacy service_role JWT also needs the Authorization header
        headers["Authorization"] = f"Bearer {key}"
    return headers


class Store:
    def __init__(self, url: str | None = SUPABASE_URL, key: str | None = SUPABASE_SECRET_KEY):
        self.http = httpx.Client(base_url=f"{(url or '').rstrip('/')}/rest/v1", headers=auth_headers(url, key), timeout=30)

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
