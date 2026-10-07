"""The portal fetcher inside the scheduler and CLI, on a real PostgreSQL, with a fake portal."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import sie.sources.bornan.fetch as fetch_mod
from portal_fake import UA, Portal, real_pages
from sie.cli import app
from sie.config import Settings
from sie.pipeline.runner import SourceInput
from sie.pipeline.scheduler import ScheduleStatus, SourceTask, run_scheduled
from sie.reference import seed_reference
from sie.sources.bornan.fetch import PortalClient, fetch_capture
from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
        http_user_agent=UA,
        portal_fetch_enabled=True,
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def task(portal) -> SourceTask:
    def fetch() -> SourceInput:
        content = fetch_capture(PortalClient(UA, get=portal, sleep=lambda s: None))
        return SourceInput(
            source=SOURCE,
            url="portal:AG2026/medals",
            content=content,
            content_type="application/json",
            extension="json",
            parse=lambda raw: capture_to_parsed(raw, "asiad-2026"),
        )

    return SourceTask(source=SOURCE, url="portal:AG2026/medals", fetch=fetch)


def count(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()


def test_a_scheduled_portal_refresh_loads_then_is_unchanged(seeded, settings):
    first = run_scheduled(seeded, settings, task(Portal(real_pages())), sleep=lambda s: None)
    assert first.status == ScheduleStatus.SUCCEEDED and first.fetch_attempts == 1
    placings = count(seeded, "SELECT count(*) FROM placings WHERE is_current")
    assert placings == 30 + 123

    second = run_scheduled(seeded, settings, task(Portal(real_pages())), sleep=lambda s: None)
    assert second.status == ScheduleStatus.SUCCEEDED
    assert second.final.raw_outcome == "unchanged"
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_current") == placings
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


def test_portal_down_is_three_recorded_fetch_failures_and_no_data_change(seeded, settings):
    pages = real_pages()
    pages["ARC/medals/discipline"] = 503
    portal = Portal(pages)
    result = run_scheduled(seeded, settings, task(portal), sleep=lambda s: None)
    assert result.status == ScheduleStatus.FAILED and result.fetch_attempts == 3
    assert (
        count(
            seeded, "SELECT count(*) FROM ingest_runs WHERE error_summary LIKE '[fetch_failure]%'"
        )
        == 3
    )
    assert count(seeded, "SELECT count(*) FROM placings") == 0
    assert (
        portal.calls.count("ARC/medals/discipline") == 3
    )  # attempt limit respected, nothing hammered
    assert "SWM/medals/discipline" not in portal.calls


def test_a_failed_refresh_keeps_the_last_good_data(seeded, settings):
    run_scheduled(seeded, settings, task(Portal(real_pages())), sleep=lambda s: None)
    before = count(seeded, "SELECT count(*) FROM placings WHERE is_current")
    pages = real_pages()
    pages["SWM/medals/discipline"] = 500
    run_scheduled(seeded, settings, task(Portal(pages)), sleep=lambda s: None)
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_current") == before


@pytest.fixture()
def cli_env(monkeypatch, settings, tmp_path):
    for key, value in {
        "DATABASE_URL": settings.database_url,
        "DATA_DIR": str(tmp_path),
        "COMPETITION_ID": "asiad-2026",
        "HTTP_USER_AGENT": UA,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PORTAL_FETCH_ENABLED", raising=False)


def test_the_cli_refuses_when_the_kill_switch_is_off_and_records_nothing(seeded, cli_env):
    res = CliRunner().invoke(app, ["scheduled-run", "portal"])
    assert res.exit_code == 2 and "PORTAL_FETCH_ENABLED" in res.output
    assert count(seeded, "SELECT count(*) FROM ingest_runs") == 0


def test_the_cli_refuses_the_placeholder_user_agent(seeded, cli_env, monkeypatch):
    monkeypatch.setenv("PORTAL_FETCH_ENABLED", "true")
    monkeypatch.setenv(
        "HTTP_USER_AGENT", "SIE-research/0.1 (set HTTP_USER_AGENT with a contact address)"
    )
    res = CliRunner().invoke(app, ["scheduled-run", "portal"])
    assert res.exit_code == 2 and "HTTP_USER_AGENT" in res.output
    assert count(seeded, "SELECT count(*) FROM ingest_runs") == 0


def test_the_cli_portal_run_end_to_end(seeded, cli_env, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_FETCH_ENABLED", "true")
    portal = Portal(real_pages())
    monkeypatch.setattr(fetch_mod, "urllib_get", portal)
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = CliRunner().invoke(app, ["scheduled-run", "portal"])
    assert res.exit_code == 0, res.output
    assert "official: succeeded" in res.output
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_current") == 153

    out = tmp_path / "cap.json"
    res = CliRunner().invoke(app, ["fetch-portal", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert sorted(json.loads(out.read_text())["medals"]) == ["ARC", "SWM"]
