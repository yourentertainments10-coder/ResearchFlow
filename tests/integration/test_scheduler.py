"""Scheduler, retries and locking on a real PostgreSQL. Operational behaviour only."""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy.exc
from sqlalchemy import text

from sie.config import Settings
from sie.db.locks import source_lock
from sie.pipeline.failure import FailureCategory
from sie.pipeline.load import LoadError
from sie.pipeline.observe import load_run_summary
from sie.pipeline.raw import RawStoreError
from sie.pipeline.runner import SourceInput
from sie.pipeline.runner import run_ingest as real_run_ingest
from sie.pipeline.scheduler import (
    STUCK_MESSAGE,
    FetchError,
    ScheduleStatus,
    SourceTask,
    close_stuck_runs,
    run_scheduled,
)
from sie.reference import seed_reference
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "tests/fixtures/manual"
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


def csv_input(name="golden.csv", source=manual.SOURCE, parse=None) -> SourceInput:
    return SourceInput(
        source=source,
        url=f"file://{name}",
        content=(MANUAL / name).read_bytes(),
        content_type="text/csv",
        extension="csv",
        parse=parse
        or (lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200)),
    )


def task(fetch, source=manual.SOURCE) -> SourceTask:
    return SourceTask(source=source, url="https://example.org/feed", fetch=fetch)


