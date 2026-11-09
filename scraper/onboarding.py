"""Onboarding: turn 'what's your goal?' answers into filters, scoring keywords, notes for Claude
and ready-made search URLs.

Only areas whose site IDs have been checked against the live sites are in REGIONS. Anything else
the user types becomes an include-filter, and they add a search URL for it in Settings."""
from __future__ import annotations

from typing import Optional

from .analysis import DEFAULT_SCORING

# Verified Nov 2026: each URL pattern below returned real results pages for rent and sale.
REGIONS = {
    "table_view": {
        "label": "Table View & Blouberg side",
        "suburbs": ["Table View", "Tableview", "Sunridge", "West Riding", "Flamingo Vlei"],
        "p24": "table-view/blouberg/western-cape/11589",
        "pp": "western-cape/cape-town/blouberg/table-view/444",
        "gumtree": "table-view",
        "pages": 4,
    },
    "milnerton": {
        "label": "Milnerton, Century City & Burgundy Estate",
        "suburbs": ["Century City", "Burgundy Estate", "Burgundy", "Milnerton", "Milnerton Ridge",
                    "Royal Ascot", "Summer Greens", "Sanddrift", "Tygerhof", "Rugby"],
        "p24": "milnerton/western-cape/433",
        "pp": "western-cape/cape-town/milnerton/1399",
        "gumtree": "milnerton",
        "pages": 6,
    },
}

PROPERTY_TYPES = ["Apartment", "Townhouse", "Simplex", "Duplex", "Cluster", "Penthouse", "Loft",
                  "Cottage", "House"]

PRIORITIES = {
    "value": "Best value for money",
    "security": "Security (secure complex, access control)",
    "parking": "Parking",
    "pets": "Pet friendly",
    "available_soon": "Available soon",
    "space": "As much space as possible",
    "furnished": "Furnished",
    "unfurnished": "Unfurnished",
    "near_transport": "Near transport / MyCiTi",
}

PET_KEYWORDS = ["pet friendly", "pet-friendly", "pets allowed", "pets welcome", "pets considered"]
TRANSPORT_KEYWORDS = ["myciti", "close to transport", "near transport", "bus route"]

RENT_EXCLUDES = ["room to rent", "room for rent", "student", "airbnb", "short term", "short-term",
                 "holiday", "per night", "office", "commercial", "parking bay", "storage"]
SALE_EXCLUDES = ["vacant land", "office", "commercial", "parking bay", "storage", "share block"]


def catalog() -> dict:
    return {
        "regions": [{"id": k, "label": v["label"], "suburbs": v["suburbs"]} for k, v in REGIONS.items()],
        "property_types": PROPERTY_TYPES,
        "priorities": [{"id": k, "label": v} for k, v in PRIORITIES.items()],
    }


def _int(v) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _list(v) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    return [s.strip() for s in (v or []) if isinstance(s, str) and s.strip()]


def _rand(n: int) -> str:
    return "R" + f"{n:,}".replace(",", " ")


def validate(a: dict) -> list[str]:
    errors = []
    if a.get("goal") not in ("rent", "buy"):
        errors.append("Choose rent or buy.")
    if not _int(a.get("budget_max")):
        errors.append("Set a maximum budget.")
    elif _int(a.get("budget_min")) and _int(a["budget_min"]) > _int(a["budget_max"]):
        errors.append("Minimum budget is above the maximum.")
    if not a.get("regions") and not _list(a.get("include_suburbs")):
        errors.append("Pick at least one area (or type one in).")
    if not a.get("types"):
        errors.append("Pick at least one property type.")
    unknown = [r for r in a.get("regions") or [] if r not in REGIONS]
    if unknown:
        errors.append(f"Unknown area: {', '.join(unknown)}")
    return errors


def build(a: dict) -> dict:
    """answers -> {filters, scoring, notes_for_claude, searches, summary}. Call validate() first."""
    rent = a["goal"] == "rent"
    kind = "rent" if rent else "sale"
    bmax = _int(a["budget_max"])
    bmin = _int(a.get("budget_min")) or (4000 if rent else 300000)
    beds = sorted({int(b) for b in a.get("beds") or [] if str(b).isdigit()})
    types = [t for t in a.get("types") or [] if t in PROPERTY_TYPES]
    regions = [r for r in a.get("regions") or [] if r in REGIONS]
    prios = [p for p in a.get("priorities") or [] if p in PRIORITIES]

    include = []
    for r in regions:
        include += REGIONS[r]["suburbs"]
    include += _list(a.get("include_suburbs"))
    include = list(dict.fromkeys(include))

    filters = {
        "kind": kind,
        "min_price": bmin,
        "max_price": bmax,
        "min_beds": beds[0] if beds else None,
        "max_beds": None if not beds or beds[-1] >= 4 else beds[-1],     # '4' means 4+
        "min_size_m2": _int(a.get("min_size_m2")),
        "monthly_price_only": rent,
        "property_types": types,
        "include_suburbs": include,
        "exclude_suburbs": _list(a.get("exclude_suburbs")),
        "exclude_keywords": list(RENT_EXCLUDES if rent else SALE_EXCLUDES),
    }

    bonus: list[str] = []
    if "security" in prios:
        bonus += DEFAULT_SCORING["bonus_keywords"]
    if "pets" in prios:
        bonus += PET_KEYWORDS
    if "parking" in prios and "secure parking" not in bonus:
        bonus.append("secure parking")
    if "near_transport" in prios:
        bonus += TRANSPORT_KEYWORDS
    scoring = {"bonus_keywords": list(dict.fromkeys(bonus)),
               "red_flag_keywords": list(DEFAULT_SCORING["red_flag_keywords"])}

    return {"filters": filters, "scoring": scoring,
            "notes_for_claude": notes(a, kind, bmin, bmax, beds, types, regions, prios, include, filters),
            "searches": searches(kind, regions, types, bmax)}


