from urllib.parse import urlsplit

from scraper import onboarding
from scraper.core import finalise
from scraper.pipeline import reject_reason
from scraper.sites import Property24, adapter_for
from tests.test_parsers import load

RENT = {"goal": "rent", "budget_max": 13000, "budget_min": None, "beds": [1, 2], "types": ["Apartment", "Townhouse"],
        "regions": ["table_view", "milnerton"], "include_suburbs": "", "exclude_suburbs": "Parklands",
        "priorities": ["value", "security", "pets"], "extra": "Small dog."}


def test_validate():
    assert onboarding.validate(RENT) == []
    errs = onboarding.validate({"goal": "x", "budget_max": None, "regions": [], "types": []})
    assert len(errs) == 4
    assert onboarding.validate(dict(RENT, budget_min=20000)) == ["Minimum budget is above the maximum."]
    assert "Unknown area" in onboarding.validate(dict(RENT, regions=["atlantis"]))[0]


def test_build_rent():
    b = onboarding.build(RENT)
    f = b["filters"]
    assert f["kind"] == "rent" and f["max_price"] == 13000 and f["min_price"] == 4000
    assert (f["min_beds"], f["max_beds"]) == (1, 2)
    assert "Century City" in f["include_suburbs"] and "Table View" in f["include_suburbs"]
    assert f["exclude_suburbs"] == ["Parklands"] and f["monthly_price_only"] is True
    assert "secure complex" in b["scoring"]["bonus_keywords"] and "pet friendly" in b["scoring"]["bonus_keywords"]
    n = b["notes_for_claude"]
    assert "long-term rental" in n and "R13 000 per month" in n and "Parklands" in n and "Small dog." in n
    assert "1 bedroom or 2 bedroom" in n and "Security" in n
    # searches: every URL routes to an adapter; capped searches carry the budget; samples don't
    for s in b["searches"]:
        adapter_for(s["url"])
        assert "/to-rent/" in s["url"] or "RENT" in s["url"]
    capped = [s for s in b["searches"] if "Market sample" not in s["name"] and "gumtree" not in s["url"]]
    assert capped and all("13000" in s["url"] for s in capped)
    assert all("13000" not in s["url"] for s in b["searches"] if "Market sample" in s["name"])
    assert any("apartment-flat" in s["url"] for s in b["searches"])
    assert any("/property/house/" in s["url"] for s in b["searches"])     # townhouse -> house category


def test_build_buy_and_4plus():
    b = onboarding.build(dict(RENT, goal="buy", budget_max=2500000, beds=[3, 4], types=["House"],
                              regions=["milnerton"], priorities=[]))
    f = b["filters"]
    assert f["kind"] == "sale" and f["min_price"] == 300000 and f["monthly_price_only"] is False
    assert (f["min_beds"], f["max_beds"]) == (3, None)          # 4 means 4+
    assert all(("/for-sale/" in s["url"]) or ("SALE" in s["url"]) for s in b["searches"])
    assert not any("apartment-flat" in s["url"] for s in b["searches"])
    assert b["scoring"]["bonus_keywords"] == []
    assert "to buy" in b["notes_for_claude"]


def test_typed_in_area_only_has_no_searches():
    b = onboarding.build(dict(RENT, regions=[], include_suburbs="Sunningdale, Edgemead"))
    assert b["searches"] == [] and b["filters"]["include_suburbs"] == ["Sunningdale", "Edgemead"]


def test_apply_to_config_keeps_other_keys():
    cfg = {"max_pages": 4, "searches": [{"name": "mine", "url": "https://www.property24.com/x/1"}],
           "filters": {"min_size_m2": 40}, "scoring": {"bonus_keywords": ["x"], "parking_points": 6}}
    b = onboarding.build(RENT)
    kept = onboarding.apply_to_config(cfg, b, RENT, replace_searches=False)
    assert kept["searches"] == cfg["searches"] and kept["max_pages"] == 4
    assert kept["scoring"]["parking_points"] == 6 and kept["onboarding"]["done"] is True
    replaced = onboarding.apply_to_config(cfg, b, RENT, replace_searches=True)
    assert replaced["searches"] == b["searches"]


def test_p24_sale_page_from_live_site():
    url = "https://www.property24.com/for-sale/table-view/blouberg/western-cape/11589?sp=pt%3d2000000"
    ls = [finalise(li) for li in Property24().parse(load("property24_tableview_sale.html"), url)]
    assert len(ls) >= 20
    assert all(li.kind == "sale" for li in ls)                  # incl. /new-developments-for-sale/ tiles
    dev = [li for li in ls if "New development" in li.badges]
    assert dev and all(urlsplit(li.url).path.startswith("/new-developments-for-sale/") for li in dev)
    homes = [li for li in ls if not dev or li not in dev]
    assert all(li.price and li.price >= 500000 for li in homes if li.price)
    sale_filters = onboarding.build(dict(RENT, goal="buy", budget_max=2000000))["filters"]
    assert any(reject_reason(li, sale_filters) is None for li in homes)
