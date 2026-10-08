"""Orchestrate one ingestion run: raw store, parse, normalise, validate, quarantine, load.

This is the only ingestion path. A source supplies bytes and a pure ``parse`` function; everything
after that is shared, so the portal capture and a manual CSV can never drift apart.

Transactions (docs/ARCHITECTURE.md section 5):
1. open the run, committed on its own, so a run that dies later is still on record;
2. store the raw bytes, committed next, so the input is never lost if a later step fails;
3. parse, normalise, validate, quarantine and load in one transaction (all or nothing);
4. close the run as ``success`` or ``failed``. A failure is stored as ``[category] detail`` where the
   category names the stage that broke (``pipeline/failure.py``), which fixes what is safe to retry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, text

from sie.config import Settings
from sie.pipeline.conflicts import conflict_policy
from sie.pipeline.failure import FailureCategory, format_error_summary
from sie.pipeline.load import LoadError, load_placed, write_quarantine
from sie.pipeline.models import NormalisedResult, Rejection
from sie.pipeline.normalise import load_reference_index, normalise
from sie.pipeline.raw import store_raw
from sie.pipeline.validate import validate
from sie.reference import normalise_key
from sie.sources.base import ParsedResult

log = logging.getLogger("sie.pipeline.runner")

Parser = Callable[[bytes], list[ParsedResult]]


@dataclass(frozen=True)
class SourceInput:
    """What a source hands to the runner."""

    source: str  # 'official', 'manual', ...: stored on runs, raw documents and placings
    url: str  # the raw document key, e.g. capture:<name> or file://<name>
    content: bytes
    content_type: str
    extension: str
    parse: Parser


@dataclass
class IngestResult:
    run_id: int
    status: str  # success | failed
    raw_version_id: int | None  # None when nothing was stored (fetch or raw-store failure)
    raw_outcome: str  # new_version | unchanged | reverted | not_stored
    events: int = 0
    placings: dict[str, int] = field(default_factory=dict)
    rows_seen: int = 0
    rows_quarantined: int = 0
    duplicates_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    failure_category: FailureCategory | None = None


def run_ingest(
    engine: Engine, src: SourceInput, settings: Settings, now: datetime | None = None
) -> IngestResult:
    now = now or datetime.now(UTC)
    raw_dir = _raw_dir(settings)

    competition_id = _open_run_check(engine, settings)
    run_id = _open_run(engine, competition_id, src.source, now)
    try:
        with engine.begin() as conn:
            raw = store_raw(
                conn,
                source=src.source,
                url=src.url,
                content=src.content,
                content_type=src.content_type,
                extension=src.extension,
                now=now,
                run_id=run_id,
                backend=settings.raw_store_backend,
                raw_dir=raw_dir,
            )
    except (
        Exception
    ) as exc:  # nothing was stored: say so and close the run, never leave it 'running'
        result = IngestResult(run_id, "failed", None, "not_stored")
        _fail(result, FailureCategory.RAW_STORE, exc)
        _finish(engine, result, now=datetime.now(UTC))
        return result
    log.info(
        "raw stored",
        extra={"run_id": run_id, "source": src.source, "url": src.url, "outcome": raw.outcome},
    )
    result = IngestResult(run_id, "running", raw.version_id, raw.outcome)
    stage = FailureCategory.PARSE

    try:
        with engine.begin() as conn:
            parsed = src.parse(src.content)
            stage = FailureCategory.VALIDATION
            ref = load_reference_index(conn, settings.reference_dir)
            competition = ref.competitions[normalise_key(settings.competition_id)]
            normalised: list[NormalisedResult] = []
            rejections: list[Rejection] = []
            for row in parsed:
                outcome = normalise(row, ref, competition)
                (rejections if isinstance(outcome, Rejection) else normalised).append(outcome)  # type: ignore[arg-type]
            checked = validate(normalised, ref, competition)
            rejections += checked.rejections
            stage = FailureCategory.LOAD
            write_quarantine(
                conn, rejections, run_id=run_id, raw_version_id=raw.version_id, now=now
            )
            summary = load_placed(
                conn,
                checked.placed,
                competition,
                raw_version_id=raw.version_id,
                run_id=run_id,
                source=src.source,
                now=now,
                policy=conflict_policy(settings),
            )
            result.events = summary.events
            result.placings = summary.placings
            result.rows_seen = len(parsed)
            result.rows_quarantined = len(rejections)
            result.duplicates_skipped = checked.duplicates_skipped
            result.warnings = checked.warnings
        result.status = "success"
    except Exception as exc:  # the raw input and the run record are already safe; say what failed
        _fail(result, stage, exc)

    _finish(engine, result, now=datetime.now(UTC))
    log.info(
        "run finished",
        extra={
            "run_id": run_id,
            "status": result.status,
            "rows_seen": result.rows_seen,
            "rows_quarantined": result.rows_quarantined,
            "placings": result.placings,
            "failure_category": result.failure_category,
        },
    )
    _log_summary(engine, run_id)
    return result


def record_fetch_failure(
    engine: Engine,
    settings: Settings,
    *,
    source: str,
    url: str,
    error: BaseException | str,
    http_status: int | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """Record that a source could not be fetched. Changes no raw version and no data.

    The failure is visible in three places: a failed run (category ``fetch_failure``), a
    ``raw_fetches`` row with outcome ``error`` and the HTTP status, and freshness (the source turns
    ``failing``). The document's latest version is left exactly as it was, so a failed fetch can never
    replace good evidence. Retrying is safe because nothing else happened.
    """
    now = now or datetime.now(UTC)
    competition_id = _open_run_check(engine, settings)
    run_id = _open_run(engine, competition_id, source, now)
    detail = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    if http_status is not None:
        detail = f"HTTP {http_status}: {detail}"
    with engine.begin() as conn:
        doc_id = conn.execute(
            text(
                """INSERT INTO raw_documents (source, url, first_seen_at) VALUES (:s, :u, :n)
                   ON CONFLICT (source, url) DO UPDATE SET source = EXCLUDED.source RETURNING id"""
            ),
            {"s": source, "u": url, "n": now},
        ).scalar_one()
        conn.execute(
            text(
                """INSERT INTO raw_fetches (document_id, version_id, run_id, fetched_at, http_status,
                                            outcome, error)
                   VALUES (:d, NULL, :r, :n, :h, 'error', :e)"""
            ),
            {"d": doc_id, "r": run_id, "n": now, "h": http_status, "e": detail[:500]},
        )
    result = IngestResult(run_id, "failed", None, "not_stored")
    _fail(result, FailureCategory.FETCH, detail)
    _finish(engine, result, now=datetime.now(UTC), docs_fetched=0)
    log.error("fetch failed", extra={"run_id": run_id, "source": source, "url": url})
    _log_summary(engine, run_id)
    return result


def _open_run_check(engine: Engine, settings: Settings) -> int:
    with engine.connect() as conn:
        competition_id = conn.execute(
            text("SELECT id FROM competitions WHERE code = :c"), {"c": settings.competition_id}
        ).scalar_one_or_none()
    if competition_id is None:
        raise LoadError(
            f"competition {settings.competition_id!r} missing: run `sie seed-reference` first"
        )
    return competition_id


def _open_run(engine: Engine, competition_id: int, source: str, now: datetime) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                """INSERT INTO ingest_runs (competition_id, started_at, status, source)
                   VALUES (:c, :n, 'running', :s) RETURNING id"""
            ),
            {"c": competition_id, "n": now, "s": source},
        ).scalar_one()


def _fail(result: IngestResult, category: FailureCategory, error: BaseException | str) -> None:
    result.status = "failed"
    result.failure_category = category
    result.error = format_error_summary(category, error)
    log.error(
        "run failed",
        extra={"run_id": result.run_id, "failure_category": category, "error": result.error},
    )


def _log_summary(engine: Engine, run_id: int) -> None:
    from sie.pipeline.observe import load_run_summary

    with engine.connect() as conn:
        log.info("run summary", extra={"run_summary": load_run_summary(conn, run_id).to_dict()})


def _finish(
    engine: Engine, result: IngestResult, *, now: datetime, docs_fetched: int | None = None
) -> None:
    loaded = sum(result.placings.values())
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE ingest_runs
                   SET finished_at = :n, status = :st, docs_fetched = :df, docs_changed = :ch,
                       rows_loaded = :ld, rows_quarantined = :q, error_summary = :e
                   WHERE id = :r"""
            ),
            {
                "n": now,
                "st": result.status,
                "df": (1 if result.raw_version_id is not None else 0)
                if docs_fetched is None
                else docs_fetched,
                "ch": 1 if result.raw_outcome in ("new_version", "reverted") else 0,
                "ld": loaded if result.status == "success" else 0,
                "q": result.rows_quarantined if result.status == "success" else 0,
                "e": result.error,
                "r": result.run_id,
            },
        )


def _raw_dir(settings: Settings) -> Path:
    return settings.data_dir / "raw"
