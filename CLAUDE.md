# CLAUDE.md

Personal property-listing scraper for South African sites (Property24, Private Property, Gumtree),
with a local Flask web UI, a value score, and a JSON export for Claude. Personal use only.
See README.md for user-facing docs.

## Commands

```powershell
.venv\Scripts\python -m pytest                      # all tests (offline; uses saved real pages)
.venv\Scripts\python web_ui.py                      # web UI on http://127.0.0.1:8765
.venv\Scripts\python property_scraper.py            # CLI: all searches in searches.json
.venv\Scripts\python property_scraper.py --url URL --max-pages 1   # cheap live check of one search
.venv\Scripts\python property_scraper.py --rescore STAMP           # re-filter/re-score a saved run, no network
```

Setup from scratch: `scripts\setup.bat` (or `scripts/setup.sh`). Always use `.venv`'s Python.

## Layout

- `scraper/core.py`: `Listing` dataclass, text parsers (`parse_price`, `parse_size`,
  `price_period_from`, `parse_available`...), `finalise()` (site-independent clean-up), and
  `Fetcher` (polite HTTP + Playwright fallback, retry/backoff, `Blocked`, `Cancelled`).
- `scraper/sites/`: one `SiteAdapter` subclass per site; `base.py` has pagination, browser fallback
  and error handling; `__init__.py` holds the `SITES` registry and `adapter_for(url)`.
- `scraper/pipeline.py`: `run()` (scrape -> dedupe -> market -> score -> filter -> outputs),
  `rescore_run()`, filters (`reject_reason`), cross-site dedupe, CSV/JSON writers, and
  `HOW_TO_INTERPRET` / `FIELD_NOTES` (the instructions embedded in the Claude export).
- `scraper/onboarding.py`: the "What's your goal?" wizard's brain. `REGIONS` (only site IDs
  verified live), `validate()`, `build()` -> filters/scoring/notes/search URLs, `apply_to_config()`.
  The UI (`webui/onboarding.js`) calls `/api/onboarding/preview` and `/api/onboarding/apply`;
  `save=false` returns session-only `overrides` that the UI sends with `/api/run` and re-score.
  Saved answers live under `onboarding` in `searches.json`.
- `scraper/geo.py`: no-go zones (`filters.no_go_zones`: `area` polygons, `road_side` = a traced
  polyline + left/right of drawing direction, judged by the nearest segment, ends extended
  indefinitely) and `zone_verdict()`. Pure maths; `webui/map.js` mirrors `side_of_road` for drawing.
- `scraper/places.py`: Nominatim client (1 req/s, generic User-Agent, everything cached in
  `output/places_cache.json`). Map clicks reverse-geocode at street level then search the suburb
  name: reverse at suburb zoom returns electoral wards in Cape Town. Some suburbs are OSM points only.
- `scraper/analysis.py`: LOESS fair-price curve, `sweet_spot()`, `score_listings()`, plus
  `METHOD` / `SCORING_NOTES` text that goes into the export.
- `web_ui.py` + `webui/`: Flask API (one background scrape job at a time) and a no-build vanilla
  JS front end; `webui/charts.js` is a hand-rolled SVG scatter + monotone spline (no CDN).
  `webui/map.js` is the Map tab on vendored Leaflet (`webui/vendor/leaflet`) + OSM tiles.
- `searches.json`: user config (searches, filters, notes_for_claude, scoring keywords). The UI
  rewrites it (backup in `searches.backup.json`). Don't overwrite the user's values.
- `output/`: runs as `listings_<stamp>.csv` + `claude_export_<stamp>.json`, `seen_ids.json`,
  `locations.json` (pins from listing pages, incl. negatives), `places_cache.json`, `debug/`.
  Generated data, git-ignored. Don't delete the user's runs.

## Rules that must not regress

- **Politeness**: sequential requests only, randomised delay >= 3 s (`Fetcher`, CLI clamps
  `--delay` to 3), modest `max_pages`. Never add parallelism, CAPTCHA solving, login, proxy
  rotation or other evasion. On 403/CAPTCHA raise `Blocked`, stop that site, and report it.
- **Verify against real pages, not guesses.** Selectors came from fetched HTML. When a site
  changes, fetch one page (`--debug` saves it to `output/debug/`), inspect, then save it as a
  fixture under `tests/fixtures/` and update tests. Prefer stable hooks (listing-URL/ID
  patterns, `data-*`, JSON-LD, embedded app state) over styling classes; keep text fallbacks.
