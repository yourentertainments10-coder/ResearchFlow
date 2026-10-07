"""Source health: freshness (``freshness.py``) plus what an operator needs to act on it.

Nothing here re-derives freshness: the four statuses (fresh, stale, failing, never_succeeded) come
from ``source_freshness`` and keep their meaning. Health adds the last failed run, how many runs
failed in a row since the last success, and runs stuck in ``running``. Read-only.

Thresholds come from existing configuration: ``FRESHNESS_THRESHOLD_MINUTES`` for staleness, the fetch
attempt limit of the retry contract for repeated failures, and the scheduler's stuck-run age.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, text

from sie.pipeline.alerts import (
    REPEATED_FAILURE_THRESHOLD,
    Alert,
    evaluate_alerts,
    sort_alerts,
)
from sie.pipeline.failure import parse_error_summary
from sie.pipeline.freshness import SourceFreshness, source_freshness
from sie.pipeline.observe import stuck_runs
from sie.pipeline.scheduler import DEFAULT_STUCK_AFTER


@dataclass(frozen=True)
class RunRef:
    run_id: int
    finished_at: datetime | None
    failure_category: str | None = None  # only for failed runs; None for 'abandoned' stuck runs
    error_detail: str | None = None


@dataclass(frozen=True)
class SourceHealth:
    competition: str
    source: str
    freshness: SourceFreshness
    last_success: RunRef | None
    last_failure: RunRef | None
    consecutive_failures: (
        int  # failed runs since the last success (all failed runs if none succeeded)
    )
    stuck_run_ids: list[int] = field(default_factory=list)
    stuck_after_seconds: int = 0
    repeated_failure_threshold: int = REPEATED_FAILURE_THRESHOLD

    @property
    def status(self) -> str:
        return str(self.freshness.status)

    def to_dict(self) -> dict[str, Any]:
        fr = self.freshness.to_dict()
        return {
            "competition": self.competition,
            "source": self.source,
            "status": self.status,
            "checked_at": fr["checked_at"],
            "last_success": _run(self.last_success),
            "last_failure": _run(self.last_failure),
            "consecutive_failures": self.consecutive_failures,
            "stuck_run_ids": list(self.stuck_run_ids),
            "last_change_at": fr["last_change_at"],
            "fingerprint": fr["fingerprint"],
            "raw_artifacts": fr["artifacts"],
            "thresholds": {
                "max_age_seconds": fr["max_age_seconds"],
                "repeated_failures": self.repeated_failure_threshold,
                "stuck_after_seconds": self.stuck_after_seconds,
            },
        }


@dataclass(frozen=True)
class HealthReport:
    checked_at: datetime
    competition: str
    sources: list[SourceHealth]
    alerts: list[Alert]

    @property
    def healthy(self) -> bool:
        return not self.alerts

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "competition": self.competition,
            "healthy": self.healthy,
            "sources": [s.to_dict() for s in self.sources],
            "alerts": [a.to_dict() for a in self.alerts],
        }


def _run(ref: RunRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {
        "run_id": ref.run_id,
        "finished_at": ref.finished_at.isoformat() if ref.finished_at else None,
        "failure_category": ref.failure_category,
        "error_detail": ref.error_detail,
    }


def discover_sources(conn: Connection, competition: str) -> list[str]:
    """Sources that have at least one run for the competition, sorted."""
    return sorted(
        conn.execute(
            text(
                """SELECT DISTINCT r.source FROM ingest_runs r
                   JOIN competitions c ON c.id = r.competition_id
                   WHERE c.code = :c AND r.source IS NOT NULL"""
            ),
            {"c": competition},
        ).scalars()
    )


def source_health(
    conn: Connection,
    competition: str,
    source: str,
    now: datetime,
    *,
    max_age: timedelta,
    stuck_after: timedelta = DEFAULT_STUCK_AFTER,
    repeated_failures: int = REPEATED_FAILURE_THRESHOLD,
) -> SourceHealth:
    fresh = source_freshness(conn, competition, source, now, max_age)
    base = {"c": competition, "s": source}
    success = conn.execute(
        text(
            """SELECT r.id, r.finished_at FROM ingest_runs r JOIN competitions c ON c.id = r.competition_id
               WHERE c.code = :c AND r.source = :s AND r.status = 'success'
               ORDER BY r.id DESC LIMIT 1"""
        ),
        base,
    ).one_or_none()
    failure = conn.execute(
        text(
            """SELECT r.id, r.finished_at, r.error_summary FROM ingest_runs r
               JOIN competitions c ON c.id = r.competition_id
               WHERE c.code = :c AND r.source = :s AND r.status = 'failed'
               ORDER BY r.id DESC LIMIT 1"""
        ),
        base,
    ).one_or_none()
    failed_in_a_row = conn.execute(
        text(
            """SELECT count(*) FROM ingest_runs r JOIN competitions c ON c.id = r.competition_id
               WHERE c.code = :c AND r.source = :s AND r.status = 'failed' AND r.id > :after"""
        ),
        {**base, "after": success.id if success else 0},
    ).scalar_one()
    category, detail = parse_error_summary(failure.error_summary) if failure else (None, "")
    return SourceHealth(
        competition=competition,
        source=source,
        freshness=fresh,
        last_success=RunRef(success.id, success.finished_at) if success else None,
        last_failure=(
            RunRef(
                failure.id, failure.finished_at, str(category) if category else None, detail or None
            )
            if failure
            else None
        ),
        consecutive_failures=failed_in_a_row,
        stuck_run_ids=_stuck_for(conn, competition, source, now, stuck_after),
        stuck_after_seconds=int(stuck_after.total_seconds()),
        repeated_failure_threshold=repeated_failures,
    )


def _stuck_for(
    conn: Connection, competition: str, source: str, now: datetime, older_than: timedelta
) -> list[int]:
    ids = set(stuck_runs(conn, now, older_than))  # reuse the foundation's definition of "stuck"
    mine = conn.execute(
        text(
            """SELECT r.id FROM ingest_runs r JOIN competitions c ON c.id = r.competition_id
               WHERE c.code = :c AND r.source = :s"""
        ),
        {"c": competition, "s": source},
    ).scalars()
    return sorted(ids & set(mine))


def check_health(
    conn: Connection,
    competition: str,
    sources: list[str],
    now: datetime,
    *,
    max_age: timedelta,
    stuck_after: timedelta = DEFAULT_STUCK_AFTER,
    repeated_failures: int = REPEATED_FAILURE_THRESHOLD,
) -> HealthReport:
    """Evaluate each source independently. The result does not depend on the order of ``sources``."""
    healths = [
        source_health(
            conn,
            competition,
            source,
            now,
            max_age=max_age,
            stuck_after=stuck_after,
            repeated_failures=repeated_failures,
        )
        for source in sorted(set(sources))
    ]
    alerts = sort_alerts(
        a for h in healths for a in evaluate_alerts(h, repeated_failures=repeated_failures)
    )
    return HealthReport(now, competition, healths, alerts)
