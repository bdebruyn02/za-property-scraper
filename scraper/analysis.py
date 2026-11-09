"""Market analysis: a smoothed 'fair price' curve (LOESS spline of rent vs floor size), the
size 'sweet spot', and a transparent 0-100 value score per listing.

Everything here is deliberately simple and explainable, because the export tells Claude
exactly how these numbers were made (see METHOD / SCORING_NOTES)."""
from __future__ import annotations

import re
import statistics
from typing import Optional

from .core import Listing

NON_RESIDENTIAL = {"Commercial", "Land", "Farm", "Room"}

DEFAULT_SCORING = {
    "bonus_keywords": ["secure complex", "security complex", "secure estate", "security estate",
                       "access control", "access-controlled", "24 hour security", "24h security",
                       "24-hour security", "gated", "guard", "electric fence", "biometric", "secure parking"],
    "bonus_keyword_points": 8,
    "red_flag_keywords": ["deposit before viewing", "pay before viewing", "pay to view",
                          "whatsapp only", "currently overseas", "western union", "send money"],
    "red_flag_points": -20,
    "parking_points": 4,
    "available_soon_points": 3,
    "available_soon_days": 45,
    "sweet_spot_points": 3,
    "suspicious_value_pct": 40,
    "suspicious_points": -15,
}

METHOD = (
    "fair_price_curve is a LOESS (locally weighted linear regression, tricube weights, span 60%) fit "
    "of price (monthly rent when kind is rent, asking price when kind is sale) against floor size over "
    "every residential listing of that kind scraped this run "
    "(including ones that failed your filters; commercial, rooms, land and per-day lets excluded), "
    "then forced non-decreasing and drawn as a smooth spline. Caveat: the default searches ask the "
    "sites for rents <= your max price, so the top of the curve is flattened by that cap. "
    "fair_price for a listing is the curve at its floor size; when size is unknown or outside the "
    "curve's range it is the median rent of scraped listings with the same bedroom count. "
    "The sweet_spot is the flattest stretch of the curve (lowest third of the marginal rent per extra "
    "m2, ignoring the curve's outer 10%): the sizes where more space costs least. It is searched over "
    "all sizes, then compared with max_price: within_budget is yes/partly/no, budget_buys_up_to_m2 is "
    "where the curve crosses your max rent, and extra_rent_to_reach says how far the band is above it."
)

SCORING_NOTES = (
    "score (0-100) = 50 + value points + bonuses - penalties. Value points = value_pct capped to "
    "+/-30 when fair_price comes from floor size; when size is unknown (flagged) it is half of "
    "value_pct capped to +/-15, because a bedroom median is a weak comparison. "
    "Bonuses: security keywords in title/excerpt (+{bonus_keyword_points}); parking count >= 1 or a "
    "secure/covered/garage parking badge (+{parking_points}); available now or within "
    "{available_soon_days} days (+{available_soon_points}); floor size inside the sweet spot and at or "
    "below fair price (+{sweet_spot_points}). Penalties: more than {suspicious_value_pct}% below fair "
    "price (possible scam or error, {suspicious_points}); scam-pattern keywords ({red_flag_points}). "
    "value_pct = (fair_price - price) / fair_price * 100, so +15 means 15% cheaper than the market "
    "curve predicts. flags explain anything unusual. The score is a starting point for ranking, not "
    "a verdict: the excerpt is only ~200 characters, so missing a security keyword does not mean the "
    "place is insecure."
)


