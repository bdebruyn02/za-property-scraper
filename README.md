# Property scraper (personal use)

Scrapes rental/sale search results from **Property24**, **Private Property** and **Gumtree**,
scores every listing for value against a fitted market curve, and gives you:

- a **web UI** to configure, run and browse results, with a value chart showing the size sweet spot
- a CSV for Excel
- a JSON export for Claude that **includes instructions on how to interpret it**

Polite by design: one request at a time, a randomised 3–6 s gap between requests, small page
caps, retry with backoff on 429/503, and it **stops a site on 403 or a CAPTCHA page** (it never
tries to solve CAPTCHAs or log in).

## Quick start (Windows)

1. Install Python 3.10+ from python.org (tick "Add python.exe to PATH").
2. Double-click **`scripts\setup.bat`** (one time; installs everything into `.venv`).
3. Double-click **`scripts\start_ui.bat`**. Your browser opens at http://127.0.0.1:8765.

macOS/Linux: `sh scripts/setup.sh`, then `sh scripts/start_ui.sh`.

Manual setup, if you prefer:

```powershell
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # headless-browser fallback (optional)
python -m pytest                        # parser tests against saved real pages
python web_ui.py                        # or: python property_scraper.py
```

## How to use the web UI

The UI only listens on your own machine (127.0.0.1).

0. **What's your goal?** The first time you open the UI, a short wizard asks: rent or buy, budget,
   bedrooms, minimum size, property type, areas (and areas to avoid), what matters most, and
   anything else in your own words. From that it builds your filters, value-score keywords
   (e.g. security, pets, parking, transport), the brief Claude gets, and ready-made search URLs for
   the areas it knows (listed in `REGIONS` in `scraper/onboarding.py`). You
   can edit the brief on the last step. Options there:
   - **Save as my settings**: ticked writes everything to `searches.json` (previous version kept as
     `searches.backup.json`). Unticked, the goal is used **for this browser session only**: a
     banner shows it's active, runs and re-scores use it, and your saved settings stay untouched.
     The banner's *Save as my settings* / *Discard* buttons let you change your mind.
   - **Replace my searches with these**: untick to keep your own search URLs.
   - **Start a scrape now**.

   Reopen it any time with **Change goal** in the top bar; it remembers your answers. Typed-in
   areas outside the known ones become an area filter; add a search URL for them in Settings.

