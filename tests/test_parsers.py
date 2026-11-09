"""Parse real result pages saved from the live sites (tests/fixtures/, Nov 2026) and check
price, beds and URL for every card against an independent source in the same page."""
import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper.core import finalise
from scraper.sites import Gumtree, PrivateProperty, Property24, adapter_for
from scraper.sites.gumtree import devalue_decode

FIX = Path(__file__).parent / "fixtures"

P24_URL = "https://www.property24.com/to-rent/table-view/blouberg/western-cape/11589"
PP_URL = "https://www.privateproperty.co.za/to-rent/western-cape/cape-town/blouberg/table-view/444"
GT_URL = "https://www.gumtree.co.za/property/apartment-flat/table-view?propertyListingType=RENT"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def parsed(cls, fixture, url):
    return [finalise(li) for li in cls().parse(load(fixture), url)]


# --------------------------------------------------------------------------- Property24
@pytest.fixture(scope="module")
def p24():
    return parsed(Property24, "property24_tableview_rent.html", P24_URL)


def test_p24_finds_every_card(p24):
    soup = BeautifulSoup(load("property24_tableview_rent.html"), "lxml")
    ids = {c["data-listing-number"] for c in soup.select("[data-listing-number]")}
    assert len(p24) == len(ids) == 21
    assert {li.listing_id for li in p24} == ids


def test_p24_every_card_price_beds_url(p24):
    soup = BeautifulSoup(load("property24_tableview_rent.html"), "lxml")
    for li in p24:
        card = [c for c in soup.select(f"[data-listing-number='{li.listing_id}']")
                if not c.select_one("[data-listing-number]")][0]
        # URL: absolute, clean, ends with the listing number, rental path
        assert re.fullmatch(rf"https://www\.property24\.com/to-rent/[a-z0-9-]+/[a-z0-9-]+/western-cape/\d+/{li.listing_id}", li.url)
        assert li.kind == "rent"
        # price: microdata where present, else the visible 'R x xxx'
        meta = card.select_one("[itemprop=price][content]")
        if meta and meta["content"].isdigit():
            assert li.price == int(meta["content"])
        elif li.price_text and li.price_text.startswith("POA"):
            assert li.price is None
        else:
            assert li.price is not None and 1000 <= li.price < 100000, li.price_text
        # beds: the 'Bedrooms' feature icon, when the card has one
        bed = card.select_one(".p24_featureDetails[title=Bedrooms]")
        if bed:
            assert li.bedrooms == float(bed.get_text(strip=True))
        else:
            assert li.property_type == "Commercial"


def test_p24_spot_checks(p24):
    by = {li.listing_id: li for li in p24}
    a = by["117704254"]
    assert (a.price, a.bedrooms, a.bathrooms, a.parking, a.size_m2) == (20000, 2, 2, 2, 75)
    assert a.title == "2 Bedroom Apartment" and a.suburb == "Beachfront"
    assert a.address == "Atlantic Terraces, 32 Coral Road"
    assert a.agency == "RE/MAX Property Associates - Western Cape"
    assert a.available == "now" and "Available Now" in a.badges
    # 'pro' (promoted) tile: different markup, price must not swallow the '2' of '2 Bedroom'
    pro = by["117661901"]
    assert (pro.price, pro.bedrooms, pro.size_m2) == (13000, 2, 56)
    assert pro.title == "2 Bedroom Apartment" and pro.suburb == "Table View"
    assert pro.agency == "RE/MAX Property Associates - Western Cape"   # learned from logo
    # listed by owner
    assert by["117745835"].agency == "Private landlord"
    # house: 'Erf Size' is land, not floor size
    assert by["117789599"].size_m2 is None
    # short-term let priced per day
    d = by["117785149"]
    assert d.price == 3800 and d.price_period == "day" and "Priced per day" in d.badges
    # dated availability
    assert by["117571973"].available.endswith("-12-01")


# --------------------------------------------------------------------------- Private Property
@pytest.fixture(scope="module")
def pp():
    return parsed(PrivateProperty, "privateproperty_tableview_rent.html", PP_URL)


def test_pp_every_card_price_beds_url(pp):
    soup = BeautifulSoup(load("privateproperty_tableview_rent.html"), "lxml")
    cards = {re.search(r"(RR\d+)$", a["href"]).group(1): a for a in soup.select("a.listing-result")}
    assert len(pp) == len(cards) == 20
    for li in pp:
        card = cards[li.listing_id]
        ld = json.loads(card.find("script", type="application/ld+json").string)
        assert re.fullmatch(r"RR\d{6,8}", li.listing_id)
        assert li.url == ld["url"]
        assert li.kind == "rent"
        ld_beds = {p["name"]: p["value"] for p in ld["additionalProperty"]}.get("Bedrooms")
        assert li.bedrooms == float(ld_beds)
        visible = card.select_one(".listing-result__price").get_text(" ", strip=True)
        digits = int(re.sub(r"\D", "", visible.split("per")[0]))
        assert li.price == digits
        assert li.agency


