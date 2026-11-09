"""Map locations: parsing from real saved pages, the lookup cache, and no-go zones in the
pipeline. No network: fetchers and OpenStreetMap are stubbed."""
import json
import re
from pathlib import Path

import requests

from scraper.core import Blocked, Listing
from scraper.pipeline import (_listing_from_csv, build_export, export_listing, filter_rows,
                              locate_listings, lookup_suburbs, reject_reason, write_csv)
from scraper.sites.privateproperty import PrivateProperty
from scraper.sites.property24 import Property24

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- parsing
def test_property24_listing_page_location():
    html = read("property24_listing_detail.html")
    loc = Property24().detail_location(html)
    assert loc == {"lat": -33.826539, "lon": 18.497234, "precision": "exact",
                   "street_address": "1 145 On Blaauwberg, 145 Blaauwberg Road"}
    # independent source in the same page: the JSON-LD Place block
    ld = re.search(r'"streetAddress":"([^"]+)"[^{}]*\},"latitude":(-?[\d.]+),"longitude":(-?[\d.]+)', html)
    assert ld and ld.group(1) == loc["street_address"]
    assert (float(ld.group(2)), float(ld.group(3))) == (loc["lat"], loc["lon"])


def test_property24_inexact_location_is_approx():
    html = read("property24_listing_detail.html").replace('"haveExactLocation":true', '"haveExactLocation":false')
    loc = Property24().detail_location(html)
    assert loc["precision"] == "approx" and "street_address" not in loc


def test_property24_results_page_centroid_is_ignored():
    # results pages carry South Africa's centroid (-30.969313, 24.22227), not a listing's location
    assert Property24().detail_location(read("property24_tableview_rent.html")) is None
    assert all(li.lat is None for li in Property24().parse(read("property24_tableview_rent.html"), "https://www.property24.com"))


def test_privateproperty_card_geo():
    html = read("privateproperty_tableview_rent.html")
    listings = PrivateProperty().parse(html, "https://www.privateproperty.co.za")
    pinned = [li for li in listings if li.lat is not None]
    # independent count: GeoCoordinates blocks in the raw page
    assert len(pinned) == html.count('"@type":"GeoCoordinates"') == 2
    assert {(li.lat, li.lon) for li in pinned} == {(-33.83263, 18.51524), (-33.8197181, 18.4953155)}
    assert all(li.location_precision == "exact" for li in pinned)
    assert all(li.location_precision is None for li in listings if li.lat is None)


# --------------------------------------------------------------------------- lookups
class StubFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url):
        self.calls.append(url)
        p = self.pages[url]
        if isinstance(p, Exception):
            raise p
        return p


def P24(lid, **kw):
    return Listing(site="property24", listing_id=lid, url=f"https://www.property24.com/x/{lid}", **kw)


def test_locate_listings_fetches_once_and_caches(tmp_path):
    detail = read("property24_listing_detail.html")
    gone = requests.HTTPError("404")
    gone.response = type("R", (), {"status_code": 404})()
    a, b, c, d = P24("1"), P24("2"), P24("3"), P24("4")
    f = StubFetcher({a.url: detail, b.url: "<html>no map here</html>", c.url: gone, d.url: detail})
    ads = {"property24": Property24()}
    cache = tmp_path / "locations.json"

    n = locate_listings([a, b, c, d], [a, b, c, d], ads, f, cache, max_lookups=3)
    assert n == 2 and len(f.calls) == 3                 # capped at 3; the 404 isn't a fetched page
    assert (a.lat, a.lon, a.location_precision) == (-33.826539, 18.497234, "exact")
    assert a.address == "1 145 On Blaauwberg, 145 Blaauwberg Road"
    assert b.lat is None and d.lat is None
    saved = json.loads(cache.read_text())
    assert saved["property24:2"]["lat"] is None and saved["property24:3"]["note"] == "listing page gone"

    # second run: cached answers apply, only the one never tried gets fetched
    a2, b2, c2, d2 = P24("1"), P24("2"), P24("3"), P24("4")
    f2 = StubFetcher({d2.url: detail})
    locate_listings([a2, b2, c2, d2], [a2, b2, c2, d2], ads, f2, cache, max_lookups=40)
    assert f2.calls == [d2.url] and a2.lat == -33.826539 and d2.location_precision == "exact"

    # no fetcher (e.g. re-score): cache only
    a3 = P24("1")
    assert locate_listings([a3], [a3], ads, None, cache) == 0 and a3.lat == -33.826539


def test_locate_listings_stops_site_when_blocked(tmp_path):
    a, b = P24("1"), P24("2")
    f = StubFetcher({a.url: Blocked("HTTP 403"), b.url: read("property24_listing_detail.html")})
    ads = {"property24": Property24()}
    locate_listings([a, b], [a, b], ads, f, tmp_path / "loc.json")
    assert f.calls == [a.url] and ads["property24"].blocked     # never tries the next page


