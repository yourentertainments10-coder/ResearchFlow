"""Run one source on a schedule: lock, recover stuck runs, fetch with retries, ingest, retry a load once.

This layer only *applies* the retry contract defined in ``failure.py``; it does not change it:

* fetch failures: up to ``RETRY_RULES[FETCH].max_auto_attempts`` attempts in total, with backoff;
* load failures: up to ``RETRY_RULES[LOAD].max_auto_attempts`` automatic retries, reusing the bytes
  already fetched (so nothing is fetched or stored twice);
* parse, validation and raw-store failures: never retried here, the result is returned as it is.

Every attempt is a normal ingestion run, so it has its own ``ingest_runs`` row and summary. A rerun of
the same bytes is idempotent (``runner.py``), which is what makes retrying safe.

A source supplies a ``SourceTask``: a ``fetch`` callable returning the bytes and parser to ingest. No
fetcher for the official portal is registered, because its terms of use are still open (D1 in
``DECISIONS.md``); files supplied by the owner go through the same path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Engine, text

from sie.config import Settings
from sie.db.locks import source_lock
from sie.pipeline.failure import FailureCategory, retry_rule
from sie.pipeline.runner import IngestResult, SourceInput, record_fetch_failure, run_ingest

log = logging.getLogger("sie.pipeline.scheduler")

DEFAULT_STUCK_AFTER = timedelta(hours=1)
BACKOFF_BASE_SECONDS = 2.0
STUCK_MESSAGE = "abandoned: the process ended without closing this run (closed by the scheduler)"


class FetchError(Exception):
    """A source could not be fetched. ``http_status`` is kept on the failed run when known."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class SourceTask:
    source: str  # the name used for runs, raw documents and the lock
    url: str  # the document key used if the fetch fails before any bytes arrive
    fetch: Callable[[], SourceInput]


class ScheduleStatus(StrEnum):
    SUCCEEDED = "succeeded"  # the final attempt succeeded
    FAILED = "failed"  # the final attempt failed (see ``final``)
    SKIPPED_LOCKED = "skipped_locked"  # another run of this source holds the lock; nothing was done


@dataclass
class ScheduleResult:
    source: str
    status: ScheduleStatus
    attempts: list[IngestResult] = field(default_factory=list)  # every run opened, in order
    fetch_attempts: int = 0
    load_retries: int = 0
    stuck_closed: list[int] = field(default_factory=list)

    @property
    def final(self) -> IngestResult | None:
        return self.attempts[-1] if self.attempts else None


def backoff_seconds(attempt: int, base: float = BACKOFF_BASE_SECONDS) -> float:
    """Wait after failed attempt ``attempt`` (1-based): base, 2*base, 4*base. No jitter, so it is testable."""
    return base * 2 ** (attempt - 1)


def close_stuck_runs(
    engine: Engine,
    settings: Settings,
    *,
    source: str | None,
    now: datetime,
    older_than: timedelta = DEFAULT_STUCK_AFTER,
) -> list[int]:
    """Close runs left ``running`` by a process that died, as failed. They carry no failure category.

    Only touches ``ingest_runs``. Raw evidence and loaded data are unaffected (the load of such a run
    was one transaction, so it either committed fully or not at all). Never call this for a source
    while a run of it may legitimately be alive unless that run holds the source lock.
    """
    with engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    """UPDATE ingest_runs r SET status = 'failed', finished_at = :n, error_summary = :m
                       FROM competitions c
                       WHERE c.id = r.competition_id AND c.code = :c AND r.status = 'running'
                         AND r.started_at < :t AND (CAST(:s AS TEXT) IS NULL OR r.source = :s)
                       RETURNING r.id"""
                ),
                {
                    "n": now,
                    "m": STUCK_MESSAGE,
                    "c": settings.competition_id,
                    "t": now - older_than,
                    "s": source,
                },
            ).scalars()
        )


def run_scheduled(
    engine: Engine,
    settings: Settings,
    task: SourceTask,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    stuck_after: timedelta = DEFAULT_STUCK_AFTER,
) -> ScheduleResult:
    result = ScheduleResult(task.source, ScheduleStatus.FAILED)
    with source_lock(engine, settings.competition_id, task.source) as acquired:
        if not acquired:
            result.status = ScheduleStatus.SKIPPED_LOCKED
            log.warning("run skipped: another run holds the lock", extra={"source": task.source})
            return result

        # We hold the lock, so no scheduled run of this source is alive. Anything still 'running'
        # and older than the threshold belongs to a process that died.
        result.stuck_closed = close_stuck_runs(
            engine, settings, source=task.source, now=now(), older_than=stuck_after
        )
        if result.stuck_closed:
            log.warning(
                "closed stuck runs", extra={"source": task.source, "runs": result.stuck_closed}
            )

        src = _fetch_with_retries(engine, settings, task, result, sleep, now)
        if src is None:
            return result
        _ingest_with_retry(engine, settings, src, result, now)
        result.status = (
            ScheduleStatus.SUCCEEDED
            if result.final is not None and result.final.status == "success"
            else ScheduleStatus.FAILED
        )
    return result


def _fetch_with_retries(
    engine: Engine,
    settings: Settings,
    task: SourceTask,
    result: ScheduleResult,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> SourceInput | None:
    limit = retry_rule(FailureCategory.FETCH).max_auto_attempts
    for attempt in range(1, limit + 1):
        result.fetch_attempts = attempt
        try:
            return task.fetch()
        except Exception as exc:  # any failure of the fetch step is a fetch failure
            failed = record_fetch_failure(
                engine,
                settings,
                source=task.source,
                url=task.url,
                error=exc,
                http_status=getattr(exc, "http_status", None),
                now=now(),
            )
            result.attempts.append(failed)
            if attempt < limit:
                sleep(backoff_seconds(attempt))
    return None


def _ingest_with_retry(
    engine: Engine,
    settings: Settings,
    src: SourceInput,
    result: ScheduleResult,
    now: Callable[[], datetime],
) -> None:
    retries = retry_rule(FailureCategory.LOAD).max_auto_attempts
    while True:
        run = run_ingest(engine, src, settings, now=now())
        result.attempts.append(run)
        retry_load = (
            run.status == "failed"
            and run.failure_category == FailureCategory.LOAD
            and result.load_retries < retries
        )
        if not retry_load:
            return
        result.load_retries += (
            1  # same bytes: the raw store reports 'unchanged', nothing is stored twice
        )