def test_pp_spot_checks(pp):
    by = {li.listing_id: li for li in pp}
    a = by["RR4642267"]
    assert (a.price, a.bedrooms, a.bathrooms, a.parking, a.size_m2) == (11000, 2, 1, 1, 46)
    assert a.title == "2 Bedroom Townhouse" and a.property_type == "Townhouse"
    assert a.suburb == "Sunridge" and a.agency == "RE/MAX"
    assert a.available.endswith("-12-01") and "New" in a.badges
    b = by["RR4369685"]
    assert b.address == "208 San Norina Gardens, 32 Tritonia Road" and b.price == 12900
    assert by["RR4820803"].price_period == "day"
    assert by["RR4812456"].available == "now"


# --------------------------------------------------------------------------- Gumtree
def _search_results(name):
    raw = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', load(name), re.S).group(1)
    return devalue_decode(json.loads(raw))["pinia"]["listings"]["searchResults"]


@pytest.mark.parametrize("fixture", ["gumtree_tableview_apartments_rent.html",
                                     "gumtree_tableview_apartments_rent_p2.html"])
def test_gumtree_every_card_price_beds_url(fixture):
    html = load(fixture)
    results = {r["ref"]: r for r in _search_results(fixture)}
    ls = parsed(Gumtree, fixture, GT_URL)
    assert len(ls) == len(results) == 25
    for li in ls:
        r = results[li.listing_id]
        pr = r["price"]
        expected = pr["priceMinor"] // 100 if pr["priceType"] == "ASKING_PRICE" else None
        assert li.price == expected
        beds = {a["key"]: a["value"] for a in r["summaryAttributes"]}.get("numberBedrooms")
        if beds and beds[0].isdigit():
            assert li.bedrooms == float(re.match(r"\d+", beds).group(0))
        # URL is the real card link present in the page
        assert li.url.endswith("-" + li.listing_id)
        assert f'href="{li.url.replace("https://www.gumtree.co.za", "")}"' in html
        assert li.kind == "rent"


def test_gumtree_ignores_sponsored_and_spot_checks():
    ls = parsed(Gumtree, "gumtree_tableview_apartments_rent.html", GT_URL)
    by = {li.listing_id: li for li in ls}
    a = by["ls26yyxyhybljhv1kwy"]
    assert (a.price, a.bedrooms, a.bathrooms, a.suburb) == (8000, 1, 1, "Table View")
    assert a.title == "Flatlet to rent" and "Secure Parking" in a.badges
    assert a.available == "2026-10-01"
    assert by["ls26nt6bwhdpmyhc5xv"].bedrooms == 0      # STUDIO_OR_BACHELOR_PAD
    assert by["ls26nt6bwhdpmyhc5xv"].size_m2 is None    # '4 m²' is junk
    assert by["ls263hb0rnm6x65jnsb"].size_m2 is None    # '1 m²'


def test_gumtree_unfiltered_page_kind():
    ls = parsed(Gumtree, "gumtree_tableview_all.html", "https://www.gumtree.co.za/property/table-view")
    kinds = {li.listing_id: li.kind for li in ls}
    assert kinds["ls2593930dm29vpkk5k"] == "sale"       # propertyListingType=SALE
    assert kinds["ls26yyxyhybljhv1kwy"] == "rent"
    assert kinds["ls26yhn4lf97dgyp5ng"] == "rent"       # Rooms & Roommates category


# --------------------------------------------------------------------------- pagination / routing
def test_page_urls():
    assert Property24().page_url(P24_URL, 1) == P24_URL
    assert Property24().page_url(P24_URL, 3) == P24_URL + "/p3"
    assert Property24().page_url(P24_URL + "/p2?sp=pt%3d13000", 4) == P24_URL + "/p4?sp=pt%3d13000"
    assert PrivateProperty().page_url(PP_URL + "?tp=13000", 2) == PP_URL + "?tp=13000&page=2"
    assert PrivateProperty().page_url(PP_URL + "?page=5", 1) == PP_URL
    assert Gumtree().page_url(GT_URL, 2) == GT_URL + "&page=2"


def test_adapter_routing():
    assert adapter_for(P24_URL) is Property24
    assert adapter_for(PP_URL) is PrivateProperty
    assert adapter_for(GT_URL) is Gumtree
    with pytest.raises(ValueError):
        adapter_for("https://www.example.com/x")
