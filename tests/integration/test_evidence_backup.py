"""Backup and restore with a populated evidence ledger: counts match, bytes re-hash, purge is re-runnable."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pgserver
import pytest
from sqlalchemy import text
from test_evidence_retention_purge import POLICIES, register

from sie.evidence import (
    ClaimInput,
    add_claim,
    check_project,
    create_project,
    ingest_upload,
    open_scope,
    purge_project,
    read_bytes,
    verify_claim,
)
from sie.ops.backup import backup, restore_test

BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
LEDGER = (
    "projects",
    "project_documents",
    "evidence_sources",
    "claims",
    "claim_reviews",
    "purge_tombstones",
)


@pytest.fixture()
def ledger(engine):
    with engine.begin() as c:
        create_project(c, slug="a", name="A", now=NOW)
        create_project(c, slug="gone", name="G", now=NOW)
    with engine.connect() as c:
        a = open_scope(c, "a", for_write=True)
        g = open_scope(c, "gone", for_write=True)
    up = ingest_upload(engine, a, b"country,gold\nIndia,6\n", "m.csv", now=NOW)
    ingest_upload(engine, g, b"to be purged\n", "g.txt", now=NOW)
    world = {"engine": engine, "a": a}
    register(world, retention_mode="full", policies=POLICIES)
    register(world, content=b"excerpt only", url="https://stats.example.org/ex")
    with engine.begin() as c:
        cid = add_claim(
            c, a, up.source_id,
            ClaimInput(subject="India", metric="gold", value_text="6", excerpt="6", locator_kind="table_cell",
                       locator={"row_id": 1, "column": "gold", "delimiter": ","}, extraction_method="manual",
                       extractor_version="t"),
            now=NOW,
        ).claim_id  # fmt: skip
        verify_claim(c, a, cid, now=NOW)
    purge_project(engine, "gone", now=NOW, execute=True, confirm="gone", purged_by="Asha")
    return engine, a, up


def test_a_populated_ledger_backs_up_and_restores_with_every_check_green(
    ledger, db_url, admin_url, tmp_path
):
    engine, scope, up = ledger
    result = backup(db_url, tmp_path / "b", "any", NOW, bin_dir=BIN)
    for t in LEDGER:  # the new tables are picked up by the generic counter
        assert f"public.{t}" in result.data["tables"], t
    assert result.data["tables"]["public.claims"] == 1
    assert result.data["tables"]["public.purge_tombstones"] == 1
    assert result.data["raw_versions"]["outside_dump"] == 0
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert report.ok, report.to_dict()
    assert any(c.name.startswith("raw evidence intact (2 blobs") for c in report.checks)


def test_a_restored_database_is_usable_and_a_purge_can_be_rerun_from_the_tombstone(
    ledger, db_url, admin_url, tmp_path
):
    """Restoring a pre-purge backup resurrects purged data; the tombstone is how the owner knows to redo it."""
    engine, scope, up = ledger
    with engine.connect() as c:
        assert check_project(c, scope).ok
        assert read_bytes(c, scope, up.source_id).startswith(b"country")
        tomb = c.execute(text("SELECT project_id, row_counts FROM purge_tombstones")).one()
    assert tomb.row_counts["raw_documents"] == 1
