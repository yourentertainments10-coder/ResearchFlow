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
)
from sie.answer import verify
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.collect import NothingCollected
from sie.research.pipeline import collect_url, run_research
from sie.research.report import AnalysisItem, analyze, build_report, plan_from_dict
from sie.research.store import ResearchStore

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
    cache = FetchCache(tmp_path / "cache")
    live = web_fetcher(site, Ticker())
    disc = CachedFetcher(live, cache, max_age=timedelta(hours=24), now=lambda: NOW)
    data = CachedFetcher(live, cache, max_age=timedelta(days=7), now=lambda: NOW)
    return site, disc, data, ResearchStore(tmp_path / "store")


def go(disc, data, store, **kw):
    return run_research(
        QUESTION,
        store=store,
        discovery_fetcher=disc,
        data_fetcher=data,
        catalogues_path=ROOT / "data" / "evidence" / "catalogues.csv",
        authority_path=ROOT / "data" / "evidence" / "source_authority.csv",
        **kw,
    )


def test_discovery_leads_to_a_stored_dataset_with_provenance(tmp_path):
    site, disc, data, store = world(tmp_path)
    run = go(disc, data, store)
    assert not run.reused and len(run.tables) == 1 and len(run.stored) == 1
    t = run.tables[0]
    assert t.url == DATA_URL and t.retrieved_at == NOW.isoformat() and t.rows == 4
    assert run.tiers[DATA_URL] == 1  # a .gov.uk host
    assert store.list_datasets()[0]["title"].startswith("General election results by party")
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
    later = CachedFetcher(web_fetcher(site, Ticker()), FetchCache(tmp_path / "cache"),
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
