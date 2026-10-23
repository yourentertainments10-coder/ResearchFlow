"""Conflicts between claims of one project.

Detection is a deterministic rule and never resolves anything: two claims of a project that name the
same subject, metric, period and unit but state different values are recorded as an open conflict. A
person decides what it means (a different scope, a different period, one source is wrong). Nothing here
changes a claim, and nothing here touches the sports ``source_conflicts`` table or the ADR-032 rule.

Open conflicts must be disclosed: ``adapters.figure_from_claim`` attaches a ``conflict`` gap to a figure
whose claim is in one, and the answer contract then refuses an answer that does not mention it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, text

from sie.evidence.errors import EvidenceError, NotFound
from sie.evidence.locators import nfc
from sie.evidence.scope import ProjectScope, scoped_api

RESOLUTIONS = ("explained", "resolved_a", "resolved_b", "dismissed")
MAX_GROUP = 200  # claims per (subject, metric, period, unit); more than this needs a human to look
MAX_PAIRS = 10_000


@dataclass(frozen=True)
class ConflictInfo:
    id: int
    claim_a_id: int
    claim_b_id: int
    kind: str
    status: str
    note: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


def _same_value(a: tuple[str, Decimal | None], b: tuple[str, Decimal | None]) -> bool:
    (text_a, num_a), (text_b, num_b) = a, b
    if num_a is not None and num_b is not None:
        return num_a == num_b  # tolerance zero: 6 and 6.0 agree, 6 and 6.01 do not
    return nfc(text_a).strip().casefold() == nfc(text_b).strip().casefold()


def _norm(value: str | None) -> str:
    return nfc(value).strip().casefold() if value else ""


@scoped_api
def detect_conflicts(conn: Connection, scope: ProjectScope, *, now: datetime) -> list[int]:
    """Record every new disagreement among the project's non-rejected claims. Idempotent.

    Returns the ids of the conflicts created by this call.
    """
    rows = conn.execute(
        text(
            """SELECT c.id, c.subject, c.metric, c.period, c.unit, c.value_text, c.value_numeric
               FROM claims c JOIN evidence_claims_current v
                 ON v.claim_id = c.id AND v.project_id = c.project_id
               WHERE c.project_id = :p AND v.status <> 'rejected' ORDER BY c.id"""
        ),
        {"p": scope.project_id},
    ).all()
    groups: dict[tuple[str, str, str, str], list] = {}
    for r in rows:
        key = (_norm(r.subject), _norm(r.metric), _norm(r.period), _norm(r.unit))
        groups.setdefault(key, []).append(r)
    created: list[int] = []
    pairs = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        if len(members) > MAX_GROUP:
            raise EvidenceError(
                f"{len(members)} claims share one subject, metric, period and unit; "
                "review them before conflict detection can be trusted"
            )
        for a, b in itertools.combinations(members, 2):  # ids ascend, so a.id < b.id
            pairs += 1
            if pairs > MAX_PAIRS:
                raise EvidenceError("too many claim pairs to compare; review the claims first")
            if _same_value((a.value_text, a.value_numeric), (b.value_text, b.value_numeric)):
                continue
            new = conn.execute(
                text(
                    """INSERT INTO claim_conflicts (project_id, claim_a_id, claim_b_id, kind, status,
                                                    created_at)
                       VALUES (:p, :a, :b, 'value_differs', 'open', :t)
                       ON CONFLICT (claim_a_id, claim_b_id) DO NOTHING RETURNING id"""
                ),
                {"p": scope.project_id, "a": a.id, "b": b.id, "t": now},
            ).scalar_one_or_none()
            if new is not None:
                created.append(new)
    return created


_COLUMNS = "id, claim_a_id, claim_b_id, kind, status, note, created_at, resolved_at, resolved_by"


@scoped_api
def list_conflicts(
    conn: Connection, scope: ProjectScope, status: str | None = None
) -> list[ConflictInfo]:
    if status is None:
        rows = conn.execute(
            text(f"SELECT {_COLUMNS} FROM claim_conflicts WHERE project_id = :p ORDER BY id"),
            {"p": scope.project_id},
        ).all()
    else:
        rows = conn.execute(
            text(
                f"""SELECT {_COLUMNS} FROM claim_conflicts
                    WHERE project_id = :p AND status = :s ORDER BY id"""
            ),
            {"p": scope.project_id, "s": status},
        ).all()
    return [ConflictInfo(*tuple(r)) for r in rows]


@scoped_api
def resolve_conflict(
    conn: Connection,
    scope: ProjectScope,
    conflict_id: int,
    *,
    status: str,
    note: str,
    resolved_by: str,
    now: datetime,
) -> ConflictInfo:
    """A named person records what a conflict turned out to be. Claims themselves are not changed.

    ``explained`` (a different scope or period) and ``dismissed`` keep both claims usable. ``resolved_a``
    or ``resolved_b`` names the claim to use; the other is then refused by ``figure_from_claim``.
    """
    if status not in RESOLUTIONS:
        raise EvidenceError(f"status must be one of {RESOLUTIONS}")
    if not note.strip() or not resolved_by.strip():
        raise EvidenceError("a resolution needs a note and a named person")
    if resolved_by.startswith("system:"):
        raise EvidenceError("a conflict is resolved by a named person, not a system actor")
    row = conn.execute(
        text(
            """UPDATE claim_conflicts SET status = :s, note = :n, resolved_by = :b, resolved_at = :t
               WHERE project_id = :p AND id = :c AND status = 'open'
               RETURNING id"""
        ),
        {
            "s": status,
            "n": note.strip(),
            "b": resolved_by.strip(),
            "t": now,
            "p": scope.project_id,
            "c": conflict_id,
        },
    ).one_or_none()
    if row is None:
        # Missing, someone else's, or already resolved: all the same answer to the caller.
        raise NotFound("no open conflict with that id")
    return next(c for c in list_conflicts(conn, scope) if c.id == conflict_id)
