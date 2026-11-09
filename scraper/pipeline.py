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

import requests

from . import geo
from .analysis import build_market, score_listings, scoring_notes
from .core import Blocked, Cancelled, Fetcher, Listing
from .places import Places
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
8. filters_applied.no_go_zones (if present) are places on the map I never want to live: drawn
   areas, or everything on one side of a road. Listings known to be inside one were already
   removed. A flag starting "may be in no-go zone" means the location is only approximate or
   only the suburb is known: treat it as a real risk, and say which address to confirm. lat/lon
   and location describe where a listing is (see field_notes); listings without them are placed
   by suburb only.

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
    "filtered out; an unknown price does when a price filter is set. "
    "lat/lon (WGS84) is the site's own map pin: from the Private Property results card, or from the "
    "Property24 listing page. location is 'exact' when the site says the pin is the actual address "
    "(Property24 'haveExactLocation'; Private Property only publishes a pin when the advertiser "
    "shows the address) and 'approx' when it is only roughly placed. Gumtree gives no coordinates. "
    "No-go zones reject a listing only on an exact pin, or when its whole suburb outline "
    "(OpenStreetMap) is inside the zone; an unknown location never filters a listing out."
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


def zone_check(li: Listing, f: dict, suburb_places: Optional[dict] = None) -> Optional[tuple[str, str]]:
    """No-go zone verdict for one listing: ('reject', reason) | ('flag', text) | None."""
    zones = geo.active_zones(f)
    if not zones:
        return None
    place = (suburb_places or {}).get(_norm(li.suburb)) if li.suburb else None
    return geo.zone_verdict(li.lat, li.lon, li.location_precision, place, zones)


def without_zones(f: dict) -> dict:
    return {k: v for k, v in f.items() if k != "no_go_zones"}


def reject_reason(li: Listing, f: dict, suburb_places: Optional[dict] = None) -> Optional[str]:
    """None if the listing passes the filters, else a short reason. suburb_places
    (norm suburb -> places.Places result) lets no-go zones judge listings without a map pin."""
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
    v = zone_check(li, f, suburb_places)
    if v and v[0] == "reject":
        return v[1]
    return None


def filter_rows(listings: list[Listing], f: dict, suburb_places: Optional[dict],
                is_new: dict[str, bool]) -> list[tuple[Listing, Optional[str], bool]]:
    """(listing, reject reason, is_new) for each listing. Call after scoring: adds the
    'may be in no-go zone' flag to listings that pass (scoring resets flags)."""
    rows = []
    for li in listings:
        reason = reject_reason(li, f, suburb_places)
        if reason is None:
            v = zone_check(li, f, suburb_places)
            if v and v[0] == "flag" and v[1] not in li.flags:
                li.flags.append(v[1])
        rows.append((li, reason, is_new.get(li.key, False)))
    return rows


# --------------------------------------------------------------------------- locations
def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("Could not read %s - starting afresh", path)
    return {}


def _apply_location(li: Listing, loc: dict):
    if loc.get("lat") is None or li.lat is not None:
        return
    li.lat, li.lon, li.location_precision = loc["lat"], loc["lon"], loc.get("precision") or "approx"
    if not li.address and loc.get("street_address"):
        li.address = loc["street_address"]


def locate_listings(all_listings: list[Listing], candidates: list[Listing], adapters: dict,
                    fetcher: Optional[Fetcher], cache_path: Path, max_lookups: int = 40) -> int:
    """Give listings a map pin. Every listing gets any cached location; then, if a fetcher is
    given, up to max_lookups `candidates` from sites with locate_via_detail have their own page
    fetched (sequentially, same politeness as result pages). Results, including 'no location
    on the page', are cached in cache_path so a listing is fetched at most once ever.
    Returns the number of pages fetched."""
    cache = _load_json(cache_path)
    for li in all_listings:
        if li.key in cache:
            _apply_location(li, cache[li.key])
    if fetcher is None or max_lookups <= 0:
        return 0
    todo = [li for li in candidates if li.lat is None and li.key not in cache
            and getattr(adapters.get(li.site), "locate_via_detail", False)]
    if not todo:
        return 0
    if len(todo) > max_lookups:
        log.info("Locations: %d listings need a page lookup; doing the first %d this run "
                 "(the rest next time).", len(todo), max_lookups)
        todo = todo[:max_lookups]
    log.info("Looking up map locations on %d listing pages (one at a time; cached for next time)...", len(todo))
    fetched, today = 0, dt.date.today().isoformat()
    try:
        for i, li in enumerate(todo, 1):
            ad = adapters[li.site]
            if ad.blocked:
                continue
            try:
                html = ad.fetch_detail(fetcher, li.url)
            except Blocked as e:
                log.error("  BLOCKED by %s on a listing page: %s - no more lookups there.", li.site, e)
                continue
            except requests.HTTPError as e:
                if getattr(e.response, "status_code", None) in (404, 410):
                    cache[li.key] = {"lat": None, "checked": today, "note": "listing page gone"}
                else:
                    log.warning("  %s - skipping", e)
                continue
            except requests.RequestException as e:
                log.warning("  network error (%s) - skipping", e)
                continue
            fetched += 1
            loc = ad.detail_location(html) or {"lat": None}
            cache[li.key] = dict(loc, checked=today)
            _apply_location(li, loc)
            log.info("  [%d/%d] %s: %s", i, len(todo), li.key,
                     f"{loc['precision']} location" if loc.get("lat") is not None
                     else loc.get("note") or "no location on the page")
    except Cancelled:
        log.warning("  location lookups stopped by user")
    finally:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return fetched


