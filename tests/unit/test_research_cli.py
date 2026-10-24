"""`sie research ...` over a simulated network. Exit codes, files written, no paid fallback."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
from sie.cli import app
from sie.evidence.web import RawResponse
from sie.research.cache import CachedFetcher, FetchCache

ROOT = Path(__file__).resolve().parents[2]
DATA_URL = "https://data.example.gov.uk/election/results.csv"
QUESTION = "how many seats did each party win in the general election results"
OP = '{"op":"group_aggregate","by":["party"],"metrics":["seats:sum"],"sort":"seats:sum"}'
PLAN = '{"parse_number":[{"column":"seats"}]}'
runner = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "evidence").mkdir(parents=True)
    for name in ("catalogues.csv", "source_authority.csv"):
        shutil.copy(ROOT / "data" / "evidence" / name, data / "evidence" / name)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/none")
    site = Site(
        {
            CKAN: json_page(ckan_result(ELECTION_DATASET)),
            OPENALEX: json_page({"results": []}),
            DATA_URL: csv_page(RESULTS_CSV),
        }
    )

    def fake_build(settings, now=None):
        cache = FetchCache(settings.research_dir / "cache")
        live = web_fetcher(site, Ticker())
        mk = lambda age: CachedFetcher(live, cache, max_age=age, now=lambda: NOW)  # noqa: E731
        return mk(timedelta(hours=24)), mk(timedelta(days=7))

    monkeypatch.setattr("sie.research.pipeline.build_fetchers", fake_build)
    return {"site": site, "tmp": tmp_path}


def test_ask_writes_a_report_then_reuses_the_stored_data(env):
    out = env["tmp"] / "out"
    args = ["research", "ask", QUESTION, "--op", OP, "--clean", PLAN, "--out", str(out)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0 and "collected new data" in first.output
    report = (out / "report.md").read_text()
    assert "Alpha: 310" in report and DATA_URL in report
    assert json.loads((out / "answer.json").read_text())["question"] == QUESTION
    assert (out / "chart.svg").read_text().startswith("<svg")
    seen = len(env["site"].requests)
    second = runner.invoke(app, args)
    assert second.exit_code == 0 and "reused stored data" in second.output
    assert len(env["site"].requests) == seen  # the follow-up needed no network at all


def test_ask_with_every_free_source_down_exits_1_and_says_nothing_paid_was_tried(env):
    env["site"].pages[CKAN] = RawResponse(429, {}, b"quota")
    env["site"].pages[OPENALEX] = RawResponse(503, {}, b"down")
    result = runner.invoke(app, ["research", "ask", QUESTION, "--out", str(env["tmp"] / "o")])
    assert result.exit_code == 1
    assert "Nothing paid was tried" in result.output and "quota_exhausted" in result.output
    assert not (env["tmp"] / "o" / "report.md").exists()


def test_invalid_operation_json_is_a_usage_error(env):
    result = runner.invoke(app, ["research", "ask", QUESTION, "--op", "{not json"])
    assert result.exit_code == 2 and "invalid JSON" in result.output


def test_an_invalid_operation_is_a_usage_error_and_writes_nothing(env):
    out = env["tmp"] / "o"
    bad = '{"op":"group_aggregate","by":["nope"],"metrics":["seats:sum"]}'
    result = runner.invoke(
        app, ["research", "ask", QUESTION, "--op", bad, "--clean", PLAN, "--out", str(out)]
    )
    assert result.exit_code == 2 and not (out / "report.md").exists()


def test_discover_prints_ranked_leads_and_provider_outcomes(env):
    result = runner.invoke(app, ["research", "discover", QUESTION])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidates"][0]["url"] == DATA_URL and payload["candidates"][0]["tier"] == 1
    assert {p["status"] for p in payload["providers"]} == {"ok"}


def test_collect_stores_a_named_url_and_lists_it(env):
    result = runner.invoke(app, ["research", "collect", DATA_URL, "--title", "Election"])
    assert result.exit_code == 0 and "stored:" in result.output and "rows 4" in result.output
    again = runner.invoke(app, ["research", "collect", DATA_URL])
    assert "already stored" in again.output
    listed = runner.invoke(app, ["research", "datasets"])
    assert DATA_URL in listed.output and "4 rows" in listed.output


def test_collect_refusal_exits_1(env):
    env["site"].pages[DATA_URL] = RawResponse(403, {}, b"no")
    result = runner.invoke(app, ["research", "collect", DATA_URL])
    assert result.exit_code == 1 and "refused" in result.output
