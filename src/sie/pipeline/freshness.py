"""Source freshness: when did we last get good data from a source, and is it still current?

Read-only. Built from ``ingest_runs``, ``raw_fetches`` and ``raw_versions``, so it adds no tables.
The threshold comes from ``Settings.freshness_threshold_minutes`` (passed in by the caller).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, text


class Freshness(StrEnum):
    NEVER_SUCCEEDED = "never_succeeded"  # no successful run yet
    FAILING = "failing"  # the latest finished attempt failed after the last success
    STALE = "stale"  # last success is older than the threshold
    FRESH = "fresh"


def classify_freshness(
    last_success_at: datetime | None,
    last_attempt_status: str | None,
    now: datetime,
    max_age: timedelta,
) -> Freshness:
    """Pure. ``last_attempt_status`` is the latest *finished* attempt ('success' or 'failed')."""
    if last_success_at is None:
        return Freshness.NEVER_SUCCEEDED
    if last_attempt_status == "failed":
        return Freshness.FAILING
    if now - last_success_at > max_age:
        return Freshness.STALE
    return Freshness.FRESH


@dataclass(frozen=True)
class RawArtifact:
    """Where the evidence behind the current data lives."""

    url: str
    version_id: int
    version_no: int
    sha256: str
    storage_backend: str
    path: str | None  # key under the raw directory for 'fs'; None for 'db' (see raw_blobs)
    size_bytes: int | None
    first_fetched_at: datetime


@dataclass(frozen=True)
class SourceFreshness:
    competition: str
    source: str
    status: Freshness
    checked_at: datetime
    max_age_seconds: int
    last_attempt_at: datetime | None
    last_attempt_status: str | None
    last_success_at: datetime | None
    age_seconds: float | None  # since the last success
    last_change_at: datetime | None  # when the source content last differed from before
    fingerprint: str | None  # one hash over the latest version of every document
    artifacts: list[RawArtifact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        for key in ("checked_at", "last_attempt_at", "last_success_at", "last_change_at"):
            data[key] = data[key].isoformat() if data[key] else None
        for art in data["artifacts"]:
            art["first_fetched_at"] = art["first_fetched_at"].isoformat()
        return data


def fingerprint(artifacts: list[RawArtifact]) -> str | None:
    """Order-independent: changes only if some document's content (or the document set) changes."""
    if not artifacts:
        return None
    lines = sorted(f"{a.url}\t{a.sha256}" for a in artifacts)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def source_freshness(
    conn: Connection, competition: str, source: str, now: datetime, max_age: timedelta
) -> SourceFreshness:
    runs = conn.execute(
        text(
            """SELECT r.status, r.started_at, r.finished_at
               FROM ingest_runs r JOIN competitions c ON c.id = r.competition_id
               WHERE c.code = :c AND r.source = :s AND r.status <> 'running'
               ORDER BY r.id DESC"""
        ),
        {"c": competition, "s": source},
    ).all()
    last_attempt = runs[0] if runs else None
    last_success = next((r for r in runs if r.status == "success"), None)

    docs = conn.execute(
        text(
            """SELECT d.url, v.id AS version_id, v.version_no, v.sha256, v.storage_backend, v.path,
                      v.size_bytes, v.first_fetched_at
               FROM raw_documents d JOIN raw_versions v ON v.id = d.latest_version_id
               WHERE d.source = :s AND EXISTS (
                   SELECT 1 FROM raw_fetches f JOIN ingest_runs r ON r.id = f.run_id
                   JOIN competitions c ON c.id = r.competition_id
                   WHERE f.document_id = d.id AND c.code = :c)
               ORDER BY d.url"""
        ),
        {"c": competition, "s": source},
    ).all()
    artifacts = [RawArtifact(*tuple(d)) for d in docs]
    last_change = max((a.first_fetched_at for a in artifacts), default=None)

    last_success_at = last_success.finished_at if last_success else None
    return SourceFreshness(
        competition=competition,
        source=source,
        status=classify_freshness(
            last_success_at, last_attempt.status if last_attempt else None, now, max_age
        ),
        checked_at=now,
        max_age_seconds=int(max_age.total_seconds()),
        last_attempt_at=last_attempt.finished_at if last_attempt else None,
        last_attempt_status=last_attempt.status if last_attempt else None,
        last_success_at=last_success_at,
        age_seconds=(now - last_success_at).total_seconds() if last_success_at else None,
        last_change_at=last_change,
        fingerprint=fingerprint(artifacts),
        artifacts=artifacts,
    )
