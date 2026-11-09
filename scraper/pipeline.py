"""Run searches, filter, de-duplicate, remember seen listings, and write outputs."""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import re
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from .analysis import build_market, score_listings, scoring_notes
from .core import Fetcher, Listing
from .sites import SITES, adapter_for

log = logging.getLogger("scraper")

EXPORT_FORMAT = "property-scraper/claude-export v1"

HOW_TO_INTERPRET = """\
You are helping me choose a property. This file is a machine export from my personal scraper of
South African property sites (Property24, Private Property, Gumtree). Read it like this:

1. notes_for_claude says what I'm looking for, in my words. It overrides everything else.
2. filters_applied were already applied: every listing below passed them. summary describes the set.
3. market.fair_price_curve is a smoothed curve of [floor size m2, typical price] fitted to
   everything scraped this run. market.sweet_spot is the affordable size band where extra space is
   cheapest. market.method explains exactly how both were computed, including their caveats.
4. Each listing has fair_price (what the curve predicts for it), value_pct (% cheaper than fair,
   + is good), score (0-100, how it was built is in scoring_notes) and flags. Listings are sorted
   by score, best first. Treat score as a first pass, not a verdict: re-rank with judgement.
5. dupe_of means the same unit is probably listed on another site; mention both links but count
   it once.
6. excerpt is only the first ~200 characters of the ad. Absence of a feature there is NOT evidence
   it's missing; say "unknown, ask" instead of guessing.
7. suburb is what the site reports. Gumtree advertisers sometimes file ads under the wrong area, so
   if the title or excerpt names a different suburb (e.g. suburb "Milnerton" but "...in Kensington"),
   trust the text and say so.

What I want back:
- A ranked shortlist of the best 5-10 properties for value AND fit with my notes. For each: price,
  beds, size, suburb, why it's good, what's unknown or risky, and the link.
- Anything that looks too good to be true (far below market, scam wording, per-day pricing,
  'room' listings) called out separately; don't shortlist those without a warning.
- A short read of the market: what my budget realistically buys where, and whether the sweet spot
  suggests stretching or saving.
- 3-5 questions I should ask agents for my top picks (security, parking, pets, deposit, lease
  length, utilities), tailored to what is unknown for each.
Never invent listing details that aren't in this file.
"""

FIELD_NOTES = (
    "price_zar is whole rands; for rentals it is per month unless price_period says otherwise "
    "(non-monthly prices are excluded when filters.monthly_price_only is true). "
    "size_m2 is floor size only (land/erf size is ignored; values under 10 m2 are treated as junk). "
    "available is an ISO date or 'now'; dates shown on cards without a year are assumed to be the "
    "next such date. beds/baths may be fractional (2.5 = 2 + guest toilet). On Gumtree, parking is "
    "a type, not a count, and appears in badges (e.g. 'Secure Parking'); agency is the advertiser's "
    "display name. excerpt is the first ~200 characters of the card description: use it to judge "
    "things like 'secure complex', pets, furnished. dupe_of points to the earlier listing on another "
    "site with the same price, beds and suburb (and floor size within 3 m2 when both are known): "
    "probably the same unit listed twice. Unknown beds/type/size never cause a listing to be "
    "filtered out; an unknown price does when a price filter is set."
)


