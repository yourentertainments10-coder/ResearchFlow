"""Fetched pages become project evidence (excerpt by default); the CLI refuses before any network call."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from sie.cli import app
from sie.evidence import (
    FetchRefused,
    WebFetcher,
    create_project,
    fetch_into_project,
    get_source,
    open_scope,
)
from sie.evidence.web import RawResponse

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
UA = "SIE-research/0.1 (contact: owner@example.org)"
URL = "https://results.example.org/summary"


def make_fetcher(pages, robots="User-agent: *\nAllow: /\n"):
    def get(url, headers, timeout, max_bytes):
        if url.endswith("/robots.txt"):
            return RawResponse(200, {"content-type": "text/plain"}, robots.encode())
        return pages.get(url, RawResponse(404, {}, b"x"))

    t = [0.0]
    return WebFetcher(
        user_agent=UA, get=get, resolver=lambda h, p: ["93.184.216.34"], clock=lambda: t[0],
        sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: NOW,
    )  # fmt: skip


@pytest.fixture()
def world(engine):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
        create_project(c, slug="b", name="B", now=NOW)
    with engine.connect() as c:
        return engine, open_scope(c, "a", for_write=True), open_scope(c, "b", for_write=True)


def test_a_fetched_page_is_recorded_as_an_excerpt_source_with_its_urls(world):
    engine, a, _ = world
    f = make_fetcher(
        {URL: RawResponse(200, {"content-type": "text/html"}, b"<html>BJP 303</html>")}
    )
    page, res = fetch_into_project(engine, a, f, URL, title="Results", tier=1, now=NOW)
    with engine.connect() as c:
        s = get_source(c, a, res.source_id)
    assert (s.kind, s.retention_mode, s.http_status, s.tier) == ("web", "excerpt", 200, 1)
    assert s.size_bytes == len(page.body) and s.final_url == URL
    with engine.connect() as c:  # no bytes were stored
        assert c.execute(text("SELECT count(*) FROM raw_blobs")).scalar_one() == 0


def test_fetching_again_is_idempotent_and_projects_stay_apart(world):
    engine, a, b = world
    pages = {URL: RawResponse(200, {"content-type": "text/html"}, b"same")}
    first = fetch_into_project(engine, a, make_fetcher(pages), URL, title="R", tier=2, now=NOW)[1]
    again = fetch_into_project(engine, a, make_fetcher(pages), URL, title="R", tier=2, now=NOW)[1]
    other = fetch_into_project(engine, b, make_fetcher(pages), URL, title="R", tier=2, now=NOW)[1]
    assert again.source_id == first.source_id and again.outcome == "unchanged"
    assert other.source_id != first.source_id
    with engine.connect() as c, pytest.raises(Exception, match="not found|no such"):
        get_source(c, a, other.source_id)


def test_a_refused_fetch_writes_nothing(world):
    engine, a, _ = world
    f = make_fetcher(
        {URL: RawResponse(200, {"content-type": "text/html"}, b"x")},
        robots="User-agent: *\nDisallow: /\n",
    )
    with pytest.raises(FetchRefused):
        fetch_into_project(engine, a, f, URL, title="R", tier=2, now=NOW)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM evidence_sources")).scalar_one() == 0


def test_cli_checks_the_url_without_any_request_unless_executed(engine, db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url.render_as_string(hide_password=False))
    runner = CliRunner()
    dry = runner.invoke(app, ["evidence", "fetch", "a", URL, "--title", "T"])
    assert dry.exit_code == 0 and "dry run" in dry.stdout
    bad = runner.invoke(
        app, ["evidence", "fetch", "a", "http://results.example.org/x", "--title", "T"]
    )
    assert bad.exit_code == 1 and "bad_scheme" in bad.output
    placeholder = runner.invoke(app, ["evidence", "fetch", "a", URL, "--title", "T", "--execute"])
    assert (
        placeholder.exit_code == 1 and "user_agent" in placeholder.output
    )  # default UA is the placeholder
