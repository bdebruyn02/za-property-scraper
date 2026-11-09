#!/usr/bin/env python
"""Personal property-listing scraper for South African sites (Property24, Private Property,
Gumtree). Personal use only: one request at a time, 3s+ randomised delay, modest page
counts, never solves CAPTCHAs or logs in.

  python property_scraper.py                       # run every search in searches.json
  python property_scraper.py --new-only            # only listings not seen on earlier runs
  python property_scraper.py --url "<search url>"  # quick one-off (filters from searches.json)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scraper.pipeline import console_table, load_config, rescore_run, run
from scraper.sites import adapter_for

HERE = Path(__file__).resolve().parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "searches.json"), help="searches/filters file (default: searches.json)")
    ap.add_argument("--url", action="append", help="search URL to run instead of the config's searches (repeatable)")
    ap.add_argument("--new-only", action="store_true", help="output only listings not seen in earlier runs")
    ap.add_argument("--debug", action="store_true", help="save page-1 HTML of each search to output/debug/")
    ap.add_argument("--max-pages", type=int, help="cap on result pages per search (default: config max_pages or 5)")
    ap.add_argument("--no-browser", action="store_true", help="never fall back to headless Chromium")
    ap.add_argument("--no-filters", action="store_true", help="ignore the config's filters (export everything)")
    ap.add_argument("--no-locations", action="store_true",
                    help="don't open Property24 listing pages to find map locations (cached ones still apply)")
    ap.add_argument("--max-location-lookups", type=int, metavar="N",
                    help="cap on listing pages opened for map locations per run (default: config "
                         "max_location_lookups, else 40)")
    ap.add_argument("--delay", type=float, default=3.0, help="minimum seconds between requests (min 3)")
    ap.add_argument("--output-dir", default=str(HERE / "output"))
    ap.add_argument("--rescore", metavar="STAMP",
                    help="re-apply current filters/scoring to a saved run (e.g. 20261109_195417), no scraping")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config(Path(args.config))
    if args.rescore:
        res = rescore_run(cfg, args.rescore, Path(args.output_dir), apply_filters=not args.no_filters)
        print(f"Re-scored {args.rescore}: {res['export']['summary']['matching']} matches")
        print("\n15 cheapest matches:\n" + console_table(res["matching"]) + "\n")
        print(f"CSV:           {res['csv']}\nClaude export: {res['json']}")
        return 0
    if args.url:
        searches = [{"name": f"cli-{i + 1}", "url": u} for i, u in enumerate(args.url)]
    else:
        searches = cfg["searches"]
    if not searches:
        print("No searches: add some to searches.json or pass --url.", file=sys.stderr)
        return 2
    for s in searches:
        try:
            adapter_for(s["url"])
        except ValueError as e:
            print(f"Search '{s['name']}': {e}", file=sys.stderr)
            return 2

    res = run(cfg, searches, Path(args.output_dir), new_only=args.new_only, debug=args.debug,
              max_pages=args.max_pages, allow_browser=not args.no_browser,
              min_delay=max(3.0, args.delay), apply_filters=not args.no_filters,
              locate=not args.no_locations, max_location_lookups=args.max_location_lookups)

    print()
    print("Sites:")
    for site, st in res["site_stats"].items():
        status = f"BLOCKED - {st['blocked']}" if st["blocked"] else "ok"
        via = " (headless browser)" if st["browser"] else ""
        print(f"  {site:<16} searches={st['searches']} listings={st['listings']:<4} {status}{via}")
    summ = res["export"]["summary"]
    label = "new listings" if args.new_only else "listings"
    print(f"\n{summ['scraped_total']} {label} scraped, {summ['matching']} match the filters, "
          f"{summ['possible_cross_site_duplicates']} possible cross-site duplicates "
          f"({res['requests']} HTTP requests).")
    print(f"\n15 cheapest matches:\n{console_table(res['matching'])}\n")
    print(f"CSV:           {res['csv']}")
    print(f"Claude export: {res['json']}")
    return 1 if any(st["blocked"] for st in res["site_stats"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
