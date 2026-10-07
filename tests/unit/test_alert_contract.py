"""The alert contract and the notification abstraction, without a database."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from sie.pipeline.alerts import (
    REPEATED_FAILURE_THRESHOLD,
    Alert,
    AlertKind,
    Severity,
    evaluate_alerts,
    partition_alerts,
)
from sie.pipeline.failure import FailureCategory, retry_rule
from sie.pipeline.freshness import Freshness, SourceFreshness
from sie.pipeline.health import RunRef, SourceHealth
from sie.pipeline.notify import DeliveryResult, LogNotifier, RecordingNotifier, deliver

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def health(
    status, failures=0, stuck=(), source="official", age=None, last_failure=None
) -> SourceHealth:
    last_success = NOW - timedelta(seconds=age) if age is not None else None
    fresh = SourceFreshness(
        competition="asiad-2026", source=source, status=status, checked_at=NOW, max_age_seconds=1800,
        last_attempt_at=NOW, last_attempt_status="failed" if failures else "success",
        last_success_at=last_success, age_seconds=age, last_change_at=None, fingerprint=None,
    )  # fmt: skip
    return SourceHealth(
        "asiad-2026", source, fresh,
        last_success=RunRef(1, last_success) if last_success else None,
        last_failure=last_failure or (RunRef(9, NOW, "fetch_failure", "HTTP 503") if failures else None),
        consecutive_failures=failures, stuck_run_ids=list(stuck), stuck_after_seconds=3600,
    )  # fmt: skip


def kinds(h):
    return [a.kind for a in evaluate_alerts(h)]


def test_the_repeated_failure_threshold_is_the_fetch_attempt_limit_of_the_retry_contract():
    assert REPEATED_FAILURE_THRESHOLD == retry_rule(FailureCategory.FETCH).max_auto_attempts == 3


def test_a_healthy_source_raises_no_alert():
    assert kinds(health(Freshness.FRESH, age=60)) == []


def test_a_stale_source_alerts_with_the_age_and_the_limit():
    (alert,) = evaluate_alerts(health(Freshness.STALE, age=7200))
    assert alert.kind == AlertKind.STALE and alert.severity == Severity.WARNING
    assert alert.evidence["age_seconds"] == 7200 and alert.evidence["max_age_seconds"] == 1800


def test_a_source_that_never_ran_alerts_differently_from_one_that_only_failed():
    (none,) = evaluate_alerts(health(Freshness.NEVER_SUCCEEDED))
    assert none.kind == AlertKind.NEVER_SUCCEEDED and "no run has been recorded" in none.message
    both = evaluate_alerts(health(Freshness.NEVER_SUCCEEDED, failures=3))
    assert [a.kind for a in both] == [AlertKind.NEVER_SUCCEEDED, AlertKind.REPEATED_FAILURES]
    assert "after 3 failed run(s)" in both[0].message


@pytest.mark.parametrize(("failures", "alerted"), [(1, False), (2, False), (3, True), (7, True)])
def test_repeated_failures_alert_only_from_the_threshold(failures, alerted):
    got = kinds(health(Freshness.FAILING, failures=failures, age=60))
    assert (AlertKind.REPEATED_FAILURES in got) is alerted
    assert AlertKind.STALE not in got  # failing is its own state, not staleness


def test_a_repeated_failure_alert_carries_the_evidence_to_act_on():
    (alert,) = evaluate_alerts(health(Freshness.FAILING, failures=3, age=60))
    assert alert.severity == Severity.CRITICAL
    assert alert.evidence == {
        "consecutive_failures": 3, "threshold": 3, "last_failed_run_id": 9,
        "last_failure_category": "fetch_failure", "last_error": "HTTP 503",
    }  # fmt: skip


def test_stuck_runs_alert_independently_of_freshness():
    (alert,) = evaluate_alerts(health(Freshness.FRESH, age=60, stuck=[4, 5]))
    assert alert.kind == AlertKind.STUCK_RUNS and alert.evidence["run_ids"] == [4, 5]


def test_alerts_are_deterministic_and_ordered_by_kind():
    h = health(Freshness.STALE, failures=3, stuck=[2], age=9000)
    first, second = evaluate_alerts(h), evaluate_alerts(h)
    assert [a.to_dict() for a in first] == [a.to_dict() for a in second]
    assert [a.kind for a in first] == [
        AlertKind.REPEATED_FAILURES,
        AlertKind.STALE,
        AlertKind.STUCK_RUNS,
    ]


def test_the_key_identifies_the_condition_not_the_moment():
    a = evaluate_alerts(health(Freshness.STALE, age=7200))[0]
    b = evaluate_alerts(health(Freshness.STALE, age=9999))[0]
    assert a.key == b.key == "asiad-2026:official:stale"


def test_partitioning_against_the_previous_check():
    stale = evaluate_alerts(health(Freshness.STALE, age=7200))
    new, still, resolved = partition_alerts(stale, set())
    assert [a.key for a in new] == [stale[0].key] and still == [] and resolved == []
    new, still, resolved = partition_alerts(stale, {stale[0].key, "asiad-2026:official:stuck_runs"})
    assert new == [] and len(still) == 1 and resolved == ["asiad-2026:official:stuck_runs"]


# --- notification abstraction ---------------------------------------------------------------------------


def alerts_for(*statuses) -> list[Alert]:
    return [a for s in statuses for a in evaluate_alerts(health(s, age=7200, source=f"s{id(s)}"))]


def test_a_recording_notifier_receives_exactly_the_alerts_and_reports_them_delivered():
    notifier, batch = RecordingNotifier(), evaluate_alerts(health(Freshness.STALE, age=7200))
    result = deliver(notifier, batch)
    assert notifier.sent == batch and result.ok and result.delivered == [batch[0].key]


def test_nothing_is_sent_for_an_empty_batch():
    notifier = RecordingNotifier()
    assert deliver(notifier, []).ok and notifier.sent == []


def test_a_raising_notifier_never_raises_and_every_alert_is_reported_lost():
    class Broken:
        def send(self, alerts):
            raise ConnectionError("webhook down")

    batch = evaluate_alerts(health(Freshness.STALE, failures=3, age=7200))
    result = deliver(Broken(), batch)
    assert not result.ok and set(result.failed) == {a.key for a in batch}
    assert "ConnectionError: webhook down" in next(iter(result.failed.values()))


def test_a_notifier_that_silently_drops_an_alert_is_reported():
    class Lossy:
        def send(self, alerts):
            return DeliveryResult(delivered=[alerts[0].key])

    batch = evaluate_alerts(health(Freshness.STALE, failures=3, age=7200))
    result = deliver(Lossy(), batch)
    assert result.delivered == [batch[0].key] and result.failed == {
        batch[1].key: "not reported as delivered"
    }


def test_the_log_notifier_logs_critical_as_error_and_warning_as_warning(caplog):
    batch = evaluate_alerts(health(Freshness.STALE, failures=3, age=7200))
    with caplog.at_level(logging.WARNING, logger="sie.alerts"):
        result = LogNotifier().send(batch)
    assert result.ok
    levels = {r.levelname for r in caplog.records}
    assert levels == {"ERROR", "WARNING"}
