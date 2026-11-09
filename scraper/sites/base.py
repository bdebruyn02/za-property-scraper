"""Base class for site adapters. To add a site: subclass SiteAdapter, set `name` and
`domains`, implement `page_url` and `parse`, and add it to SITES in sites/__init__.py."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..core import Blocked, Cancelled, Fetcher, Listing, finalise

log = logging.getLogger("scraper")


class SiteAdapter:
    name: str = "base"
    domains: tuple[str, ...] = ()
    base_url: str = ""
    # Start with headless Chromium instead of requests (for JS-only sites).
    prefer_browser: bool = False

    def __init__(self):
        self.use_browser = self.prefer_browser
        self.blocked: Optional[str] = None     # reason, once the site has refused us

    # ---- per-site hooks -------------------------------------------------
    def page_url(self, url: str, page: int) -> str:
        raise NotImplementedError

    def parse(self, html: str, page_url: str) -> list[Listing]:
        raise NotImplementedError

    def is_empty_results_page(self, html: str) -> bool:
        """True if the page legitimately has zero results (so 0 cards != JS-rendered)."""
        low = html.lower()
        return any(s in low for s in ("no results", "no properties found", "0 results",
                                       "we couldn't find", "no listings found"))

    # ---- shared helpers -------------------------------------------------
    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def absolute(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def fetch(self, fetcher: Fetcher, url: str) -> str:
        """Fetch a page, switching this site to the browser if requests is refused or the
        page arrives without listing cards (JS-rendered). Raises Blocked if both fail."""
        if self.use_browser:
            return fetcher.get_rendered(url)
        try:
            html = fetcher.get(url)
        except Blocked as e:
            if not fetcher.allow_browser:
                raise
            log.warning("  %s refused plain HTTP (%s); retrying with headless browser", self.name, e)
            self.use_browser = True
            return fetcher.get_rendered(url)
        if not self.parse(html, url) and not self.is_empty_results_page(html) and fetcher.allow_browser:
            log.warning("  %s returned no cards via plain HTTP - page may be JS-rendered; "
                        "retrying with headless browser", self.name)
            rendered = fetcher.get_rendered(url)
            if self.parse(rendered, url):
                self.use_browser = True
                return rendered
        return html

    def scrape(self, fetcher: Fetcher, url: str, max_pages: int, search_name: str,
               debug_dir: Optional[Path] = None) -> list[Listing]:
        """Walk result pages until one adds nothing new, or max_pages is reached."""
        found: dict[str, Listing] = {}
        for page in range(1, max_pages + 1):
            purl = self.page_url(url, page)
            try:
                html = self.fetch(fetcher, purl)
            except Cancelled:
                log.warning("  stopped by user")
                break
            except Blocked as e:
                self.blocked = str(e)
                log.error("  BLOCKED by %s: %s - stopping this site.", self.name, e)
                break
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status == 404 and page > 1:           # Private Property 404s past the last page
                    log.info("  page %d: 404 - end of results", page)
                else:
                    log.error("  %s - giving up on this search", e)
                break
            except requests.RequestException as e:
                log.error("  network error (%s) - giving up on this search", e)
                break
            if debug_dir is not None and page == 1:
                debug_dir.mkdir(parents=True, exist_ok=True)
                safe = "".join(c if c.isalnum() else "_" for c in search_name)[:60]
                (debug_dir / f"{self.name}_{safe}_p1.html").write_text(html, encoding="utf-8")
            listings = [finalise(li) for li in self.parse(html, purl)]
            new = 0
            for li in listings:
                if li.key not in found:
                    li.search = search_name
                    found[li.key] = li
                    new += 1
            log.info("  page %d: %d cards, %d new", page, len(listings), new)
            if new == 0:
                break
        return list(found.values())
