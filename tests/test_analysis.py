import datetime as dt

from scraper.analysis import build_market, loess, score_listings
from scraper.core import Listing


def L(i, price, size=None, beds=2, **kw):
    return Listing(site="property24", listing_id=str(i), url=f"https://x/{i}", kind="rent", price=price,
                   price_period="month", size_m2=size, bedrooms=beds, property_type="Apartment", **kw)


def test_loess_recovers_a_line():
    xs = [float(x) for x in range(20, 120, 5)]
    ys = [3000 + 100 * x for x in xs]
    out = loess(xs, ys, [40.0, 80.0])
    assert abs(out[0] - 7000) < 1 and abs(out[1] - 11000) < 1


def market_listings():
    # steep up to 50 m2, nearly flat 50-80 m2, steep again after: the flat part is the sweet spot
    out = []
    for i, s in enumerate(range(25, 121, 3)):
        if s <= 50:
            p = 4000 + 160 * s
        elif s <= 80:
            p = 12000 + 15 * (s - 50)
        else:
            p = 12450 + 200 * (s - 80)
        out.append(L(i, int(p), s))
    return out


def test_market_curve_and_sweet_spot():
    ls = market_listings()
    m = build_market(ls, {"min_price": 2000, "max_price": 20000})
    curve = m["fair_price_curve"]
    assert len(curve) == 40
    assert all(b[1] >= a[1] for a, b in zip(curve, curve[1:]))       # non-decreasing
    sw = m["sweet_spot"]
    assert 45 <= sw["size_m2_from"] <= 62 and 68 <= sw["size_m2_to"] <= 85, sw
    assert sw["marginal_rent_per_extra_m2"] < sw["typical_marginal_rent_per_extra_m2"]
    assert sw["within_budget"] == "yes"
    tight = build_market(market_listings(), {"min_price": 2000, "max_price": 10000})["sweet_spot"]
    assert tight["within_budget"] == "no" and tight["extra_rent_to_reach"] > 0
    assert tight["budget_buys_up_to_m2"] < tight["size_m2_from"]


def test_curve_excludes_non_market_listings():
    ls = market_listings() + [
        L(900, 900, 60),                                              # below min_price
        Listing(site="p", listing_id="d", url="u", kind="rent", price=3500, price_period="day", size_m2=60),
        Listing(site="p", listing_id="c", url="u", kind="rent", price=25000, size_m2=60, property_type="Commercial"),
    ]
    assert build_market(ls, {"min_price": 2000})["points_used"] == len(market_listings())


def test_scores():
    m = build_market(market_listings(), {"min_price": 2000, "max_price": 20000})
    fair65 = [p for p in m["fair_price_curve"] if p[0] >= 65][0][1]
    cheap = L("a", int(fair65 * 0.85), 65, parking=1, available="now", excerpt="Lovely unit in a secure complex")
    pricey = L("b", int(fair65 * 1.2), 65)
    scam = L("c", int(fair65 * 0.4), 65, excerpt="I am currently overseas, send money for keys")
    nosize = L("d", 9000, None, beds=2)
    score_listings([cheap, pricey, scam, nosize], m, today=dt.date(2026, 11, 9))
    assert 13 <= cheap.value_pct <= 17
    assert cheap.score > 80 and "in the size sweet spot" in cheap.flags
    assert pricey.value_pct < 0 and pricey.score < 50
    assert scam.score < cheap.score and any("scam pattern" in f for f in scam.flags)
    assert any("below market" in f for f in scam.flags)
    assert nosize.fair_price == m["median_rent_by_beds"]["2"]
    assert nosize.score <= 50 + 15 + 3 + 4 + 8
    assert "no floor size: fair price from bedroom median" in nosize.flags
