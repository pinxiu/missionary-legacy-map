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


class RetryTransport(httpx.HTTPTransport):
    """Retry a request that hit a passing server or network error (502/503/504, a dropped connection), with backoff.
    Only requests that are safe to repeat: reads, updates and deletes by filter, and upserts (POST with on_conflict).
    A plain insert is not retried, since the first attempt may have written the row. One 502 from Supabase mid-run
    used to fail a whole pipeline run (Oct 15)."""

    def __init__(self, tries: int = 4, backoff_s: float = 2.0, **kw):
        super().__init__(**kw)
        self.tries, self.backoff_s = tries, backoff_s

    @staticmethod
    def repeatable(request: httpx.Request) -> bool:
        return request.method in ("GET", "HEAD", "PATCH", "DELETE") or \
            (request.method == "POST" and "on_conflict" in request.url.params)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import time
        if not self.repeatable(request):
            return super().handle_request(request)
        for attempt in range(self.tries):
            last = attempt == self.tries - 1
            try:
                resp = super().handle_request(request)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout):
                if last:
                    raise
            else:
                if resp.status_code not in (502, 503, 504) or last:
                    return resp
                resp.close()
            time.sleep(self.backoff_s * (2 ** attempt))
        raise RuntimeError("unreachable")


class Store:
    def __init__(self, url: str | None = SUPABASE_URL, key: str | None = SUPABASE_SECRET_KEY):
        self.http = httpx.Client(base_url=f"{(url or '').rstrip('/')}/rest/v1", headers=auth_headers(url, key), timeout=30,
                                 transport=RetryTransport())

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

    # Unique column per table, for stable paging (everything else has an `id`).
    PAGE_KEY = {"resource_pool": "hash", "extractions": "hash", "llm_cache": "key", "app_settings": "key", "regions": "slug"}

    def _get(self, table: str, params: dict) -> list[dict]:
        r = self.http.get(f"/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def select(self, table: str, page: int = 1000, **params) -> list[dict]:
        """Matching rows. PostgREST caps one response (1000 rows) and truncates silently, which once hid the newest sites
        from the coverage counts, so this pages through the whole result unless the caller passes its own `limit`."""
        if "limit" in params or "offset" in params:
            return self._get(table, params)
        key = self.PAGE_KEY.get(table, "id")
        order = f"{params.pop('order')},{key}" if "order" in params else key
        out: list[dict] = []
        while True:
            rows = self._get(table, {**params, "order": order, "limit": str(page), "offset": str(len(out))})
            out += rows
            if len(rows) < page:
                return out

    def select_all(self, table: str, page: int = 1000, order: str | None = None, **params) -> list[dict]:
        """Every matching row (same as `select` without a limit; kept for existing callers)."""
        return self.select(table, page=page, **({"order": order} if order and order != self.PAGE_KEY.get(table, "id") else {}), **params)

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
