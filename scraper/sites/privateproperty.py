"""privateproperty.co.za - server-rendered; each card is <a class="listing-result"
href=".../RR4642267"> with an embedded JSON-LD Residence block. RR... = rental, T... = sale."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core import (Listing, beds_from_title, canon_badge, clean, parse_available,
                    parse_number, parse_price, parse_size, property_type_from)
from .base import SiteAdapter

ID_RE = re.compile(r"/((?:RR|T)\d+)/?(?:\?|$)")


class PrivateProperty(SiteAdapter):
    name = "privateproperty"
    domains = ("privateproperty.co.za",)
    base_url = "https://www.privateproperty.co.za"

    def page_url(self, url: str, page: int) -> str:
        parts = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "page"]
        if page > 1:
            q.append(("page", str(page)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))

    def parse(self, html: str, page_url: str) -> list[Listing]:
        s = self.soup(html)
        out: list[Listing] = []
        seen: set[str] = set()
        for card in s.find_all("a", href=ID_RE):
            # Only real result cards (not 'similar listings' links): need a price or features
            if not card.select_one("[class*=price], [class*=feature]"):
                continue
            lid = ID_RE.search(card["href"]).group(1)
            if lid in seen:
                continue
            seen.add(lid)
            url = self.absolute(card["href"]).split("?")[0]

            ld = {}
            ld_el = card.find("script", type="application/ld+json")
            if ld_el and ld_el.string:
                try:
                    ld = json.loads(ld_el.string)
                except ValueError:
                    ld = {}
            ld_props = {p.get("name", "").lower(): p.get("value") for p in ld.get("additionalProperty", [])
                        if isinstance(p, dict)}

            price_el = card.select_one(".listing-result__price, [class*=price]")
            price_text = clean(price_el.get_text(" ")) if price_el else None

            title_el = card.select_one(".listing-result__title")
            title = None
            if title_el:
                t = title_el.find(string=True, recursive=False)
                title = clean(t) if t else clean(title_el.get_text(" "))
            title = title or clean(card.get("title"))

            feats = {f.get("title", "").lower(): clean(f.get_text(" ")) for f in card.select(".listing-result__feature")}

            suburb_el = card.select_one(".listing-result__desktop-suburb, .listing-result__mobile-suburb")
            suburb = clean(suburb_el.get_text()) if suburb_el else None
            locality = (ld.get("address") or {}).get("addressLocality")       # 'Sunridge, Blouberg'
            if not suburb and locality:
                suburb = clean(locality.split(",")[0])
            addr_bits = [clean(x.get_text(" ")) for x in card.select(".listing-result__address span[title]")]
            address = ", ".join(b for b in addr_bits if b) or None

            badges, available = [], None
            for b in card.select(".listing-banner, .listing-result__badge"):
                txt = clean(b.get_text(" ")) or ""
                if "available" in txt.lower():
                    available = parse_available(txt)
                    if available == "now":
                        badges.append("Available Now")
                elif txt:
                    badges.append(canon_badge(txt))

            agency = None
            logo = card.select_one(".listing-result__advertiser img[alt], .listing-result__logo[alt]")
            if logo:
                agency = clean(logo["alt"])
            if not agency:
                adv = card.select_one(".listing-result__advertiser")
                agency = clean(adv.get_text(" ")) if adv else None

            desc = card.select_one(".listing-result__description")
            beds = parse_number(feats.get("bedrooms") or ld_props.get("bedrooms"))
            if beds is None:
                beds = beds_from_title(title)

            out.append(Listing(
                site=self.name, listing_id=lid, url=url,
                kind="rent" if lid.startswith("RR") or "/to-rent/" in url else "sale",
                title=title, price=parse_price(price_text), price_text=price_text,
                suburb=suburb, address=address,
                property_type=property_type_from(title),
                bedrooms=beds,
                bathrooms=parse_number(feats.get("bathrooms") or ld_props.get("bathrooms")),
                parking=parse_number(feats.get("parking spaces") or feats.get("garages")),
                size_m2=parse_size(feats.get("floor size")),
                available=available, badges=list(dict.fromkeys(badges)), agency=agency,
                excerpt=(clean(desc.get_text(" ")) or "")[:200] or None if desc else None,
            ))
        return out