def count(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


class Flaky:
    """A fetch that fails ``failures`` times, then returns the input."""

    def __init__(self, failures, src=None, status=503):
        self.failures, self.calls, self.status, self.src = failures, 0, status, src

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise FetchError("upstream unavailable", http_status=self.status)
        return self.src or csv_input()


def run(engine, settings, t, sleeps=None, **kw):
    return run_scheduled(
        engine, settings, t, sleep=(sleeps.append if sleeps is not None else lambda _s: None),
        now=lambda: T0, **kw,
    )  # fmt: skip


# --- a normal run --------------------------------------------------------------------------------------


def test_a_successful_scheduled_run_ingests_once_and_releases_the_lock(seeded, settings):
    result = run(seeded, settings, task(lambda: csv_input()))
    assert result.status == ScheduleStatus.SUCCEEDED
    assert (result.fetch_attempts, result.load_retries, len(result.attempts)) == (1, 0, 1)
    assert count(seeded, "SELECT count(*) FROM placings") > 0
    with source_lock(seeded, settings.competition_id, manual.SOURCE) as free:
        assert free is True


# --- fetch retries: at most 3 attempts ----------------------------------------------------------------


def test_a_fetch_that_recovers_is_retried_with_backoff_and_every_failure_is_on_record(
    seeded, settings
):
    fetch, sleeps = Flaky(failures=2), []
    result = run(seeded, settings, task(fetch), sleeps)
    assert result.status == ScheduleStatus.SUCCEEDED and fetch.calls == 3
    assert sleeps == [2.0, 4.0]
    assert [a.failure_category for a in result.attempts] == [
        FailureCategory.FETCH, FailureCategory.FETCH, None,
    ]  # fmt: skip
    assert count(seeded, "SELECT count(*) FROM raw_fetches WHERE outcome = 'error'") == 2
    assert count(seeded, "SELECT count(*) FROM raw_fetches WHERE http_status = 503") == 2
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


def test_a_fetch_that_never_recovers_stops_after_three_attempts_and_stores_nothing(
    seeded, settings
):
    fetch, sleeps = Flaky(failures=99), []
    result = run(seeded, settings, task(fetch), sleeps)
    assert result.status == ScheduleStatus.FAILED and fetch.calls == 3
    assert sleeps == [2.0, 4.0]  # no pointless wait after the last attempt
    assert count(seeded, "SELECT count(*) FROM ingest_runs WHERE status = 'failed'") == 3
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 0
    assert count(seeded, "SELECT count(*) FROM placings") == 0
    assert result.final.failure_category == FailureCategory.FETCH


def test_an_unexpected_exception_in_the_fetch_step_is_a_fetch_failure(seeded, settings):
    def broken():
        raise RuntimeError("dns exploded")

    result = run(seeded, settings, task(broken))
    assert result.final.failure_category == FailureCategory.FETCH and result.fetch_attempts == 3


# --- load retry: at most 1 automatic retry ------------------------------------------------------------


def _total(engine, value):
    with engine.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = :v"), {"v": value})


def test_a_transient_load_failure_is_retried_once_on_the_bytes_already_fetched(
    seeded, settings, monkeypatch
):
    _total(seeded, 1)  # makes the first load fail
    calls = []

    def flaky_ingest(engine, src, st, now=None):
        result = real_run_ingest(engine, src, st, now=now)
        calls.append(result.status)
        if len(calls) == 1:
            _total(engine, None)  # the cause clears before the retry
        return result

    monkeypatch.setattr("sie.pipeline.scheduler.run_ingest", flaky_ingest)
    fetch = Flaky(failures=0)
    result = run(seeded, settings, task(fetch))
    assert result.status == ScheduleStatus.SUCCEEDED
    assert calls == ["failed", "success"] and result.load_retries == 1
    assert fetch.calls == 1  # not fetched again
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1  # nothing stored twice
    assert [a.failure_category for a in result.attempts] == [FailureCategory.LOAD, None]
    assert count(seeded, "SELECT count(*) FROM placings") > 0


def test_a_persistent_load_failure_is_retried_exactly_once_then_reported(seeded, settings):
    _total(seeded, 1)
    result = run(seeded, settings, task(lambda: csv_input()))
    assert result.status == ScheduleStatus.FAILED and result.load_retries == 1
    assert len(result.attempts) == 2 and result.final.failure_category == FailureCategory.LOAD
    assert count(seeded, "SELECT count(*) FROM placings") == 0  # all or nothing, both times
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


# --- no automatic retry for parse, validation, raw store ---------------------------------------------


def _one_attempt(seeded, result, category):
    assert result.status == ScheduleStatus.FAILED and len(result.attempts) == 1
    assert result.load_retries == 0 and result.final.failure_category == category
    assert count(seeded, "SELECT count(*) FROM ingest_runs") == 1


def test_a_parse_failure_is_not_retried(seeded, settings):
    def explode(_raw):
        raise ValueError("unexpected structure")

    result = run(seeded, settings, task(lambda: csv_input(parse=explode)))
    _one_attempt(seeded, result, FailureCategory.PARSE)
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1  # the evidence is kept


def test_a_validation_failure_is_not_retried(seeded, settings, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("rules missing")

    monkeypatch.setattr("sie.pipeline.runner.validate", broken)
    _one_attempt(
        seeded, run(seeded, settings, task(lambda: csv_input())), FailureCategory.VALIDATION
    )


def test_a_raw_store_failure_is_not_retried(seeded, settings, monkeypatch):
    def refuse(*_a, **_k):
        raise RawStoreError("conflict")

    monkeypatch.setattr("sie.pipeline.runner.store_raw", refuse)
    _one_attempt(
        seeded, run(seeded, settings, task(lambda: csv_input())), FailureCategory.RAW_STORE
    )


# --- locking and concurrency --------------------------------------------------------------------------


def test_the_lock_is_exclusive_per_source_and_independent_across_sources(seeded, settings):
    comp = settings.competition_id
    with source_lock(seeded, comp, "official") as first:
        assert first is True
        with source_lock(seeded, comp, "official") as second:
            assert second is False  # same source: busy
        with source_lock(seeded, comp, "manual") as other:
            assert other is True  # a different source is unaffected
    with source_lock(seeded, comp, "official") as again:
        assert again is True  # released on exit


def test_a_duplicate_run_is_skipped_while_another_is_active_and_other_sources_still_run(
    seeded, settings
):
    started, release = threading.Event(), threading.Event()
    box = {}

    def slow_fetch():
        started.set()
        assert release.wait(20), "test deadlock"
        return csv_input()

    first = threading.Thread(
        target=lambda: box.update(first=run(seeded, settings, task(slow_fetch, "manual"))),
        daemon=True,
    )
    first.start()
    try:
        assert started.wait(20)
        dup = run(seeded, settings, task(lambda: csv_input(), "manual"))
        assert dup.status == ScheduleStatus.SKIPPED_LOCKED and dup.attempts == []
        assert count(seeded, "SELECT count(*) FROM ingest_runs") == 0  # the duplicate wrote nothing

        # an independent source is not blocked by the busy one
        other = run(seeded, settings, task(lambda: csv_input("golden.csv", "official"), "official"))
        assert other.status == ScheduleStatus.FAILED or other.status == ScheduleStatus.SUCCEEDED
        assert other.status != ScheduleStatus.SKIPPED_LOCKED
    finally:
        release.set()
        first.join(30)
    assert box["first"].status == ScheduleStatus.SUCCEEDED
    assert count(seeded, "SELECT count(*) FROM ingest_runs WHERE source = 'manual'") == 1
    with source_lock(seeded, settings.competition_id, "manual") as free:
        assert free is True


def test_many_simultaneous_runs_of_one_source_produce_exactly_one_run(seeded, settings):
    gate, results = threading.Barrier(5), []

    def go():
        gate.wait(20)
        results.append(run(seeded, settings, task(lambda: (_pause(), csv_input())[1])))

    def _pause():
        threading.Event().wait(0.5)  # hold the lock long enough for the others to arrive

    threads = [threading.Thread(target=go, daemon=True) for _ in range(5)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    statuses = sorted(r.status for r in results)
    assert statuses.count(ScheduleStatus.SUCCEEDED) == 1
    assert statuses.count(ScheduleStatus.SKIPPED_LOCKED) == 4
    assert count(seeded, "SELECT count(*) FROM ingest_runs") == 1
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


def test_the_lock_is_released_after_a_failed_run(seeded, settings):
    def explode(_raw):
        raise ValueError("bad")

    assert (
        run(seeded, settings, task(lambda: csv_input(parse=explode))).status
        == ScheduleStatus.FAILED
    )
    with source_lock(seeded, settings.competition_id, manual.SOURCE) as free:
        assert free is True


def test_the_lock_is_released_when_the_run_raises_a_setup_error(engine, settings):
    with pytest.raises(LoadError, match="seed-reference"):  # competition not seeded
        run(engine, settings, task(lambda: csv_input()))
    with source_lock(engine, settings.competition_id, manual.SOURCE) as free:
        assert free is True


def test_the_lock_dies_with_its_connection(seeded, settings):
    """A crashed process must not leave a source locked: ending the session frees the lock."""
    cm = source_lock(seeded, settings.competition_id, manual.SOURCE)
    assert cm.__enter__() is True
    with source_lock(seeded, settings.competition_id, manual.SOURCE) as blocked:
        assert blocked is False
    cm.gen.gi_frame.f_locals[
        "conn"
    ].invalidate()  # the session ends without an unlock: a dead process
    with source_lock(seeded, settings.competition_id, manual.SOURCE) as free:
        assert free is True
    with pytest.raises(
        sqlalchemy.exc.SQLAlchemyError
    ):  # the dead session cannot unlock; that must surface, not hang
        cm.__exit__(None, None, None)


# --- reruns and stuck runs ----------------------------------------------------------------------------


def test_a_rerun_after_a_failed_run_succeeds_and_changes_nothing_twice(seeded, settings):
    def explode(_raw):
        raise ValueError("parser bug")

    failed = run(seeded, settings, task(lambda: csv_input(parse=explode)))
    ok = run(seeded, settings, task(lambda: csv_input()))
    again = run(seeded, settings, task(lambda: csv_input()))
    assert [r.status for r in (failed, ok, again)] == [
        ScheduleStatus.FAILED, ScheduleStatus.SUCCEEDED, ScheduleStatus.SUCCEEDED,
    ]  # fmt: skip
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1
    assert ok.final.raw_version_id == failed.final.raw_version_id == again.final.raw_version_id
    with seeded.connect() as c:
        assert load_run_summary(c, again.final.run_id).outcome == "unchanged"
    assert count(seeded, "SELECT count(*) FROM placings WHERE NOT is_current") == 0


def _insert_running(engine, source, started_at):
    with engine.begin() as c:
        cid = c.execute(text("SELECT id FROM competitions")).scalar_one()
        return c.execute(
            text(
                "INSERT INTO ingest_runs (competition_id, started_at, status, source) "
                "VALUES (:c, :t, 'running', :s) RETURNING id"
            ),
            {"c": cid, "t": started_at, "s": source},
        ).scalar_one()


def test_a_scheduled_run_closes_its_sources_stuck_runs_before_running(seeded, settings):
    stuck = _insert_running(seeded, manual.SOURCE, T0 - timedelta(hours=5))
    young = _insert_running(seeded, manual.SOURCE, T0 - timedelta(minutes=5))
    foreign = _insert_running(seeded, "official", T0 - timedelta(hours=5))

    result = run(seeded, settings, task(lambda: csv_input()))
    assert result.status == ScheduleStatus.SUCCEEDED and result.stuck_closed == [stuck]
    with seeded.connect() as c:
        rows = {
            r.id: r
            for r in c.execute(
                text("SELECT id, status, error_summary, finished_at FROM ingest_runs")
            )
        }
    assert rows[stuck].status == "failed" and rows[stuck].error_summary == STUCK_MESSAGE
    assert rows[stuck].finished_at is not None
    assert rows[young].status == "running"  # might be alive: left alone
    assert rows[foreign].status == "running"  # another source is not this run's business
    with seeded.connect() as c:
        summary = load_run_summary(c, stuck)
    assert (
        summary.failure_category is None and summary.status == "failed"
    )  # no new category invented


def test_recovery_can_close_stuck_runs_for_every_source_and_never_touches_data(seeded, settings):
    run(seeded, settings, task(lambda: csv_input()))
    placings, versions = (
        count(seeded, f"SELECT count(*) FROM {t}") for t in ("placings", "raw_versions")
    )
    a = _insert_running(seeded, "manual", T0 - timedelta(hours=3))
    b = _insert_running(seeded, "official", T0 - timedelta(hours=3))
    closed = close_stuck_runs(seeded, settings, source=None, now=T0, older_than=timedelta(hours=1))
    assert sorted(closed) == [a, b]
    assert (
        close_stuck_runs(seeded, settings, source=None, now=T0, older_than=timedelta(hours=1)) == []
    )
    assert count(seeded, "SELECT count(*) FROM placings") == placings
    assert count(seeded, "SELECT count(*) FROM raw_versions") == versions