- **The Claude export is a contract** (`export_format: property-scraper/claude-export v1`).
  Changes must be additive; when you change fields, scoring or the curve, update
  `HOW_TO_INTERPRET`, `FIELD_NOTES`, `METHOD` and/or `SCORING_NOTES` so Claude can still read it.
  Null/empty fields are dropped from export listings.
- Unknown beds/type/size never filter a listing out; an unknown price does if a price filter is set.
- No-go zones reject only on an `exact` pin or a suburb outline wholly inside a zone; approximate
  pins and straddling/point-only suburbs get a "may be in no-go zone" flag; unknown location is kept.
- Listing-page lookups (`pipeline.locate_listings`) go through the same `Fetcher`, only for new
  listings that pass every other filter, capped (`max_location_lookups`, default 40), cached
  forever (including "no location"), and stop on `Blocked`.
- `size_m2` is floor size only (never erf/land); values < 10 m2 are junk -> None.

## Site quirks (as of Nov 2026)

- **Property24**: cards `[data-listing-number]` (skip wrappers that contain another one);
  price from `itemprop=price content`; "pro" tiles use different markup (price regex needs SA
  digit grouping, agency learned from the logo URL); `listed_by_owner.png` = private landlord.
  Pagination `/p2` path suffix; filters in `?sp=` (e.g. `pt%3d13000`). Results pages carry only the
  SA centroid (-30.969313, 24.22227): ignore it. The listing page's `GoogleMapsFactory` props have
  `latitude`/`longitude`/`haveExactLocation`/`streetAddress`; hidden-address listings have no map,
  only `ContactForAddressForm`.
- **Private Property**: `a.listing-result` cards with embedded JSON-LD; ID `RR…`/`T…` comes from
  the URL (`data-listing-id` is an internal number). `?page=N`; a page past the end returns 404
  (treated as end of results). Price filter `?tp=`. Card JSON-LD has `geo` only when the advertiser
  shows the address; the listing page has the same block (no need to fetch it).
- **Gumtree**: Nuxt; decode `#__NUXT_DATA__` (devalue) and use `pinia.listings.searchResults`
  only (ignore `sponsoredResults`). URLs need a category path or the filter is dropped by a
  redirect. Results are padded with other areas; `lr`/`ls` ref prefixes do NOT mean rent/sale.
  Parking is a type (badge), not a count. Advertisers sometimes mis-file the suburb. No coordinates
  anywhere (results or listing page): suburb only.
- Both P24 and PP mix **per-day holiday lets** into rentals ("R 3 800 per day"): `price_period`
  handles it; `monthly_price_only` filters them.

## Analysis notes

- The fair-price curve needs uncapped prices: keep the "Market sample" searches (no price cap,
  2 pages) or the curve flattens at the budget and the sweet spot becomes meaningless.
- Sweet spot = flattest stretch of the whole curve (lowest third of marginal rent per m2,
  outer 10% ignored), then compared with `max_price` (`within_budget` yes/partly/no).
- When fair price comes from the bedroom median (no size), value counts half (cap +/-15).
- Works for rent **and** sale: the curve uses listings of `filters.kind`; P24 new-development
  tiles ('From R…' prices) are excluded from the curve.
- To add an onboarding region, verify its P24/PP IDs and Gumtree slug against the live sites for
  both rent and sale first, then add it to `onboarding.REGIONS`.

## Adding a site

New `scraper/sites/<site>.py` subclassing `SiteAdapter` (`name`, `domains`, `base_url`,
`page_url`, `parse`; `prefer_browser = True` if JS-only) -> add to `SITES` -> save a real results
page to `tests/fixtures/` -> add per-card tests like `tests/test_parsers.py` (check price, beds and
URL against an independent source in the same page) -> one live `--url ... --max-pages 1` run.

## Working style

- UI tests that click "Save" overwrite the user's `searches.json`: back it up first and restore it.
- Run `pytest` after changes. For UI changes, start `web_ui.py` and check the page (headless
  Playwright screenshots work well); there should be no console errors.
- Live scraping costs real requests to real sites: keep live checks to one search / one page
  unless a full run is actually needed.
- Windows + Git Bash environment: beware shell heredocs mangling `\b` / `\n` escapes in
  Python source. Write patch scripts with a file-writing tool instead.
