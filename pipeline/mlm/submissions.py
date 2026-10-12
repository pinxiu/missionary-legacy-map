"""Turn visitor submissions (Supabase) into GitHub issues. No model calls; runs every 15 minutes.

Each issue gets labels `submission`, `kind:<kind>` and `needs-triage`. The feedback agent then does a
first pass and moves it to `needs-approval`; the owner's `approved` label triggers mlm.apply.
"""

from __future__ import annotations

import json
import os
import subprocess

from .config import notice
from .store import Store

KIND_TITLE = {
    "new_site": ("New site", "新遗迹"), "correction": ("Correction", "更正"), "new_detail": ("New detail", "补充"),
    "photo": ("Photo", "照片"), "feedback": ("Feedback", "反馈"),
}
LABELS = {
    "submission": "1d76db", "needs-triage": "fbca04", "needs-approval": "d93f0b", "approved": "0e8a16",
    "applied": "5319e7", "needs-human": "b60205", "kind:new_site": "c5def5", "kind:correction": "c5def5",
    "kind:new_detail": "c5def5", "kind:photo": "c5def5", "kind:feedback": "c5def5",
}


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def ensure_labels() -> None:
    for name, color in LABELS.items():
        subprocess.run(["gh", "label", "create", name, "--color", color, "--force"], capture_output=True, text=True)


def issue_body(sub: dict, site: dict | None, photo_urls: list[tuple[dict, str]]) -> str:
    p = sub.get("payload") or {}
    lines = [f"**Kind:** {sub['kind']}  ·  **Language:** {sub.get('lang') or '?'}  ·  **Submitted:** {sub['created_at'][:16]} UTC"]
    if site:
        lines.append(f"**Site:** {site['name_en']} {site.get('name_zh') or ''} (`{site['slug']}`)")
    if p.get("name"):
        lines.append(f"**Proposed name:** {p['name']}  ·  **Type:** {p.get('site_type', '?')}")
    if p.get("change"):
        lines.append(f"**Change:** {p['change']}")
    if p.get("lat") is not None:
        lines.append(f"**Pinned location:** {p['lat']}, {p['lng']} (via {p.get('located_by', '?')}"
                     f"{'; search: ' + p['search_label'] if p.get('search_label') else ''}) "
                     f"[OpenStreetMap](https://www.openstreetmap.org/?mlat={p['lat']}&mlon={p['lng']}#map=17/{p['lat']}/{p['lng']})")
    if p.get("years"):
        lines.append(f"**Year(s):** {p['years']}")
    if p.get("text"):
        lines += ["", "**Submitted text:**", "", "> " + str(p["text"]).replace("\n", "\n> ")]
    if p.get("caption"):
        lines.append(f"**Caption:** {p['caption']}  ·  **Year taken:** {p.get('year_taken') or '?'}")
    if p.get("source"):
        lines.append(f"**Source given:** {p['source']}")
    if photo_urls:
        lines += ["", f"**Photos ({len(photo_urls)}):** _private preview links, valid 30 days_", ""]
        for ph, url in photo_urls:
            lines.append(f"- `{ph['photo_id']}`{' · ' + ph['caption'] if ph.get('caption') else ''}")
            lines.append(f"  <img src=\"{url}\" width=\"320\">")
    if sub.get("contact"):
        lines.append(f"**Contact (private repo only):** {sub['contact']}")
    lines += ["", "<details><summary>Raw submission</summary>", "", "```json",
              json.dumps({k: sub[k] for k in ("id", "kind", "site_id", "photo_id", "lang", "payload")}, ensure_ascii=False, indent=2),
              "```", "</details>", "", "---", "_Awaiting the AI first pass (`needs-triage`)._"]
    return "\n".join(lines)


def sync(store: Store | None = None, limit: int = 20) -> int:
    store = store or Store()
    subs = store.select("submissions", select="*", issue_number="is.null", order="created_at", limit=str(limit))
    if not subs:
        notice("submissions: nothing new")
        return 0
    ensure_labels()
    sites = {s["id"]: s for s in store.select("sites", select="id,slug,name_en,name_zh")}
    made = 0
    for sub in subs:
        site = sites.get(sub.get("site_id"))
        photo_urls: list[tuple[dict, str]] = []
        if sub["kind"] == "photo":
            pl = sub.get("payload") or {}
            batch = pl.get("photos") or ([{"photo_id": sub.get("photo_id"), "upload_path": pl["upload_path"],
                                           "caption": pl.get("caption")}] if pl.get("upload_path") else [])
            for ph in batch[:12]:
                try:
                    photo_urls.append((ph, store.sign_url("photo-uploads", ph["upload_path"])))
                except Exception as e:  # noqa: BLE001
                    notice(f"could not sign photo URL: {e}", "warning")
        p = sub.get("payload") or {}
        subject = (site or {}).get("name_en") or p.get("name") or str(p.get("text", ""))[:60] or sub["id"][:8]
        title = f"[{KIND_TITLE[sub['kind']][0]} {KIND_TITLE[sub['kind']][1]}] {subject}"[:200]
        url = gh("issue", "create", "--title", title, "--body", issue_body(sub, site, photo_urls),
                 "--label", "submission", "--label", f"kind:{sub['kind']}", "--label", "needs-triage")
        number = int(url.rstrip("/").split("/")[-1])
        store.update("submissions", {"id": sub["id"]}, {"issue_number": number})
        made += 1
    notice(f"submissions: created {made} issue(s)")
    return made


if __name__ == "__main__":
    os.environ.setdefault("GH_PROMPT_DISABLED", "1")
    sync()
