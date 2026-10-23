"""Upload ingestion: idempotent, per project, honest about run_id, and bounded."""

from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from sie.evidence import (
    BytesNotRetained,
    IngestBusy,
    ProjectClosed,
    UploadRejected,
    create_project,
    get_source,
    ingest_upload,
    list_sources,
    open_scope,
    read_bytes,
    sanitise_filename,
    upload_url,
)
from sie.evidence import ingest as ingest_module

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
CSV = b"country,gold\nIndia,6\nJapan,8\n"


def count(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


@pytest.fixture()
def project(engine):
    with engine.begin() as c:
        create_project(c, slug="alpha", name="Alpha", now=NOW)
        return open_scope(c, "alpha", for_write=True)


def test_an_upload_lands_in_the_raw_store_and_the_ledger(engine, project):
    r = ingest_upload(
        engine, project, CSV, "Medals 2026.csv", now=NOW, claimed_media_type="text/csv"
    )
    sha = hashlib.sha256(CSV).hexdigest()
    assert (r.sha256, r.outcome, r.created_source) == (sha, "new_version", True)
    with engine.connect() as c:
        doc = c.execute(text("SELECT source, url FROM raw_documents")).one()
        ver = c.execute(
            text(
                "SELECT sha256, storage_backend, size_bytes, content_type, version_no FROM raw_versions"
            )
        ).one()
        fetch = c.execute(text("SELECT run_id, outcome FROM raw_fetches")).one()
        pd = c.execute(
            text("SELECT role, original_filename, claimed_media_type FROM project_documents")
        ).one()
        src = c.execute(
            text("SELECT kind, retention_mode, tier, size_bytes, title FROM evidence_sources")
        ).one()
        blob = c.execute(text("SELECT content, compression FROM raw_blobs")).one()
    assert tuple(doc) == ("upload", upload_url(project.project_id, sha))
    assert tuple(ver) == (sha, "db", len(CSV), "text/csv", 1)  # an upload is always version 1
    assert tuple(fetch) == (None, "new_version")  # run_id is NULL: an upload has no ingest run
    assert tuple(pd) == ("upload", "Medals_2026.csv", "text/csv")
    assert tuple(src) == ("upload", "full", 5, len(CSV), "Medals_2026.csv")
    assert gzip.decompress(bytes(blob[0])) == CSV and blob[1] == "gzip"
    assert read_bytes_via(engine, project, r.source_id) == CSV


def read_bytes_via(engine, scope, source_id):
    with engine.connect() as c:
        return read_bytes(c, scope, source_id)


def test_the_same_upload_twice_is_idempotent(engine, project):
    first = ingest_upload(engine, project, CSV, "a.csv", now=NOW)
    second = ingest_upload(engine, project, CSV, "renamed.csv", now=NOW)
    assert second.source_id == first.source_id and second.outcome == "unchanged"
    assert not second.created_source
    for table, expected in (
        ("raw_documents", 1), ("raw_versions", 1), ("raw_blobs", 1),
        ("project_documents", 1), ("evidence_sources", 1),
        ("raw_fetches", 2),  # every attempt is logged, as for any fetch
    ):  # fmt: skip
        assert count(engine, f"SELECT count(*) FROM {table}") == expected
    with engine.connect() as c:  # the first name is kept; the second is not silently swapped in
        assert list_sources(c, project)[0].original_filename == "a.csv"


def test_different_bytes_are_different_sources(engine, project):
    a = ingest_upload(engine, project, b"one", "a", now=NOW)
    b = ingest_upload(engine, project, b"two", "b", now=NOW)
    assert a.source_id != b.source_id and a.document_id != b.document_id
    with engine.connect() as c:
        assert [s.id for s in list_sources(c, project)] == [a.source_id, b.source_id]


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\data.csv", "data.csv"),
        ("..hidden.csv", "hidden.csv"),
        ("naïve name (1).csv", "na_ve_name__1_.csv"),
        ("", "upload"),
        ("///", "upload"),
        ("a" * 300 + ".csv", ("a" * 100)),
        ("tab\tnew\nline.csv", "tab_new_line.csv"),
    ],
)
def test_filenames_become_inert_display_names(raw, clean):
    assert sanitise_filename(raw) == clean


def test_filename_is_never_part_of_the_stored_identity(engine, project):
    r = ingest_upload(engine, project, CSV, "../../etc/passwd", now=NOW)
    with engine.connect() as c:
        assert "passwd" not in c.execute(text("SELECT url FROM raw_documents")).scalar_one()
        assert get_source(c, project, r.source_id).original_filename == "passwd"


@pytest.mark.parametrize(
    ("content", "code"), [(b"", "empty"), ("text not bytes", "not_bytes"), (b"x" * 11, "too_large")]
)
def test_rejected_uploads_write_nothing(engine, project, content, code):
    with pytest.raises(UploadRejected) as err:
        ingest_upload(engine, project, content, "f", now=NOW, max_bytes=10)
    assert err.value.code == code
    assert count(engine, "SELECT count(*) FROM raw_documents") == 0


def test_the_default_cap_is_25_mb_and_configurable(engine, project, monkeypatch):
    from sie.config import get_settings

    assert get_settings().evidence_max_upload_bytes == 25 * 1024 * 1024
    monkeypatch.setenv("EVIDENCE_MAX_UPLOAD_BYTES", "5")
    with pytest.raises(UploadRejected):
        ingest_upload(engine, project, b"123456", "f", now=NOW)


