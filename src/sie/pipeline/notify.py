"""Delivering alerts, without tying the pipeline to any provider.

A ``Notifier`` takes a batch of alerts and reports what happened. Concrete channels (a webhook for
``NOTIFY_WEBHOOK_URL``, email, chat) are small adapters added later; none is built here. ``deliver``
is the only function callers use: it never raises, so a broken channel cannot hide the health result
or crash the job, and it tells the caller exactly which alerts were not delivered.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sie.pipeline.alerts import Alert

log = logging.getLogger("sie.alerts")


@dataclass
class DeliveryResult:
    delivered: list[str] = field(default_factory=list)  # alert keys
    failed: dict[str, str] = field(default_factory=dict)  # alert key -> reason

    @property
    def ok(self) -> bool:
        return not self.failed


class Notifier(Protocol):
    def send(self, alerts: Sequence[Alert]) -> DeliveryResult:
        """Deliver the alerts. Report per-alert failures in the result; may also raise."""
        ...


class LogNotifier:
    """Writes each alert to the application log. The default, and the only built-in channel."""

    def send(self, alerts: Sequence[Alert]) -> DeliveryResult:
        result = DeliveryResult()
        for alert in alerts:
            level = logging.ERROR if alert.severity == "critical" else logging.WARNING
            log.log(level, alert.message, extra={"alert": alert.to_dict()})
            result.delivered.append(alert.key)
        return result


class RecordingNotifier:
    """Keeps alerts in memory. For tests and for callers that want to inspect what would be sent."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alerts: Sequence[Alert]) -> DeliveryResult:
        self.sent.extend(alerts)
        return DeliveryResult(delivered=[a.key for a in alerts])


def deliver(notifier: Notifier, alerts: Sequence[Alert]) -> DeliveryResult:
    """Send ``alerts`` through ``notifier``. Nothing is sent for an empty batch. Never raises."""
    if not alerts:
        return DeliveryResult()
    try:
        result = notifier.send(alerts)
    except Exception as exc:  # a failing channel must not break the job; say which alerts were lost
        reason = f"{type(exc).__name__}: {exc}"[:200]
        log.error("alert delivery failed", extra={"error": reason})
        return DeliveryResult(failed={a.key: reason for a in alerts})
    missing = {a.key for a in alerts} - set(result.delivered) - set(result.failed)
    for key in sorted(missing):  # a notifier that silently drops an alert is reported as a failure
        result.failed[key] = "not reported as delivered"
    return result
