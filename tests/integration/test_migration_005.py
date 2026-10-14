"""Migration 005: the evidence ledger is additive, lock-safe and enforces its own rules in the database."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from sie.db.roles import APPEND_ONLY, PIPELINE, READER, apply_roles

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
NEW_TABLES = {
    "projects",
    "purge_tombstones",
    "project_documents",
    "evidence_sources",
    "claims",
    "claim_reviews",
    "claim_conflicts",
}
SHA = "a" * 64
SHA2 = "b" * 64


def _tables(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
    }


def _schema_fingerprint(conn, tables: set[str]) -> dict:
    """Columns, constraints and indexes of the given tables, and the definition of every view."""
    out: dict = {}
    for t in sorted(tables):
        out[t] = {
            "columns": [
                tuple(r)
                for r in conn.execute(
                    text(
                        """SELECT column_name, data_type, is_nullable, column_default
                           FROM information_schema.columns WHERE table_schema='public' AND table_name=:t
                           ORDER BY ordinal_position"""
                    ),
                    {"t": t},
                )
            ],
            "constraints": sorted(
                r[0]
                for r in conn.execute(
                    text(
                        """SELECT conname || ':' || pg_get_constraintdef(oid) FROM pg_constraint
                           WHERE conrelid = ('public.' || :t)::regclass"""
                    ),
                    {"t": t},
                )
            ),
            "indexes": sorted(
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t"
                    ),
                    {"t": t},
                )
            ),
        }
    for v in conn.execute(
        text(
            "SELECT schemaname || '.' || viewname, definition FROM pg_views WHERE schemaname IN ('public','reporting')"
        )
    ):
        out["view:" + v[0]] = v[1]
    return out


def test_005_changes_no_existing_table_column_constraint_index_or_view(alembic_cfg, empty_db_url):
    command.upgrade(alembic_cfg, "004")
    eng = create_engine(empty_db_url)
    with eng.connect() as c:
        before_tables = _tables(c)
        before = _schema_fingerprint(c, before_tables)
    command.upgrade(alembic_cfg, "005")
    with eng.connect() as c:
        after = _schema_fingerprint(c, before_tables)
        assert _tables(c) - before_tables == NEW_TABLES
    new_views = set(after) - set(before)
    assert new_views == {"view:public.evidence_claims_current"}  # the only addition besides tables
    assert {k: v for k, v in after.items() if k in before} == before  # nothing old changed shape
    eng.dispose()


def test_005_sql_sets_local_timeouts_first():
    from importlib.resources import files

    sql = (files("sie.db") / "sql" / "005_evidence_ledger.sql").read_text(encoding="utf-8")
    statements = [
        line for line in sql.splitlines() if line.strip() and not line.lstrip().startswith("--")
    ]
    assert statements[0].startswith("SET LOCAL lock_timeout")
    assert statements[1].startswith("SET LOCAL statement_timeout")


def test_only_raw_documents_and_raw_versions_are_referenced_outside_the_new_tables(engine):
    """The lock footprint: foreign keys into old tables are what take SHARE ROW EXCLUSIVE locks."""
    with engine.connect() as c:
        rows = c.execute(
            text(
                """SELECT DISTINCT cf.relname FROM pg_constraint k
                   JOIN pg_class ck ON ck.oid = k.conrelid
                   JOIN pg_class cf ON cf.oid = k.confrelid
                   WHERE k.contype='f' AND ck.relname = ANY(:new)"""
            ),
            {"new": sorted(NEW_TABLES)},
        ).all()
    assert {r[0] for r in rows} - NEW_TABLES == {"raw_documents", "raw_versions"}


def test_lock_timeout_makes_a_blocked_migration_fail_fast_and_leave_nothing(
    alembic_cfg, empty_db_url
):
    command.upgrade(alembic_cfg, "004")
    eng = create_engine(empty_db_url)
    blocker = eng.connect()
    tx = blocker.begin()
    blocker.execute(text("LOCK TABLE raw_versions IN ROW EXCLUSIVE MODE"))  # what an ingest holds
    started = time.monotonic()
    with pytest.raises(OperationalError) as err:
        command.upgrade(alembic_cfg, "005")
    assert time.monotonic() - started < 30
    assert err.value.orig.sqlstate == "55P03"  # lock_timeout, not a hang
    tx.rollback()
    blocker.close()
    with eng.connect() as c:
        assert not (NEW_TABLES & _tables(c))  # atomic: nothing half-created
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "004"
    command.upgrade(alembic_cfg, "005")  # and it succeeds once the writer is gone
    with eng.connect() as c:
        assert _tables(c) >= NEW_TABLES
    eng.dispose()


def test_a_reader_is_not_blocked_while_005_runs(engine):
    """SHARE ROW EXCLUSIVE lets plain reads through (checked by holding the same lock open)."""
    with engine.connect() as holder:
        tx = holder.begin()
        holder.execute(text("LOCK TABLE raw_versions IN SHARE ROW EXCLUSIVE MODE"))
        other = create_engine(engine.url)
        with other.connect() as c:
            c.execute(text("SET lock_timeout = '1s'"))
            assert c.execute(text("SELECT count(*) FROM raw_versions")).scalar_one() == 0
        other.dispose()
        tx.rollback()


def test_downgrade_removes_the_ledger_with_data_in_it_and_upgrade_can_repeat(
    alembic_cfg, empty_db_url
):
    command.upgrade(alembic_cfg, "005")
    eng = create_engine(empty_db_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO projects (slug, name, created_at) VALUES ('p1','P1',:t)"), {"t": T0}
        )
        c.execute(
            text("INSERT INTO raw_documents (source, url, first_seen_at) VALUES ('upload','x',:t)"),
            {"t": T0},
        )
    command.downgrade(alembic_cfg, "004")
    with eng.connect() as c:
        assert not (NEW_TABLES & _tables(c))
        assert (
            c.execute(text("SELECT count(*) FROM raw_documents")).scalar_one() == 1
        )  # raw rows stay
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "004"
    command.upgrade(alembic_cfg, "005")
    with eng.connect() as c:
        assert _tables(c) >= NEW_TABLES
    eng.dispose()


# ---- constraints --------------------------------------------------------------------------------


def _project(c, slug="p1"):
    return c.execute(
        text("INSERT INTO projects (slug, name, created_at) VALUES (:s,:s,:t) RETURNING id"),
        {"s": slug, "t": T0},
    ).scalar_one()


def _full_source(c, project_id, *, sha=SHA, doc_url="u"):
    doc = c.execute(
        text(
            "INSERT INTO raw_documents (source, url, first_seen_at) VALUES ('upload',:u,:t) RETURNING id"
        ),
        {"u": doc_url, "t": T0},
    ).scalar_one()
    rv = c.execute(
        text(
            """INSERT INTO raw_versions (document_id, version_no, sha256, storage_backend, first_fetched_at)
               VALUES (:d, 1, :h, 'db', :t) RETURNING id"""
        ),
        {"d": doc, "h": sha, "t": T0},
    ).scalar_one()
    return c.execute(
        text(
            """INSERT INTO evidence_sources (project_id, kind, retention_mode, raw_version_id, content_sha256,
                                             size_bytes, title, tier, retrieved_at, created_at)
               VALUES (:p, 'upload', 'full', :rv, :h, 3, 't', 5, :t, :t) RETURNING id"""
        ),
        {"p": project_id, "rv": rv, "h": sha, "t": T0},
    ).scalar_one()


def _claim(c, project_id, source_id, claim_hash="c" * 64):
    return c.execute(
        text(
            """INSERT INTO claims (project_id, source_id, subject, metric, value_text, locator_kind, locator,
                                   excerpt, excerpt_sha256, extraction_method, extractor_version, claim_hash,
                                   created_at)
               VALUES (:p, :s, 'sub', 'met', '1', 'none', '{}', 'x', :h, 'manual', 'v1', :ch, :t) RETURNING id"""
        ),
        {"p": project_id, "s": source_id, "h": SHA, "ch": claim_hash, "t": T0},
    ).scalar_one()


def test_a_claim_cannot_point_at_another_projects_source(engine):
    with engine.begin() as c:
        a, b = _project(c, "a"), _project(c, "b")
        source_b = _full_source(c, b)
    with pytest.raises(IntegrityError), engine.begin() as c:
        _claim(c, a, source_b)


def test_a_review_and_a_conflict_cannot_cross_projects(engine):
    with engine.begin() as c:
        a, b = _project(c, "a"), _project(c, "b")
        sa, sb = _full_source(c, a, doc_url="ua"), _full_source(c, b, sha=SHA2, doc_url="ub")
        ca1, ca2 = _claim(c, a, sa, "1" * 64), _claim(c, a, sa, "2" * 64)
        cb = _claim(c, b, sb, "3" * 64)
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, at)
                   VALUES (:p, :c, 'unverified', 'r', 'system:x', :t)"""
            ),
            {"p": a, "c": cb, "t": T0},
        )
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO claim_conflicts (project_id, claim_a_id, claim_b_id, kind, status, created_at)
                   VALUES (:p, :x, :y, 'value_differs', 'open', :t)"""
            ),
            {"p": a, "x": ca1, "y": cb, "t": T0},
        )
    with engine.begin() as c:  # same project works, and the pair must be ordered
        c.execute(
            text(
                """INSERT INTO claim_conflicts (project_id, claim_a_id, claim_b_id, kind, status, created_at)
                   VALUES (:p, :x, :y, 'value_differs', 'open', :t)"""
            ),
            {"p": a, "x": ca1, "y": ca2, "t": T0},
        )
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO claim_conflicts (project_id, claim_a_id, claim_b_id, kind, status, created_at)
                   VALUES (:p, :x, :y, 'scope_differs', 'open', :t)"""
            ),
            {"p": a, "x": ca2, "y": ca1, "t": T0},
        )