def test_an_archived_project_takes_no_evidence(engine, project):
    with engine.begin() as c:
        c.execute(text("UPDATE projects SET archived_at = :t"), {"t": NOW})
    with pytest.raises(ProjectClosed):
        ingest_upload(engine, project, CSV, "a.csv", now=NOW)
    assert count(engine, "SELECT count(*) FROM raw_documents") == 0
    with engine.connect() as c:
        with pytest.raises(ProjectClosed):
            open_scope(c, "alpha", for_write=True)
        assert (
            open_scope(c, "alpha").slug == "alpha"
        )  # reading an archived project is still allowed


def test_a_purged_project_cannot_be_opened(engine, project):
    with engine.begin() as c:
        c.execute(text("UPDATE projects SET purged_at = :t"), {"t": NOW})
    with engine.connect() as c, pytest.raises(ProjectClosed):
        open_scope(c, "alpha")


def test_a_scope_cannot_be_forged():
    from sie.evidence import EvidenceError, ProjectScope

    with pytest.raises(EvidenceError):
        ProjectScope(1, "x")


def test_bytes_are_rechecked_against_the_ledger_hash(engine, project):
    r = ingest_upload(engine, project, CSV, "a.csv", now=NOW)
    with engine.begin() as c:  # corrupt the stored bytes as the owner could
        c.execute(text("UPDATE raw_blobs SET content = :c"), {"c": gzip.compress(b"tampered")})
    from sie.pipeline.raw import RawStoreError

    with pytest.raises(RawStoreError, match="no longer match"):
        read_bytes_via(engine, project, r.source_id)


def test_the_decompression_ceiling_applies_when_reading(engine, project, monkeypatch):
    r = ingest_upload(engine, project, b"a" * 5000, "a", now=NOW)
    monkeypatch.setenv("EVIDENCE_MAX_DECOMPRESSED_BYTES", "1000")
    from sie.evidence import EvidenceError

    with pytest.raises(EvidenceError, match="ceiling"):
        read_bytes_via(engine, project, r.source_id)


def test_ingestion_forces_read_committed_even_on_a_stricter_engine(db_url):
    from sqlalchemy import create_engine

    strict = create_engine(db_url, isolation_level="REPEATABLE READ")
    with strict.begin() as c:
        create_project(c, slug="s", name="S", now=NOW)
        scope = open_scope(c, "s", for_write=True)
    assert ingest_upload(strict, scope, CSV, "a", now=NOW).outcome == "new_version"
    strict.dispose()


def test_the_isolation_guard_refuses_a_stricter_transaction(db_url):
    """Called directly inside a REPEATABLE READ transaction, the attempt refuses to run."""
    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    with eng.begin() as c:
        create_project(c, slug="s", name="S", now=NOW)
        scope = open_scope(c, "s", for_write=True)
    with (
        eng.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
        pytest.raises(IngestBusy, match="READ COMMITTED"),
    ):
        ingest_module._one_attempt(conn, scope, CSV, "0" * 64, "a", None, "a", None, NOW, None)
    eng.dispose()


class _Orig(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _collide(times, sqlstate="40001"):
    state = {"left": times}

    def hook(point):
        if point == "stored" and state["left"] > 0:
            state["left"] -= 1
            raise OperationalError("stmt", {}, _Orig(sqlstate))

    return hook, state


def test_a_collision_is_retried_and_leaves_exactly_one_copy(engine, project):
    hook, state = _collide(2)
    r = ingest_upload(engine, project, CSV, "a", now=NOW, hook=hook, sleep=lambda s: None)
    assert state["left"] == 0 and r.outcome == "new_version"
    assert (
        count(engine, "SELECT count(*) FROM raw_versions") == 1
    )  # the failed attempts rolled back
    assert count(engine, "SELECT count(*) FROM raw_fetches") == 1


def test_collisions_beyond_the_bound_raise_busy_and_write_nothing(engine, project):
    hook, _ = _collide(99, "40P01")
    with pytest.raises(IngestBusy):
        ingest_upload(engine, project, CSV, "a", now=NOW, hook=hook, sleep=lambda s: None)
    for table in ("raw_documents", "raw_versions", "raw_blobs", "raw_fetches", "evidence_sources"):
        assert count(engine, f"SELECT count(*) FROM {table}") == 0


def test_other_database_errors_are_not_retried_or_swallowed(engine, project):
    hook, state = _collide(1, "22001")
    with pytest.raises(OperationalError):
        ingest_upload(engine, project, CSV, "a", now=NOW, hook=hook, sleep=lambda s: None)
    assert state["left"] == 0


def test_an_excerpt_only_source_has_no_bytes_to_read(engine, project):
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256, size_bytes,
                       title, requested_url, tier, retrieved_at, created_at)
                   VALUES (:p, 'web', 'excerpt', :h, 3, 't', 'https://e.org/a', 3, :t, :t)"""
            ),
            {"p": project.project_id, "h": "a" * 64, "t": NOW},
        )
        sid = c.execute(text("SELECT id FROM evidence_sources")).scalar_one()
    with engine.connect() as c, pytest.raises(BytesNotRetained):
        read_bytes(c, project, sid)