1. **Settings**: set things up once.
   - *What you're looking for*: plain-English notes. These go into the export and Claude
     treats them as the most important instruction, so be specific (must-haves, deal-breakers).
   - *Filters*: budget, beds, minimum size, property types, areas to include/exclude, keywords
     to exclude. Leaving "Include areas" empty means anywhere.
   - *Value score*: bonus keywords (e.g. security wording) and red-flag (scam) keywords.
   - *Searches*: paste search-result URLs from the sites (see
     [How to copy search URLs](#how-to-copy-search-urls)). Untick a search to skip it without
     deleting it. Press **Save** (your previous file is kept as `searches.backup.json`).
2. **Run scrape**: press **Start**. The log shows each page as it's fetched; a full run takes
   2–3 minutes. **Stop** ends the run early and still saves what was collected. Options:
   *New listings only* (skip anything seen before), *Max pages per search*, *Ignore filters*.
3. **Results**: the matching listings, **best score first**. Click column headers to sort, search
   the text, toggle sites, hide duplicates, or tick *Show filtered-out listings* to see everything
   scraped and why each one was dropped. Click a title to open the ad.
4. **Value chart**: two charts, explained below.
   Changed a filter or keyword afterwards? **Re-score** (top right) applies the current settings
   to the selected run without scraping again, and saves it as a new run.
5. **Map**: pick areas and mark places you never want to live (see [The map](#the-map)).
6. **Claude export** (top right): download it and upload it to Claude, e.g. *"Here's today's
   property export. Give me your shortlist."* The file tells Claude how to read it and what to
   give back. The *Run* dropdown switches between past runs.

### The map

The **Map** tab uses OpenStreetMap (map data © OpenStreetMap contributors; the map library,
Leaflet, ships with the app).

- **Pick suburbs**: click anywhere in a suburb, then *Include* or *Avoid*. This edits the same
  include/avoid lists as Settings, and the suburb's outline is drawn on the map.
- **Draw no-go area**: click the corners of an area you never want to live in, then *Done*.
- **Block one side of a road**: click along the road from one end to the other (follow the bends),
  press *Done*, then click on the side you don't want. The road counts as carrying on straight
  past both ends, so everything on that side is blocked, however far away. The shading shows
  roughly what's covered.
- Press **Save**, then **Re-score** to apply it to the run you're looking at. The next scrape uses
  it too. Zones can be renamed, switched off or deleted in the list.

**How listings are judged against a zone.** That depends on what's known about where a listing is:

| What's known | Result |
|---|---|
| Exact map pin from the site, inside a zone | filtered out |
| Approximate pin inside a zone | kept, flagged "may be in no-go zone" |
| Suburb only, whole outline inside a zone | filtered out |
| Suburb only, outline straddles a zone (or OpenStreetMap only has its centre, and that's inside) | kept, flagged |
| Nothing | kept |

Where the pins come from:
- **Private Property** puts a pin on the results card when the advertiser shows the address.
- **Property24** only shows it on the listing's own page. So after a scrape, the app opens each
  new *matching* Property24 listing once (at most 40 a run, same 3–6 s gap). It remembers the
  answer in `output/locations.json`, so a listing is never fetched twice. Untick *Find map
  locations* on the Run tab (or use `--no-locations`) to skip this. Many Property24 advertisers
  hide the address ("contact for address"), so expect pins for only part of the listings.
- **Gumtree** gives no coordinates; its listings are judged by suburb.

Suburb outlines come from OpenStreetMap's Nominatim service: at most one lookup a second, only
for suburbs that matter, and cached in `output/places_cache.json`. Some suburbs exist in
OpenStreetMap only as a point, never an outline. Listings in those suburbs are flagged, never
filtered out.

### Reading the value chart

- **Rent vs floor size**: each dot is a listing, coloured by site (grey = didn't pass your
  filters). The black line is the **fair-price spline**: a smoothed curve of what places of each
  size typically rent for, fitted to everything scraped. **Dots below the line are cheaper than the
  market for their size.** The dashed line is your max rent.
- **Rent per m²**: the same listings in rand per m². The curve usually falls with size and then
  flattens.
- **The shaded band is the sweet spot**: the affordable sizes where each extra m² adds the least to
  the rent. *The best value is a listing inside the band and below the line.* The panel under the
  charts gives the numbers (e.g. "each extra m² adds about R 15 here vs R 60 typically").
- Hover a dot for details; click it to open the listing.

### How the score works

`score` (0–100) = 50 + `value_pct` (capped at ±30) + bonuses − penalties:

| | points |
|---|---|
| `value_pct`: how much cheaper (+) or dearer (−) than the fair price for its size | ±30 max |
| security keyword in title/excerpt (configurable) | +8 |
| has parking (count, or a secure/covered/garage badge) | +4 |
| available now or within 45 days | +3 |
| floor size inside the sweet spot and at or below fair price | +3 |
| more than 40% below market (often a scam, typo or a room) | −15 |
| scam-pattern wording (configurable) | −20 |

When floor size is unknown, the fair price is the median rent for the same bedroom count, and the
listing gets a flag saying so. Treat the score as a sort order, not a verdict: the description
snippet is only ~200 characters.

**Why there are "Market sample" searches:** the main searches ask the sites for rents ≤ your
budget, so they only see the cheap end of the market and the fair-price curve would go flat at your
budget. The market-sample searches (no price cap, 2 pages each, about 6 extra requests) let the curve
see what bigger and dearer places cost. Keep at least one enabled per area.

### What Claude receives

`claude_export_<stamp>.json` contains, in order: `how_to_interpret` (instructions: what to read,
what to distrust, and what to answer with: a ranked shortlist, warnings, a market read and
questions for agents), your `notes_for_claude`, the searches and filters, a `summary`, the
`market` (fair-price curve, sweet spot, median rent by bedrooms, and the method in words),
`scoring_notes`, `field_notes`, and the matching `listings` sorted by score. Empty fields are
dropped to keep it small. The Help tab in the UI shows the exact instructions text.

## Command line

```powershell
python property_scraper.py                 # every search in searches.json
python property_scraper.py --new-only      # only listings not seen on a previous run
python property_scraper.py --url "https://www.property24.com/to-rent/<suburb>/<city>/<province>/<area-id>"
```

Each run writes to `output/`:

| File | What |
|---|---|
| `listings_<stamp>.csv` | every scraped listing, all fields, plus `matches_filters` / `filter_reason` columns so you can see why something was dropped |
| `claude_export_<stamp>.json` | matching listings only, compact (`property-scraper/claude-export v1`); upload this to Claude |
| `seen_ids.json` | every listing ID ever seen, with first-seen date (drives `--new-only`) |
| `locations.json` | map pins found on listing pages (and "none found"), so each listing is opened at most once |
| `places_cache.json` | suburb outlines from OpenStreetMap |
| `debug/` | page-1 HTML of each search (with `--debug`) |

It also prints the 15 cheapest matches.

### CLI flags

| Flag | Meaning |
|---|---|
| `--config FILE` | searches/filters file (default `searches.json`) |
| `--url URL` | run this search URL instead of the config's searches (repeatable); config filters still apply |
| `--new-only` | output only listings not in `seen_ids.json` |
| `--debug` | save page-1 HTML for each search to `output/debug/` |
| `--max-pages N` | cap on result pages per search (default: `max_pages` in config, else 5) |
| `--no-filters` | ignore the config filters |
| `--no-browser` | never fall back to headless Chromium |
| `--no-locations` | don't open Property24 listing pages for map pins (cached pins still apply) |
| `--max-location-lookups N` | cap on listing pages opened for pins per run (default: `max_location_lookups` in config, else 40) |
| `--delay S` | minimum seconds between requests (never below 3) |
| `--output-dir DIR` | default `output/` |
| `--rescore STAMP` | re-apply current filters/scoring to a saved run, no scraping (same as the UI's *Re-score*; with no-go zones it may look up suburb outlines on OpenStreetMap) |
| `-v` | verbose logging |

Exit code is 1 if any site blocked us, so a scheduler can flag it.

## searches.json

```json
{
  "notes_for_claude": "free text that goes into the export for Claude to read",
  "max_pages": 4,
  "filters": {
    "kind": "rent", "min_price": 4000, "max_price": 13000,
    "min_beds": 1, "max_beds": 2, "min_size_m2": null,
    "monthly_price_only": true,
    "property_types": ["Apartment", "Townhouse"],
    "include_suburbs": ["Sea Point", "Green Point", "Rondebosch"],
    "exclude_suburbs": ["Woodstock"],
    "exclude_keywords": ["student", "airbnb"],
    "no_go_zones": [
      {"name": "Industrial strip", "type": "area", "points": [[-33.90, 18.60], [-33.90, 18.62], [-33.92, 18.61]]},
      {"name": "East of Main Rd", "type": "road_side", "side": "right",
       "points": [[-33.95, 18.47], [-33.92, 18.48]]}
    ]
  },
  "searches": [
    {"name": "P24 Sea Point", "url": "https://www.property24.com/to-rent/<suburb>/<city>/<province>/<area-id>?sp=pt%3d13000",
     "max_pages": 4}
  ]
}
```

Filter rules:
- An unknown bedroom count, type or size never excludes a listing. An unknown price (POA) does, if a price filter is set.
- `monthly_price_only` drops rentals priced **per day/week/night**. Both P24 and Private Property mix
  short-term holiday lets (e.g. "R 3 800 per day") into normal rental searches.
- `include_suburbs` / `exclude_suburbs` match on whole words in the suburb, address and title, so
  "Rondebosch" also matches "Rondebosch East". Gumtree pads its results with listings from all over
  Cape Town once local ones run out, so `include_suburbs` matters there.
- `no_go_zones` are easiest to draw on the Map tab. `points` are `[lat, lon]`. For `road_side`,
  `side` is left/right relative to the order the road was traced in. `"enabled": false` keeps a
  zone but ignores it.
- Types: Apartment, Townhouse, Simplex, Duplex, Cluster, Penthouse, Loft, Cottage, Room, House,
  Commercial, Farm, Land.

## How to copy search URLs

Do the search on the site with your filters, go to the results list, and copy the address bar.

- **Property24**: e.g. `https://www.property24.com/to-rent/<suburb>/<city>/<province>/<area-id>`.
  Filters live in `?sp=...` (e.g. `?sp=pt%3d13000` = max R13 000). Leave off any `/p2` page suffix.
- **Private Property**: e.g. `.../to-rent/<province>/<city>/<area>/<suburb>/<area-id>?tp=13000`
  (`fp`/`tp` = price from/to). Leave off `&page=`.
- **Gumtree**: use a URL **with a category**, e.g.
  `https://www.gumtree.co.za/property/apartment-flat/<suburb>?propertyListingType=RENT`.
  The bare `/property/<suburb>?propertyListingType=RENT` redirects and loses the rent filter.

The `<area-id>` is the number at the end of the site's own results URL for that area; copy it
from the address bar rather than guessing.

## Schedule a daily `--new-only` run

**Windows Task Scheduler** (PowerShell, adjust the path):

```powershell
$dir = "C:\path\to\za-property-scraper"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" `
          -Argument "property_scraper.py --new-only" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 7:30am
Register-ScheduledTask -TaskName "PropertyScraper" -Action $action -Trigger $trigger `
          -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

Or in the GUI: Task Scheduler → Create Basic Task → Daily → *Start a program* →
`scripts\daily_new_only.bat` (it logs to `output\daily.log`). Scheduled runs show up in the web UI's
*Run* dropdown like any other run.

**cron** (macOS/Linux), `crontab -e`:

```
30 7 * * * cd /path/to/za-property-scraper && .venv/bin/python property_scraper.py --new-only >> output/cron.log 2>&1
```

## Adding a site

1. Create `scraper/sites/<site>.py` with a `SiteAdapter` subclass: set `name`, `domains`,
   `base_url`; implement `page_url(url, page)` and `parse(html, url) -> list[Listing]`.
   Set `prefer_browser = True` if the site only renders with JavaScript.
2. Add the class to `SITES` in `scraper/sites/__init__.py`.
3. Fetch one real results page, save it to `tests/fixtures/`, and add a test like those in
   `tests/test_parsers.py`.

Prefer stable hooks (listing-URL patterns, `data-*` attributes, JSON-LD, embedded app state) over
styling classes, and keep the generic text helpers in `scraper/core.py` (`parse_price`,
`parse_size`, `beds_from_title`) as fallbacks.

## How each site is parsed (as of Nov 2026)

- **Property24**: server-rendered. Cards are `[data-listing-number]`, with `itemprop="price" content=...`
  microdata and `.p24_featureDetails[title=Bedrooms|Bathrooms|Parking Spaces|Floor Size]`.
  Promoted "pro" tiles use different markup and are handled separately. "Erf Size" is land and is ignored.
  The listing's own page has the map pin in its map widget config (`haveExactLocation` says whether it
  is the real address); when the advertiser hides the address there is no map at all.
- **Private Property**: server-rendered `a.listing-result` cards with an embedded JSON-LD
  `Residence` block. The listing ID (`RR…` rent / `T…` sale) comes from the URL. A page past the last one returns 404.
  The JSON-LD has a `geo` pin when the advertiser shows the address (the listing page adds nothing more).
- **Gumtree**: Nuxt app. Listings come from the `__NUXT_DATA__` payload (`searchResults` only;
  sponsored ads are ignored). Parking is a type (Secure/Covered/Garage), shown as a badge.
  The `lr`/`ls` prefix of a listing ref does **not** mean rent/sale.

## Project layout

```
web_ui.py              local web UI (Flask) - start here
property_scraper.py    command-line entry point
searches.json          your searches, filters, notes and scoring keywords (edited by the UI)
scraper/core.py        Listing model, text parsers, polite fetcher (+ Playwright fallback)
scraper/sites/         one adapter per site, sharing base.py
scraper/pipeline.py    run searches, filter, de-duplicate, outputs, Claude instructions
scraper/analysis.py    fair-price spline (LOESS), sweet spot, value score
scraper/geo.py         no-go zones: point-in-area, side-of-road, suburb-outline checks
scraper/places.py      suburb outlines from OpenStreetMap (Nominatim), cached
webui/                 the UI's HTML/CSS/JS (no build step; map tiles need internet)
webui/vendor/leaflet/  Leaflet 1.9.4 map library (BSD-2), vendored
scripts/               setup / start / daily-run helpers
tests/                 pytest, with real pages saved under tests/fixtures/
```

On scraping tools: this uses the standard Python stack (requests + BeautifulSoup/lxml, with
Playwright's headless Chromium for JavaScript pages). A crawler framework such as Scrapy is built
for large concurrent crawls; at ~25 polite sequential requests per run it would add complexity
without making anything faster or more reliable.
