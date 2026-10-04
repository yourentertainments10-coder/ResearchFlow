"""Least-privilege roles (docs/DATABASE.md section 13), tested by actually connecting as each role."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from sie.db.placings import apply_placing
from sie.db.roles import PIPELINE, READER, apply_roles

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
READER_PW = "r%ead'er pass 1"  # quote, percent sign and space must survive intact
PIPELINE_PW = "pipe;line\\pass 2"


@pytest.fixture()
def seeded(engine):
    """A database with one current placing, and both roles created."""
    with engine.begin() as c:
        comp = c.execute(
            text(
                "INSERT INTO competitions (code, name, timezone) VALUES ('t','T','Asia/Tokyo') RETURNING id"
            )
        ).scalar_one()
        ind = c.execute(
            text("INSERT INTO countries (code, name) VALUES ('IND','India') RETURNING id")
        ).scalar_one()
        sport = c.execute(
            text("INSERT INTO sports (name) VALUES ('Archery') RETURNING id")
        ).scalar_one()
        disc = c.execute(
            text("INSERT INTO disciplines (sport_id, name) VALUES (:s, 'Archery') RETURNING id"),
            {"s": sport},
        ).scalar_one()
        event = c.execute(
            text(
                """INSERT INTO events (competition_id, discipline_id, name, name_raw, gender, participation, status)
                   VALUES (:c, :d, 'Compound Team', 'x', 'Women', 'Team', 'completed') RETURNING id"""
            ),
            {"c": comp, "d": disc},
        ).scalar_one()
        doc = c.execute(
            text(
                "INSERT INTO raw_documents (source, url, first_seen_at) VALUES ('official','u',:t) RETURNING id"
            ),
            {"t": T0},
        ).scalar_one()
        rv = c.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                   VALUES (:d, 1, 'aa', 'p', :t) RETURNING id"""
            ),
            {"d": doc, "t": T0},
        ).scalar_one()
        apply_placing(
            c, event_id=event, medal="Gold", slot=1, country_id=ind, entrant_id=None,
            raw_version_id=rv, source="official", now=T0,
        )  # fmt: skip
        apply_roles(c, reader_password=READER_PW, pipeline_password=PIPELINE_PW)
    return {"event": event, "country": ind, "rv": rv, "comp": comp}


def _as(db_url, role, password):
    return create_engine(db_url.set(username=role, password=password))


@pytest.fixture()
def reader(db_url, seeded):
    eng = _as(db_url, READER, READER_PW)
    yield eng
    eng.dispose()


@pytest.fixture()
def pipeline(db_url, seeded):
    eng = _as(db_url, PIPELINE, PIPELINE_PW)
    yield eng
    eng.dispose()


# --- reader ----------------------------------------------------------------------------------------


def test_reader_can_read_the_reporting_views(reader):
    with reader.connect() as c:
        assert c.execute(text("SELECT count(*) FROM reporting.medal_facts")).scalar_one() == 1
        row = c.execute(
            text("SELECT country_code, sport, gender, medal FROM reporting.medal_facts")
        ).one()
        assert tuple(row) == ("IND", "Archery", "Women", "Gold")
        assert c.execute(text("SELECT count(*) FROM reporting.events")).scalar_one() == 1


@pytest.mark.parametrize(
    "table",
    ["placings", "raw_versions", "quarantine", "ingest_runs", "v_medal_facts", "alembic_version"],
)
def test_reader_cannot_read_operational_tables(reader, table):
    with pytest.raises(ProgrammingError, match="permission denied"), reader.connect() as c:
        c.execute(text(f"SELECT * FROM public.{table}"))


