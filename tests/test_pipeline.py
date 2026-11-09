import datetime as dt

from scraper.core import Listing, parse_available, parse_price, parse_size, price_period_from
from scraper.pipeline import export_listing, mark_cross_site_duplicates, reject_reason


def L(site, lid, **kw):
    return Listing(site=site, listing_id=lid, url=f"https://x/{lid}", **kw)


def test_text_fallbacks():
    assert parse_price("R 12 500") == 12500
    assert parse_price("R12,500 per month") == 12500
    assert parse_price("R 1 250 000") == 1250000
    assert parse_price("R 13 000 2 Bedroom Apartment") == 13000
    assert parse_price("POA") is None
    assert parse_size("78 m²") == 78 and parse_size("78m2") == 78
    assert price_period_from("R 3 800 per day") == "day"
    assert price_period_from("R9000pm") == "month"
    today = dt.date(2026, 11, 9)
    assert parse_available("AVAILABLE:01 DEC", today) == "2026-12-01"
    assert parse_available("Available: 01 Jan", today) == "2027-01-01"
    assert parse_available("AVAILABLE NOW", today) == "now"


def test_cross_site_duplicates():
    a = L("property24", "1", price=13000, bedrooms=2, suburb="Table View", size_m2=63)
    b = L("privateproperty", "RR1", price=13000, bedrooms=2, suburb="Table View", size_m2=65)
    c = L("privateproperty", "RR2", price=13000, bedrooms=2, suburb="Table View", size_m2=80)
    d = L("property24", "2", price=13000, bedrooms=2, suburb="Table View")   # same site: not a cross-site dupe
    e = L("gumtree", "g1", price=13000, bedrooms=2, suburb="table view")     # size unknown -> dupe
    n = mark_cross_site_duplicates([b, c, a, d, e])
    assert b.dupe_of == "property24:1"
    assert c.dupe_of == "property24:2"        # sizes differ by 17 from a, but d has no size
    assert d.dupe_of is None and a.dupe_of is None
    assert e.dupe_of == "property24:1"
    assert n == 3


FILTERS = {"kind": "rent", "min_price": 4000, "max_price": 13000, "min_beds": 1, "max_beds": 2,
           "property_types": ["Apartment", "Townhouse"], "monthly_price_only": True,
           "include_suburbs": ["Table View", "Milnerton"], "exclude_suburbs": ["Parklands"],
           "exclude_keywords": ["student"]}


def test_filters():
    ok = L("p", "1", kind="rent", price=12000, price_period="month", bedrooms=2,
           property_type="Apartment", suburb="Table View")
    assert reject_reason(ok, FILTERS) is None
    assert reject_reason(L("p", "2", kind="rent", price=14000, suburb="Table View"), FILTERS) == "price>13000"
    assert reject_reason(L("p", "3", kind="rent", price=3800, price_period="day", suburb="Table View"),
                         FILTERS) == "priced per day"
    assert "excluded area" in reject_reason(L("p", "4", kind="rent", price=9000, suburb="Parklands"), FILTERS)
    assert "not in include" in reject_reason(L("p", "5", kind="rent", price=9000, suburb="Bellville"), FILTERS)
    assert reject_reason(L("p", "6", kind="rent", price=9000, suburb="Milnerton Ridge"), FILTERS) is None
    assert reject_reason(L("p", "7", kind="rent", price=9000, bedrooms=3, suburb="Table View"), FILTERS) == "beds>2"
    assert reject_reason(L("p", "8", kind="rent", price=9000, property_type="House", suburb="Table View"),
                         FILTERS) == "type=House"
    assert reject_reason(L("p", "9", kind="rent", price=None, suburb="Table View"), FILTERS) == "no price"
    # unknown beds/type are kept
    assert reject_reason(L("p", "10", kind="rent", price=9000, suburb="Table View"), FILTERS) is None
    assert "keyword" in reject_reason(L("p", "11", kind="rent", price=9000, suburb="Table View",
                                        excerpt="Ideal for a student"), FILTERS)


def test_export_drops_empty_fields():
    d = export_listing(L("property24", "1", kind="rent", price=12000, price_period="month", bedrooms=2.0))
    assert d == {"id": "property24:1", "kind": "rent", "price_zar": 12000, "beds": 2, "url": "https://x/1"}
