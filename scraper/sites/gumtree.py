"""gumtree.co.za - Nuxt app. Results are server-rendered into <script id="__NUXT_DATA__">
(a 'devalue'-encoded flat array). We decode it and read pinia.listings.searchResults,
deliberately ignoring sponsoredResults (promoted ads from other areas).

Notes from the live site (Nov 2026):
  * Use a category path to keep filters, e.g.
      /property/apartment-flat/table-view?propertyListingType=RENT
    The bare /property/table-view?propertyListingType=RENT 302-redirects and drops the filter.
  * Results are padded with listings from the wider metro once local ones run out, so use
    the include_suburbs / exclude_suburbs filters.
  * The 'lr'/'ls' prefix of listing refs does NOT mean rent/sale.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from ..core import Listing, beds_from_title, clean, parse_available, parse_number, property_type_from
from .base import SiteAdapter

_DEVALUE_WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "NuxtError", "Object"}


def devalue_decode(arr: list):
    """Minimal decoder for the Nuxt 3 / devalue payload format."""
    memo: dict[int, object] = {}

    def h(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                tag = v[0]
                if tag in _DEVALUE_WRAPPERS:
                    memo[i] = r = h(v[1]) if len(v) > 1 else None
                    return r
                if tag in ("EmptyRef", "EmptyShallowRef"):
                    memo[i] = None
                    return None
                if tag == "Date":
                    memo[i] = v[1]
                    return v[1]
                if tag == "Set":
                    memo[i] = r = [h(x) for x in v[1:]]
                    return r
                if tag == "Map":
                    memo[i] = r = {}
                    for k, x in zip(v[1::2], v[2::2]):
                        r[str(h(k))] = h(x)
                    return r
            out: list = []
            memo[i] = out
            out.extend(h(x) for x in v)
            return out
        if isinstance(v, dict):
            out_d: dict = {}
            memo[i] = out_d
            for k, x in v.items():
                out_d[k] = h(x)
            return out_d
        memo[i] = v
        return v

    return h(0)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class Gumtree(SiteAdapter):
    name = "gumtree"
    domains = ("gumtree.co.za",)
    base_url = "https://www.gumtree.co.za"

    def page_url(self, url: str, page: int) -> str:
        parts = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
        if page > 1:
            q.append(("page", str(page)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))

    @staticmethod
    def _store(html: str):
        m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None, {}
        raw = m.group(1)
        cats = {ref: name for ref, _slug_, name in
                re.findall(r'"([a-z0-9]{5})","([a-z0-9-]+)","([^"]{2,60})"', raw)}
        try:
            root = devalue_decode(json.loads(raw))
            return (root or {}).get("pinia", {}).get("listings", {}), cats
        except (ValueError, AttributeError, IndexError, TypeError):
            return None, cats

    def is_empty_results_page(self, html: str) -> bool:
        store, _ = self._store(html)
        return store is not None and not store.get("searchResults")

    def parse(self, html: str, page_url: str) -> list[Listing]:
        store, cats = self._store(html)
        if not store:
            return []
        hrefs = {}
        for h in re.findall(r'href="(/property/[^"?#]+)"', html):
            hrefs[h.rsplit("-", 1)[-1]] = h
        url_kind = (parse_qs(urlsplit(page_url).query).get("propertyListingType") or [None])[0]

        out: list[Listing] = []
        for r in store.get("searchResults") or []:
            if not isinstance(r, dict) or not r.get("ref"):
                continue
            ref = r["ref"]
            title = clean(r.get("title"))
            attrs = {a.get("key"): a.get("value") for a in (r.get("summaryAttributes") or []) if isinstance(a, dict)}

            loc_path = [p.strip() for p in (r.get("locationDisplayNamePath") or "").split(">")]
            suburb = loc_path[-1] if len(loc_path) >= 3 else None
            href = hrefs.get(ref)
            if not href and title:
                href = f"/property/{_slug(suburb or loc_path[-1] or 'south-africa')}/{_slug(title)}-{ref}"
            url = self.absolute(href) if href else f"{self.base_url}/property/{ref}"

            price_info = r.get("price") or {}
            price = None
            if price_info.get("priceType") == "ASKING_PRICE" and price_info.get("priceMinor") is not None:
                price = int(price_info["priceMinor"]) // 100
                price_text = "R " + f"{price:,}".replace(",", " ")
            else:
                price_text = {"POA": "POA", "FREE": "Free", "SWAP_TRADE": "Swap/Trade"}.get(
                    price_info.get("priceType"), price_info.get("priceType"))

            cat_name = cats.get(r.get("categoryRef") or "", "")
            kind_attr = attrs.get("propertyListingType") or url_kind
            if kind_attr:
                kind = "rent" if str(kind_attr).upper() == "RENT" else "sale"
            elif "room" in cat_name.lower() or re.search(r"\b(to rent|to let|rental|per month|p/?m)\b", title or "", re.I):
                kind = "rent"
            elif re.search(r"\bfor sale\b", title or "", re.I):
                kind = "sale"
            else:
                kind = None

            beds_raw = attrs.get("numberBedrooms")
            if beds_raw and "STUDIO" in str(beds_raw).upper():
                beds = 0.0
            else:
                beds = parse_number(beds_raw)
            if beds is None:
                beds = beds_from_title(title)

            badges = []
            parking_raw = attrs.get("parking")
            if parking_raw and parking_raw != "NONE":
                badges.append(str(parking_raw).replace("_", " ").title())
            if any(p.get("type", "").upper().startswith("URGENT") for p in (r.get("promotions") or []) if isinstance(p, dict)):
                badges.append("Urgent")

            avail = parse_available(attrs.get("availabilityStartDate"))
            seller = (r.get("seller") or {}).get("sellerDisplayName")

            out.append(Listing(
                site=self.name, listing_id=ref, url=url, kind=kind,
                title=title, price=price, price_text=price_text,
                suburb=suburb, address=None,
                property_type=property_type_from(cat_name) or property_type_from(title),
                bedrooms=beds,
                bathrooms=parse_number(attrs.get("numberBathrooms")),
                parking=None,          # Gumtree gives a parking *type*, kept as a badge
                size_m2=parse_number(attrs.get("areaInMeters")),
                available=avail, badges=badges, agency=clean(seller),
                excerpt=(clean(r.get("description")) or "")[:200] or None,
            ))
        return out
