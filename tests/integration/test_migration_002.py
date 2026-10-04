"""Migration 002: competition scoping, daily-snapshot uniqueness, reporting schema."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

T0 = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)  # 01:30 on 2026-10-02 in Asia/Tokyo

SNAP = """INSERT INTO analytics_snapshots
            (competition_id, created_at, as_of, local_date, kind, data_fingerprint, analytics_version)
          VALUES (:c, :t, :t, :d, :k, 'f', '1')"""


def _competition(conn, code, tz="Asia/Tokyo"):
    return conn.execute(
        text("INSERT INTO competitions (code, name, timezone) VALUES (:c, :c, :tz) RETURNING id"),
        {"c": code, "tz": tz},
    ).scalar_one()


def _columns(conn, table, schema="public"):
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": schema, "t": table},
    )
    return {r[0] for r in rows}


# --- upgrade with existing rows --------------------------------------------------------------------


def test_upgrade_backfills_when_there_is_exactly_one_competition(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "001")
    eng = create_engine(empty_db_url)
    with eng.begin() as c:
        _competition(c, "only")
        c.execute(
            text(
                """INSERT INTO analytics_snapshots (created_at, as_of, kind, data_fingerprint, analytics_version)
                   VALUES (:t, :t, 'daily', 'f', '1')"""
            ),
            {"t": T0},
        )
        c.execute(
            text("INSERT INTO ingest_runs (started_at, status) VALUES (:t, 'success')"), {"t": T0}
        )
    command.upgrade(alembic_cfg, "002")
    with eng.connect() as c:
        comp = c.execute(text("SELECT id FROM competitions")).scalar_one()
        snap = c.execute(text("SELECT competition_id, local_date FROM analytics_snapshots")).one()
        run = c.execute(text("SELECT competition_id FROM ingest_runs")).scalar_one()
    eng.dispose()
    assert snap.competition_id == comp and run == comp
    assert str(snap.local_date) == "2026-10-02"  # converted in the competition's timezone, not UTC


def test_upgrade_refuses_to_guess_between_several_competitions(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "001")
    eng = create_engine(empty_db_url)
    with eng.begin() as c:
        _competition(c, "a")
        _competition(c, "b")
        c.execute(
            text("INSERT INTO ingest_runs (started_at, status) VALUES (:t, 'success')"), {"t": T0}
        )
    with pytest.raises(Exception, match="competition_id"):
        command.upgrade(alembic_cfg, "002")
    with eng.connect() as c:  # the failed migration rolled back completely
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "001"
        assert "competition_id" not in _columns(c, "ingest_runs")
    eng.dispose()


def test_upgrade_on_an_empty_database_needs_no_data(alembic_cfg):
    command.upgrade(alembic_cfg, "head")  # fresh installs must not trip over the backfill steps


# --- scoping and daily snapshot rule ---------------------------------------------------------------


def test_runs_and_snapshots_require_a_competition(engine):
    with engine.begin() as c:
        comp = _competition(c, "x")
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("INSERT INTO ingest_runs (started_at, status) VALUES (now(), 'running')"))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO analytics_snapshots (created_at, as_of, local_date, kind,
                       data_fingerprint, analytics_version)
                   VALUES (now(), now(), '2026-10-02', 'daily', 'f', '1')"""
            )
        )
    with engine.begin() as c:  # and works once the competition is given
        c.execute(
            text(
                "INSERT INTO ingest_runs (competition_id, started_at, status) VALUES (:c, now(), 'running')"
            ),
            {"c": comp},
        )


def test_one_daily_snapshot_per_competition_per_local_day(engine):
    with engine.begin() as c:
        a, b = _competition(c, "a"), _competition(c, "b")
        c.execute(text(SNAP), {"c": a, "t": T0, "d": "2026-10-02", "k": "daily"})
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text(SNAP), {"c": a, "t": T0, "d": "2026-10-02", "k": "daily"})
    with engine.begin() as c:
        c.execute(text(SNAP), {"c": a, "t": T0, "d": "2026-10-03", "k": "daily"})  # next day: fine
        c.execute(
            text(SNAP), {"c": b, "t": T0, "d": "2026-10-02", "k": "daily"}
        )  # other competition


def test_change_snapshots_may_repeat_on_the_same_day(engine):
    with engine.begin() as c:
        a = _competition(c, "a")
        for _ in range(3):
            c.execute(text(SNAP), {"c": a, "t": T0, "d": "2026-10-02", "k": "change"})
        assert c.execute(text("SELECT count(*) FROM analytics_snapshots")).scalar_one() == 3


# --- views and reporting schema --------------------------------------------------------------------


def test_medal_facts_views_carry_the_competition(engine):
    with engine.connect() as c:
        assert "competition_id" in _columns(c, "v_medal_facts")
        assert "competition_id" in _columns(c, "medal_facts", "reporting")
        views = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT table_name FROM information_schema.views WHERE table_schema = 'reporting'"
                )
            )
        }
    assert views == {"medal_facts", "events", "snapshots", "snapshot_rows"}


def test_reporting_exposes_no_operational_data(engine):
    forbidden = {"path", "sha256", "payload_json", "error_summary", "run_id", "data_fingerprint"}
    with engine.connect() as c:
        for view in ("medal_facts", "events", "snapshots", "snapshot_rows"):
            assert not (_columns(c, view, "reporting") & forbidden), view


# --- downgrade -------------------------------------------------------------------------------------


def test_downgrade_restores_the_001_shape_and_upgrade_can_repeat(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "001")
    eng = create_engine(empty_db_url)
    with eng.connect() as c:
        assert "competition_id" not in _columns(c, "analytics_snapshots")
        assert "competition_id" not in _columns(c, "ingest_runs")
        assert "competition_id" not in _columns(c, "v_medal_facts")
        assert not c.execute(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'reporting'")
        ).first()
        assert not c.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = 'ux_snapshots_daily'")
        ).first()
    command.upgrade(alembic_cfg, "head")
    with eng.connect() as c:
        assert "competition_id" in _columns(c, "analytics_snapshots")
    eng.dispose()
