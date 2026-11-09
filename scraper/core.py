"""Shared model, text-parsing helpers and the polite HTTP fetcher."""
from __future__ import annotations

import datetime as dt
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import requests

log = logging.getLogger("scraper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# --------------------------------------------------------------------------- model
@dataclass
class Listing:
    site: str
    listing_id: str
    url: str
    kind: Optional[str] = None          # "rent" | "sale"
    title: Optional[str] = None
    price: Optional[int] = None         # ZAR, whole rands
    price_text: Optional[str] = None
    price_period: Optional[str] = None  # "month" | "day" | "night" | "week" | None (sale)
    suburb: Optional[str] = None
    address: Optional[str] = None
    property_type: Optional[str] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    parking: Optional[float] = None
    size_m2: Optional[float] = None     # floor size only (never erf/land size)
    available: Optional[str] = None     # ISO date, or "now"
    badges: list[str] = field(default_factory=list)
    agency: Optional[str] = None
    excerpt: Optional[str] = None       # short description snippet from the card
    search: Optional[str] = None        # name of the search that found it
    dupe_of: Optional[str] = None       # "site:id" of a likely cross-site duplicate
    # filled in by analysis.py
    fair_price: Optional[int] = None    # what the market curve says this listing "should" cost
    value_pct: Optional[int] = None     # % below fair price (+ = cheaper than market)
    score: Optional[int] = None         # 0-100 overall value score
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.site}:{self.listing_id}"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- helpers
def clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_price(text: Optional[str]) -> Optional[int]:
    """'R 12 500', 'R12,500 per month', 'R 1 250 000' -> int. 'POA' -> None.
    Requires SA digit grouping so 'R 13 000 2 Bedroom' gives 13000, not 130002."""
    if not text:
        return None
    m = re.search(r"R\s*(\d{1,3}(?:[\s ,]\d{3})+|\d+)(?:\.\d{2})?", text)
    if not m:
        return None
    return int(re.sub(r"\D", "", m.group(1)))


def price_period_from(text: Optional[str]) -> Optional[str]:
    """'R 3 800 per day' -> 'day'; 'R 9 000 pm' / 'R9000pm' -> 'month'."""
    if not text:
        return None
    if re.search(r"(?<![a-z])pm\b|p/m\b|per\s+month|/\s*month", text, re.I):
        return "month"
    m = re.search(r"(?:per|/)\s*(day|night|week)\b", text, re.I)
    return m.group(1).lower() if m else None


