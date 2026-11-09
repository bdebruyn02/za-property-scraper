"""Site registry. To add a site (IOL Property, Rawson, Pam Golding, ...):
  1. create scraper/sites/<name>.py with a SiteAdapter subclass (see base.py),
  2. add the class to SITES below,
  3. save a real results page under tests/fixtures/ and add a test.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .base import SiteAdapter
from .gumtree import Gumtree
from .privateproperty import PrivateProperty
from .property24 import Property24

SITES: list[type[SiteAdapter]] = [Property24, PrivateProperty, Gumtree]


def adapter_for(url: str) -> type[SiteAdapter]:
    host = urlsplit(url).netloc.lower()
    for cls in SITES:
        if any(host == d or host.endswith("." + d) for d in cls.domains):
            return cls
    raise ValueError(f"No adapter for {host!r}. Supported: "
                     + ", ".join(d for c in SITES for d in c.domains))
