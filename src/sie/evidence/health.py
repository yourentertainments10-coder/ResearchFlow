"""Project health (ADR-043): which sources are old, expired or gone, and what that means for answers.

Read-only and computed from the ledger tables; nothing is written. Health describes process state, not
truth: ``stale`` means "potentially outdated", never "false", and no status here says a claim is right
(docs/EVIDENCE_LEDGER_DESIGN.md sections 3 and 8).

Source states:

* ``pruned``   the stored bytes were actually removed (``pruned_at`` is set). Never reported as current.
* ``expired``  the retention deadline (``retain_until``) has passed; the bytes may still be stored until
               ``sie evidence prune`` runs.
* ``stale``    a web source older than the configured maximum age.
* ``current``  a web source within the maximum age and retention.
* ``upload``   an uploaded file. It has no refresh cycle, so age alone never makes it stale. An
               ``old_upload`` flag is informational and never changes ``attention_needed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, text

from sie.answer.models import Gap, GapKind
from sie.evidence.scope import ProjectScope, scoped_api


class SourceState(StrEnum):
    PRUNED = "pruned"
    EXPIRED = "expired"
    STALE = "stale"
    CURRENT = "current"
    UPLOAD = "upload"


# States that need attention (and make the health command exit 1). ``upload`` and ``current`` do not.
ATTENTION_STATES = frozenset({SourceState.PRUNED, SourceState.EXPIRED, SourceState.STALE})


def classify_source(
    *,
    kind: str,
    retrieved_at: datetime,
    retain_until: datetime | None,
    pruned_at: datetime | None,
    now: datetime,
    web_max_age: timedelta,
) -> SourceState:
    """Pure. Order matters: removed bytes beat an elapsed deadline, which beats age."""
    if pruned_at is not None:
        return SourceState.PRUNED
    if kind == "upload":
        return SourceState.UPLOAD
    if retain_until is not None and retain_until <= now:
        return SourceState.EXPIRED
    if now - retrieved_at > web_max_age:
        return SourceState.STALE
    return SourceState.CURRENT


def is_old_upload(kind: str, retrieved_at: datetime, now: datetime, old_after: timedelta) -> bool:
    """Informational only: an upload older than the configured age. Never a failure."""
    return kind == "upload" and now - retrieved_at > old_after


@dataclass(frozen=True)
class SourceHealthItem:
    source_id: int
    kind: str
    title: str
    state: SourceState
    retrieved_at: datetime
    age_seconds: float
    retain_until: datetime | None
    pruned_at: datetime | None
    old_upload: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "title": self.title,
            "state": str(self.state),
            "retrieved_at": self.retrieved_at.isoformat(),
            "age_seconds": self.age_seconds,
            "retain_until": self.retain_until.isoformat() if self.retain_until else None,
            "pruned_at": self.pruned_at.isoformat() if self.pruned_at else None,
            "old_upload": self.old_upload,
        }


@dataclass(frozen=True)
class ProjectHealth:
    project: str
    checked_at: datetime
    web_max_age_days: int
    old_upload_days: int
    sources: tuple[SourceHealthItem, ...]
    claims_by_status: dict[str, int]
    claims_on_pruned_sources: int
    claims_on_expired_sources: int
    xlsx_claims_pending: int
    open_conflict_ids: tuple[int, ...]
    attention: tuple[str, ...] = field(default_factory=tuple)

    @property
    def no_sources(self) -> bool:
        return not self.sources

    @property
    def attention_needed(self) -> bool:
        """Drives the exit code. An empty project and old uploads do not set it."""
        return bool(self.attention)

    @property
    def overall(self) -> str:
        if self.no_sources:
            return "no_sources"
        return "attention" if self.attention_needed else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "overall": self.overall,
            "checked_at": self.checked_at.isoformat(),
            "web_max_age_days": self.web_max_age_days,
            "old_upload_days": self.old_upload_days,
            "no_sources": self.no_sources,
            "sources": [s.to_dict() for s in self.sources],
            "claims_total": sum(self.claims_by_status.values()),
            "claims_by_status": dict(sorted(self.claims_by_status.items())),
            "claims_on_pruned_sources": self.claims_on_pruned_sources,
            "claims_on_expired_sources": self.claims_on_expired_sources,
            "xlsx_claims_pending": self.xlsx_claims_pending,
            "open_conflict_ids": list(self.open_conflict_ids),
            "attention": list(self.attention),
        }


def _attention(items: list[SourceHealthItem], conflicts: tuple[int, ...]) -> tuple[str, ...]:
    found: list[str] = []
    for state in (SourceState.PRUNED, SourceState.EXPIRED, SourceState.STALE):
        n = sum(1 for i in items if i.state is state)
        if n:
            found.append(f"{n} {state} source(s)")
    if conflicts:
        found.append(f"{len(conflicts)} open conflict(s)")
    return tuple(found)


@scoped_api
def project_health(
    conn: Connection,
    scope: ProjectScope,
    now: datetime,
    web_max_age: timedelta,
    old_upload_after: timedelta,
) -> ProjectHealth:
    """Read-only health of one project. ``now`` and both limits come from the caller (settings)."""
    rows = conn.execute(
        text(
            "SELECT s.id, s.kind, s.title, s.retrieved_at, s.retain_until, s.pruned_at "
            "FROM evidence_sources s WHERE s.project_id = :p ORDER BY s.id"
        ),
        {"p": scope.project_id},
    ).all()
    items = tuple(
        SourceHealthItem(
            source_id=r.id,
            kind=r.kind,
            title=r.title,
            state=classify_source(
                kind=r.kind,
                retrieved_at=r.retrieved_at,
                retain_until=r.retain_until,
                pruned_at=r.pruned_at,
                now=now,
                web_max_age=web_max_age,
            ),
            retrieved_at=r.retrieved_at,
            age_seconds=(now - r.retrieved_at).total_seconds(),
            retain_until=r.retain_until,
            pruned_at=r.pruned_at,
            old_upload=is_old_upload(r.kind, r.retrieved_at, now, old_upload_after),
        )
        for r in rows
    )
    state_of = {i.source_id: i.state for i in items}

    claim_rows = conn.execute(
        text(
            "SELECT c.source_id, c.locator_kind, v.status, count(*) AS n "
            "FROM claims c JOIN evidence_claims_current v "
            "  ON v.claim_id = c.id AND v.project_id = c.project_id "
            "WHERE c.project_id = :p GROUP BY c.source_id, c.locator_kind, v.status"
        ),
        {"p": scope.project_id},
    ).all()
    by_status: dict[str, int] = {}
    on_pruned = on_expired = xlsx_pending = 0
    for r in claim_rows:
        by_status[r.status] = by_status.get(r.status, 0) + r.n
        if state_of.get(r.source_id) is SourceState.PRUNED:
            on_pruned += r.n
        elif state_of.get(r.source_id) is SourceState.EXPIRED:
            on_expired += r.n
        if r.locator_kind == "xlsx_cell" and r.status == "unverified":
            xlsx_pending += r.n

    conflicts = tuple(
        conn.execute(
            text(
                "SELECT id FROM claim_conflicts WHERE project_id = :p AND status = 'open' "
                "ORDER BY id"
            ),
            {"p": scope.project_id},
        ).scalars()
    )
    return ProjectHealth(
        project=scope.slug,
        checked_at=now,
        web_max_age_days=web_max_age.days,
        old_upload_days=old_upload_after.days,
        sources=items,
        claims_by_status=by_status,
        claims_on_pruned_sources=on_pruned,
        claims_on_expired_sources=on_expired,
        xlsx_claims_pending=xlsx_pending,
        open_conflict_ids=conflicts,
        attention=_attention(list(items), conflicts),
    )


def gaps_from_health(report: ProjectHealth) -> tuple[Gap, ...]:
    """The gaps an answer drawing on this project must disclose. Counts come from the report only."""
    gaps: list[Gap] = []
    count = {s: sum(1 for i in report.sources if i.state is s) for s in SourceState}
    if report.no_sources:
        gaps.append(
            Gap(
                id="project_no_sources",
                kind=GapKind.MISSING_DATA,
                message="The project has no sources, so there is no evidence to answer from.",
            )
        )
    if count[SourceState.STALE]:
        gaps.append(
            Gap(
                id="project_stale_sources",
                kind=GapKind.STALE,
                message=f"{count[SourceState.STALE]} web source(s) are older than "
                f"{report.web_max_age_days} days and may be outdated.",
            )
        )
    if count[SourceState.EXPIRED]:
        gaps.append(
            Gap(
                id="project_expired_sources",
                kind=GapKind.STALE,
                message=f"{count[SourceState.EXPIRED]} source(s) passed their retention deadline.",
            )
        )
    if count[SourceState.PRUNED]:
        gaps.append(
            Gap(
                id="project_pruned_sources",
                kind=GapKind.MISSING_DATA,
                message=f"{count[SourceState.PRUNED]} source(s) had their stored bytes removed; "
                f"{report.claims_on_pruned_sources} claim(s) can no longer be re-checked.",
            )
        )
    if report.open_conflict_ids:
        gaps.append(
            Gap(
                id="project_open_conflicts",
                kind=GapKind.CONFLICT,
                message=f"{len(report.open_conflict_ids)} claim conflict(s) are unresolved.",
            )
        )
    unverified = report.claims_by_status.get("unverified", 0)
    if unverified:
        gaps.append(
            Gap(
                id="project_unverified_claims",
                kind=GapKind.UNVERIFIED,
                message=f"{unverified} claim(s) have not been checked against their source.",
            )
        )
    return tuple(gaps)
