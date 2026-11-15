"""`sie research probe`: robots.txt only, same rules as every fetch (offline, injected network)."""

from __future__ import annotations

from datetime import UTC, datetime

from sie.evidence.web import RawResponse, WebFetcher
from sie.research.pipeline import probe_urls

UA = "SIE-research/0.1 (contact: owner@example.org)"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


class Site:
    """A fake network: robots.txt only; any other request is recorded and answered 404."""

    def __init__(self, robots="User-agent: *\nAllow: /\n", robots_status=200):
        self.robots, self.robots_status = robots, robots_status
        self.requests: list[str] = []

    def get(self, url, headers, timeout, max_bytes):
        self.requests.append(url)
        if url.endswith("/robots.txt"):
            return RawResponse(
                self.robots_status, {"content-type": "text/plain"}, self.robots.encode()
            )
        return RawResponse(404, {}, b"no")


class Ticker:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fetcher(site, resolver=lambda h, p: ["93.184.216.34"]):
    clock = Ticker()
    return WebFetcher(
        user_agent=UA,
        get=site.get,
        resolver=resolver,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
    )


ROBOTS = "User-agent: *\nDisallow: /api/\nCrawl-delay: 5\nAllow: /\n"


def test_probe_reports_allowed_disallowed_and_crawl_delay_and_asks_only_for_robots():
    site = Site(robots=ROBOTS)
    got = probe_urls(
        ["https://data.example.org/files/a.csv", "https://data.example.org/api/search?q=x"],
        fetcher(site),
    )
    assert [g["allowed"] for g in got] == [True, False]
    assert got[0]["crawl_delay"] == 5.0
    assert "do not fetch" in str(got[1]["detail"])
    assert site.requests == ["https://data.example.org/robots.txt"]  # once per host, no page


def test_probe_reports_a_refusal_instead_of_assuming_allowed():
    site = Site(robots_status=503)
    got = probe_urls(["https://data.example.org/x"], fetcher(site))
    assert got[0]["allowed"] is None and "robots" in str(got[0]["detail"])


def test_probe_refuses_non_https_and_private_addresses_without_a_request():
    site = Site()
    got = probe_urls(
        ["http://data.example.org/x", "https://internal.example.org/x"],
        fetcher(site, resolver=lambda h, p: ["10.0.0.5"]),
    )
    assert [g["allowed"] for g in got] == [None, None] and site.requests == []
