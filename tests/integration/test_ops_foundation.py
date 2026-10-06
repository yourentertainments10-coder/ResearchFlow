"""Phase 6 foundations on a real PostgreSQL: failure categories, retry safety, summaries, freshness.

These tests cover only operational behaviour. They do not touch analytics, reports or the dashboard.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.config import Settings
from sie.pipeline.failure import FailureCategory, RetryPolicy
from sie.pipeline.freshness import Freshness, source_freshness
from sie.pipeline.observe import RunOutcome, load_run_summary, stuck_runs
from sie.pipeline.raw import RawStoreError, read_raw
from sie.pipeline.runner import SourceInput, record_fetch_failure, run_ingest
from sie.reference import seed_reference
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "tests/fixtures/manual"
MAX_AGE = timedelta(minutes=30)
T0 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="fs",
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def csv_source(name="golden.csv", url=None, content=None, parse=None) -> SourceInput:
    return SourceInput(
        source=manual.SOURCE,
        url=url or f"file://{name}",
        content=content if content is not None else (MANUAL / name).read_bytes(),
        content_type="text/csv",
        extension="csv",
        parse=parse
        or (lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200)),
    )


def count(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def summary(engine, run_id):
    with engine.connect() as c:
        return load_run_summary(c, run_id)


def freshness(engine, settings, now):
    with engine.connect() as c:
        return source_freshness(c, settings.competition_id, manual.SOURCE, now, MAX_AGE)


# --- run lifecycle and structured summary ---------------------------------------------------------------


def test_a_successful_run_has_a_complete_json_safe_summary(seeded, settings):
    result = run_ingest(seeded, csv_source(), settings, now=T0)
    s = summary(seeded, result.run_id)
    assert (s.competition, s.source, s.status) == (settings.competition_id, "manual", "success")
    assert s.outcome == RunOutcome.SUCCEEDED
    assert s.started_at <= s.finished_at and s.duration_seconds >= 0
    assert (s.docs_fetched, s.docs_changed) == (1, 1) and s.rows_loaded > 0
    assert s.failure_category is None and s.error_detail is None
    json.dumps(s.to_dict())  # serialisable as is


def test_quarantined_rows_are_counted_by_reason_and_change_the_outcome(seeded, settings):
    result = run_ingest(seeded, csv_source("golden_with_bad_rows.csv"), settings, now=T0)
    s = summary(seeded, result.run_id)
    assert s.outcome == RunOutcome.SUCCEEDED_WITH_QUARANTINE
    assert s.rows_quarantined == 3 and sum(s.quarantine_by_reason.values()) == 3


def test_an_unchanged_rerun_has_the_unchanged_outcome_and_the_same_quarantine_picture(
    seeded, settings
):
    run_ingest(seeded, csv_source(), settings, now=T0)
    again = run_ingest(seeded, csv_source(), settings, now=T0 + timedelta(minutes=10))
    assert summary(seeded, again.run_id).outcome == RunOutcome.UNCHANGED

    first = run_ingest(seeded, csv_source("golden_with_bad_rows.csv"), settings, now=T0)
    second = run_ingest(seeded, csv_source("golden_with_bad_rows.csv"), settings, now=T0)
    assert (
        summary(seeded, first.run_id).quarantine_by_reason
        == summary(seeded, second.run_id).quarantine_by_reason
    )


def test_an_unknown_run_is_an_error_not_an_empty_summary(seeded):
    with pytest.raises(LookupError):
        summary(seeded, 999)


# --- failure categories and the retry contract ---------------------------------------------------------


def test_a_parse_failure_is_categorised_keeps_the_raw_input_and_is_retry_after_fix(
    seeded, settings
):
    def explode(_raw):
        raise ValueError("unexpected structure")

    result = run_ingest(seeded, csv_source(content=b"x,y\n", parse=explode), settings, now=T0)
    assert result.failure_category == FailureCategory.PARSE
    s = summary(seeded, result.run_id)
    assert (s.status, s.outcome) == ("failed", RunOutcome.FAILED)
    assert s.failure_category == "parse_failure" and "unexpected structure" in s.error_detail
    assert s.retry_policy == RetryPolicy.AFTER_FIX and s.max_auto_attempts == 0
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1  # evidence kept


def test_a_failure_in_the_validation_stage_is_categorised(seeded, settings, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("rule table missing")

    monkeypatch.setattr("sie.pipeline.runner.validate", broken)
    result = run_ingest(seeded, csv_source(), settings, now=T0)
    assert result.failure_category == FailureCategory.VALIDATION
    assert summary(seeded, result.run_id).retry_policy == RetryPolicy.AFTER_FIX
    assert count(seeded, "SELECT count(*) FROM placings") == 0


def test_a_load_failure_is_categorised_retryable_once_and_rolls_back_everything(seeded, settings):
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = 1"))
    result = run_ingest(seeded, csv_source(), settings, now=T0)
    assert result.failure_category == FailureCategory.LOAD
    s = summary(seeded, result.run_id)
    assert s.failure_category == "load_failure" and "EVENT_TOTAL_EXCEEDED" in s.error_detail
    assert (s.retry_policy, s.max_auto_attempts) == (RetryPolicy.AUTO, 1)
    for table in ("placings", "events", "quarantine"):
        assert count(seeded, f"SELECT count(*) FROM {table}") == 0, table


def test_a_raw_store_failure_closes_the_run_stores_nothing_and_needs_a_person(
    seeded, settings, monkeypatch
):
    def refuse(*_a, **_k):
        raise RawStoreError("key already exists with different bytes")

    monkeypatch.setattr("sie.pipeline.runner.store_raw", refuse)
    result = run_ingest(seeded, csv_source(), settings, now=T0)
    assert result.failure_category == FailureCategory.RAW_STORE
    assert result.raw_version_id is None and result.raw_outcome == "not_stored"
    s = summary(seeded, result.run_id)
    assert s.status == "failed" and s.finished_at is not None  # never left 'running'
    assert s.retry_policy == RetryPolicy.MANUAL
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 0
    assert count(seeded, "SELECT count(*) FROM placings") == 0


def test_a_retry_after_every_kind_of_failure_is_idempotent(seeded, settings):
    src = csv_source()
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = 1"))
    failed = run_ingest(seeded, src, settings, now=T0)
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = NULL"))
    ok = run_ingest(seeded, src, settings, now=T0 + timedelta(minutes=1))
    again = run_ingest(seeded, src, settings, now=T0 + timedelta(minutes=2))
    assert (failed.status, ok.status, again.status) == ("failed", "success", "success")
    assert failed.raw_version_id == ok.raw_version_id == again.raw_version_id
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1
    assert count(seeded, "SELECT count(*) FROM placings") == count(
        seeded, "SELECT count(*) FROM placings WHERE is_current"
    )


# --- fetch failures never touch raw evidence -----------------------------------------------------------


def test_a_fetch_failure_is_recorded_without_creating_or_changing_any_raw_version(seeded, settings):
    ok = run_ingest(seeded, csv_source(url="https://example.org/feed"), settings, now=T0)
    versions_before = count(seeded, "SELECT count(*) FROM raw_versions")
    latest_before = count(seeded, "SELECT latest_version_id FROM raw_documents")

    failed = record_fetch_failure(
        seeded,
        settings,
        source=manual.SOURCE,
        url="https://example.org/feed",
        error=TimeoutError("read timed out"),
        http_status=503,
        now=T0 + timedelta(minutes=5),
    )
    assert failed.failure_category == FailureCategory.FETCH and failed.raw_version_id is None
    assert count(seeded, "SELECT count(*) FROM raw_versions") == versions_before
    assert count(seeded, "SELECT latest_version_id FROM raw_documents") == latest_before
    with seeded.connect() as c:
        fetch = c.execute(
            text(
                "SELECT outcome, http_status, version_id, error FROM raw_fetches WHERE run_id = :r"
            ),
            {"r": failed.run_id},
        ).one()
        assert read_raw(c, ok.raw_version_id, settings.data_dir / "raw")  # the good bytes remain
    assert fetch.outcome == "error" and fetch.http_status == 503 and fetch.version_id is None
    assert "read timed out" in fetch.error
    s = summary(seeded, failed.run_id)
    assert (s.docs_fetched, s.docs_changed) == (0, 0)
    assert (s.failure_category, s.retry_policy, s.max_auto_attempts) == ("fetch_failure", "auto", 3)
    assert "HTTP 503" in s.error_detail


def test_a_fetch_failure_for_a_new_url_creates_a_document_but_no_version(seeded, settings):
    record_fetch_failure(
        seeded, settings, source="official", url="https://x.test/a", error="boom", now=T0
    )
    assert count(seeded, "SELECT count(*) FROM raw_documents") == 1
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 0
    assert count(seeded, "SELECT count(*) FROM raw_documents WHERE latest_version_id IS NULL") == 1


def test_a_fetch_failure_on_an_unseeded_database_is_a_setup_error(engine, settings):
    from sie.pipeline.load import LoadError

    with pytest.raises(LoadError, match="seed-reference"):
        record_fetch_failure(engine, settings, source="official", url="u", error="boom")
    assert count(engine, "SELECT count(*) FROM ingest_runs") == 0


def test_raw_bytes_are_never_silently_overwritten(tmp_path):
    from sie.pipeline.raw import _write_once

    _write_once(tmp_path, "s/2026-10-04/090000_abc.csv", b"original")
    _write_once(tmp_path, "s/2026-10-04/090000_abc.csv", b"original")  # same bytes: fine
    with pytest.raises(RawStoreError):
        _write_once(tmp_path, "s/2026-10-04/090000_abc.csv", b"different")
    assert (tmp_path / "s/2026-10-04/090000_abc.csv").read_bytes() == b"original"


# --- source freshness ----------------------------------------------------------------------------------


def test_a_source_that_never_ran_has_no_fingerprint_and_has_never_succeeded(seeded, settings):
    f = freshness(seeded, settings, T0)
    assert f.status == Freshness.NEVER_SUCCEEDED
    assert f.fingerprint is None and f.artifacts == [] and f.last_success_at is None


def test_freshness_follows_success_staleness_failure_and_recovery(seeded, settings):
    ok = run_ingest(seeded, csv_source(url="https://example.org/feed"), settings, now=T0)
    done = summary(seeded, ok.run_id).finished_at

    fresh = freshness(seeded, settings, done + timedelta(minutes=10))
    assert fresh.status == Freshness.FRESH and fresh.last_attempt_status == "success"
    assert freshness(seeded, settings, done + timedelta(hours=2)).status == Freshness.STALE

    record_fetch_failure(
        seeded, settings, source=manual.SOURCE, url="https://example.org/feed", error="down", now=T0
    )
    failing = freshness(seeded, settings, done + timedelta(minutes=10))
    assert failing.status == Freshness.FAILING
    assert failing.last_success_at == fresh.last_success_at  # the failure did not erase the success

    run_ingest(
        seeded, csv_source(url="https://example.org/feed"), settings, now=T0 + timedelta(hours=1)
    )
    assert freshness(seeded, settings, datetime.now(UTC)).status == Freshness.FRESH


def test_the_fingerprint_and_artifact_reference_track_the_raw_content(seeded, settings):
    url = "https://example.org/feed"
    one = run_ingest(seeded, csv_source(url=url), settings, now=T0)
    a = freshness(seeded, settings, T0)
    art = a.artifacts[0]
    with seeded.connect() as c:
        sha = c.execute(
            text("SELECT sha256 FROM raw_versions WHERE id = :v"), {"v": one.raw_version_id}
        ).scalar_one()
    assert (art.url, art.version_no, art.sha256, art.storage_backend) == (url, 1, sha, "fs")
    assert (settings.data_dir / "raw" / art.path).exists()  # the reference resolves to real bytes

    run_ingest(seeded, csv_source(url=url), settings, now=T0 + timedelta(minutes=5))
    assert (
        freshness(seeded, settings, T0).fingerprint == a.fingerprint
    )  # same bytes, same fingerprint

    run_ingest(seeded, csv_source("golden_reallocated.csv", url=url), settings, now=T0)
    b = freshness(seeded, settings, T0)
    assert b.fingerprint != a.fingerprint and b.artifacts[0].version_no == 2
    json.dumps(b.to_dict())


def test_freshness_is_per_source(seeded, settings):
    run_ingest(seeded, csv_source(), settings, now=T0)
    with seeded.connect() as c:
        other = source_freshness(c, settings.competition_id, "official", T0, MAX_AGE)
    assert other.status == Freshness.NEVER_SUCCEEDED


def test_a_run_that_never_closed_is_reported_as_stuck(seeded, settings):
    with seeded.begin() as c:
        cid = c.execute(text("SELECT id FROM competitions")).scalar_one()
        stuck = c.execute(
            text(
                "INSERT INTO ingest_runs (competition_id, started_at, status, source) "
                "VALUES (:c, :t, 'running', 'manual') RETURNING id"
            ),
            {"c": cid, "t": T0},
        ).scalar_one()
    with seeded.connect() as c:
        assert stuck_runs(c, T0 + timedelta(hours=3), timedelta(hours=1)) == [stuck]
        assert stuck_runs(c, T0 + timedelta(minutes=5), timedelta(hours=1)) == []
