"""property24.com - server-rendered results; each card carries data-listing-number and
schema.org microdata (itemprop=price content="20000")."""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from ..core import (Listing, beds_from_title, canon_badge, clean, parse_available,
                    parse_number, parse_price, parse_size, plausible_za_coords, property_type_from)
from .base import SiteAdapter


PRICE_TEXT_RE = re.compile(
    r"(?:From\s+)?R\s?\d{1,3}(?:[\s ]\d{3})*(?:\s+per\s+(?:day|night|week|month|m²))?"
    r"|\bPOA(?:\s+per\s+(?:day|night|week|month))?", re.I)


def _logo_id(src: str) -> str:
    m = re.search(r"prop24\.com/(\d+)", src)
    return m.group(1) if m else src


_MAP_PROPS_RE = re.compile(r'"name"\s*:\s*"GoogleMapsFactory"\s*,\s*"props"\s*:\s*')
_LD_GEO_RE = re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)\s*,\s*"longitude"\s*:\s*(-?\d+\.\d+)')


def _map_props(html: str) -> Optional[dict]:
    """The listing page's map widget config: {"type": ..., "props": {latitude, longitude,
    haveExactLocation, showLocation, streetAddress, ...}}."""
    dec = json.JSONDecoder()
    for m in _MAP_PROPS_RE.finditer(html):
        try:
            obj, _ = dec.raw_decode(html, m.end())
        except ValueError:
            continue
        inner = obj.get("props") if isinstance(obj, dict) else None
        if isinstance(inner, dict) and "latitude" in inner:
            return inner
    return None