_SOURCE_COLUMNS = (
    "project_id, kind, retention_mode, raw_version_id, content_sha256, size_bytes, title, requested_url, "
    "tier, terms_ref, retain_until, pruned_at, retrieved_at, created_at"
)


def _insert_source(c, project_id, **over):
    """A valid excerpt-mode web source, with named fields replaced to break exactly one rule."""
    row = {
        "project_id": project_id,
        "kind": "web",
        "retention_mode": "excerpt",
        "raw_version_id": None,
        "content_sha256": SHA2,
        "size_bytes": 10,
        "title": "t",
        "requested_url": "https://e.org/x",
        "tier": 3,
        "terms_ref": None,
        "retain_until": None,
        "pruned_at": None,
        "retrieved_at": T0,
        "created_at": T0,
    }
    row.update(over)
    names = _SOURCE_COLUMNS.split(", ")
    c.execute(
        text(
            f"INSERT INTO evidence_sources ({_SOURCE_COLUMNS}) VALUES ({', '.join(':' + n for n in names)})"
        ),
        row,
    )


def test_the_baseline_excerpt_source_is_valid_so_each_case_below_fails_for_its_own_reason(engine):
    with engine.begin() as c:
        p = _project(c)
        _insert_source(c, p)


@pytest.mark.parametrize(
    "over",
    [
        {"retention_mode": "full"},  # full needs bytes
        {"retention_mode": "full", "terms_ref": "policy", "retain_until": T0},  # still no bytes
        {"retention_mode": "excerpt", "raw_version_id": "RV"},  # bytes kept although excerpt-only
        {"kind": "upload", "retention_mode": "excerpt"},  # an upload is always kept in full
        {
            "kind": "web",
            "retention_mode": "full",
            "raw_version_id": "RV",
            "retain_until": T0,
        },  # no policy
        {
            "kind": "web",
            "retention_mode": "full",
            "raw_version_id": "RV",
            "terms_ref": "p",
        },  # no end date
        {"pruned_at": T0},  # pruned rows must be metadata-only
        {"requested_url": None},  # a web source needs its URL
        {"tier": 6},
        {"tier": 0},
        {"content_sha256": "xyz"},
        {"size_bytes": -1},
        {"title": ""},
    ],
    ids=lambda o: ",".join(f"{k}={'RV' if v == 'RV' else v}" for k, v in o.items()),
)
def test_evidence_source_rules_are_enforced_by_the_database(engine, over):
    with engine.begin() as c:
        p = _project(c)
        _full_source(c, p)
        rv = c.execute(text("SELECT id FROM raw_versions")).scalar_one()
    over = {k: (rv if v == "RV" else v) for k, v in over.items()}
    with pytest.raises(IntegrityError) as err, engine.begin() as c:
        _insert_source(c, p, **over)
    assert type(err.value.orig).__name__ == "CheckViolation"  # a rule, not an incidental collision


