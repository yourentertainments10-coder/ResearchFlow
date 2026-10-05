"""Migration 003: raw bytes in the database, per-placing source note, run source."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from sie.db.roles import PIPELINE, READER, apply_roles

T0 = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
READER_PW = "reader pass 1"
PIPELINE_PW = "pipeline pass 2"

NEW_VERSION = """INSERT INTO raw_versions (document_id, version_no, sha256, path, storage_backend, first_fetched_at)
                 VALUES (:d, :n, :h, :p, :b, :t) RETURNING id"""


def _columns(conn, table):
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    return {r[0]: r[1] for r in rows}


def _document(conn, url="u"):
    return conn.execute(
        text(
            "INSERT INTO raw_documents (source, url, first_seen_at) VALUES ('official', :u, :t) RETURNING id"
        ),
        {"u": url, "t": T0},
    ).scalar_one()


def _version(conn, doc, n=1, *, path="p", backend="fs"):
    return conn.execute(
        text(NEW_VERSION), {"d": doc, "n": n, "h": f"h{n}", "p": path, "b": backend, "t": T0}
    ).scalar_one()


# --- upgrade ---------------------------------------------------------------------------------------


def test_upgrade_keeps_existing_raw_versions_as_file_backed(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "002")
    eng = create_engine(empty_db_url)
    with eng.begin() as c:
        doc = _document(c)
        c.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                   VALUES (:d, 1, 'aa', 'raw/a.json', :t)"""
            ),
            {"d": doc, "t": T0},
        )
    command.upgrade(alembic_cfg, "003")
    with eng.connect() as c:
        row = c.execute(text("SELECT storage_backend, path FROM raw_versions")).one()
    assert tuple(row) == ("fs", "raw/a.json")  # nothing existing is relabelled or lost


def test_new_columns_exist_and_are_optional(engine):
    with engine.connect() as c:
        assert _columns(c, "placings")["source_note"] == "YES"
        assert _columns(c, "ingest_runs")["source"] == "YES"
        assert _columns(c, "raw_versions")["path"] == "YES"
        assert _columns(c, "raw_versions")["storage_backend"] == "NO"
        assert set(_columns(c, "raw_blobs")) == {"version_id", "content", "compression"}


# --- where the raw bytes live ----------------------------------------------------------------------


def test_a_db_backed_version_needs_no_path_but_a_file_backed_one_does(engine):
    with engine.begin() as c:
        doc = _document(c)
        _version(c, doc, 1, path=None, backend="db")  # bytes live in raw_blobs: no path
    for backend in ("fs", "s3"):
        with pytest.raises(IntegrityError), engine.begin() as c:
            _version(c, doc, 2, path=None, backend=backend)


def test_storage_backend_is_limited_to_the_documented_values(engine):
    with engine.begin() as c:
        doc = _document(c)
    with pytest.raises(IntegrityError), engine.begin() as c:
        _version(c, doc, 1, path="p", backend="ftp")


def test_a_blob_belongs_to_exactly_one_version_and_only_gzip_is_allowed(engine):
    with engine.begin() as c:
        doc = _document(c)
        v1 = _version(c, doc, 1, path=None, backend="db")
        c.execute(
            text("INSERT INTO raw_blobs (version_id, content) VALUES (:v, :b)"),
            {"v": v1, "b": b"\x1f\x8bpayload"},
        )
    with pytest.raises(IntegrityError), engine.begin() as c:  # a second blob for the same version
        c.execute(
            text("INSERT INTO raw_blobs (version_id, content) VALUES (:v, :b)"),
            {"v": v1, "b": b"x"},
        )
    with pytest.raises(IntegrityError), engine.begin() as c:  # no such version
        c.execute(
            text("INSERT INTO raw_blobs (version_id, content) VALUES (999999, :b)"), {"b": b"x"}
        )
    with engine.begin() as c:
        v2 = _version(c, doc, 2, path=None, backend="db")
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text("INSERT INTO raw_blobs (version_id, content, compression) VALUES (:v, :b, 'zip')"),
            {"v": v2, "b": b"x"},
        )
    with engine.connect() as c:  # the stored bytes come back exactly as written
        stored = c.execute(
            text("SELECT content, compression FROM raw_blobs WHERE version_id = :v"), {"v": v1}
        ).one()
    assert (bytes(stored[0]), stored[1]) == (b"\x1f\x8bpayload", "gzip")


# --- least privilege -------------------------------------------------------------------------------


@pytest.fixture()
def with_roles(engine, db_url):
    with engine.begin() as c:
        doc = _document(c)
        v = _version(c, doc, 1, path=None, backend="db")
        apply_roles(c, reader_password=READER_PW, pipeline_password=PIPELINE_PW)
    pipeline = create_engine(db_url.set(username=PIPELINE, password=PIPELINE_PW))
    reader = create_engine(db_url.set(username=READER, password=READER_PW))
    yield {"pipeline": pipeline, "reader": reader, "version": v}
    pipeline.dispose()
    reader.dispose()


def test_pipeline_may_add_blobs_but_not_change_or_delete_them(with_roles):
    pipeline, v = with_roles["pipeline"], with_roles["version"]
    with pipeline.begin() as c:
        c.execute(
            text("INSERT INTO raw_blobs (version_id, content) VALUES (:v, :b)"), {"v": v, "b": b"x"}
        )
    for statement in ("UPDATE raw_blobs SET content = content", "DELETE FROM raw_blobs"):
        with pytest.raises(ProgrammingError, match="permission denied"), pipeline.begin() as c:
            c.execute(text(statement))


def test_reader_cannot_read_raw_blobs(with_roles):
    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        with_roles["reader"].connect() as c,
    ):
        c.execute(text("SELECT * FROM public.raw_blobs"))


def test_pipeline_may_set_the_source_note_and_run_source(with_roles):
    with with_roles["pipeline"].begin() as c:
        c.execute(text("UPDATE placings SET source_note = source_note"))
        c.execute(text("UPDATE ingest_runs SET source = source"))


# --- downgrade -------------------------------------------------------------------------------------


def test_downgrade_restores_the_002_shape_and_upgrade_can_repeat(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "head")
    eng = create_engine(empty_db_url)
    with eng.begin() as c:
        doc = _document(c)
        _version(c, doc, 1, path=None, backend="db")  # a db-backed row without a path
    command.downgrade(alembic_cfg, "002")
    with eng.connect() as c:
        assert "storage_backend" not in _columns(c, "raw_versions")
        assert _columns(c, "raw_versions")["path"] == "NO"
        assert "source_note" not in _columns(c, "placings")
        assert "source" not in _columns(c, "ingest_runs")
        assert not _columns(c, "raw_blobs")
        assert c.execute(text("SELECT path FROM raw_versions")).scalar_one() == "unknown"
    command.upgrade(alembic_cfg, "head")
    with eng.connect() as c:
        assert "raw_blobs" in {
            r[0]
            for r in c.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        }