# --------------------------------------------------------------------------- maths
def loess(xs: list[float], ys: list[float], grid: list[float], frac: float = 0.6) -> list[float]:
    n = len(xs)
    k = max(4, int(round(frac * n)))
    out = []
    for x0 in grid:
        dists = sorted(abs(x - x0) for x in xs)
        h = dists[min(k, n) - 1] or 1e-9
        w = [(1 - min(abs(x - x0) / h, 1) ** 3) ** 3 for x in xs]
        sw = sum(w)
        if sw <= 0:
            out.append(statistics.median(ys))
            continue
        mx = sum(wi * x for wi, x in zip(w, xs)) / sw
        my = sum(wi * y for wi, y in zip(w, ys)) / sw
        sxx = sum(wi * (x - mx) ** 2 for wi, x in zip(w, xs))
        sxy = sum(wi * (x - mx) * (y - my) for wi, x, y in zip(w, xs, ys))
        slope = sxy / sxx if sxx > 1e-9 else 0.0
        out.append(my + slope * (x0 - mx))
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    i = (len(sorted_vals) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def _interp(curve: list[tuple[float, float]], x: float) -> float:
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


# --------------------------------------------------------------------------- analysis
def _is_market_listing(li: Listing, kind: str, min_price: float) -> bool:
    return (li.kind == kind and li.price is not None and li.price >= min_price
            and (kind != "rent" or li.price_period in (None, "month"))
            and li.property_type not in NON_RESIDENTIAL
            and "New development" not in li.badges)          # 'From R…' prices, not one unit


def build_market(all_listings: list[Listing], filters: dict) -> dict:
    kind = filters.get("kind") or "rent"
    min_price = filters.get("min_price") or (2000 if kind == "rent" else 100000)
    pool = [li for li in all_listings if _is_market_listing(li, kind, min_price)]
    pts = sorted((li.size_m2, li.price) for li in pool if li.size_m2 and 15 <= li.size_m2 <= 600)
    market: dict = {"method": METHOD, "kind": kind, "points_used": len(pts), "fair_price_curve": [],
                    "sweet_spot": None, "median_rent_by_beds": {}}

    by_beds: dict[int, list[int]] = {}
    for li in pool:
        if li.bedrooms is not None:
            by_beds.setdefault(int(li.bedrooms), []).append(li.price)
    market["median_rent_by_beds"] = {str(b): int(statistics.median(v)) for b, v in sorted(by_beds.items())
                                     if len(v) >= 3}
    if pool:
        market["median_rent_all"] = int(statistics.median(li.price for li in pool))

    if len(pts) < 10:
        return market
    xs = [p[0] for p in pts]
    ys = [float(p[1]) for p in pts]
    lo, hi = _percentile(xs, 0.05), _percentile(xs, 0.95)
    grid = [lo + (hi - lo) * i / 39 for i in range(40)]
    fit = loess(xs, ys, grid)
    running = 0.0
    curve = []
    for x, y in zip(grid, fit):                       # rent shouldn't fall as size grows
        running = max(running, y)
        curve.append((round(x, 1), round(running)))
    market["fair_price_curve"] = [list(p) for p in curve]

    market["sweet_spot"] = sweet_spot(curve, filters.get("max_price"))
    return market


def sweet_spot(curve: list[tuple[float, float]], budget: Optional[float]) -> Optional[dict]:
    """Flattest stretch of the fair-price curve: where an extra m2 adds the least rent.
    Searched over the whole curve (not only what's affordable) so it can say 'stretch a bit'."""
    n = len(curve)
    if n < 6:
        return None
    raw = []
    for i in range(n):
        a, b = curve[max(i - 1, 0)], curve[min(i + 1, n - 1)]
        raw.append((b[1] - a[1]) / (b[0] - a[0]) if b[0] != a[0] else 0.0)
    slopes = [statistics.mean(raw[max(0, i - 2):i + 3]) for i in range(n)]     # smooth out wiggles
    # ignore the outer 10% of the curve: LOESS ends are the least reliable
    lo_i, hi_i = n // 10, n - 1 - n // 10
    inner = list(range(lo_i, hi_i + 1))
    cutoff = sorted(slopes[i] for i in inner)[len(inner) // 3]
    best = min(inner, key=lambda i: slopes[i])
    i0 = i1 = best
    while i0 > lo_i and slopes[i0 - 1] <= cutoff:
        i0 -= 1
    while i1 < hi_i and slopes[i1 + 1] <= cutoff:
        i1 += 1
    while i1 - i0 < 3:                                    # at least a few grid steps wide
        if i0 > lo_i and (i1 >= hi_i or slopes[i0 - 1] <= slopes[i1 + 1]):
            i0 -= 1
        elif i1 < hi_i:
            i1 += 1
        else:
            break
    out = {
        "size_m2_from": round(curve[i0][0]), "size_m2_to": round(curve[i1][0]),
        "fair_rent_from": int(curve[i0][1]), "fair_rent_to": int(curve[i1][1]),
        "marginal_rent_per_extra_m2": round(statistics.mean(slopes[i0:i1 + 1])),
        "typical_marginal_rent_per_extra_m2": round(statistics.median(slopes[lo_i:hi_i + 1])),
    }
    if budget:
        affordable = [p for p in curve if p[1] <= budget]
        out["budget_buys_up_to_m2"] = round(affordable[-1][0]) if affordable else None
        if curve[i1][1] <= budget:
            out["within_budget"] = "yes"
        elif curve[i0][1] <= budget:
            out["within_budget"] = "partly"
        else:
            out["within_budget"] = "no"
            out["extra_rent_to_reach"] = int(curve[i0][1] - budget)
    return out


def score_listings(listings: list[Listing], market: dict, scoring: Optional[dict] = None,
                   today=None) -> None:
    import datetime as dt
    sc = {**DEFAULT_SCORING, **(scoring or {})}
    today = today or dt.date.today()
    curve = [tuple(p) for p in market.get("fair_price_curve") or []]
    beds_med = {int(k): v for k, v in (market.get("median_rent_by_beds") or {}).items()}
    sweet = market.get("sweet_spot")

    def words(text: str, needles: list[str]) -> Optional[str]:
        t = re.sub(r"\s+", " ", text.lower())
        return next((n for n in needles if n.lower() in t), None)

    for li in listings:
        li.flags = []
        if li.dupe_of:
            li.flags.append(f"probably the same unit as {li.dupe_of}")
        if li.bedrooms is None:
            li.flags.append("bedrooms unknown")
        if li.price is None or li.kind != market.get("kind", "rent"):
            li.fair_price = li.value_pct = li.score = None
            if li.price is None:
                li.flags.append("no price (POA)")
            continue

        fair, by_size = None, False
        if curve and li.size_m2 and curve[0][0] * 0.8 <= li.size_m2 <= curve[-1][0] * 1.2:
            fair, by_size = _interp(curve, li.size_m2), True
        elif li.bedrooms is not None and int(li.bedrooms) in beds_med:
            fair = beds_med[int(li.bedrooms)]
        elif market.get("median_rent_all"):
            fair = market["median_rent_all"]
        li.fair_price = int(round(fair)) if fair else None
        li.value_pct = int(round((fair - li.price) / fair * 100)) if fair else None

        # a bedroom median is a much weaker comparison than size: count it half, capped at +/-15
        value_points = max(-30, min(30, li.value_pct or 0)) if by_size else max(-15, min(15, (li.value_pct or 0) / 2))
        score = 50 + value_points
        text = " ".join(x for x in (li.title, li.excerpt) if x)
        if words(text, sc["bonus_keywords"]):
            score += sc["bonus_keyword_points"]
        if (li.parking or 0) >= 1 or any(b in ("Secure Parking", "Covered", "Garage") for b in li.badges):
            score += sc["parking_points"]
        if li.available == "now":
            score += sc["available_soon_points"]
        elif li.available:
            try:
                if (dt.date.fromisoformat(li.available) - today).days <= sc["available_soon_days"]:
                    score += sc["available_soon_points"]
            except ValueError:
                pass
        if (sweet and li.size_m2 and sweet["size_m2_from"] <= li.size_m2 <= sweet["size_m2_to"]
                and (li.value_pct or 0) >= 0):
            score += sc["sweet_spot_points"]
            li.flags.append("in the size sweet spot")
        if li.value_pct is not None and li.value_pct > sc["suspicious_value_pct"]:
            score += sc["suspicious_points"]
            li.flags.append(f"{li.value_pct}% below market: verify it's real (scam/typo/room?)")
        rf = words(text, sc["red_flag_keywords"])
        if rf:
            score += sc["red_flag_points"]
            li.flags.append(f"scam pattern: '{rf}'")
        if li.size_m2 is None:
            li.flags.append("no floor size: fair price from bedroom median")
        li.score = max(0, min(100, int(score)))


def scoring_notes(scoring: Optional[dict] = None) -> str:
    return SCORING_NOTES.format(**{**DEFAULT_SCORING, **(scoring or {})})
