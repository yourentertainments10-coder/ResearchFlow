"""The alert contract: which conditions of a source's health raise an alert, and what an alert holds.

Pure and deterministic: the same ``SourceHealth`` always yields the same alerts, in the same order.
It decides *whether* something needs attention; delivering it is ``notify.py``'s job, and remembering
what was already delivered is the delivery layer's job (alerts are level-triggered: one is produced
for as long as its condition holds, and ``Alert.key`` is the stable identity for de-duplication).

Conditions and the numbers behind them (nothing here is a new threshold):

* ``never_succeeded``: the freshness status is ``never_succeeded`` (including a source that has not run at all).
* ``stale``: the freshness status is ``stale``; the limit is ``FRESHNESS_THRESHOLD_MINUTES``.
* ``repeated_failures``: at least ``REPEATED_FAILURE_THRESHOLD`` failed runs since the last success. That
  threshold is the fetch attempt limit of the locked retry contract, so one scheduled run that exhausts
  its fetch retries is already a repeated failure. A single failure is not alerted: it may be retried.
* ``stuck_runs``: runs still ``running`` past the stuck threshold the scheduler already uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sie.pipeline.failure import FailureCategory, retry_rule

if TYPE_CHECKING:  # health.py imports this module; the type is only needed for annotations
    from sie.pipeline.health import SourceHealth

REPEATED_FAILURE_THRESHOLD = retry_rule(FailureCategory.FETCH).max_auto_attempts


class AlertKind(StrEnum):
    NEVER_SUCCEEDED = "never_succeeded"
    REPEATED_FAILURES = "repeated_failures"
    STALE = "stale"
    STUCK_RUNS = "stuck_runs"


class Severity(StrEnum):
    WARNING = "warning"  # needs a look soon
    CRITICAL = "critical"  # data is missing or the source is broken


SEVERITY = {
    AlertKind.NEVER_SUCCEEDED: Severity.CRITICAL,
    AlertKind.REPEATED_FAILURES: Severity.CRITICAL,
    AlertKind.STALE: Severity.WARNING,
    AlertKind.STUCK_RUNS: Severity.WARNING,
}
_ORDER = {kind: i for i, kind in enumerate(AlertKind)}


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    severity: Severity
    competition: str
    source: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity for de-duplication: the same condition on the same source is the same alert."""
        return f"{self.competition}:{self.source}:{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"], data["severity"], data["key"] = str(self.kind), str(self.severity), self.key
        return data


def evaluate_alerts(
    health: SourceHealth, *, repeated_failures: int = REPEATED_FAILURE_THRESHOLD
) -> list[Alert]:
    """The alerts for one source. A healthy source returns an empty list."""
    out: list[Alert] = []
    where = (health.competition, health.source)

    def add(kind: AlertKind, message: str, **evidence: Any) -> None:
        out.append(Alert(kind, SEVERITY[kind], *where, message, evidence))

    status = str(health.freshness.status)
    if status == "never_succeeded":
        attempts = health.consecutive_failures
        detail = f"after {attempts} failed run(s)" if attempts else "no run has been recorded"
        add(
            AlertKind.NEVER_SUCCEEDED,
            f"{health.source} has never succeeded: {detail}",
            failed_runs=attempts,
            last_failed_run_id=health.last_failure.run_id if health.last_failure else None,
        )
    elif status == "stale":
        add(
            AlertKind.STALE,
            f"{health.source} is stale: last success {int(health.freshness.age_seconds or 0)}s ago, "
            f"limit {health.freshness.max_age_seconds}s",
            age_seconds=int(health.freshness.age_seconds or 0),
            max_age_seconds=health.freshness.max_age_seconds,
            last_success_at=_iso(health.freshness.last_success_at),
        )
    if health.consecutive_failures >= repeated_failures:
        failure = health.last_failure
        add(
            AlertKind.REPEATED_FAILURES,
            f"{health.source} has failed {health.consecutive_failures} times in a row",
            consecutive_failures=health.consecutive_failures,
            threshold=repeated_failures,
            last_failed_run_id=failure.run_id if failure else None,
            last_failure_category=failure.failure_category if failure else None,
            last_error=failure.error_detail if failure else None,
        )
    if health.stuck_run_ids:
        add(
            AlertKind.STUCK_RUNS,
            f"{health.source} has {len(health.stuck_run_ids)} run(s) stuck in 'running'",
            run_ids=list(health.stuck_run_ids),
            stuck_after_seconds=health.stuck_after_seconds,
        )
    return sorted(out, key=lambda a: _ORDER[a.kind])


def sort_alerts(alerts: Iterable[Alert]) -> list[Alert]:
    return sorted(alerts, key=lambda a: (a.competition, a.source, _ORDER[a.kind]))


def partition_alerts(
    current: Iterable[Alert], previously_active: set[str]
) -> tuple[list[Alert], list[Alert], list[str]]:
    """Split into (new, still active, resolved keys) against the keys active at the last check."""
    current = sort_alerts(current)
    keys = {a.key for a in current}
    new = [a for a in current if a.key not in previously_active]
    still = [a for a in current if a.key in previously_active]
    return new, still, sorted(previously_active - keys)


def _iso(value) -> str | None:
    return value.isoformat() if value else None
