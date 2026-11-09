"""Suburb outlines from OpenStreetMap's Nominatim service (open data, ODbL).

Used for (1) the map's "click a suburb" picker and (2) judging listings whose only known
location is their suburb against no-go zones.

Usage policy (https://operations.osmfoundation.org/policies/nominatim/): at most 1 request per
second, an identifying User-Agent, and cache results. We do all three: every answer (including
"not found") is stored in output/places_cache.json and reused.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("scraper")

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = "za-property-scraper/1.0 (personal, low-volume; local web UI)"
MIN_INTERVAL = 1.1                     # seconds between Nominatim requests
SIMPLIFY = 0.0003                      # polygon_threshold in degrees (~30 m), keeps outlines small

_lock = threading.Lock()               # one Nominatim request at a time, process-wide
_last = 0.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _get(path: str, params: dict) -> object:
    global _last
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(f"{NOMINATIM}/{path}", params={**params, "format": "jsonv2"},
                             headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=20)
        finally:
            _last = time.monotonic()
    r.raise_for_status()
    return r.json()


def _place(hit: dict) -> dict:
    addr = hit.get("address") or {}
    name = (addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")
            or addr.get("city_district") or hit.get("name") or "").strip()
    geom = hit.get("geojson")
    if geom and geom.get("type") not in ("Polygon", "MultiPolygon"):
        geom = None
    return {"name": name, "display_name": hit.get("display_name"), "lat": float(hit["lat"]),
            "lon": float(hit["lon"]), "geometry": geom, "osm": f"{hit.get('osm_type')}/{hit.get('osm_id')}"}


class Places:
    """Cached suburb lookups. `online=False` answers from the cache only."""

    def __init__(self, cache_path: Path, online: bool = True):
        self.path = cache_path
        self.online = online
        self.lookups = 0
        try:
            self.data = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except ValueError:
            log.warning("Could not read %s - starting a fresh places cache", cache_path)
            self.data = {}
        self.data.setdefault("search", {})
        self.data.setdefault("reverse", {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    # ---- name -> outline, near a bounding box ---------------------------------------
    def suburb(self, name: str, bbox: tuple[float, float, float, float]) -> Optional[dict]:
        """Best match for a suburb name inside bbox (south, west, north, east), or None."""
        key = _norm(name)
        if not key:
            return None
        s, w, n, e = bbox
        entries = self.data["search"].setdefault(key, [])
        for ent in entries:                                   # cached answer for an overlapping box
            if ent.get("place"):
                p = ent["place"]
                if s <= p["lat"] <= n and w <= p["lon"] <= e:
                    return p
            else:
                cs, cw, cn, ce = ent["bbox"]
                if cs <= s and cw <= w and cn >= n and ce >= e:   # searched a box covering this one
                    return None
        if not self.online:
            return None
        try:
            hits = _get("search", {"q": name, "countrycodes": "za", "viewbox": f"{w},{n},{e},{s}",
                                   "bounded": 1, "polygon_geojson": 1, "polygon_threshold": SIMPLIFY,
                                   "addressdetails": 1, "limit": 5})
        except (requests.RequestException, ValueError) as ex:
            log.warning("  OpenStreetMap lookup for '%s' failed: %s", name, ex)
            return None
        self.lookups += 1
        hits = [h for h in hits if isinstance(h, dict) and "lat" in h]
        # prefer areas (suburbs/neighbourhoods) with an outline over points, roads or shops
        hits.sort(key=lambda h: (h.get("geojson", {}).get("type") not in ("Polygon", "MultiPolygon"),
                                 h.get("addresstype") not in ("suburb", "neighbourhood", "quarter",
                                                              "city_district", "residential", "town",
                                                              "village", "hamlet")))
        place = _place(hits[0]) if hits else None
        entries.append({"bbox": [s, w, n, e], "place": place, "checked": dt.date.today().isoformat()})
        self.save()
        return place

    # ---- point -> suburb (map clicks) ----------------------------------------------
    def at(self, lat: float, lon: float) -> Optional[dict]:
        """Suburb at a point. Reverse-geocoding at suburb zoom returns electoral wards in Cape
        Town, so we reverse at street level, read the suburb name from the address, then look
        that name up nearby for its outline (some suburbs are only a point in OSM)."""
        key = f"{lat:.4f},{lon:.4f}"
        if key in self.data["reverse"]:
            name = self.data["reverse"][key]
        else:
            if not self.online:
                return None
            hit = _get("reverse", {"lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1})
            self.lookups += 1
            addr = (hit or {}).get("address") or {} if isinstance(hit, dict) else {}
            name = (addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")
                    or addr.get("residential") or addr.get("town") or addr.get("village"))
            self.data["reverse"][key] = name
            self.save()
        if not name:
            return None
        place = self.suburb(name, (lat - .05, lon - .05, lat + .05, lon + .05))
        if place:
            place = dict(place, name=name)
        return place
