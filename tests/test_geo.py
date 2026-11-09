"""No-go zone geometry (scraper/geo.py). Coordinates are around Cape Town's west coast but the
shapes are synthetic, so the expected answers can be checked by eye."""
from scraper import geo

# A road running due north along lon 18.50, from lat -33.86 to -33.80.
ROAD_N = [[-33.86, 18.50], [-33.80, 18.50]]
EAST = (-33.83, 18.52)
WEST = (-33.83, 18.48)
SQUARE = [[-33.84, 18.49], [-33.84, 18.51], [-33.82, 18.51], [-33.82, 18.49]]


def test_point_in_polygon():
    assert geo.point_in_polygon((-33.83, 18.50), SQUARE)
    assert not geo.point_in_polygon((-33.83, 18.52), SQUARE)
    assert not geo.point_in_polygon((-33.85, 18.50), SQUARE)


def test_side_of_road_relative_to_drawing_direction():
    # heading north, east is on the right
    assert geo.side_of_road(EAST, ROAD_N) == "right"
    assert geo.side_of_road(WEST, ROAD_N) == "left"
    # same road drawn southwards: sides swap
    south = list(reversed(ROAD_N))
    assert geo.side_of_road(EAST, south) == "left"


def test_road_extends_past_its_ends():
    # far beyond the northern end, still east of the line's continuation
    assert geo.side_of_road((-33.60, 18.505), ROAD_N) == "right"
    assert geo.side_of_road((-34.10, 18.495), ROAD_N) == "left"


def test_bent_road_uses_nearest_stretch():
    # north along 18.50, then turns east along -33.80
    road = [[-33.86, 18.50], [-33.80, 18.50], [-33.80, 18.56]]
    assert geo.side_of_road((-33.83, 18.52), road) == "right"    # inside the bend, below the east leg
    assert geo.side_of_road((-33.78, 18.53), road) == "left"     # north of the east-going leg
    assert geo.side_of_road((-33.83, 18.48), road) == "left"     # west of the north-going leg


def test_valid_zone():
    assert geo.valid_zone({"type": "area", "points": SQUARE}) is None
    assert geo.valid_zone({"type": "road_side", "points": ROAD_N, "side": "right"}) is None
    assert "3 points" in geo.valid_zone({"type": "area", "points": SQUARE[:2]})
    assert "side" in geo.valid_zone({"type": "road_side", "points": ROAD_N, "side": "up"})
    assert geo.valid_zone({"type": "road_side", "points": [[-33.8, 18.5], [-33.8, 18.5]], "side": "left"})
    assert geo.valid_zone({"type": "area", "points": [[-33.8, "x"], [1, 2], [3, 4]]})
    assert geo.valid_zone({"type": "circle", "points": SQUARE})


def test_active_zones_skips_disabled_and_broken():
    f = {"no_go_zones": [{"name": "a", "type": "area", "points": SQUARE},
                         {"name": "b", "type": "area", "points": SQUARE, "enabled": False},
                         {"name": "c", "type": "area", "points": []}]}
    assert [z["name"] for z in geo.active_zones(f)] == ["a"]
    assert geo.active_zones({}) == []


ROAD_ZONE = {"name": "East of road", "type": "road_side", "points": ROAD_N, "side": "right"}
AREA_ZONE = {"name": "Square", "type": "area", "points": SQUARE}


def _poly(lon_from, lon_to, lat_from=-33.85, lat_to=-33.81):
    ring = [[lon_from, lat_from], [lon_to, lat_from], [lon_to, lat_to], [lon_from, lat_to], [lon_from, lat_from]]
    return {"lat": (lat_from + lat_to) / 2, "lon": (lon_from + lon_to) / 2,
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_verdict_exact_pin():
    assert geo.zone_verdict(*EAST, "exact", None, [ROAD_ZONE]) == ("reject", "in no-go zone 'East of road'")
    assert geo.zone_verdict(*WEST, "exact", None, [ROAD_ZONE]) is None
    # an exact pin outside every zone is not overridden by its suburb straddling a zone
    assert geo.zone_verdict(*WEST, "exact", _poly(18.48, 18.52), [ROAD_ZONE]) is None


def test_verdict_approximate_pin_only_flags():
    kind, text = geo.zone_verdict(*EAST, "approx", None, [ROAD_ZONE])
    assert kind == "flag" and "approximate" in text


def test_verdict_by_suburb_outline():
    # wholly east of the road -> reject; straddling -> flag; wholly west -> nothing
    assert geo.zone_verdict(None, None, None, _poly(18.51, 18.53), [ROAD_ZONE])[0] == "reject"
    kind, text = geo.zone_verdict(None, None, None, _poly(18.48, 18.52), [ROAD_ZONE])
    assert kind == "flag" and "straddles" in text
    assert geo.zone_verdict(None, None, None, _poly(18.46, 18.49), [ROAD_ZONE]) is None


def test_verdict_suburb_as_point_never_rejects():
    point_only = {"lat": -33.83, "lon": 18.52, "geometry": None}
    assert geo.zone_verdict(None, None, None, point_only, [ROAD_ZONE])[0] == "flag"
    assert geo.zone_verdict(None, None, None, {**point_only, "lon": 18.48}, [ROAD_ZONE]) is None


def test_area_zone_inside_a_suburb_is_partial():
    big = _poly(18.45, 18.55, -33.90, -33.75)          # contains the whole SQUARE zone
    assert geo.zone_verdict(None, None, None, big, [AREA_ZONE])[0] == "flag"
    small = _poly(18.495, 18.505, -33.835, -33.825)    # entirely inside the square
    assert geo.zone_verdict(None, None, None, small, [AREA_ZONE])[0] == "reject"


def test_unknown_location_is_kept():
    assert geo.zone_verdict(None, None, None, None, [ROAD_ZONE]) is None
    assert geo.zone_verdict(*EAST, "exact", None, []) is None


def test_describe_zone_for_export():
    d = geo.describe_zone(ROAD_ZONE)
    assert d["name"] == "East of road" and "right" in d["kind"] and "-33.8600,18.5000" in d["kind"]
    assert geo.describe_zone(AREA_ZONE)["kind"] == "area drawn on the map"
