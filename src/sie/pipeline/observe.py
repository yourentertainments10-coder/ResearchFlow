"""Structured run summary and a deterministic run outcome, read back from ``ingest_runs``.

The database row is the single source of truth: the CLI, the logs and tests all build their summary
with ``load_run_summary`` so they cannot disagree. Nothing here changes data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, text

from sie.pipeline.failure import RetryPolicy, parse_error_summary, retry_rule


class RunOutcome(StrEnum):
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED_WITH_QUARANTINE = "succeeded_with_quarantine"  # loaded, but some rows need attention
    UNCHANGED = "unchanged"  # succeeded and the source bytes were identical to the last version
    SUCCEEDED = "succeeded"


def derive_outcome(
    status: str, docs_changed: int | None, rows_quarantined: int | None
) -> RunOutcome:
    """A pure function of what the run recorded. Same inputs, same outcome, always."""
    if status == "running":
        return RunOutcome.RUNNING
    if status != "success":
        return RunOutcome.FAILED
    if (rows_quarantined or 0) > 0:
        return RunOutcome.SUCCEEDED_WITH_QUARANTINE
    if docs_changed == 0:
        return RunOutcome.UNCHANGED
    return RunOutcome.SUCCEEDED


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    competition: str
    source: str | None
    status: str  # the stored lifecycle: running | success | failed
    outcome: RunOutcome
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    docs_fetched: int | None
    docs_changed: int | None
    rows_loaded: int | None
    rows_quarantined: int | None
    quarantine_by_reason: dict[str, int] = field(default_factory=dict)
    failure_category: str | None = None
    retry_policy: str | None = None
    max_auto_attempts: int | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe: datetimes as ISO strings, enums as their values."""
        data = asdict(self)
        for key in ("started_at", "finished_at"):
            data[key] = data[key].isoformat() if data[key] else None
        data["outcome"] = str(self.outcome)
        return data


def load_run_summary(conn: Connection, run_id: int) -> RunSummary:
    row = conn.execute(
        text(
            """SELECT r.id, c.code AS competition, r.source, r.status, r.started_at, r.finished_at,
                      r.docs_fetched, r.docs_changed, r.rows_loaded, r.rows_quarantined,
                      r.error_summary
               FROM ingest_runs r JOIN competitions c ON c.id = r.competition_id
               WHERE r.id = :r"""
        ),
        {"r": run_id},
    ).one_or_none()
    if row is None:
        raise LookupError(f"no ingest run {run_id}")
    # Quarantine rows belong to the raw version, so a re-run of the same bytes still sees them.
    by_reason = {
        reason: n
        for reason, n in conn.execute(
            text(
                """SELECT q.reason, count(*) FROM quarantine q
                   WHERE NOT q.resolved AND q.raw_version_id IN
                         (SELECT version_id FROM raw_fetches WHERE run_id = :r AND version_id IS NOT NULL)
                   GROUP BY q.reason ORDER BY q.reason"""
            ),
            {"r": run_id},
        )
    }
    category, detail = parse_error_summary(row.error_summary)
    rule = retry_rule(category) if category else None
    return RunSummary(
        run_id=row.id,
        competition=row.competition,
        source=row.source,
        status=row.status,
        outcome=derive_outcome(row.status, row.docs_changed, row.rows_quarantined),
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_seconds=(
            (row.finished_at - row.started_at).total_seconds() if row.finished_at else None
        ),
        docs_fetched=row.docs_fetched,
        docs_changed=row.docs_changed,
        rows_loaded=row.rows_loaded,
        rows_quarantined=row.rows_quarantined,
        quarantine_by_reason=by_reason,
        failure_category=str(category) if category else None,
        retry_policy=str(rule.policy) if rule else None,
        max_auto_attempts=rule.max_auto_attempts if rule else None,
        error_detail=detail or None,
    )


def stuck_runs(conn: Connection, now: datetime, older_than: timedelta) -> list[int]:
    """Runs still marked ``running`` long after they started: the process died without closing them."""
    return list(
        conn.execute(
            text(
                "SELECT id FROM ingest_runs WHERE status = 'running' AND started_at < :t ORDER BY id"
            ),
            {"t": now - older_than},
        ).scalars()
    )


__all__ = [
    "RetryPolicy",
    "RunOutcome",
    "RunSummary",
    "derive_outcome",
    "load_run_summary",
    "stuck_runs",
]
