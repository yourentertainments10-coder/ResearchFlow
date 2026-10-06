"""Migration 004: per-placing result date and source country label (ADR-025, ADR-026)."""

from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text


def _columns(conn, table, schema="public"):
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
        ),
        {"s": schema, "t": table},
    )
    return {r[0]: r[1] for r in rows}


def test_new_placing_columns_are_optional(engine):
    with engine.connect() as c:
        cols = _columns(c, "placings")
    assert cols["result_date"] == "YES" and cols["source_country"] == "YES"


def test_views_gain_the_columns_at_the_end(engine):
    with engine.connect() as c:
        public = list(_columns(c, "v_medal_facts"))
        reporting = list(_columns(c, "medal_facts", "reporting"))
    assert public[-2:] == ["result_date", "source_country"]
    assert reporting[-2:] == ["result_date", "source_country"]
    assert "competition_id" in public[:-2]  # nothing was reordered or dropped


def test_reporting_still_hides_operational_columns(engine):
    with engine.connect() as c:
        reporting = set(_columns(c, "medal_facts", "reporting"))
    assert not reporting & {"raw_version_id", "valid_from", "valid_to", "source", "source_note"}


def test_downgrade_restores_the_003_shape_and_upgrade_can_repeat(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    eng = create_engine(empty_db_url)
    command.downgrade(alembic_cfg, "003")
    with eng.connect() as c:
        assert "result_date" not in _columns(c, "placings")
        assert "source_country" not in _columns(c, "placings")
        assert "result_date" not in _columns(c, "v_medal_facts")
        assert "result_date" not in _columns(c, "medal_facts", "reporting")
        assert "country_code" in _columns(c, "medal_facts", "reporting")
    command.upgrade(alembic_cfg, "head")
    with eng.connect() as c:
        assert "result_date" in _columns(c, "medal_facts", "reporting")
