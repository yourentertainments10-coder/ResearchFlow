"""The fetch cache: write once, dedupe by content, never alter, fall back to a stale copy flagged."""

from __future__ import annotations

from datetime import timedelta

import pytest

from research_fakes import NOW, Site, csv_page, web_fetcher
from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache

URL = "https://data.example.gov.uk/a.csv"
DAY = timedelta(days=1)


def make(tmp_path, site, *, clock=None, **kw):
    cache = FetchCache(tmp_path / "cache")
    now = clock or [NOW]
    return (
        CachedFetcher(web_fetcher(site), cache, max_age=DAY, now=lambda: now[0], **kw),
        cache,
        now,
    )


def test_a_cached_page_is_served_without_a_request(tmp_path):
    site = Site({URL: csv_page("a,b\n1,2\n")})
    fetcher, _, _ = make(tmp_path, site)
    first = fetcher.fetch(URL)
    seen = len(site.requests)
    second = fetcher.fetch(URL)
    assert not first.from_cache and second.from_cache and not second.stale
    assert second.page.body == first.page.body and len(site.requests) == seen


def test_an_expired_entry_is_fetched_again(tmp_path):
    site = Site({URL: csv_page("a,b\n1,2\n")})
    fetcher, _, clock = make(tmp_path, site)
    fetcher.fetch(URL)
    before = len(site.api_requests())
    clock[0] = NOW + timedelta(days=2)
    again = fetcher.fetch(URL)
    assert not again.from_cache and len(site.api_requests()) == before + 1


def test_a_failed_live_fetch_falls_back_to_the_old_copy_and_says_so(tmp_path):
    site = Site({URL: csv_page("a,b\n1,2\n")})
    fetcher, _, clock = make(tmp_path, site)
    fetcher.fetch(URL)
    clock[0] = NOW + timedelta(days=9)
    site.pages[URL] = RawResponse(500, {}, b"down")
    # a fresh fetcher: the first one stopped this host after nothing, but the second sees the 500
    fetcher2 = CachedFetcher(web_fetcher(site), fetcher.cache, max_age=DAY, now=lambda: clock[0])
    result = fetcher2.fetch(URL)
    assert result.from_cache and result.stale and "live fetch failed" in result.reason
    assert result.page.body == b"a,b\n1,2\n"


def test_without_a_cached_copy_a_failure_is_raised_not_hidden(tmp_path):
    site = Site({URL: RawResponse(429, {}, b"slow down")})
    fetcher, _, _ = make(tmp_path, site)
    with pytest.raises(FetchRefused):
        fetcher.fetch(URL)


def test_stale_fallback_can_be_switched_off(tmp_path):
    site = Site({URL: csv_page("a\n1\n")})
    fetcher, cache, clock = make(tmp_path, site, use_stale_on_failure=False)
    fetcher.fetch(URL)
    clock[0] = NOW + timedelta(days=9)
    site.pages[URL] = RawResponse(500, {}, b"down")
    strict = CachedFetcher(
        web_fetcher(site), cache, max_age=DAY, now=lambda: clock[0], use_stale_on_failure=False
    )
    with pytest.raises(FetchRefused):
        strict.fetch(URL)


def test_identical_content_from_two_urls_is_stored_once(tmp_path):
    other = "https://data.example.gov.uk/b.csv"
    site = Site({URL: csv_page("a\n1\n"), other: csv_page("a\n1\n")})
    fetcher, cache, _ = make(tmp_path, site)
    fetcher.fetch(URL)
    fetcher.fetch(other)
    assert len(list((cache.root / "blobs").iterdir())) == 1
    assert len(list((cache.root / "index").iterdir())) == 2


def test_a_changed_blob_is_refused_loudly(tmp_path):
    site = Site({URL: csv_page("a\n1\n")})
    fetcher, cache, _ = make(tmp_path, site)
    fetcher.fetch(URL)
    blob = next((cache.root / "blobs").iterdir())
    blob.write_bytes(b"tampered")
    with pytest.raises(EvidenceError, match="no longer matches"):
        cache.get(URL)


def test_a_missing_entry_is_none(tmp_path):
    assert FetchCache(tmp_path / "c").get(URL) is None


def test_no_temp_files_are_left_behind(tmp_path):
    site = Site({URL: csv_page("a\n1\n")})
    fetcher, cache, _ = make(tmp_path, site)
    fetcher.fetch(URL)
    leftovers = [p for p in cache.root.rglob(".tmp-*")]
    assert leftovers == []