# --------------------------------------------------------------------------- config
def load_config(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {"searches": [], "filters": {}, "notes_for_claude": ""}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("searches", [])
    cfg.setdefault("filters", {})
    cfg.setdefault("notes_for_claude", "")
    for s in cfg["searches"]:
        if "url" not in s:
            raise ValueError(f"search without url in {path}: {s}")
        s.setdefault("name", s["url"])
    return cfg


# --------------------------------------------------------------------------- filters
def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _contains_any(text: str, needles: list[str]) -> Optional[str]:
    t = f" {_norm(text)} "
    for n in needles:
        if f" {_norm(n)} " in t:
            return n
    return None


def reject_reason(li: Listing, f: dict) -> Optional[str]:
    """None if the listing passes the filters, else a short reason."""
    if f.get("kind") and li.kind and li.kind != f["kind"]:
        return f"kind={li.kind}"
    if f.get("monthly_price_only", True) and li.kind == "rent" and li.price_period not in (None, "month"):
        return f"priced per {li.price_period}"
    if f.get("min_price") is not None or f.get("max_price") is not None:
        if li.price is None:
            return "no price"
        if f.get("min_price") is not None and li.price < f["min_price"]:
            return f"price<{f['min_price']}"
        if f.get("max_price") is not None and li.price > f["max_price"]:
            return f"price>{f['max_price']}"
    if li.bedrooms is not None:
        if f.get("min_beds") is not None and li.bedrooms < f["min_beds"]:
            return f"beds<{f['min_beds']}"
        if f.get("max_beds") is not None and li.bedrooms > f["max_beds"]:
            return f"beds>{f['max_beds']}"
    if f.get("min_size_m2") is not None and li.size_m2 is not None and li.size_m2 < f["min_size_m2"]:
        return f"size<{f['min_size_m2']}"
    types = f.get("property_types") or []
    if types and li.property_type and li.property_type.lower() not in {t.lower() for t in types}:
        return f"type={li.property_type}"
    place = " ".join(x for x in (li.suburb, li.address, li.title) if x)
    hit = _contains_any(place, f.get("exclude_suburbs") or [])
    if hit:
        return f"excluded area {hit}"
    inc = f.get("include_suburbs") or []
    if inc and not _contains_any(place, inc):
        return f"area {li.suburb or '?'} not in include_suburbs"
    kw = _contains_any(" ".join(x for x in (li.title, li.excerpt) if x), f.get("exclude_keywords") or [])
    if kw:
        return f"keyword '{kw}'"
    return None


# --------------------------------------------------------------------------- duplicates
def mark_cross_site_duplicates(listings: list[Listing]) -> int:
    """Same price + beds + suburb on a *different* site (and floor size within 3 m² if both
    known) -> dupe_of = key of the first such listing. Returns the number flagged."""
    order = {cls.name: i for i, cls in enumerate(SITES)}
    groups: dict[tuple, list[Listing]] = {}
    for li in sorted(listings, key=lambda x: order.get(x.site, 99)):
        if li.price is None or li.bedrooms is None or not li.suburb:
            continue
        groups.setdefault((li.price, li.bedrooms, _norm(li.suburb)), []).append(li)
    flagged = 0
    for group in groups.values():
        for i, li in enumerate(group):
            for prev in group[:i]:
                if prev.site == li.site or prev.dupe_of:
                    continue
                if li.size_m2 is not None and prev.size_m2 is not None and abs(li.size_m2 - prev.size_m2) > 3:
                    continue
                li.dupe_of = prev.key
                flagged += 1
                break
    return flagged


# --------------------------------------------------------------------------- seen ids
def load_seen(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("Could not read %s - starting a fresh seen list", path)
    return {}


def save_seen(path: Path, seen: dict, listings: list[Listing], today: str):
    for li in listings:
        seen.setdefault(li.key, today)
    path.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- outputs
CSV_FIELDS = ["site", "listing_id", "url", "kind", "title", "price", "price_text", "price_period",
              "suburb", "address", "property_type", "bedrooms", "bathrooms", "parking", "size_m2",
              "available", "badges", "agency", "excerpt", "search", "dupe_of",
              "score", "value_pct", "fair_price", "flags", "is_new", "matches_filters", "filter_reason"]


def _num(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def write_csv(path: Path, rows: list[tuple[Listing, Optional[str], bool]]):
    with path.open("w", newline="", encoding="utf-8-sig") as fh:     # BOM so Excel reads R/m² right
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for li, reason, is_new in rows:
            d = {k: _num(v) for k, v in li.to_dict().items() if k in CSV_FIELDS}
            d["badges"] = "; ".join(li.badges)
            d["flags"] = "; ".join(li.flags)
            d["is_new"] = "yes" if is_new else "no"
            d["matches_filters"] = "yes" if reason is None else "no"
            d["filter_reason"] = reason or ""
            w.writerow(d)


def export_listing(li: Listing) -> dict:
    d = {
        "id": li.key, "kind": li.kind, "price_zar": li.price,
        "price_period": li.price_period if li.price_period not in (None, "month") else None,
        "title": li.title, "type": li.property_type, "suburb": li.suburb, "address": li.address,
        "beds": _num(li.bedrooms), "baths": _num(li.bathrooms), "parking": _num(li.parking),
        "size_m2": _num(li.size_m2), "available": li.available, "badges": li.badges,
        "agency": li.agency, "score": li.score, "value_pct": li.value_pct, "fair_price": li.fair_price,
        "flags": li.flags, "excerpt": li.excerpt, "dupe_of": li.dupe_of, "url": li.url,
    }
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def build_export(cfg: dict, searches: list[dict], filters: dict, new_only: bool,
                 scraped: list[Listing], matching: list[Listing], dupes: int,
                 market: Optional[dict] = None, site_stats: Optional[dict] = None) -> dict:
    prices = sorted(li.price for li in matching if li.price is not None)
    return {
        "export_format": EXPORT_FORMAT,
        "how_to_interpret": HOW_TO_INTERPRET,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes_for_claude": cfg.get("notes_for_claude", ""),
        "searches": [{"name": s["name"], "url": s["url"]} for s in searches],
        "filters_applied": filters,
        "new_only": new_only,
        "summary": {
            "scraped_total": len(scraped),
            "matching": len(matching),
            "by_site": dict(Counter(li.site for li in matching)),
            "by_suburb": dict(Counter(li.suburb or "unknown" for li in matching).most_common()),
            "price_min": prices[0] if prices else None,
            "price_median": int(statistics.median(prices)) if prices else None,
            "price_max": prices[-1] if prices else None,
            "possible_cross_site_duplicates": sum(1 for li in matching if li.dupe_of),
            "sites": site_stats or {},
        },
        "market": market or {},
        "scoring_notes": scoring_notes(cfg.get("scoring")),
        "field_notes": FIELD_NOTES,
        "listings": [export_listing(li) for li in matching],
    }


def console_table(matching: list[Listing], n: int = 15) -> str:
    rows = sorted((li for li in matching if li.price is not None), key=lambda x: x.price)[:n]
    if not rows:
        return "(no matching listings)"
    head = f"{'Price':>9} {'Score':>5}  {'Bd':>3} {'m²':>4}  {'Type':<10} {'Suburb':<18} {'Avail':<10} {'Site':<15} Title"
    lines = [head, "-" * len(head)]
    for li in rows:
        lines.append(
            f"R{li.price:>8,}".replace(",", " ") + f" {li.score if li.score is not None else '':>5}"
            + f"  {_num(li.bedrooms) if li.bedrooms is not None else '?':>3}"
            + f" {_num(li.size_m2) if li.size_m2 is not None else '':>4}"
            + f"  {(li.property_type or '')[:10]:<10} {(li.suburb or '?')[:18]:<18}"
            + f" {(li.available or '')[:10]:<10} {li.site[:15]:<15} {(li.title or '')[:40]}"
            + (" [dupe]" if li.dupe_of else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- run
def run(cfg: dict, searches: list[dict], out_dir: Path, *, new_only: bool = False,
        debug: bool = False, max_pages: Optional[int] = None, allow_browser: bool = True,
        min_delay: float = 3.0, apply_filters: bool = True, cancel=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    filters = cfg.get("filters", {}) if apply_filters else {}
    default_pages = max_pages or cfg.get("max_pages", 5)
    searches = [s for s in searches if s.get("enabled", True)]
    fetcher = Fetcher(min_delay=min_delay, max_delay=min_delay + 3, allow_browser=allow_browser,
                      cancel=cancel)
    adapters: dict[str, object] = {}
    site_stats: dict[str, dict] = {}
    scraped: dict[str, Listing] = {}
    try:
        for s in searches:
            if fetcher.cancelled:
                log.warning("Cancelled - writing what was collected so far.")
                break
            cls = adapter_for(s["url"])
            ad = adapters.setdefault(cls.name, cls())
            st = site_stats.setdefault(cls.name, {"searches": 0, "listings": 0, "blocked": None,
                                                  "browser": False})
            if ad.blocked:
                log.warning("Skipping '%s' - %s already blocked us this run.", s["name"], cls.name)
                continue
            pages = min(int(s.get("max_pages", default_pages)), max_pages or 10**6)
            log.info("[%s] %s  (up to %d pages)", cls.name, s["name"], pages)
            found = ad.scrape(fetcher, s["url"], pages, s["name"], out_dir / "debug" if debug else None)
            st["searches"] += 1
            st["listings"] += len(found)
            st["blocked"] = ad.blocked
            st["browser"] = ad.use_browser
            for li in found:
                scraped.setdefault(li.key, li)
    finally:
        fetcher.close()

    listings = list(scraped.values())
    mark_cross_site_duplicates(listings)
    market = build_market(listings, cfg.get("filters", {}))
    score_listings(listings, market, cfg.get("scoring"))

    seen_path = out_dir / "seen_ids.json"
    seen = load_seen(seen_path)
    today = dt.date.today().isoformat()
    rows = []
    for li in listings:
        is_new = li.key not in seen
        if new_only and not is_new:
            continue
        rows.append((li, reject_reason(li, filters), is_new))
    save_seen(seen_path, seen, listings, today)

    out = write_outputs(cfg, searches, filters, new_only, rows, market, site_stats, out_dir)
    return dict(out, cancelled=fetcher.cancelled, site_stats=site_stats, requests=fetcher.requests_made)


def write_outputs(cfg: dict, searches: list[dict], filters: dict, new_only: bool,
                  rows: list[tuple[Listing, Optional[str], bool]], market: dict, site_stats: dict,
                  out_dir: Path) -> dict:
    matching = [li for li, reason, _ in rows if reason is None]
    matching.sort(key=lambda x: (x.score is None, -(x.score or 0), x.price or 0))
    rows.sort(key=lambda r: (r[1] is not None, r[0].score is None, -(r[0].score or 0)))
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    while (out_dir / f"claude_export_{stamp}.json").exists():      # two runs in the same second
        time.sleep(1)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"listings_{stamp}.csv"
    json_path = out_dir / f"claude_export_{stamp}.json"
    write_csv(csv_path, rows)
    export = build_export(cfg, searches, filters, new_only, [r[0] for r in rows], matching,
                          sum(1 for li in matching if li.dupe_of), market, site_stats)
    json_path.write_text(json.dumps(export, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "stamp": stamp, "rows": rows, "matching": matching,
            "export": export}


def _listing_from_csv(r: dict) -> Listing:
    def f(k):
        return float(r[k]) if r.get(k) not in (None, "") else None
    return Listing(
        site=r["site"], listing_id=r["listing_id"], url=r["url"], kind=r.get("kind") or None,
        title=r.get("title") or None, price=int(float(r["price"])) if r.get("price") else None,
        price_text=r.get("price_text") or None, price_period=r.get("price_period") or None,
        suburb=r.get("suburb") or None, address=r.get("address") or None,
        property_type=r.get("property_type") or None, bedrooms=f("bedrooms"), bathrooms=f("bathrooms"),
        parking=f("parking"), size_m2=f("size_m2"), available=r.get("available") or None,
        badges=[b for b in (r.get("badges") or "").split("; ") if b], agency=r.get("agency") or None,
        excerpt=r.get("excerpt") or None, search=r.get("search") or None)


def rescore_run(cfg: dict, stamp: str, out_dir: Path, apply_filters: bool = True) -> dict:
    """Re-apply the current filters and scoring to a saved run (no network). Writes a new run."""
    with (out_dir / f"listings_{stamp}.csv").open(encoding="utf-8-sig") as fh:
        raw = list(csv.DictReader(fh))
    old = json.loads((out_dir / f"claude_export_{stamp}.json").read_text(encoding="utf-8"))
    listings = [_listing_from_csv(r) for r in raw]
    is_new = {f"{r['site']}:{r['listing_id']}": r.get("is_new") == "yes" for r in raw}
    mark_cross_site_duplicates(listings)
    market = build_market(listings, cfg.get("filters", {}))
    score_listings(listings, market, cfg.get("scoring"))
    filters = cfg.get("filters", {}) if apply_filters else {}
    rows = [(li, reject_reason(li, filters), is_new.get(li.key, False)) for li in listings]
    site_stats = (old.get("summary") or {}).get("sites") or {}
    out = write_outputs(cfg, old.get("searches") or [], filters, bool(old.get("new_only")), rows,
                        market, site_stats, out_dir)
    out["export"]["rescored_from"] = stamp
    out["json"].write_text(json.dumps(out["export"], indent=1, ensure_ascii=False), encoding="utf-8")
    return out
