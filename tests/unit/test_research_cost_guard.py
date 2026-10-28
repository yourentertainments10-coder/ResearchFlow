"""Cost guard: quota exhaustion, timeouts and provider failures never reach a paid or unregistered endpoint.

All network behaviour here is simulated (a fake site). A passing run proves the code paths; it is not a
live fetch from a real catalogue.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from research_fakes import (
    CKAN,
    ELECTION_DATASET,
    NOW,
    OPENALEX,
    RESULTS_CSV,
    Site,
    Ticker,
    ckan_result,
    csv_page,
    json_page,
    web_fetcher,
    write_test_catalogues,
)
from sie.evidence.errors import FetchRefused
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.catalogues import build_providers, load_catalogues
from sie.research.guard import NEXT_STEP, classify_failure, search_all
from sie.research.pipeline import run_research
from sie.research.store import ResearchStore

ROOT = Path(__file__).resolve().parents[2]
REGISTERED = {"data.gov.uk", "api.openalex.org"}
DATA_URL = "https://data.example.gov.uk/election/results.csv"


def setup(tmp_path, site, *, clock=None):
    fetcher = CachedFetcher(
        web_fetcher(site, Ticker()),
        FetchCache(tmp_path / "cache"),
        max_age=timedelta(hours=24),
        now=lambda: (clock or [NOW])[0],
    )
    catalogues_path = write_test_catalogues(tmp_path / "catalogues.csv")
    fetcher.catalogues_path = catalogues_path
    entries = load_catalogues(catalogues_path)
    return fetcher, build_providers(entries, fetcher)


def only_registered_hosts(site, extra=()):
    assert site.hosts() <= REGISTERED | set(extra), site.hosts()


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (RawResponse(429, {}, b"quota"), "quota_exhausted"),
        (RawResponse(503, {}, b"down"), "unavailable"),
        (RawResponse(403, {}, b"no"), "unavailable"),
        (FetchRefused("network", "the request failed: TimeoutError"), "unavailable"),
        (json_page({"unexpected": True}), "bad_response"),
    ],
)
def test_a_failing_provider_is_reported_and_skipped_never_replaced(response, status, tmp_path):
    site = Site({CKAN: response, OPENALEX: json_page({"results": []})})
    _, providers = setup(tmp_path, site)
    hits, outcomes = search_all(providers, "general election results", 10)
    by_name = {o.provider: o for o in outcomes}
    assert by_name["data.gov.uk"].status == status and NEXT_STEP in by_name["data.gov.uk"].detail
    assert by_name["openalex"].status == "ok"  # the other free source still ran
    assert hits == []
    only_registered_hosts(site)  # nothing else was ever contacted


def test_a_429_stops_that_host_for_the_run_and_is_not_retried(tmp_path):
    site = Site({CKAN: RawResponse(429, {}, b"quota"), OPENALEX: json_page({"results": []})})
    _, providers = setup(tmp_path, site)
    search_all(providers, "election", 10)
    search_all(providers, "election results", 10)
    ckan_calls = [u for u in site.api_requests() if u.startswith(CKAN)]
    assert len(ckan_calls) == 1  # the second search did not call the exhausted provider again
    second = search_all(providers, "turnout", 10)[1]
    assert {o.provider: o.status for o in second}["data.gov.uk"] == "quota_exhausted"


def test_robots_refusal_is_reported_not_bypassed(tmp_path):
    site = Site({CKAN: json_page(ckan_result())}, robots="User-agent: *\nDisallow: /api/\n")
    _, providers = setup(tmp_path, site)
    _, outcomes = search_all(providers, "election", 10)
    refused = next(o for o in outcomes if o.provider == "data.gov.uk")
    assert refused.status == "refused" and "robots" in refused.detail
    assert not [u for u in site.api_requests() if u.startswith(CKAN)]


def test_old_cached_search_results_are_used_when_the_provider_runs_out_of_quota(tmp_path):
    clock = [NOW]
    site = Site(
        {CKAN: json_page(ckan_result(ELECTION_DATASET)), OPENALEX: json_page({"results": []})}
    )
    _, providers = setup(tmp_path, site, clock=clock)
    first, _ = search_all(providers, "general election results", 10)
    assert len(first) == 1
    clock[0] = NOW + timedelta(days=5)  # the cached answer is now past its lifetime
    site.pages[CKAN] = RawResponse(429, {}, b"quota")
    # new fetcher instances so the earlier stop state does not hide the behaviour
    _, providers2 = setup(tmp_path, site, clock=clock)
    again, outcomes = search_all(providers2, "general election results", 10)
    assert [h.url for h in again] == [h.url for h in first]
    assert {o.status for o in outcomes} == {"ok"}
    only_registered_hosts(site)


def test_when_every_free_source_fails_the_run_asks_for_manual_input(tmp_path):
    site = Site({CKAN: RawResponse(429, {}, b"q"), OPENALEX: RawResponse(503, {}, b"d")})
    fetcher, _ = setup(tmp_path, site)
    run = run_research(
        "general election results",
        store=ResearchStore(tmp_path / "store"),
        discovery_fetcher=fetcher,
        data_fetcher=fetcher,
        catalogues_path=fetcher.catalogues_path,
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv",
    )
    assert run.needs_manual_input and not run.tables
    assert {o.status for o in run.outcomes} == {"quota_exhausted", "unavailable"}
    only_registered_hosts(site)


def test_a_data_fetch_timeout_skips_the_lead_and_names_it(tmp_path):
    site = Site(
        {
            CKAN: json_page(ckan_result(ELECTION_DATASET)),
            OPENALEX: json_page({"results": []}),
            DATA_URL: FetchRefused("network", "the request failed: TimeoutError"),
        }
    )
    fetcher, _ = setup(tmp_path, site)
    run = run_research(
        "general election results",
        store=ResearchStore(tmp_path / "store"),
        discovery_fetcher=fetcher,
        data_fetcher=fetcher,
        catalogues_path=ROOT / "data" / "evidence" / "catalogues.csv",
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv",
    )
    assert run.needs_manual_input
    assert any(DATA_URL in p for p in run.problems)
    only_registered_hosts(site, extra={"data.example.gov.uk"})


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (FetchRefused("status", "HTTP 429 from x: stopped, not retried"), "quota_exhausted"),
        (
            FetchRefused("stopped", "requests to x were stopped earlier: HTTP 429"),
            "quota_exhausted",
        ),
        (FetchRefused("stopped", "requests to x were stopped earlier: HTTP 503"), "unavailable"),
        (FetchRefused("network", "TimeoutError"), "unavailable"),
        (FetchRefused("robots", "disallowed"), "refused"),
        (FetchRefused("unregistered_host", "no"), "refused"),
        (ValueError("x"), "bad_response"),
    ],
)
def test_failure_classification(exc, status):
    assert classify_failure(exc)[0] == status


def test_no_research_code_names_a_paid_service_or_carries_a_credential():
    """A static guard over the package: no key, token, payment or paid-provider wiring can be added unseen."""
    forbidden = re.compile(
        r"api[_-]?key|apikey|authorization|bearer|x-api|secret|password|\btokens?\b(?!\()|access_token|stripe|billing|"
        r"openai|anthropic|serpapi|bing|googleapis|customsearch|brave\.com|subscription",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted((ROOT / "src" / "sie" / "research").glob("*.py")) + [
        ROOT / "src" / "sie" / "research_cli.py"
    ]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if forbidden.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], offenders


def test_the_registry_has_no_endpoint_outside_the_two_verified_free_hosts():
    hosts = {e.host for e in load_catalogues(ROOT / "data" / "evidence" / "catalogues.csv")}
    assert hosts == REGISTERED


def test_csv_pages_still_go_through_the_same_safe_fetcher(tmp_path):
    site = Site({DATA_URL: csv_page(RESULTS_CSV)})
    fetcher, _ = setup(tmp_path, site)
    fetcher.fetch(DATA_URL)
    assert site.requests[0].endswith("/robots.txt")  # robots first, always
