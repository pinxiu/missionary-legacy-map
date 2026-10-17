"""Polite crawler: robots.txt aware, per-host delay, content-hash cache.

Output: one JSON file per fetched page in WORK_DIR/raw/<region>/<sha>.json
  {url, title, lang, text, links, fetched_at, source_name, content_hash}
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .config import CRAWL_DELAY_S, USER_AGENT, WORK_DIR, notice

SOURCES_FILE = Path(__file__).with_name("sources.yaml")


def load_registry() -> dict:
    return yaml.safe_load(SOURCES_FILE.read_text())


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[str]
    lang: str | None
    source_name: str
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()



# Headings of "more stories" blocks that list random other pages on every visit. Everything from the heading on is
# dropped, so a page's text (and content hash) only changes when the page itself changes.
TEASER_HEADINGS = {"Other Stories", "其他人物生平"}


def strip_teasers(text: str) -> str:
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() in TEASER_HEADINGS:
            return "\n".join(lines[:i]).rstrip() + "\n"
    return text

def html_to_page(url: str, html: str, source_name: str) -> Page:
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "footer", "header", "noscript", "aside"):
        for node in tree.css(sel):
            node.decompose()
    title = tree.css_first("title").text(strip=True) if tree.css_first("title") else url
    main = tree.css_first("main") or tree.css_first("article") or tree.css_first("#content") or tree.body
    text = main.text(separator="\n", strip=True) if main else ""
    text = strip_teasers(re.sub(r"\n{3,}", "\n\n", text))
    links = []
    for a in tree.css("a[href]"):
        href = urljoin(url, a.attributes.get("href") or "").split("#")[0]
        if href.startswith("http"):
            links.append(href)
    html_node = tree.css_first("html")
    lang = html_node.attributes.get("lang") if html_node else None
    return Page(url=url, title=title, text=text, links=sorted(set(links)), lang=lang, source_name=source_name)


class Crawler:
    def __init__(self, client: httpx.Client | None = None, delay_s: float = CRAWL_DELAY_S):
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
        )
        self.delay_s = delay_s
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(host + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[host] = rp
        return rp.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> str | None:
        host = urlparse(url).netloc
        wait = self.delay_s - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()
        try:
            r = self.client.get(url)
        except httpx.HTTPError as e:
            print(f"  ! {url}: {e}")
            return None
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            print(f"  ! {url}: HTTP {r.status_code} {r.headers.get('content-type', '')}")
            return None
        return r.text

    def crawl_source(self, source: dict) -> list[Page]:
        follow = [re.compile(p) for p in source.get("follow", [])]
        max_depth = source.get("max_depth", 99)  # 1 = start pages plus the links they list
        queue = deque((u, 0) for u in source["start_urls"])
        seen: set[str] = set()
        pages: list[Page] = []
        while queue and len(pages) < source.get("max_pages", 20):
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if not self.allowed(url):
                print(f"  - robots.txt disallows {url}")
                continue
            html = self.fetch(url)
            if html is None:
                continue
            page = html_to_page(url, html, source["name"])
            pages.append(page)
            print(f"  + {url} ({len(page.text)} chars)")
            if depth >= max_depth:
                continue
            for link in page.links:
                if link not in seen and any(p.match(link) for p in follow):
                    queue.append((link, depth + 1))
        return pages


def run(region: str, out_dir: Path | None = None, pool=None, discover_for: dict | None = None) -> Path:
    """Fetch the region's registered sources (and, with `discover_for`, a per-city search) into the raw folder.
    With a resource pool, a page whose text was already extracted or loaded (in any unit) is not written again."""
    from .pool import pool_or_none

    registry = load_registry()["regions"][region]
    out = out_dir or WORK_DIR / "raw" / region
    out.mkdir(parents=True, exist_ok=True)
    crawler = Crawler()
    pool = pool if pool is not None else pool_or_none()
    sources = list(registry["sources"])
    if discover_for:
        from .discover import discovered_sources
        sources += discovered_sources(discover_for, crawler.client)
    stats = {"fetched": 0, "known": 0, "new": 0}
    for source in sources:
        print(f"[crawl] {source['name']}")
        pages = crawler.crawl_source(source)
        notice(f"crawl[{region}] {source['name']}: {len(pages)} pages")
        for page in pages:
            stats["fetched"] += 1
            rec = page.__dict__ | {"content_hash": page.content_hash, "kind": source.get("kind", "web")}
            if pool:
                rec["pool_hash"], needs_work = pool.add(rec, found_for=region)
                if not needs_work:
                    stats["known"] += 1
                    continue
            stats["new"] += 1
            (out / f"{page.content_hash[:16]}.json").write_text(json.dumps(rec, ensure_ascii=False))
    notice(f"crawl[{region}]: {stats}")
    return out
