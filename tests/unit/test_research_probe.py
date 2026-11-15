"""`sie research probe`: robots.txt only, same rules as every fetch (offline, injected network)."""

from __future__ import annotations

from tests.unit.test_evidence_web import Site, fetcher

from sie.research.pipeline import probe_urls

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