def test_sites_without_detail_locations_are_not_fetched(tmp_path):
    pp = Listing(site="privateproperty", listing_id="RR1", url="https://pp/RR1")
    f = StubFetcher({})
    assert locate_listings([pp], [pp], {"privateproperty": PrivateProperty()}, f, tmp_path / "l.json") == 0
    assert f.calls == []


# --------------------------------------------------------------------------- zones in the pipeline
ZONE = {"name": "East of road", "type": "road_side", "points": [[-33.86, 18.50], [-33.80, 18.50]], "side": "right"}
FILTERS = {"kind": "rent", "max_price": 15000, "no_go_zones": [ZONE]}


def R(lid, **kw):
    kw.setdefault("price", 12000)
    return Listing(site="privateproperty", listing_id=lid, url=f"https://pp/{lid}", kind="rent",
                   price_period="month", **kw)


def outline(lon_from, lon_to):
    ring = [[lon_from, -33.85], [lon_to, -33.85], [lon_to, -33.81], [lon_from, -33.81], [lon_from, -33.85]]
    return {"lat": -33.83, "lon": (lon_from + lon_to) / 2, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_zones_filter_and_flag():
    east = R("1", lat=-33.83, lon=18.52, location_precision="exact", suburb="Eastside")
    west = R("2", lat=-33.83, lon=18.48, location_precision="exact", suburb="Westside")
    approx = R("3", lat=-33.83, lon=18.52, location_precision="approx")
    by_suburb = R("4", suburb="Eastside")
    straddle = R("5", suburb="Middle")
    unknown = R("6", suburb="Nowhere")
    places = {"eastside": outline(18.51, 18.53), "middle": outline(18.48, 18.52)}
    rows = filter_rows([east, west, approx, by_suburb, straddle, unknown], FILTERS, places, {})
    reasons = {li.listing_id: r for li, r, _ in rows}
    assert reasons["1"] == "in no-go zone 'East of road'"
    assert reasons["4"] == "suburb is inside no-go zone 'East of road'"
    assert reasons["2"] is None and reasons["3"] is None and reasons["5"] is None and reasons["6"] is None
    assert any("approximate" in f for f in approx.flags)
    assert any("straddles" in f for f in straddle.flags)
    assert not west.flags and not unknown.flags
    # other filters still come first
    assert reject_reason(R("7", price=20000, lat=-33.83, lon=18.52, location_precision="exact"), FILTERS) == "price>15000"


def test_lookup_suburbs_only_when_zones_and_only_unpinned():
    class FakePlaces:
        lookups = 0

        def __init__(self):
            self.asked = []

        def suburb(self, name, bbox):
            self.asked.append(name)
            return outline(18.51, 18.53)

    fp = FakePlaces()
    cands = [R("1", suburb="Pinned", lat=-33.8, lon=18.5, location_precision="exact"), R("2", suburb="Table View"),
             R("3", suburb="table view"), R("4")]
    out = lookup_suburbs(cands, FILTERS, fp)
    assert fp.asked == ["Table View"]          # once per suburb, case-insensitive
    assert set(out) == {"table view"}
    assert lookup_suburbs(cands, {"kind": "rent"}, fp) == {}


def test_export_and_csv_round_trip(tmp_path):
    li = R("1", lat=-33.8265391, lon=18.4972344, location_precision="exact", suburb="Table View")
    d = export_listing(li)
    assert d["lat"] == -33.826539 and d["lon"] == 18.497234 and d["location"] == "exact"
    assert "lat" not in export_listing(R("2"))           # null fields dropped
    exp = build_export({}, [], FILTERS, False, [li], [li], 0)
    zones = exp["filters_applied"]["no_go_zones"]
    assert zones[0]["name"] == "East of road" and "points" not in zones[0]
    assert exp["summary"]["with_map_location"] == 1
    p = tmp_path / "x.csv"
    write_csv(p, [(li, None, True)])
    import csv
    with p.open(encoding="utf-8-sig") as fh:
        back = _listing_from_csv(next(csv.DictReader(fh)))
    assert (back.lat, back.lon, back.location_precision) == (li.lat, li.lon, "exact")


def test_config_validation_of_zones():
    import web_ui
    ok = {"searches": [], "filters": {"no_go_zones": [ZONE]}}
    assert web_ui._validate(ok) == []
    bad = {"searches": [], "filters": {"no_go_zones": [dict(ZONE, side="up"), {"name": "x", "type": "area", "points": [[1, 2]]}]}}
    errs = web_ui._validate(bad)
    assert len(errs) == 2 and "East of road" in errs[0] and "3 points" in errs[1]
    assert web_ui._validate({"searches": [], "filters": {"no_go_zones": "nope"}})


def test_property24_hidden_address():
    # real page where the advertiser hides the address: no map widget, a 'contact for address' form
    html = read("property24_listing_detail_hidden_address.html")
    assert "GoogleMapsFactory" not in html
    assert Property24().detail_location(html) == {"lat": None, "note": "address hidden by the advertiser"}
