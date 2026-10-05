"""Orchestrate one ingestion run: raw store, parse, normalise, validate, quarantine, load.

This is the only ingestion path. A source supplies bytes and a pure ``parse`` function; everything
after that is shared, so the portal capture and a manual CSV can never drift apart.

Transactions (docs/ARCHITECTURE.md section 5):
1. open the run and store the raw bytes, committed first, so the input is never lost if a later step fails;
2. parse, normalise, validate, quarantine and load in one transaction (all or nothing);
3. close the run as ``success`` or ``failed`` with an error summary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, text

from sie.config import Settings
from sie.pipeline.load import LoadError, load_placed, write_quarantine
from sie.pipeline.models import NormalisedResult, Rejection
from sie.pipeline.normalise import load_reference_index, normalise
from sie.pipeline.raw import store_raw
from sie.pipeline.validate import validate
from sie.reference import normalise_key
from sie.sources.base import ParsedResult

log = logging.getLogger("sie.pipeline.runner")

ERROR_SUMMARY_CHARS = 500
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
    raw_version_id: int
    raw_outcome: str  # new_version | unchanged | reverted
    events: int = 0
    placings: dict[str, int] = field(default_factory=dict)
    rows_seen: int = 0
    rows_quarantined: int = 0
    duplicates_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def run_ingest(
    engine: Engine, src: SourceInput, settings: Settings, now: datetime | None = None
) -> IngestResult:
    now = now or datetime.now(UTC)
    raw_dir = _raw_dir(settings)

    with engine.begin() as conn:
        competition_id = conn.execute(
            text("SELECT id FROM competitions WHERE code = :c"), {"c": settings.competition_id}
        ).scalar_one_or_none()
        if competition_id is None:
            raise LoadError(
                f"competition {settings.competition_id!r} missing: run `sie seed-reference` first"
            )
        run_id = conn.execute(
            text(
                """INSERT INTO ingest_runs (competition_id, started_at, status, source)
                   VALUES (:c, :n, 'running', :s) RETURNING id"""
            ),
            {"c": competition_id, "n": now, "s": src.source},
        ).scalar_one()
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
    log.info(
        "raw stored",
        extra={"run_id": run_id, "source": src.source, "url": src.url, "outcome": raw.outcome},
    )
    result = IngestResult(run_id, "running", raw.version_id, raw.outcome)

    try:
        with engine.begin() as conn:
            parsed = src.parse(src.content)
            ref = load_reference_index(conn, settings.reference_dir)
            competition = ref.competitions[normalise_key(settings.competition_id)]
            normalised: list[NormalisedResult] = []
            rejections: list[Rejection] = []
            for row in parsed:
                outcome = normalise(row, ref, competition)
                (rejections if isinstance(outcome, Rejection) else normalised).append(outcome)  # type: ignore[arg-type]
            checked = validate(normalised, ref, competition)
            rejections += checked.rejections
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
            )
            result.events = summary.events
            result.placings = summary.placings
            result.rows_seen = len(parsed)
            result.rows_quarantined = len(rejections)
            result.duplicates_skipped = checked.duplicates_skipped
            result.warnings = checked.warnings
        result.status = "success"
    except Exception as exc:  # the raw input and the run record are already safe; say what failed
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"[:ERROR_SUMMARY_CHARS]
        log.error("run failed", extra={"run_id": run_id, "error": result.error})

    _finish(engine, result, now=datetime.now(UTC))
    log.info(
        "run finished",
        extra={
            "run_id": run_id,
            "status": result.status,
            "rows_seen": result.rows_seen,
            "rows_quarantined": result.rows_quarantined,
            "placings": result.placings,
        },
    )
    return result


def _finish(engine: Engine, result: IngestResult, *, now: datetime) -> None:
    loaded = sum(result.placings.values())
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE ingest_runs
                   SET finished_at = :n, status = :st, docs_fetched = 1, docs_changed = :ch,
                       rows_loaded = :ld, rows_quarantined = :q, error_summary = :e
                   WHERE id = :r"""
            ),
            {
                "n": now,
                "st": result.status,
                "ch": 0 if result.raw_outcome == "unchanged" else 1,
                "ld": loaded if result.status == "success" else 0,
                "q": result.rows_quarantined if result.status == "success" else 0,
                "e": result.error,
                "r": result.run_id,
            },
        )


def _raw_dir(settings: Settings) -> Path:
    return settings.data_dir / "raw"
