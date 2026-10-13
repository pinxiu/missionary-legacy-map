"""Ask the live map to reload its data right after something is published (Next.js on-demand revalidation)."""

from __future__ import annotations

import httpx

from .config import notice


def refresh(store) -> bool:
    """Never fails the calling workflow: the map also refreshes itself daily."""
    try:
        cfg = {r["key"]: r["value"] for r in store.select("app_settings", select="key,value")}
        url, token = cfg.get("site_url"), cfg.get("revalidate_token")
        if not (url and token):
            notice("refresh-site: site_url / revalidate_token not set in app_settings; skipped", "warning")
            return False
        r = httpx.post(f"{url.rstrip('/')}/api/revalidate", headers={"x-revalidate-token": token}, timeout=30)
        ok = r.status_code == 200
        notice(f"refresh-site: {'map refreshed' if ok else f'HTTP {r.status_code}'}", "notice" if ok else "warning")
        return ok
    except Exception as e:  # noqa: BLE001
        notice(f"refresh-site failed ({type(e).__name__}); the map will refresh on its daily schedule", "warning")
        return False