def test_a_valid_excerpt_only_web_source_is_accepted_and_deduplicated(engine):
    with engine.begin() as c:
        p = _project(c)
        c.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256, size_bytes, title,
                       requested_url, tier, retrieved_at, created_at)
                   VALUES (:p, 'web', 'excerpt', :h, 10, 't', 'https://e.org/a', 3, :t, :t)"""
            ),
            {"p": p, "h": SHA, "t": T0},
        )
    with pytest.raises(IntegrityError), engine.begin() as c:  # same project, URL and content
        c.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256, size_bytes, title,
                       requested_url, tier, retrieved_at, created_at)
                   VALUES (:p, 'web', 'excerpt', :h, 10, 't', 'https://e.org/a', 3, :t, :t)"""
            ),
            {"p": p, "h": SHA, "t": T0},
        )


def test_status_actors_cannot_be_forged(engine):
    with engine.begin() as c:
        p = _project(c)
        s = _full_source(c, p)
        cl = _claim(c, p, s)

    def review(status, actor):
        with engine.begin() as c:
            c.execute(
                text(
                    """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, at)
                       VALUES (:p, :c, :s, 'r', :a, :t)"""
                ),
                {"p": p, "c": cl, "s": status, "a": actor, "t": T0},
            )

    review("source_located", "system:verifier")
    review("human_confirmed", "Asha Rao")
    with pytest.raises(IntegrityError):
        review("source_located", "Asha Rao")  # a person cannot claim a machine check
    with pytest.raises(IntegrityError):
        review("human_confirmed", "system:verifier")  # a machine cannot claim a human one
    with pytest.raises(IntegrityError):
        review("verified", "system:verifier")  # no such status exists


