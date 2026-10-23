"""The evidence ledger must not change anything the Asian Games pipeline reports."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.config import Settings
from sie.evidence import create_project, ingest_upload, open_scope
from sie.pipeline.freshness import source_freshness
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "tests/fixtures/manual"
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
MAX_AGE = timedelta(days=2)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )


def _facts(engine):
    with engine.connect() as c:
        return {
            "medals": c.execute(
                text(
                    "SELECT country_code, medal, count(*) FROM v_medal_facts GROUP BY 1, 2 ORDER BY 1, 2"
                )
            ).all(),
            "reporting": c.execute(text("SELECT count(*) FROM reporting.medal_facts")).scalar_one(),
            "freshness": source_freshness(c, "asiad-2026", "manual", NOW, MAX_AGE),
            "placings": c.execute(text("SELECT count(*) FROM placings")).scalar_one(),
            "official_docs": c.execute(
                text("SELECT count(*) FROM raw_documents WHERE source <> 'upload'")
            ).scalar_one(),
        }


def test_uploads_change_no_sports_number_artifact_or_freshness(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    run_ingest(
        engine,
        SourceInput(
            source="manual",
            url="file://manual/golden.csv",
            content=(MANUAL / "golden.csv").read_bytes(),
            content_type="text/csv",
            extension="csv",
            parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
        ),
        settings,
    )
    before = _facts(engine)
    assert before["placings"] > 0 and before["freshness"].artifacts  # the baseline is not empty

    with engine.begin() as c:
        create_project(c, slug="alpha", name="Alpha", now=NOW)
        scope = open_scope(c, "alpha", for_write=True)
    for i in range(3):
        ingest_upload(engine, scope, f"col\n{i}\n".encode(), f"f{i}.csv", now=NOW)

    after = _facts(engine)
    assert after == before
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM raw_documents WHERE source = 'upload'")
            ).scalar_one()
            == 3
        )
        assert (
            c.execute(text("SELECT count(*) FROM raw_fetches WHERE run_id IS NULL")).scalar_one()
            == 3
        )