def lookup_suburbs(candidates: list[Listing], filters: dict, places: Places, cancel=None) -> dict:
    """Suburb outlines (OpenStreetMap) for candidates without an exact pin, so no-go zones can
    judge them. Only needed - and only looked up - when zones are set. norm suburb -> place."""
    zones = geo.active_zones(filters)
    if not zones:
        return {}
    bbox = geo.zones_bbox(zones)
    names: dict[str, str] = {}            # norm -> first spelling seen
    for li in candidates:
        if li.suburb and li.location_precision != "exact" and _norm(li.suburb):
            names.setdefault(_norm(li.suburb), li.suburb)
    out: dict = {}
    before = places.lookups
    for name in names.values():
        if cancel is not None and cancel.is_set():
            break
        place = places.suburb(name, bbox)
        if place:
            out[_norm(name)] = place
    if places.lookups > before:
        log.info("Looked up %d suburb outlines on OpenStreetMap (cached for next time).", places.lookups - before)
    return out


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
              "score", "value_pct", "fair_price", "flags", "is_new", "matches_filters", "filter_reason",
              "lat", "lon", "location_precision"]


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
        "lat": round(li.lat, 6) if li.lat is not None else None,
        "lon": round(li.lon, 6) if li.lon is not None else None,
        "location": li.location_precision if li.lat is not None else None,
    }
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def export_filters(filters: dict) -> dict:
    """filters_applied for the export: zones as names/descriptions instead of raw coordinates."""
    out = without_zones(filters)
    zones = geo.active_zones(filters)
    if zones:
        out["no_go_zones"] = [geo.describe_zone(z) for z in zones]
    return out


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
        "filters_applied": export_filters(filters),
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
            "with_map_location": sum(1 for li in matching if li.lat is not None),
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
        min_delay: float = 3.0, apply_filters: bool = True, cancel=None,
        locate: bool = True, max_location_lookups: Optional[int] = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    filters = cfg.get("filters", {}) if apply_filters else {}
    if max_location_lookups is None:
        max_location_lookups = int(cfg.get("max_location_lookups", 40))
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

        seen_path = out_dir / "seen_ids.json"
        seen = load_seen(seen_path)
        listings = list(scraped.values())
        is_new = {li.key: li.key not in seen for li in listings}
        in_output = [li for li in listings if is_new[li.key] or not new_only]
        # Only listings that pass every other filter are worth a page lookup or a suburb outline.
        candidates = [li for li in in_output if reject_reason(li, without_zones(filters)) is None]
        lookups = locate_listings(listings, candidates, adapters,
                                  fetcher if locate and not fetcher.cancelled else None,
                                  out_dir / "locations.json", max_location_lookups)
        for name, ad in adapters.items():
            st = site_stats[name]
            st["blocked"] = st["blocked"] or ad.blocked
            st["matching_with_map_location"] = sum(1 for li in candidates if li.site == name and li.lat is not None)
        if lookups:
            log.info("Fetched %d listing pages for map locations.", lookups)
    finally:
        fetcher.close()

    suburb_places = {}
    if geo.active_zones(filters) and not fetcher.cancelled:
        suburb_places = lookup_suburbs(candidates, filters, Places(out_dir / "places_cache.json"), cancel)

    mark_cross_site_duplicates(listings)
    market = build_market(listings, cfg.get("filters", {}))
    score_listings(listings, market, cfg.get("scoring"))

    today = dt.date.today().isoformat()
    rows = filter_rows(in_output, filters, suburb_places, is_new)
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
        excerpt=r.get("excerpt") or None, search=r.get("search") or None,
        lat=f("lat"), lon=f("lon"), location_precision=r.get("location_precision") or None)


def rescore_run(cfg: dict, stamp: str, out_dir: Path, apply_filters: bool = True,
                online_places: bool = True) -> dict:
    """Re-apply the current filters and scoring to a saved run. Never contacts the property
    sites; map locations come from the CSV and output/locations.json. If no-go zones are set,
    missing suburb outlines are looked up on OpenStreetMap (unless online_places=False).
    Writes a new run."""
    with (out_dir / f"listings_{stamp}.csv").open(encoding="utf-8-sig") as fh:
        raw = list(csv.DictReader(fh))
    old = json.loads((out_dir / f"claude_export_{stamp}.json").read_text(encoding="utf-8"))
    listings = [_listing_from_csv(r) for r in raw]
    is_new = {f"{r['site']}:{r['listing_id']}": r.get("is_new") == "yes" for r in raw}
    filters = cfg.get("filters", {}) if apply_filters else {}
    locate_listings(listings, [], {}, None, out_dir / "locations.json")     # cache only
    suburb_places = {}
    if geo.active_zones(filters):
        candidates = [li for li in listings if reject_reason(li, without_zones(filters)) is None]
        suburb_places = lookup_suburbs(candidates, filters,
                                       Places(out_dir / "places_cache.json", online=online_places))
    mark_cross_site_duplicates(listings)
    market = build_market(listings, cfg.get("filters", {}))
    score_listings(listings, market, cfg.get("scoring"))
    rows = filter_rows(listings, filters, suburb_places, is_new)
    site_stats = (old.get("summary") or {}).get("sites") or {}
    out = write_outputs(cfg, old.get("searches") or [], filters, bool(old.get("new_only")), rows,
                        market, site_stats, out_dir)
    out["export"]["rescored_from"] = stamp
    out["json"].write_text(json.dumps(out["export"], indent=1, ensure_ascii=False), encoding="utf-8")
    return out
