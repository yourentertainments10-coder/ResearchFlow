"""De-duplication state, the webhook channel and the needs_attention alert. No database."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_alert_contract import health

from sie.pipeline.alert_state import (
    FileAlertState,
    MemoryAlertState,
    notify_changes,
    plan_notifications,
)
from sie.pipeline.alerts import AlertKind, evaluate_alerts
from sie.pipeline.freshness import Freshness
from sie.pipeline.health import RunRef
from sie.pipeline.notify import DeliveryResult, RecordingNotifier, WebhookNotifier, deliver
from webhook_hook import Hook

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def stale_alerts(source="official"):
    return evaluate_alerts(health(Freshness.STALE, age=7200, source=source))


# --- needs_attention: a first failure that will not retry itself ----------------------------------------


@pytest.mark.parametrize(
    ("category", "alerted"),
    [
        ("parse_failure", True),
        ("validation_failure", True),
        ("raw_store_failure", True),
        ("fetch_failure", False),  # retried automatically: waits for repeated_failures
        ("load_failure", False),
        (None, False),  # an abandoned (stuck) run carries no category
    ],
)
def test_the_first_failure_alerts_only_when_the_contract_never_retries_it(category, alerted):
    h = health(Freshness.FAILING, failures=1, age=60, last_failure=RunRef(7, T0, category, "boom"))
    got = [a for a in evaluate_alerts(h) if a.kind == AlertKind.NEEDS_ATTENTION]
    assert bool(got) is alerted
    if alerted:
        assert (
            got[0].evidence["failure_category"] == category
            and got[0].evidence["last_error"] == "boom"
        )
        assert got[0].evidence["retry_policy"] in ("after_fix", "manual")


def test_needs_attention_clears_once_the_source_succeeds_again():
    healed = health(
        Freshness.FRESH, failures=0, age=10, last_failure=RunRef(7, T0, "parse_failure", "x")
    )
    assert evaluate_alerts(healed) == []


# --- de-duplication -------------------------------------------------------------------------------------


def test_an_alert_is_sent_once_then_suppressed_while_it_stays_open():
    state, notifier = MemoryAlertState(), RecordingNotifier()
    first = notify_changes(state, notifier, stale_alerts(), T0)
    again = notify_changes(state, notifier, stale_alerts(), T0 + timedelta(minutes=30))
    assert first.sent == ["asiad-2026:official:stale"] and first.suppressed == []
    assert again.sent == [] and again.suppressed == ["asiad-2026:official:stale"]
    assert len(notifier.sent) == 1
    rec = state.records["asiad-2026:official:stale"]
    assert rec.first_seen_at == T0.isoformat() and rec.last_seen_at != rec.first_seen_at


def test_a_resolved_alert_that_returns_is_sent_again():
    state, notifier = MemoryAlertState(), RecordingNotifier()
    notify_changes(state, notifier, stale_alerts(), T0)
    cleared = notify_changes(state, notifier, [], T0 + timedelta(hours=1))
    assert cleared.resolved == ["asiad-2026:official:stale"]
    assert state.records["asiad-2026:official:stale"].active is False
    back = notify_changes(state, notifier, stale_alerts(), T0 + timedelta(hours=2))
    assert back.sent == ["asiad-2026:official:stale"] and len(notifier.sent) == 2
    assert (
        state.records["asiad-2026:official:stale"].first_seen_at
        == (T0 + timedelta(hours=2)).isoformat()
    )


def test_a_failed_delivery_is_retried_on_the_next_check_not_forgotten():
    class Down:
        def send(self, alerts):
            raise ConnectionError("channel down")

    state = MemoryAlertState()
    lost = notify_changes(state, Down(), stale_alerts(), T0)
    assert lost.sent == [] and set(lost.failed) == {"asiad-2026:official:stale"}
    assert state.records["asiad-2026:official:stale"].last_notified_at is None
    notifier = RecordingNotifier()
    retried = notify_changes(state, notifier, stale_alerts(), T0 + timedelta(minutes=30))
    assert retried.sent == ["asiad-2026:official:stale"] and len(notifier.sent) == 1


def test_reminders_only_when_configured():
    state, notifier = MemoryAlertState(), RecordingNotifier()
    notify_changes(state, notifier, stale_alerts(), T0, renotify_after=timedelta(hours=6))
    soon = notify_changes(
        state, notifier, stale_alerts(), T0 + timedelta(hours=1), timedelta(hours=6)
    )
    later = notify_changes(
        state, notifier, stale_alerts(), T0 + timedelta(hours=7), timedelta(hours=6)
    )
    assert soon.sent == [] and later.sent == ["asiad-2026:official:stale"]
    never = notify_changes(state, notifier, stale_alerts(), T0 + timedelta(days=30))
    assert never.sent == []  # no reminder threshold: sent once


def test_sources_are_deduplicated_independently():
    state, notifier = MemoryAlertState(), RecordingNotifier()
    notify_changes(state, notifier, stale_alerts("a"), T0)
    second = notify_changes(state, notifier, stale_alerts("a") + stale_alerts("b"), T0)
    assert second.sent == ["asiad-2026:b:stale"] and second.suppressed == ["asiad-2026:a:stale"]


def test_the_plan_is_pure_and_changes_no_state():
    state = MemoryAlertState()
    plan = plan_notifications(state.load(), stale_alerts(), T0)
    assert [a.key for a in plan.to_send] == ["asiad-2026:official:stale"] and state.records == {}


def test_the_file_store_survives_a_restart_and_sets_a_corrupt_file_aside(tmp_path):
    path = tmp_path / "state" / "alerts.json"
    notify_changes(FileAlertState(path), RecordingNotifier(), stale_alerts(), T0)
    assert json.loads(path.read_text())["version"] == 1
    again = notify_changes(FileAlertState(path), RecordingNotifier(), stale_alerts(), T0)
    assert again.sent == [] and again.suppressed  # a new process still remembers

    path.write_text("{ not json")
    after = notify_changes(FileAlertState(path), RecordingNotifier(), stale_alerts(), T0)
    assert after.sent == [
        "asiad-2026:official:stale"
    ]  # worst case: a repeated alert, never a missing one
    assert (tmp_path / "state" / "alerts.json.corrupt").exists()


# --- webhook channel ------------------------------------------------------------------------------------


@pytest.fixture()
def hook():
    h = Hook()
    yield h
    h.close()


def test_the_webhook_posts_one_readable_json_batch(hook):
    batch = stale_alerts() + stale_alerts("manual")
    result = deliver(WebhookNotifier(hook.url), batch)
    assert result.ok and len(result.delivered) == 2
    ((ctype, body),) = hook.received
    assert ctype == "application/json" and len(body["alerts"]) == 2
    assert "official is stale" in body["text"] and body["text"] == body["content"]
    assert body["alerts"][0]["key"] == "asiad-2026:official:stale"


def test_a_webhook_error_status_or_unreachable_host_fails_every_alert():
    bad = Hook(status=500)
    try:
        result = deliver(WebhookNotifier(bad.url), stale_alerts())
    finally:
        bad.close()
    assert not result.ok and "HTTP 500" in next(iter(result.failed.values()))
    unreachable = deliver(WebhookNotifier("http://127.0.0.1:1/x", timeout=1), stale_alerts())
    assert not unreachable.ok


@pytest.mark.parametrize(
    "url", ["http://example.org/hook", "ftp://x/y", "file:///etc/passwd", "hook"]
)
def test_the_webhook_refuses_insecure_or_odd_urls(url):
    with pytest.raises(ValueError, match="https"):
        WebhookNotifier(url)


def test_https_urls_are_accepted():
    assert WebhookNotifier("https://hooks.example.org/abc").url.startswith("https://")


def test_an_unreported_alert_in_a_lossy_channel_is_not_marked_delivered():
    class Lossy:
        def send(self, alerts):
            return DeliveryResult(delivered=[alerts[0].key])

    state = MemoryAlertState()
    both = stale_alerts() + stale_alerts("manual")
    out = notify_changes(state, Lossy(), both, T0)
    assert len(out.sent) == 1 and len(out.failed) == 1
    assert [r.last_notified_at is not None for _, r in sorted(state.records.items())].count(
        True
    ) == 1
