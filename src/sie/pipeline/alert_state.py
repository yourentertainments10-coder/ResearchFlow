"""Alert de-duplication: send an alert when its condition appears, not on every check.

Alerts are level-triggered (``alerts.py``): one is produced for as long as its condition holds. This
module remembers which ones were already delivered, so a channel hears about each condition once,
again only if it clears and comes back, and optionally as a reminder.

Rules (``plan_notifications``, pure):
* an alert is sent if it is new, or it returned after being resolved;
* an alert whose last delivery *failed* stays unsent and is retried on the next check, so a broken
  channel never silently swallows an alert;
* an alert still open is suppressed, unless ``renotify_after`` is set and has passed;
* a condition that no longer holds is marked resolved.

State is a small JSON file written atomically (``FileAlertState``). A file store is deliberate: the
database store is a possible later migration (see ADR-030). One
writer at a time is assumed, which holds because ``sie health`` runs after the scheduled run under the
same concurrency group. A corrupt file is set aside and treated as empty, so the worst case is a
repeated alert, never a missing one. Hosted runners have no persistent disk: keep the file with the
runner's cache or run health where ``DATA_DIR`` persists.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sie.pipeline.alerts import Alert, sort_alerts
from sie.pipeline.notify import DeliveryResult, Notifier, deliver

log = logging.getLogger("sie.alerts")
STATE_VERSION = 1


@dataclass
class AlertRecord:
    key: str
    kind: str
    severity: str
    competition: str
    source: str
    first_seen_at: str
    last_seen_at: str
    last_notified_at: str | None = None
    active: bool = True
    resolved_at: str | None = None


class AlertStateStore(Protocol):
    def load(self) -> dict[str, AlertRecord]: ...

    def save(self, records: dict[str, AlertRecord]) -> None: ...


class MemoryAlertState:
    def __init__(self) -> None:
        self.records: dict[str, AlertRecord] = {}

    def load(self) -> dict[str, AlertRecord]:
        return {k: AlertRecord(**asdict(v)) for k, v in self.records.items()}

    def save(self, records: dict[str, AlertRecord]) -> None:
        self.records = {k: AlertRecord(**asdict(v)) for k, v in records.items()}


class FileAlertState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, AlertRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != STATE_VERSION:
                raise ValueError(f"unknown state version {data.get('version')!r}")
            return {k: AlertRecord(**v) for k, v in data["alerts"].items()}
        except (ValueError, TypeError, KeyError, OSError) as exc:
            aside = self.path.with_suffix(self.path.suffix + ".corrupt")
            log.error("alert state unreadable, starting empty", extra={"error": str(exc)})
            with contextlib.suppress(OSError):
                os.replace(self.path, aside)
            return {}

    def save(self, records: dict[str, AlertRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "alerts": {k: asdict(records[k]) for k in sorted(records)},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a reader sees the old file or the new one, never half


@dataclass(frozen=True)
class NotificationPlan:
    to_send: list[Alert]
    suppressed: list[str]  # keys still open and already delivered
    resolved: list[str]  # keys that were active and no longer hold


@dataclass
class NotificationOutcome:
    sent: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "sent": self.sent,
            "suppressed": self.suppressed,
            "resolved": self.resolved,
            "failed": self.failed,
        }


def plan_notifications(
    state: dict[str, AlertRecord],
    current: Iterable[Alert],
    now: datetime,
    renotify_after: timedelta | None = None,
) -> NotificationPlan:
    current = sort_alerts(current)
    to_send, suppressed = [], []
    for alert in current:
        rec = state.get(alert.key)
        if (
            rec is None
            or not rec.active
            or rec.last_notified_at is None
            or (
                renotify_after
                and now - datetime.fromisoformat(rec.last_notified_at) >= renotify_after
            )
        ):
            to_send.append(alert)
        else:
            suppressed.append(alert.key)
    keys = {a.key for a in current}
    resolved = sorted(k for k, r in state.items() if r.active and k not in keys)
    return NotificationPlan(to_send, suppressed, resolved)


def notify_changes(
    store: AlertStateStore,
    notifier: Notifier,
    alerts: Iterable[Alert],
    now: datetime,
    renotify_after: timedelta | None = None,
) -> NotificationOutcome:
    """Deliver what is new, update the state, report. Never raises for a delivery problem."""
    alerts = sort_alerts(alerts)
    state = store.load()
    plan = plan_notifications(state, alerts, now, renotify_after)
    result: DeliveryResult = deliver(notifier, plan.to_send)
    stamp = now.isoformat()

    for alert in alerts:
        rec = state.get(alert.key)
        if rec is None or not rec.active:  # a new condition, or one that came back
            rec = AlertRecord(
                alert.key, str(alert.kind), str(alert.severity), alert.competition, alert.source,
                first_seen_at=stamp, last_seen_at=stamp,
            )  # fmt: skip
            state[alert.key] = rec
        rec.last_seen_at = stamp
        rec.severity = str(alert.severity)
        if alert.key in result.delivered:
            rec.last_notified_at = stamp
    for key in plan.resolved:
        state[key].active = False
        state[key].resolved_at = stamp
    store.save(state)
    return NotificationOutcome(
        sent=sorted(result.delivered),
        suppressed=plan.suppressed,
        resolved=plan.resolved,
        failed=dict(sorted(result.failed.items())),
    )