def test_current_status_is_the_latest_review_and_defaults_to_unverified(engine):
    with engine.begin() as c:
        p = _project(c)
        s = _full_source(c, p)
        c1, c2 = _claim(c, p, s, "1" * 64), _claim(c, p, s, "2" * 64)
        for status, actor in (("source_located", "system:v"), ("disputed", "Asha Rao")):
            c.execute(
                text(
                    """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, at)
                       VALUES (:p, :c, :s, 'r', :a, :t)"""
                ),
                {"p": p, "c": c1, "s": status, "a": actor, "t": T0},
            )
    with engine.connect() as c:
        got = dict(c.execute(text("SELECT claim_id, status FROM evidence_claims_current")).all())
    assert got == {c1: "disputed", c2: "unverified"}


def test_claim_hash_makes_re_extraction_idempotent_at_the_database(engine):
    with engine.begin() as c:
        p = _project(c)
        s = _full_source(c, p)
        _claim(c, p, s)
    with pytest.raises(IntegrityError), engine.begin() as c:
        _claim(c, p, s)  # same project, source and claim_hash


# ---- roles --------------------------------------------------------------------------------------


def test_the_ledger_tables_are_append_only_for_the_pipeline_and_invisible_to_the_reader(engine):
    assert {
        "evidence_sources",
        "claims",
        "claim_reviews",
        "project_documents",
        "purge_tombstones",
    } <= set(APPEND_ONLY)
    with engine.begin() as c:
        apply_roles(c, reader_password="reader pass 1", pipeline_password="pipeline pass 2")
        p = _project(c)
        s = _full_source(c, p)
        _claim(c, p, s)
    url = engine.url
    pipe = create_engine(url.set(username=PIPELINE, password="pipeline pass 2"))
    reader = create_engine(url.set(username=READER, password="reader pass 1"))
    with pipe.begin() as c:
        assert c.execute(text("SELECT count(*) FROM claims")).scalar_one() == 1
        c.execute(
            text(
                """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, at)
                   SELECT project_id, id, 'unverified', 'r', 'system:x', now() FROM claims"""
            )
        )
        c.execute(text("UPDATE projects SET name = 'renamed'"))  # mutable bookkeeping
    for statement in (
        "UPDATE claims SET subject = 'x'",
        "UPDATE evidence_sources SET title = 'x'",
        "UPDATE claim_reviews SET reason = 'x'",
        "DELETE FROM claims",
    ):
        with pytest.raises(ProgrammingError), pipe.begin() as c:
            c.execute(text(statement))
    for table in ("claims", "evidence_sources", "projects", "evidence_claims_current"):
        with pytest.raises(ProgrammingError), reader.begin() as c:
            c.execute(text(f"SELECT * FROM {table}"))
    pipe.dispose()
    reader.dispose()
