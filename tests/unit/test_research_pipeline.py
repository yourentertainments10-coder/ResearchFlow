"""The whole free loop against a simulated network: discover, fetch, collect, store, analyse, reuse.

Every request goes to a fake site. This proves the logic end to end; it is NOT a live fetch from a real
catalogue (see docs/PHASE4_WEB_RESEARCH_DESIGN.md, 'Verification status').
"""

from __future__ import annotations

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
from sie.answer import verify
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.collect import NothingCollected
from sie.research.pipeline import collect_url, run_research
from sie.research.report import AnalysisItem, analyze, build_report, plan_from_dict
from sie.research.store import ResearchStore, StoreRules

ROOT = Path(__file__).resolve().parents[2]
DATA_URL = "https://data.example.gov.uk/election/results.csv"
QUESTION = "how many seats did each party win in the general election results"


def world(tmp_path):
    site = Site(
        {
            CKAN: json_page(ckan_result(ELECTION_DATASET)),
            OPENALEX: json_page({"results": []}),
            DATA_URL: csv_page(RESULTS_CSV),
        }
    )
    cache = FetchCache(tmp_path / "cache", now=lambda: NOW)
    live = web_fetcher(site, Ticker())
    disc = CachedFetcher(live, cache, max_age=timedelta(hours=24), now=lambda: NOW)
    disc.catalogues_path = write_test_catalogues(tmp_path / "catalogues.csv")
    data = CachedFetcher(live, cache, max_age=timedelta(days=7), now=lambda: NOW)
    rules = StoreRules.load(ROOT / "data" / "evidence")
    return site, disc, data, ResearchStore(tmp_path / "store", rules=rules, now=lambda: NOW)


def go(disc, data, store, **kw):
    return run_research(
        QUESTION,
        store=store,
        discovery_fetcher=disc,
        data_fetcher=data,
        catalogues_path=disc.catalogues_path,
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv",
        now=NOW,
        **kw,
    )


def test_discovery_leads_to_a_stored_dataset_with_provenance(tmp_path):
    site, disc, data, store = world(tmp_path)
    run = go(disc, data, store)
    assert not run.reused and len(run.tables) == 1 and len(run.stored) == 1
    t = run.tables[0]
    assert t.url == DATA_URL and t.retrieved_at == NOW.isoformat() and t.rows == 4
    assert run.tiers[DATA_URL] == 1  # a .gov.uk host
    assert store.list_datasets()[0]["extra"]["title"].startswith(
        "General election results by party"
    )
    assert {o.status for o in run.outcomes} == {"ok"}


def test_a_follow_up_reuses_the_stored_dataset_with_no_network(tmp_path):
    site, disc, data, store = world(tmp_path)
    go(disc, data, store)
    seen = len(site.requests)
    again = go(disc, data, store)
    assert again.reused and again.tables[0].rows == 4 and len(site.requests) == seen


def test_refresh_does_not_duplicate_the_dataset_and_uses_the_cache(tmp_path):
    site, disc, data, store = world(tmp_path)
    go(disc, data, store)
    seen = len(site.api_requests())
    go(disc, data, store, refresh=True)
    assert len(store.list_datasets()) == 1  # same page content, same dataset
    assert len(site.api_requests()) == seen  # discovery and the data file came from the cache


def test_the_same_data_after_expiry_is_still_not_ingested_twice(tmp_path):
    site, disc, data, store = world(tmp_path)
    go(disc, data, store)
    later = CachedFetcher(web_fetcher(site, Ticker()), FetchCache(tmp_path / "cache", now=lambda: NOW + timedelta(days=30)),
                          max_age=timedelta(seconds=1), now=lambda: NOW + timedelta(days=30))  # fmt: skip
    go(later, later, store, refresh=True)
    assert len(store.list_datasets()) == 1


def test_collect_url_reports_a_page_without_tables_as_nothing(tmp_path):
    site, _, data, store = world(tmp_path)
    site.pages["https://x.example.gov.uk/p"] = RawResponse(
        200, {"content-type": "text/html"}, b"<p>no table</p>"
    )
    with pytest.raises(NothingCollected, match="no usable HTML table"):
        collect_url("https://x.example.gov.uk/p", data, store)
    assert store.list_datasets() == []


def test_the_loop_ends_in_a_verified_evidence_linked_report(tmp_path):
    _, disc, data, store = world(tmp_path)
    run = go(disc, data, store)
    t = run.tables[0]
    spec = {"op": "group_aggregate", "by": ["party"], "metrics": ["seats:sum"], "sort": "seats:sum"}
    result = analyze(t, spec, plan_from_dict({"parse_number": [{"column": "seats"}]}))
    r = build_report(
        QUESTION, run.tables, [AnalysisItem("seats", result, "party", "seats:sum")],
        now=NOW, tiers=run.tiers,
    )  # fmt: skip
    assert verify(r.answer).ok and "Alpha: 310" in r.markdown
    assert r.answer.evidence[0].tier == 1 and DATA_URL in r.markdown and r.svg


def test_saved_data_without_the_needed_column_is_not_reused_and_more_is_collected(tmp_path):
    site, disc, data, store = world(tmp_path)
    go(disc, data, store)
    seen = len(site.api_requests())
    spec = {"op": "top_n", "by": "party", "metric": "turnout:mean"}
    again = go(disc, data, store, spec=spec)
    assert (
        not again.reused
        and again.reuse_rejected
        and "turnout" in again.reuse_rejected[0].reasons[0]
    )
    assert len(site.api_requests()) >= seen  # it went back to discovery


def test_stale_saved_data_is_a_disclosed_last_resort_only_when_nothing_newer_is_found(tmp_path):
    site, disc, data, store = world(tmp_path)
    go(disc, data, store)
    for key in list(site.pages):
        site.pages[key] = RawResponse(503, {}, b"down")
    later_now = NOW + timedelta(days=60)
    cache = FetchCache(tmp_path / "cache", now=lambda: later_now)
    live = CachedFetcher(web_fetcher(site, Ticker()), cache, max_age=timedelta(hours=24),
                         now=lambda: later_now)  # fmt: skip
    later = run_research(
        QUESTION, store=store, discovery_fetcher=live, data_fetcher=live,
        catalogues_path=disc.catalogues_path,
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv",
        now=later_now,
    )  # fmt: skip
    assert later.reused and later.stale_datasets and later.tables[0].rows == 4
    assert any("retrieved 60 days ago" in r for d in later.reuse_rejected for r in d.reasons)


def test_a_table_that_may_not_be_kept_is_used_this_run_and_listed_as_not_saved(tmp_path):
    site, disc, data, store = world(tmp_path)
    ds = dict(ELECTION_DATASET, license_id="all-rights-reserved", license_title="Restricted")
    site.pages[CKAN] = json_page(ckan_result(ds))
    run = go(disc, data, store)
    assert len(run.tables) == 1 and run.not_saved and store.list_datasets() == []
    assert run.tables[0].licence == "all-rights-reserved"
