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

    def select_all(self, table: str, page: int = 1000, **params) -> list[dict]:
        """Every matching row, paged (PostgREST caps a single response, usually at 1000 rows)."""
        out: list[dict] = []
        while True:
            rows = self.select(table, order="id", limit=str(page), offset=str(len(out)), **params)
            out += rows
            if len(rows) < page:
                return out

    def update(self, table: str, match: dict, values: dict) -> None:
        params = {k: f"eq.{v}" for k, v in match.items()}
        r = self.http.patch(f"/{table}", params=params, json=values)
        r.raise_for_status()

    def delete(self, table: str, match: dict) -> None:
        r = self.http.delete(f"/{table}", params={k: f"eq.{v}" for k, v in match.items()})
        r.raise_for_status()

    def delete_in(self, table: str, column: str, values: list[str], extra: dict | None = None) -> None:
        params = {column: f"in.({','.join(values)})"} | {k: f"eq.{v}" for k, v in (extra or {}).items()}
        r = self.http.delete(f"/{table}", params=params)
        r.raise_for_status()

    # --- storage (photos) ---
    def _storage(self) -> httpx.Client:
        base = str(self.http.base_url).replace("/rest/v1", "/storage/v1")
        return httpx.Client(base_url=base, headers={k: v for k, v in self.http.headers.items() if k.lower() != "content-type"}, timeout=60)

    def sign_url(self, bucket: str, path: str, expires_s: int = 30 * 24 * 3600) -> str:
        with self._storage() as s:
            r = s.post(f"/object/sign/{bucket}/{path}", json={"expiresIn": expires_s})
            r.raise_for_status()
            return str(s.base_url).rstrip("/") + r.json()["signedURL"].removeprefix("/storage/v1")

    def download(self, bucket: str, path: str) -> bytes:
        with self._storage() as s:
            r = s.get(f"/object/{bucket}/{path}")
            r.raise_for_status()
            return r.content

    def upload(self, bucket: str, path: str, data: bytes, content_type: str = "image/jpeg") -> None:
        with self._storage() as s:
            r = s.post(f"/object/{bucket}/{path}", content=data, headers={"Content-Type": content_type, "x-upsert": "true"})
            r.raise_for_status()

    def remove(self, bucket: str, paths: list[str]) -> None:
        with self._storage() as s:
            r = s.request("DELETE", f"/object/{bucket}", json={"prefixes": paths})
            r.raise_for_status()
