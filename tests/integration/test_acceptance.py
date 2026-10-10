"""``sie acceptance`` on a real PostgreSQL. Uses the test database, never the production one."""

from __future__ import annotations

import json
import json as _json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from portal_fake import real_pages
from sie.cli import app
from sie.config import Settings
from sie.ops.acceptance import Expected, acceptance_report
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 10, 3, 0, tzinfo=UTC)
# The two fixture disciplines (ARC 30 rows, SWM 123 rows): 153 placings.
SMALL = Expected(events=0, placings=153, gold=0, silver=0, bronze=0, countries=0)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )


@pytest.fixture()
def loaded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    pages = real_pages()
    capture = _json.dumps(
        {"medals": {k.split("/")[0]: v for k, v in pages.items() if k != "ALL/disc/data"}}
    )
    run_ingest(
        engine,
        SourceInput(
            source=SOURCE, url="portal:test", content=capture.encode(), content_type="application/json",
            extension="json", parse=lambda raw: capture_to_parsed(raw, "asiad-2026"),
        ),
        settings,
        now=NOW - timedelta(hours=1),
    )  # fmt: skip
    return engine


def by_name(report):
    return {c.name: c for c in report.checks}


def test_a_database_with_the_expected_figures_passes_what_it_can(loaded):
    # Counts are read from the data, so build the expectation from it and prove the report agrees.
    with loaded.connect() as c:
        events = c.execute(text("SELECT count(*) FROM events")).scalar_one()
        by_medal = dict(
            c.execute(
                text("SELECT medal, count(*) FROM placings WHERE is_current GROUP BY 1")
            ).all()
        )
        countries = c.execute(
            text("SELECT count(DISTINCT country_id) FROM placings WHERE is_current")
        ).scalar_one()
    exp = Expected(
        events=events, placings=153, gold=by_medal["Gold"], silver=by_medal["Silver"],
        bronze=by_medal["Bronze"], countries=countries,
    )  # fmt: skip
    checks = by_name(acceptance_report(loaded, "asiad-2026", NOW, exp))
    assert checks["current placings"].ok and checks["current placings"].observed == 153
    assert (
        checks["gold placings"].ok and checks["silver placings"].ok and checks["bronze placings"].ok
    )
    assert checks["events in the database"].ok and checks["countries with a medal"].ok
    assert checks["this check ran in a read-only transaction"].ok
    assert checks["last successful 'official' run is recent"].ok
    assert checks["runs still marked running"].ok
    assert checks["unresolved quarantined rows"].ok and checks["open conflicts"].ok


def test_the_default_figures_fail_on_partial_data_and_say_why(loaded):
    report = acceptance_report(loaded, "asiad-2026", NOW)
    checks = by_name(report)
    assert not report.ok
    assert (
        not checks["events in the database"].ok and checks["events in the database"].expected == 469
    )
    assert not checks["current placings"].ok and checks["current placings"].observed == 153
    assert checks["current placings"].expected == 1568
    assert not checks["analytics snapshots taken (run `analyze` first)"].ok


def test_a_stale_source_fails(loaded):
    report = acceptance_report(loaded, "asiad-2026", NOW + timedelta(hours=30), SMALL)
    assert not by_name(report)["last successful 'official' run is recent"].ok


def test_an_empty_database_and_an_unknown_competition_fail_cleanly(engine):
    assert not acceptance_report(engine, "nope", NOW).ok
    empty = acceptance_report(engine, "asiad-2026", NOW)
    assert not empty.ok  # no competition row: reference data was never seeded


def test_an_open_conflict_or_unresolved_quarantine_fails(loaded):
    with loaded.begin() as c:
        c.execute(
            text("UPDATE events SET is_disputed = true WHERE id = (SELECT min(id) FROM events)")
        )
    assert not by_name(acceptance_report(loaded, "asiad-2026", NOW, SMALL))["disputed events"].ok


def test_the_check_writes_nothing(loaded):
    def snapshot():
        with loaded.connect() as c:
            return {
                t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608
                for t in (
                    "events",
                    "placings",
                    "ingest_runs",
                    "raw_versions",
                    "quarantine",
                    "analytics_snapshots",
                )
            }

    before = snapshot()
    acceptance_report(loaded, "asiad-2026", NOW)
    assert snapshot() == before


def test_the_cli_prints_json_and_exits_1_on_failure(loaded, settings, monkeypatch, tmp_path):
    for key, value in {
        "DATABASE_URL": settings.database_url, "DATA_DIR": str(tmp_path), "COMPETITION_ID": "asiad-2026",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    res = CliRunner().invoke(app, ["acceptance"])
    assert res.exit_code == 1
    data = json.loads(res.stdout)
    assert data["ok"] is False and any(not c["ok"] for c in data["checks"])
    res = CliRunner().invoke(app, ["acceptance", "--events", "0", "--placings", "153", "--gold", "0",
                                   "--silver", "0", "--bronze", "0", "--countries", "0"])  # fmt: skip
    assert json.loads(res.stdout)["checks"][0]["name"].startswith("schema migrated")