class Property24(SiteAdapter):
    name = "property24"
    domains = ("property24.com",)
    base_url = "https://www.property24.com"
    # Results cards have no coordinates; the listing page's map widget does, with a
    # haveExactLocation flag (verified on a live listing page, Nov 2026).
    locate_via_detail = True

    def detail_location(self, html: str) -> Optional[dict]:
        p = _map_props(html)
        if p and plausible_za_coords(p.get("latitude"), p.get("longitude")):
            exact = p.get("haveExactLocation") is True and p.get("showLocation") is not False
            out = {"lat": float(p["latitude"]), "lon": float(p["longitude"]),
                   "precision": "exact" if exact else "approx"}
            if exact and clean(p.get("streetAddress")):
                out["street_address"] = clean(p["streetAddress"])
            return out
        # Fallback: JSON-LD on the page. It doesn't say whether the pin is exact.
        for m in _LD_GEO_RE.finditer(html):
            if plausible_za_coords(m.group(1), m.group(2)):
                return {"lat": float(m.group(1)), "lon": float(m.group(2)), "precision": "approx"}
        if '"ContactForAddressForm"' in html:     # no map; the agent gives the address on request
            return {"lat": None, "note": "address hidden by the advertiser"}
        return None

    def page_url(self, url: str, page: int) -> str:
        # .../table-view/blouberg/western-cape/11589  ->  .../11589/p2  (query string kept)
        parts = urlsplit(url)
        path = re.sub(r"/p\d+/?$", "", parts.path.rstrip("/"))
        if page > 1:
            path = f"{path}/p{page}"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    def is_empty_results_page(self, html: str) -> bool:
        return "p24_results" in html and "data-listing-number" not in html or super().is_empty_results_page(html)

    def parse(self, html: str, page_url: str) -> list[Listing]:
        s = self.soup(html)
        out: list[Listing] = []
        seen: set[str] = set()
        # Pro tiles show the agency logo without alt text; learn logo -> name from other tiles.
        logo_names = {_logo_id(i["src"]): clean(i["alt"]) for i in s.select(".p24_branding img[alt][src]")
                      if i.get("alt") and "listed_by_owner" not in i["src"]}
        for card in s.select("[data-listing-number]"):
            if card.select_one("[data-listing-number]"):      # wrapper around a real tile
                continue
            lid = card["data-listing-number"].strip()
            if not lid or lid in seen:
                continue
            link = card.find("a", href=re.compile(rf"/{lid}(?:[/?]|$)"))
            if not link:
                continue
            seen.add(lid)
            url = self.absolute(link["href"]).split("?")[0]

            img = card.select_one("img[title]")
            long_title = clean(img["title"]) if img else None   # '2 Bedroom Apartment / flat to rent in X - Y'

            price_el = card.select_one("[itemprop=price]") or card.select_one(".p24_price")
            price = None
            if price_el is not None and (price_el.get("content") or "").isdigit():
                price = int(price_el["content"])
            # 'R 13 000', 'POA', 'R 3 800 per day'. Pro tiles nest the description inside
            # .p24_price, so match the price pattern rather than taking all the text.
            m = PRICE_TEXT_RE.search(clean(price_el.get_text(" ")) or "") if price_el else None
            m = m or PRICE_TEXT_RE.search(card.get_text(" "))
            price_text = clean(m.group(0)) if m else None
            if price is None:
                price = parse_price(price_text)

            title_el = card.select_one(".p24_title")
            title = clean(title_el.get_text()) if title_el else None
            if not title:
                meta = card.select_one("meta[itemprop=name]:not([content*=' - '])")
                title = clean(meta["content"]) if meta and "Bedroom" in meta.get("content", "") else None
            if not title:                    # pro tile: <div class="p24_description">2 Bedroom Apartment in <span>
                desc = card.select_one(".p24_description")
                first = desc.find(string=True, recursive=False) if desc else None
                title = clean(re.sub(r"\s+in\s*$", "", first or "")) if first else None
            if not title and long_title:
                title = clean(re.split(r"\s+(?:to rent|for sale|on auction)\s+in\s+", long_title)[0])

            feats = {f.get("title", "").lower(): clean(f.get_text(" ")) for f in card.select(".p24_featureDetails")}
            beds = parse_number(feats.get("bedrooms")) if "bedrooms" in feats else beds_from_title(title)

            badges = [canon_badge(clean(b.get_text(" ")) or "") for b in card.select(".p24_badges li")]
            raw_badges = [clean(b.get_text(" ")) or "" for b in card.select(".p24_badges li")]
            available = next((parse_available(b) for b in raw_badges if "available" in b.lower()), None)
            badges = [b for b in badges if b and not b.lower().startswith("available:")]
            if "/new-developments-" in url:
                badges.append("New development")

            agency = None
            brand = card.select_one(".p24_branding[title]")
            if card.select_one(".p24_branding img[src*=listed_by_owner]"):
                agency = "Private landlord"
            elif brand:
                agency = clean(brand["title"].replace("Estate Agency profile for", ""))
            if not agency:
                logo = card.select_one(".p24_branding img[alt], img[class*=brandingLogo][alt]")
                agency = clean(logo["alt"]) if logo else None
            if not agency:
                logo = card.select_one(".p24_brandingHeader img[src]")
                agency = logo_names.get(_logo_id(logo["src"])) if logo else None
            if not agency and card.select_one(".p24_privateSeller, [class*=privateSeller]"):
                agency = "Private seller"

            loc = card.select_one(".p24_location")
            addr = card.select_one(".p24_address")
            excerpt = card.select_one(".p24_excerpt")

            out.append(Listing(
                site=self.name, listing_id=lid, url=url,
                # '/to-rent/', '/apartments-to-rent/', '/for-sale/', '/new-developments-for-sale/'
                kind="rent" if "to-rent/" in url else "sale" if "for-sale/" in url else None,
                title=title, price=price, price_text=price_text,
                suburb=clean(loc.get_text()) if loc else None,
                address=clean(addr.get_text()) if addr else None,
                property_type=property_type_from(title) or property_type_from(long_title),
                bedrooms=beds,
                bathrooms=parse_number(feats.get("bathrooms")),
                parking=parse_number(feats.get("parking spaces") or feats.get("garages")),
                size_m2=parse_size(feats.get("floor size")),          # 'Erf Size' is land, ignored
                available=available, badges=badges, agency=agency,
                excerpt=clean(excerpt.get_text(" "))[:200] if excerpt and clean(excerpt.get_text(" ")) else None,
            ))
        return out