def parse_number(text) -> Optional[float]:
    """'2', '2.5', '6+', '6_PLUS' -> number. Anything else -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    return float(m.group(0).replace(",", ".")) if m else None


def parse_size(text: Optional[str]) -> Optional[float]:
    """'78 m²', '78m2', '1 257 m²' -> 78.0"""
    if not text:
        return None
    m = re.search(r"([\d\s ,.]*\d)\s*m(?:²|2|\b)", text)
    if not m:
        return None
    return float(re.sub(r"[^\d.]", "", m.group(1).replace(",", "")) or 0) or None


def beds_from_title(title: Optional[str]) -> Optional[float]:
    """'2 Bedroom Apartment' -> 2, 'Bachelor/Studio' -> 0."""
    if not title:
        return None
    m = re.search(r"(\d+(?:\.\d)?)\s*(?:bed(?:room)?s?|br)\b", title, re.I)
    if m:
        return float(m.group(1))
    if re.search(r"\b(bachelor|studio)\b", title, re.I):
        return 0.0
    return None


PROPERTY_TYPES = [
    # (pattern, canonical name) - order matters, first match wins
    (r"apartment|flat\b", "Apartment"),
    (r"townhouse", "Townhouse"),
    (r"duplex", "Duplex"),
    (r"simplex", "Simplex"),
    (r"cluster", "Cluster"),
    (r"penthouse", "Penthouse"),
    (r"loft", "Loft"),
    (r"cottage|flatlet|granny", "Cottage"),
    (r"\broom", "Room"),
    (r"bachelor|studio", "Apartment"),
    (r"house|home", "House"),
    (r"commercial|office|retail|industrial|warehouse|showroom", "Commercial"),
    (r"farm|smallholding", "Farm"),
    (r"vacant land|\bland\b|\bplot\b|\berf\b", "Land"),
]


def property_type_from(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for pat, name in PROPERTY_TYPES:
        if re.search(pat, text, re.I):
            return name
    return None


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_available(text: Optional[str], today: Optional[dt.date] = None) -> Optional[str]:
    """'AVAILABLE NOW' -> 'now'; 'Available: 01 Dec' -> '2026-12-01' (year inferred);
    '2026-11-05' -> unchanged."""
    if not text:
        return None
    today = today or dt.date.today()
    t = text.strip()
    if re.search(r"\b(now|immediately)\b", t, re.I):
        return "now"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", t)
    if m and m.group(2).lower() in MONTHS:
        day, mon = int(m.group(1)), MONTHS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else today.year
        if not m.group(3) and mon < today.month - 2:   # 'Available 01 Jan' seen in Nov
            year += 1
        try:
            return dt.date(year, mon, day).isoformat()
        except ValueError:
            return None
    return None


BADGE_CANON = [
    (r"reduced", "Reduced"),
    (r"on show", "On Show"),
    (r"available\s*now|immediately", "Available Now"),
    (r"\bnew\b", "New"),
    (r"under offer", "Under Offer"),
    (r"pet", "Pet Friendly"),
]


def canon_badge(text: str) -> str:
    for pat, name in BADGE_CANON:
        if re.search(pat, text, re.I):
            return name
    return clean(text.title()) or text


def finalise(li: "Listing") -> "Listing":
    """Site-independent clean-up applied to every parsed listing."""
    if li.price_period is None and li.kind == "rent":
        li.price_period = price_period_from(li.price_text) or ("month" if li.price else None)
    if li.price_period and li.price_period != "month":
        tag = f"Priced per {li.price_period}"
        if tag not in li.badges:
            li.badges.append(tag)
    if li.size_m2 is not None and li.size_m2 < 10:    # '1 m²', '4 m²' = advertiser junk
        li.size_m2 = None
    if li.property_type is None:
        li.property_type = property_type_from(li.title)
    if li.bedrooms is None:
        li.bedrooms = beds_from_title(li.title)
    li.badges = list(dict.fromkeys(b for b in li.badges if b))
    return li


def looks_like_captcha(html: str) -> bool:
    h = html[:20000].lower()
    return any(s in h for s in (
        "g-recaptcha", "cf-challenge", "challenge-platform", "just a moment...",
        "attention required", "are you a robot", "px-captcha", "hcaptcha.com/1/api"))


# --------------------------------------------------------------------------- fetcher
class Cancelled(Exception):
    """User pressed Stop."""


class Blocked(Exception):
    """Site refused us (403 / CAPTCHA). We stop scraping that site; we never try to solve it."""


class Fetcher:
    """One request at a time, randomised delay between requests, retry/backoff on 429/503,
    and an optional headless-Chromium fallback for blocked or JS-rendered pages."""

    def __init__(self, min_delay: float = 3.0, max_delay: float = 6.0,
                 allow_browser: bool = True, max_retries: int = 3, cancel=None):
        self.min_delay, self.max_delay = min_delay, max_delay
        self.allow_browser = allow_browser
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-ZA,en;q=0.9",
        })
        self._last = 0.0
        self._pw = None
        self._browser = None
        self.requests_made = 0
        self.cancel = cancel                  # optional threading.Event (web UI 'Stop')

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel is not None and self.cancel.is_set())

    def _sleep(self, seconds: float):
        if self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            time.sleep(seconds)
        if self.cancelled:
            raise Cancelled()

    def _wait(self):
        gap = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < gap:
            self._sleep(gap - elapsed)
        self._last = time.monotonic()

    def get(self, url: str) -> str:
        """Fetch with requests. Raises Blocked on 403/CAPTCHA, HTTPError on other failures."""
        for attempt in range(self.max_retries + 1):
            self._wait()
            self.requests_made += 1
            log.debug("GET %s", url)
            r = self.session.get(url, timeout=30)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                ra = r.headers.get("Retry-After", "")
                backoff = int(ra) if ra.isdigit() else 10 * 2 ** attempt
                backoff = min(backoff, 120) + random.uniform(0, 3)
                log.warning("HTTP %s from %s - backing off %.0fs (attempt %d/%d)",
                            r.status_code, url, backoff, attempt + 1, self.max_retries)
                self._sleep(backoff)
                continue
            if r.status_code == 403:
                raise Blocked(f"HTTP 403 Forbidden for {url}")
            r.raise_for_status()
            if looks_like_captcha(r.text):
                raise Blocked(f"CAPTCHA / bot challenge page at {url}")
            return r.text
        raise requests.HTTPError(f"Gave up after {self.max_retries} retries: {url}")

    def get_rendered(self, url: str) -> str:
        """Fetch with headless Chromium (for JS-rendered pages or when requests is refused).
        Still one page at a time with the same delay; CAPTCHAs are never solved."""
        if not self.allow_browser:
            raise Blocked("browser fallback disabled (--no-browser)")
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                raise Blocked("Playwright not installed; pip install playwright && "
                              "python -m playwright install chromium") from e
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        self._wait()
        self.requests_made += 1
        log.info("  [browser] %s", url)
        page = self._browser.new_page(user_agent=USER_AGENT, locale="en-ZA")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            if resp is not None and resp.status == 403:
                raise Blocked(f"HTTP 403 Forbidden (browser) for {url}")
            html = page.content()
        finally:
            page.close()
        if looks_like_captcha(html):
            raise Blocked(f"CAPTCHA / bot challenge page (browser) at {url}")
        return html

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = self._pw = None
