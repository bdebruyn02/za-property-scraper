"""No-go zones: pure geometry, no network.

A zone lives in filters.no_go_zones and is one of:

  {"name": "Industrial strip", "type": "area", "points": [[lat, lon], ...]}        # >= 3 points
  {"name": "East of Main Rd", "type": "road_side", "points": [[lat, lon], ...],    # >= 2 points
   "side": "left" | "right"}

`points` are [lat, lon] in WGS84 as drawn on the map. For a road_side zone the points trace the
road in drawing order, and `side` is relative to that direction of travel. "Which side" is
decided by the nearest stretch of the traced road, and the road counts as continuing straight
past both ends, so a short trace still splits the whole map in two.

`enabled: false` keeps a zone saved but ignores it.

Verdicts (see zone_verdict):
  * exact location inside a zone          -> ("reject", reason)
  * approximate location inside a zone    -> ("flag", text)
  * only the suburb known: whole suburb outline inside a zone -> reject; part of it -> flag
  * nothing known about the location      -> None (unknown location never filters a listing out)
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

LatLon = Sequence[float]          # [lat, lon]

_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON_EQ = 111_320.0

# A listing with coordinates farther than this from a road's trace is still classified by side;
# this constant only guards against degenerate traces (all points identical).
_MIN_SEGMENT_M = 0.5


def active_zones(filters: dict) -> list[dict]:
    return [z for z in (filters.get("no_go_zones") or []) if z.get("enabled", True) and valid_zone(z) is None]


def valid_zone(z) -> Optional[str]:
    """None if the zone is well-formed, else what's wrong with it."""
    if not isinstance(z, dict):
        return "zone must be an object"
    t = z.get("type")
    pts = z.get("points")
    if t not in ("area", "road_side"):
        return "type must be 'area' or 'road_side'"
    if not isinstance(pts, list) or not all(_is_latlon(p) for p in pts):
        return "points must be a list of [lat, lon]"
    if t == "area" and len(pts) < 3:
        return "an area needs at least 3 points"
    if t == "road_side":
        if len(pts) < 2:
            return "a road needs at least 2 points"
        if z.get("side") not in ("left", "right"):
            return "side must be 'left' or 'right'"
        if _length_m(pts) < 5:
            return "the road trace is too short"
    return None


def _is_latlon(p) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)
            and -90 <= p[0] <= 90 and -180 <= p[1] <= 180)


# --------------------------------------------------------------------------- projection
class _Proj:
    """Local equirectangular projection to metres, accurate to well under 1% over a city."""

    def __init__(self, lat0: float):
        self.kx = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))

    def xy(self, p: LatLon) -> tuple[float, float]:
        return (p[1] * self.kx, p[0] * _M_PER_DEG_LAT)


def _length_m(pts: Sequence[LatLon]) -> float:
    pr = _Proj(pts[0][0])
    xy = [pr.xy(p) for p in pts]
    return sum(math.dist(a, b) for a, b in zip(xy, xy[1:]))


# --------------------------------------------------------------------------- primitives
def point_in_polygon(p: LatLon, poly: Sequence[LatLon]) -> bool:
    """Even-odd ray casting on lat/lon (fine at city scale)."""
    y, x = p[0], p[1]
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i][0], poly[i][1]
        y2, x2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                inside = not inside
    return inside


def side_of_road(p: LatLon, road: Sequence[LatLon]) -> str:
    """'left' or 'right' of the traced road (relative to drawing direction), judged against the
    nearest segment, with the first and last segments extended indefinitely."""
    pr = _Proj(p[0])
    P = pr.xy(p)
    xy = [pr.xy(q) for q in road]
    best = None                     # (distance, cross)
    last = len(xy) - 2
    for i, (A, B) in enumerate(zip(xy, xy[1:])):
        dx, dy = B[0] - A[0], B[1] - A[1]
        L2 = dx * dx + dy * dy
        if L2 < _MIN_SEGMENT_M ** 2:
            continue
        t = ((P[0] - A[0]) * dx + (P[1] - A[1]) * dy) / L2
        lo = -math.inf if i == 0 else 0.0           # extend the end segments
        hi = math.inf if i == last else 1.0
        t = max(lo, min(hi, t))
        cx, cy = A[0] + t * dx, A[1] + t * dy
        d = math.hypot(P[0] - cx, P[1] - cy)
        cross = dx * (P[1] - A[1]) - dy * (P[0] - A[0])
        if best is None or d < best[0] - 1e-9:
            best = (d, cross)
    if best is None:
        return "left"
    return "left" if best[1] > 0 else "right"


