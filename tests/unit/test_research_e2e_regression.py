"""End-to-end regression of live-discovery review findings, on a simulated network (NOT a live fetch).

Guards: a US 2011 election dataset or unrelated papers must never answer a UK 2024 question; an academic
paper is a reference, not data; a lead's robots status is 'not_checked' until looked up; provider failure
and an empty result end in a request for a URL or file, never a report; no request leaves the registered
free hosts; licence provenance reaches the report; lead limitations are disclosed as UNVERIFIED gaps.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from research_fakes import (
    CKAN,
    NOW,
    OPENALEX,
    RESULTS_CSV,
    Site,
    Ticker,
    ckan_result,
    csv_page,
    json_page,
    web_fetcher,
)
from sie.answer import verify
from sie.evidence.discovery import SearchHit, discover, load_authority
from sie.evidence.errors import EvidenceError
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.pipeline import run_research
from sie.research.report import AnalysisItem, analyze, build_report, plan_from_dict
from sie.research.store import ResearchStore, StoreRules

ROOT = Path(__file__).resolve().parents[2]
QUESTION = "UK general election 2024 constituency results"
UK_URL = "https://data.example.gov.uk/ge2024/constituency-results.csv"
US_URL = "https://data.example.gov/us2011/election-results.csv"
REGISTERED = {"data.gov.uk", "api.openalex.org"}


def dataset(title, url, *, notes="", licence="uk-ogl", fmt="CSV"):
    return {
        "name": title.lower().replace(" ", "-"), "title": title, "notes": notes,
        "license_id": licence, "metadata_modified": "2026-05-01T00:00:00",
        "resources": [{"url": url, "format": fmt, "name": "Results"}],
    }  # fmt: skip


def paper(title, doi):
    return {"title": title, "doi": doi, "publication_date": "2024-01-01"}


def world(tmp_path, ckan=None, openalex=None, pages=None):
    site = Site({
        CKAN: ckan if ckan is not None else json_page(ckan_result()),
        OPENALEX: openalex if openalex is not None else json_page({"results": []}),
        **(pages or {}),
    })  # fmt: skip
    cache = FetchCache(tmp_path / "cache", now=lambda: NOW)
    live = web_fetcher(site, Ticker())
    disc = CachedFetcher(live, cache, max_age=timedelta(hours=24), now=lambda: NOW)
    data = CachedFetcher(live, cache, max_age=timedelta(days=7), now=lambda: NOW)
    rules = StoreRules.load(ROOT / "data" / "evidence")
    store = ResearchStore(tmp_path / "store", rules=rules, now=lambda: NOW)
    return site, disc, data, store


def go(disc, data, store, question=QUESTION):
    return run_research(
        question, store=store, discovery_fetcher=disc, data_fetcher=data,
        catalogues_path=ROOT / "data" / "evidence" / "catalogues.csv",
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv", now=NOW,
    )  # fmt: skip


def report_for(run):
    spec = {"op": "group_aggregate", "by": ["party"], "metrics": ["seats:sum"], "sort": "seats:sum"}
    result = analyze(run.tables[0], spec, plan_from_dict({"parse_number": [{"column": "seats"}]}))
    return build_report(
        QUESTION, run.tables, [AnalysisItem("seats", result, "party", "seats:sum")],
        now=NOW, tiers=run.tiers, lead_limitations=run.lead_limitations,
        provider_notes=[f"{o.provider}: {o.status}" for o in run.outcomes if o.status != "ok"],
    )  # fmt: skip


def rejected_codes(run):
    return {r.url: r.code for r in run.discovery.rejected}


# -- relevant CSV discovery ---------------------------------------------------------------------
def test_a_relevant_uk_2024_csv_is_discovered_collected_and_reported(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL, licence="uk-ogl")
    site, disc, data, store = world(
        tmp_path, ckan=json_page(ckan_result(ds)), pages={UK_URL: csv_page(RESULTS_CSV)}
    )
    run = go(disc, data, store)
    assert len(run.tables) == 1 and run.tables[0].url == UK_URL
    top = run.discovery.candidates[0]
    assert top.kind == "dataset" and top.structured and top.format == "csv"
    assert top.relevance == 1.0 and top.limitations == ()  # place and year both stated and matching
    report = report_for(run)
    assert verify(report.answer).ok and "Alpha: 310" in report.markdown


# -- country / year mismatch --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "code", "word"),
    [
        ("US general election 2011 constituency results", "mismatch", "geography mismatch"),
        ("UK general election 2011 constituency results", "mismatch", "year mismatch"),
        ("Canada general election 2024 constituency results", "mismatch", "geography mismatch"),
    ],
)
def test_a_dataset_for_the_wrong_country_or_year_is_not_an_answer(title, code, word, tmp_path):
    ds = dataset(title, US_URL)
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)),
                                    pages={US_URL: csv_page(RESULTS_CSV)})  # fmt: skip
    run = go(disc, data, store)
    assert run.discovery.candidates == () and run.tables == [] and run.needs_manual_input
    assert rejected_codes(run)[US_URL] == code
    assert word in next(r.reason for r in run.discovery.rejected if r.url == US_URL)
    assert US_URL not in site.requests  # the wrong-place file was never even downloaded
    with pytest.raises(EvidenceError):
        build_report(QUESTION, [], [], now=NOW)  # no usable data, no report


def test_a_lead_that_does_not_state_place_or_year_is_kept_with_the_limits_disclosed(tmp_path):
    url = "https://data.example.gov.uk/elections/constituency-results.csv"
    ds = dataset("General election constituency results", url)
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)),
                                    pages={url: csv_page(RESULTS_CSV)})  # fmt: skip
    run = go(disc, data, store)
    limits = run.lead_limitations[url]
    assert any("geography not stated" in n for n in limits)
    assert any("year not stated" in n for n in limits)
    gaps = [g.message for g in report_for(run).answer.gaps]
    assert any(url in g and "not verified against the question" in g and "year not stated" in g for g in gaps)  # fmt: skip
    assert any("geography not stated" in g for g in gaps)
    assert all(g.endswith(".") for g in gaps if "search lead" in g)


# -- academic results ---------------------------------------------------------------------------
def test_unrelated_papers_are_rejected_and_a_related_paper_is_only_a_reference(tmp_path):
    papers = json_page({"results": [
        paper("Protein folding with deep networks", "https://doi.org/10.1/protein"),
        paper("Voter behaviour and the UK general election 2024 constituency results", "https://doi.org/10.1/uk24"),
        paper("The 2011 US presidential election constituency results revisited", "https://doi.org/10.1/us11"),
    ]})  # fmt: skip
    site, disc, data, store = world(tmp_path, openalex=papers)
    run = go(disc, data, store)
    assert rejected_codes(run)["https://doi.org/10.1/protein"] in {"irrelevant", "low_relevance"}
    assert rejected_codes(run)["https://doi.org/10.1/us11"] == "mismatch"
    refs = run.discovery.references
    assert [c.url for c in refs] == ["https://doi.org/10.1/uk24"]
    assert refs[0].kind == "paper" and not refs[0].structured
    assert any("not a structured dataset" in n for n in refs[0].limitations)
    assert run.discovery.usable == ()  # a paper is never data
    assert run.tables == [] and run.needs_manual_input
    assert not any("doi.org" in u for u in site.requests)  # and is never fetched


def test_a_structured_dataset_outranks_an_equally_relevant_paper(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL)
    papers = json_page({"results": [
        paper("UK general election 2024 constituency results", "https://doi.org/10.1/uk24")]})  # fmt: skip
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)), openalex=papers,
                                    pages={UK_URL: csv_page(RESULTS_CSV)})  # fmt: skip
    run = go(disc, data, store)
    assert [c.kind for c in run.discovery.candidates] == ["dataset", "paper"]
    assert [t.url for t in run.tables] == [UK_URL]


# -- provider failure ---------------------------------------------------------------------------
def test_provider_failure_is_reported_and_no_report_is_made(tmp_path):
    site, disc, data, store = world(
        tmp_path, ckan=RawResponse(500, {}, b""), openalex=RawResponse(429, {}, b"")
    )
    run = go(disc, data, store)
    statuses = {o.provider: o.status for o in run.outcomes}
    assert set(statuses.values()) <= {"unavailable", "quota_exhausted", "refused"}
    assert "ok" not in statuses.values()
    assert run.needs_manual_input and run.tables == []


def test_one_provider_failing_does_not_hide_the_other(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL)
    site, disc, data, store = world(
        tmp_path, ckan=json_page(ckan_result(ds)), openalex=RawResponse(500, {}, b""),
        pages={UK_URL: csv_page(RESULTS_CSV)},
    )  # fmt: skip
    run = go(disc, data, store)
    assert len(run.tables) == 1
    report = report_for(run)
    assert any("A free source was unavailable" in g.message for g in report.answer.gaps)


# -- licence provenance -------------------------------------------------------------------------
def test_the_catalogue_licence_travels_to_the_stored_manifest_and_the_report(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL, licence="uk-ogl")
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)),
                                    pages={UK_URL: csv_page(RESULTS_CSV)})  # fmt: skip
    run = go(disc, data, store)
    assert run.tables[0].licence == "uk-ogl" and run.tables[0].catalogue == "data.gov.uk"
    assert store.list_datasets()[0]["licence"] == "uk-ogl"
    assert "licence uk-ogl" in report_for(run).markdown


def test_a_missing_licence_is_disclosed_never_filled_in(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL, licence=None)
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)),
                                    pages={UK_URL: csv_page(RESULTS_CSV)})  # fmt: skip
    run = go(disc, data, store)
    assert run.tables[0].licence is None
    assert any("No licence or terms were stated" in g.message for g in report_for(run).answer.gaps)


# -- no paid fallback ---------------------------------------------------------------------------
def test_every_request_stays_on_registered_free_hosts_or_a_listed_data_file(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL)
    site, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)),
                                    pages={UK_URL: csv_page(RESULTS_CSV)})  # fmt: skip
    go(disc, data, store)
    assert site.hosts() <= REGISTERED | {"data.example.gov.uk"}
    assert not [
        u for u in site.requests if any(w in u.lower() for w in ("key=", "token=", "apikey"))
    ]


def test_when_everything_fails_nothing_else_is_tried(tmp_path):
    site, disc, data, store = world(tmp_path, ckan=RawResponse(500, {}, b""),
                                    openalex=RawResponse(500, {}, b""))  # fmt: skip
    run = go(disc, data, store)
    assert run.needs_manual_input
    assert site.hosts() <= REGISTERED


# -- robots status is explicit ------------------------------------------------------------------
def test_a_discovered_lead_is_not_checked_for_robots_and_never_called_verified(tmp_path):
    ds = dataset("UK general election 2024 constituency results", UK_URL)
    _, disc, data, store = world(tmp_path, ckan=json_page(ckan_result(ds)))
    run = go(disc, data, store)
    cand = run.discovery.candidates[0]
    assert cand.allowed_by_robots is None and cand.robots_status == "not_checked"
    out = run.discovery.to_dict()
    assert out["candidates"][0]["robots_status"] == "not_checked"
    assert out["candidates"][0]["role"] == "data_lead"
    assert "not evidence" in out["note"] and "not_checked" in out["note"]
    assert "verified" not in " ".join(cand.reasons).lower()


def test_the_robots_status_follows_a_real_check():
    class P:
        def search(self, q, n):
            return [SearchHit("https://data.example.gov.uk/ge/uk-2024-constituency-results.csv",
                              "UK general election 2024 constituency results")]  # fmt: skip

    rules = load_authority(ROOT / "data" / "evidence" / "source_authority.csv")
    site = Site(robots="User-agent: *\nDisallow: /ge\n")
    got = discover(P(), QUESTION, rules=rules, fetcher=web_fetcher(site))
    assert got.candidates[0].robots_status == "disallowed" and got.usable == ()
    site = Site(robots="User-agent: *\nAllow: /\n")
    got = discover(P(), QUESTION, rules=rules, fetcher=web_fetcher(site))
    assert got.candidates[0].robots_status == "allowed"
    assert urlsplit(got.candidates[0].url).path not in str(site.requests)  # robots only, no page


# -- report only from usable data ---------------------------------------------------------------
def test_a_report_needs_collected_data(tmp_path):
    site, disc, data, store = world(tmp_path)
    run = go(disc, data, store)
    assert run.tables == [] and run.needs_manual_input
    with pytest.raises(EvidenceError):
        build_report(QUESTION, run.tables, [], now=NOW)


def redirecting_world(tmp_path, target, **kw):
    """data.gov.uk's API redirects (as seen live); fetchers are built with the committed redirect hosts."""
    from sie.research.catalogues import load_catalogues

    entries = load_catalogues(ROOT / "data" / "evidence" / "catalogues.csv")
    hosts = {e.host: e.redirect_host_list for e in entries if e.redirect_host_list}
    site, disc, data, store = world(
        tmp_path, ckan=RawResponse(301, {"location": target}, b""), **kw
    )
    live = web_fetcher(site, Ticker(), redirect_hosts=hosts)
    cache = FetchCache(tmp_path / "cache2", now=lambda: NOW)
    disc = CachedFetcher(live, cache, max_age=timedelta(hours=24), now=lambda: NOW)
    data = CachedFetcher(live, cache, max_age=timedelta(days=7), now=lambda: NOW)
    return site, disc, data, store


def test_data_gov_uk_redirecting_to_its_ckan_host_still_yields_the_dataset(tmp_path):
    """Live run 2026-10-25: the API answers with a redirect to ckan.publishing.service.gov.uk. That host
    is named in catalogues.csv, so it is followed (robots and address checks included)."""
    real = "https://ckan.publishing.service.gov.uk/api/action/package_search"
    site, disc, data, store = redirecting_world(
        tmp_path, real + "?q=x&rows=30", pages={UK_URL: csv_page(RESULTS_CSV)}
    )
    site.pages[real] = json_page(ckan_result(dataset("UK general election 2024 constituency results", UK_URL)))  # fmt: skip
    run = go(disc, data, store)
    assert [t.url for t in run.tables] == [UK_URL]
    assert "https://ckan.publishing.service.gov.uk/robots.txt" in site.requests


def test_a_redirect_to_an_unnamed_host_is_a_provider_failure_naming_the_host(tmp_path):
    site, disc, data, store = redirecting_world(tmp_path, "https://evil.example.net/x")
    run = go(disc, data, store)
    assert run.needs_manual_input
    assert any("evil.example.net" in o.detail for o in run.outcomes if o.status != "ok")
    assert not any("evil" in u for u in site.requests)
