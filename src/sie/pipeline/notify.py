"""Delivering alerts, without tying the pipeline to any provider.

A ``Notifier`` takes a batch of alerts and reports what happened. Concrete channels (a webhook for
``NOTIFY_WEBHOOK_URL``, email, chat) are small adapters added later; none is built here. ``deliver``
is the only function callers use: it never raises, so a broken channel cannot hide the health result
or crash the job, and it tells the caller exactly which alerts were not delivered.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

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


class WebhookNotifier:
    """POSTs the alerts as JSON to a webhook (``NOTIFY_WEBHOOK_URL``). One request per batch.

    The body has ``alerts`` (the full alert objects) and a plain ``text`` and ``content`` summary, so
    Slack-style and Discord-style webhooks both show something readable. HTTPS only, except
    localhost for development. A non-2xx answer or a network error fails every alert in the batch,
    which is reported by ``deliver`` and retried by the de-duplication state on the next check.
    """

    LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in self.LOCAL_HOSTS
        ):
            raise ValueError("webhook URL must use https (http only for localhost)")
        self.url, self.timeout = url, timeout

    def send(self, alerts: Sequence[Alert]) -> DeliveryResult:
        lines = [f"[{a.severity}] {a.message}" for a in alerts]
        body = json.dumps(
            {
                "text": "\n".join(lines),
                "content": "\n".join(lines),
                "alerts": [a.to_dict() for a in alerts],
            }
        ).encode()
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        keys = [a.key for a in alerts]
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                if 200 <= response.status < 300:
                    return DeliveryResult(delivered=keys)
                reason = f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:200]
        return DeliveryResult(failed=dict.fromkeys(keys, reason))


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