def point_in_zone(p: LatLon, z: dict) -> bool:
    if z["type"] == "area":
        return point_in_polygon(p, z["points"])
    return side_of_road(p, z["points"]) == z["side"]


# --------------------------------------------------------------------------- outlines
def outline_rings(geometry: Optional[dict]) -> list[list[list[float]]]:
    """GeoJSON Polygon/MultiPolygon -> outer rings as [[lat, lon], ...] (holes ignored)."""
    if not geometry:
        return []
    t, c = geometry.get("type"), geometry.get("coordinates") or []
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else []
    rings = []
    for poly in polys:
        if poly and poly[0]:
            rings.append([[pt[1], pt[0]] for pt in poly[0]])
    return rings


def outline_vs_zone(rings: list[list[list[float]]], z: dict) -> str:
    """'all' if the whole outline is in the zone, 'some' if part of it, 'none' otherwise."""
    pts = [p for r in rings for p in r]
    if not pts:
        return "none"
    inside = sum(1 for p in pts if point_in_zone(p, z))
    if inside == len(pts):
        # For an area, an outline whose vertices are all inside can still poke out if the zone is
        # concave, but a zone vertex would then sit inside the outline.
        if z["type"] == "area" and any(point_in_polygon(q, r) for q in z["points"] for r in rings):
            return "some"
        return "all"
    if inside:
        return "some"
    if z["type"] == "area" and any(point_in_polygon(q, r) for q in z["points"] for r in rings):
        return "some"                 # a small zone entirely inside the suburb
    return "none"


# --------------------------------------------------------------------------- verdict
def zone_verdict(lat: Optional[float], lon: Optional[float], precision: Optional[str],
                 suburb_place: Optional[dict], zones: list[dict]) -> Optional[tuple[str, str]]:
    """('reject', reason) | ('flag', text) | None. See module docstring.
    suburb_place is a places.Places result ({lat, lon, geometry}) for the listing's suburb."""
    if not zones:
        return None
    if lat is not None and lon is not None:
        for z in zones:
            if point_in_zone((lat, lon), z):
                if precision == "exact":
                    return "reject", f"in no-go zone '{z.get('name') or 'unnamed'}'"
                return "flag", (f"may be in no-go zone '{z.get('name') or 'unnamed'}' "
                                "(the site only gives an approximate location; check the address)")
        if precision == "exact":
            return None
    if not suburb_place:
        return None
    suburb_outline = outline_rings(suburb_place.get("geometry"))
    if not suburb_outline:            # OSM has the suburb as a point only: never enough to reject
        for z in zones:
            if point_in_zone((suburb_place["lat"], suburb_place["lon"]), z):
                return "flag", (f"may be in no-go zone '{z.get('name') or 'unnamed'}' "
                                "(only the suburb is known and its centre is in the zone; check the address)")
        return None
    if suburb_outline:
        partial = None
        for z in zones:
            v = outline_vs_zone(suburb_outline, z)
            if v == "all":
                return "reject", f"suburb is inside no-go zone '{z.get('name') or 'unnamed'}'"
            if v == "some" and partial is None:
                partial = z
        if partial is not None:
            return "flag", (f"may be in no-go zone '{partial.get('name') or 'unnamed'}' "
                            "(only the suburb is known and it straddles the zone; check the address)")
    return None


def zones_bbox(zones: list[dict], pad_deg: float = 0.25) -> Optional[tuple[float, float, float, float]]:
    """(south, west, north, east) around all zone points, padded. Road zones extend past their
    ends, so the padding matters: suburbs are only looked up near the zones."""
    pts = [p for z in zones for p in z["points"]]
    if not pts:
        return None
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) - pad_deg, min(lons) - pad_deg, max(lats) + pad_deg, max(lons) + pad_deg)


def describe_zone(z: dict) -> dict:
    """Compact, human-readable form for the Claude export."""
    d = {"name": z.get("name") or "unnamed"}
    if z["type"] == "area":
        d["kind"] = "area drawn on the map"
    else:
        d["kind"] = (f"everything on the {z['side']} of a road traced on the map "
                     f"(from {_fmt(z['points'][0])} to {_fmt(z['points'][-1])}, "
                     "relative to that direction)")
    return d


def _fmt(p: LatLon) -> str:
    return f"{p[0]:.4f},{p[1]:.4f}"