def notes(a, kind, bmin, bmax, beds, types, regions, prios, include, filters) -> str:
    rent = kind == "rent"
    if beds:
        labels = ["studio" if b == 0 else ("4+ bedrooms" if b >= 4 else f"{b} bedroom") for b in beds]
        beds_txt = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]
    else:
        beds_txt = "any number of bedrooms"
    type_txt = " / ".join(t.lower() for t in types) or "any property type"
    area_txt = "; ".join(REGIONS[r]["label"] for r in regions)
    extra_areas = [s for s in _list(a.get("include_suburbs"))]
    if extra_areas:
        area_txt = (area_txt + "; " if area_txt else "") + ", ".join(extra_areas)
    price_txt = f"{_rand(bmin)}–{_rand(bmax)}" + (" per month" if rent else "")
    lines = [
        f"Goal: {'a long-term rental' if rent else 'to buy a property'}: {beds_txt}, {type_txt}, "
        f"budget {price_txt}.",
        f"Areas: {area_txt}.",
    ]
    if filters["exclude_suburbs"]:
        lines.append(f"Off limits: {', '.join(filters['exclude_suburbs'])}.")
    if filters["min_size_m2"]:
        lines.append(f"Minimum floor size: {filters['min_size_m2']} m².")
    if prios:
        lines.append("What matters most (in this order): " + "; ".join(PRIORITIES[p] for p in prios) + ".")
    extra = (a.get("extra") or "").strip()
    if extra:
        lines.append(f"In my own words: {extra}")
    lines.append("Shortlist the best options for value and fit with the above. Flag duplicates, "
                 + ("short-term lets, " if rent else "")
                 + "scams and too-good-to-be-true prices, and say what is unknown for each pick.")
    return "\n".join(lines)


def searches(kind: str, regions: list[str], types: list[str], bmax: int) -> list[dict]:
    rent = kind == "rent"
    seg = "to-rent" if rent else "for-sale"
    gt_kind = "RENT" if rent else "SALE"
    word = "rent" if rent else "sale"
    cap = _rand(bmax).replace("R", "<=R")
    out: list[dict] = []
    for r in regions:
        reg = REGIONS[r]
        label = reg["label"]
        out.append({"name": f"P24 {label} {word} {cap}",
                    "url": f"https://www.property24.com/{seg}/{reg['p24']}?sp=pt%3d{bmax}",
                    "max_pages": reg["pages"]})
        out.append({"name": f"PP {label} {word} {cap}",
                    "url": f"https://www.privateproperty.co.za/{seg}/{reg['pp']}?tp={bmax}",
                    "max_pages": 4})
        gt_cats = []
        if any(t in types for t in ("Apartment", "Penthouse", "Loft")):
            gt_cats.append(("apartment-flat", "apartments"))
        if any(t in types for t in ("House", "Townhouse", "Cluster", "Simplex", "Duplex", "Cottage")):
            gt_cats.append(("house", "houses"))
        for cat, cat_label in gt_cats:
            out.append({"name": f"Gumtree {label} {cat_label} {word}",
                        "url": f"https://www.gumtree.co.za/property/{cat}/{reg['gumtree']}?propertyListingType={gt_kind}",
                        "max_pages": 2})
    # Uncapped samples so the fair-price curve sees the whole market (see analysis.METHOD)
    for r in regions:
        reg = REGIONS[r]
        out.append({"name": f"Market sample: P24 {reg['label']} (no price cap)",
                    "url": f"https://www.property24.com/{seg}/{reg['p24']}", "max_pages": 2})
        out.append({"name": f"Market sample: PP {reg['label']} (no price cap)",
                    "url": f"https://www.privateproperty.co.za/{seg}/{reg['pp']}", "max_pages": 2})
    return out


def apply_to_config(cfg: dict, built: dict, answers: dict, replace_searches: bool) -> dict:
    """Merge a built goal into a config dict (keeps unrelated keys and any custom searches
    unless replace_searches)."""
    new = dict(cfg)
    new["filters"] = {**(cfg.get("filters") or {}), **built["filters"]}
    new["scoring"] = {**(cfg.get("scoring") or {}), **built["scoring"]}
    new["notes_for_claude"] = built["notes_for_claude"]
    if replace_searches and built["searches"]:
        new["searches"] = built["searches"]
    new["onboarding"] = {"done": True, "answers": answers}
    return new
