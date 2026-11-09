"""Retry/stop behaviour with fake HTTP responses (no network)."""
import pytest
import requests

from scraper import core
from scraper.core import Blocked, Fetcher
from scraper.sites import PrivateProperty


class FakeResp:
    def __init__(self, status, text="<html></html>", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


def fetcher_with(monkeypatch, responses):
    f = Fetcher(min_delay=0, max_delay=0, allow_browser=False)
    calls = iter(responses)
    monkeypatch.setattr(f.session, "get", lambda url, timeout: next(calls))
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    return f


def test_retries_429_then_succeeds(monkeypatch):
    f = fetcher_with(monkeypatch, [FakeResp(429, headers={"Retry-After": "5"}), FakeResp(503), FakeResp(200, "ok")])
    assert f.get("https://x") == "ok"
    assert f.requests_made == 3


def test_gives_up_after_retries(monkeypatch):
    f = fetcher_with(monkeypatch, [FakeResp(503)] * 4)
    with pytest.raises(requests.HTTPError):
        f.get("https://x")


def test_403_raises_blocked(monkeypatch):
    f = fetcher_with(monkeypatch, [FakeResp(403)])
    with pytest.raises(Blocked):
        f.get("https://x")


def test_captcha_page_is_blocked_not_solved(monkeypatch):
    f = fetcher_with(monkeypatch, [FakeResp(200, "<title>Just a moment...</title>")])
    with pytest.raises(Blocked):
        f.get("https://x")


def test_site_stops_cleanly_on_403(monkeypatch):
    f = fetcher_with(monkeypatch, [FakeResp(403)])
    ad = PrivateProperty()
    assert ad.scrape(f, "https://www.privateproperty.co.za/to-rent/x/444", 3, "t") == []
    assert "403" in ad.blocked
    assert f.requests_made == 1           # no further pages attempted