def test_reader_cannot_write_anything(reader):
    # reporting.snapshots selects from one table, so Postgres treats it as auto-updatable: this is the
    # case where only the missing privilege stands between the reader and the data.
    for statement in (
        "INSERT INTO countries (code, name) VALUES ('XXX','x')",
        "UPDATE reporting.snapshots SET kind = 'change'",
        "DELETE FROM reporting.snapshots",
        # Not the identity column: writing that one is rejected by the rewriter before privileges are
        # even looked at, so it would prove nothing about the grants.
        "INSERT INTO reporting.snapshots (kind) VALUES ('change')",
        "CREATE TABLE public.sneaky (id int)",
        "CREATE TABLE reporting.sneaky (id int)",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"), reader.connect() as c:
            c.execute(text(statement))
    # The grants themselves, asked of the catalogue rather than inferred from error messages.
    with reader.connect() as c:
        for view in ("medal_facts", "events", "snapshots", "snapshot_rows"):
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not c.execute(
                    text("SELECT has_table_privilege(current_user, :t, :p)"),
                    {"t": f"reporting.{view}", "p": priv},
                ).scalar(), f"reader holds {priv} on reporting.{view}"
            assert c.execute(
                text("SELECT has_table_privilege(current_user, :t, 'SELECT')"),
                {"t": f"reporting.{view}"},
            ).scalar(), f"reader cannot SELECT reporting.{view}"
    # Joined views are not updatable at all, which is a second, independent barrier.
    for statement in (
        "UPDATE reporting.events SET status = 'scheduled'",
        "DELETE FROM reporting.medal_facts",
    ):
        with pytest.raises((ProgrammingError, OperationalError)), reader.connect() as c:
            c.execute(text(statement))


# --- pipeline --------------------------------------------------------------------------------------


def test_pipeline_can_apply_placings_including_a_reallocation(pipeline, seeded):
    kor = None
    with pipeline.begin() as c:
        kor = c.execute(
            text("INSERT INTO countries (code, name) VALUES ('KOR','Korea') RETURNING id")
        ).scalar_one()
        change = apply_placing(
            c, event_id=seeded["event"], medal="Gold", slot=1, country_id=kor, entrant_id=None,
            raw_version_id=seeded["rv"], source="official", now=T0.replace(hour=12),
        )  # fmt: skip
    assert change == "reallocated"


def test_pipeline_can_never_delete(pipeline):
    for table in ("placings", "events", "raw_versions", "placing_history", "countries"):
        with pytest.raises(ProgrammingError, match="permission denied"), pipeline.begin() as c:
            c.execute(text(f"DELETE FROM {table}"))


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("placing_history", "reason"),
        ("raw_versions", "content_type"),
        ("raw_fetches", "error"),
        ("source_observations", "entrant_name"),
        ("analytics_snapshots", "note"),
        ("snapshot_rows", "gold"),
        ("snapshot_changes", "detail"),
    ],
)
def test_pipeline_cannot_rewrite_evidence_tables(pipeline, table, column):
    with pytest.raises(ProgrammingError, match="permission denied"), pipeline.begin() as c:
        c.execute(text(f"UPDATE {table} SET {column} = {column}"))


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("placings", "is_tie"),
        ("raw_documents", "source"),
        ("events", "status"),
        ("quarantine", "resolved"),
    ],
)
def test_pipeline_can_update_the_mutable_tables(pipeline, table, column):
    with pipeline.begin() as c:  # allowed, even though no row matches: the privilege check passes
        c.execute(text(f"UPDATE {table} SET {column} = {column}"))


def test_pipeline_cannot_touch_migration_bookkeeping(pipeline):
    with pytest.raises(ProgrammingError, match="permission denied"), pipeline.connect() as c:
        c.execute(text("SELECT * FROM alembic_version"))


def test_pipeline_can_read_reporting_but_not_create_tables(pipeline):
    with pipeline.connect() as c:
        assert c.execute(text("SELECT count(*) FROM reporting.medal_facts")).scalar_one() == 1
    with pytest.raises(ProgrammingError, match="permission denied"), pipeline.connect() as c:
        c.execute(text("CREATE TABLE public.sneaky (id int)"))


# --- applying twice, new tables --------------------------------------------------------------------


def test_apply_roles_is_idempotent_and_updates_the_password(engine, db_url, seeded):
    with engine.begin() as c:
        apply_roles(c, reader_password="new reader pw 3", pipeline_password=PIPELINE_PW)
    eng = _as(db_url, READER, "new reader pw 3")
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM reporting.events")).scalar_one() == 1
    eng.dispose()


def test_tables_created_later_by_the_owner_get_the_same_rules(engine, db_url, seeded):
    with engine.begin() as c:
        c.execute(text("CREATE TABLE public.later (id int)"))
        c.execute(text("CREATE VIEW reporting.later_view AS SELECT 1 AS one"))
        c.execute(text("INSERT INTO public.later VALUES (1)"))
    pipe = _as(db_url, PIPELINE, PIPELINE_PW)
    read = _as(db_url, READER, READER_PW)
    with pipe.begin() as c:
        c.execute(text("INSERT INTO public.later VALUES (2)"))
    with pytest.raises(ProgrammingError, match="permission denied"), pipe.begin() as c:
        c.execute(text("DELETE FROM public.later"))
    with read.connect() as c:
        assert c.execute(text("SELECT one FROM reporting.later_view")).scalar_one() == 1
    with pytest.raises(ProgrammingError, match="permission denied"), read.connect() as c:
        c.execute(text("SELECT * FROM public.later"))
    pipe.dispose()
    read.dispose()


def test_both_passwords_are_required(engine):
    with pytest.raises(ValueError), engine.begin() as c:
        apply_roles(c, reader_password="", pipeline_password="x")
