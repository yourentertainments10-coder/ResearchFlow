"""Database side of the conflict policy (docs/DATA_PIPELINE.md section 7, docs/DATABASE.md section 6).

Observations are evidence: one row per source claim per raw version, inserted once and never changed.
Conflicts are the decisions made about disagreeing claims. ``events.is_disputed`` is derived: true
while the event has at least one ``needs_review`` conflict. The caller owns the transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, text

from sie.db.placings import ChangeType, apply_placing
from sie.pipeline.conflicts import Claim, Rule, Status

OPEN = "needs_review"


class ConflictError(ValueError):
    """The conflict cannot be resolved as asked (unknown id, already closed, source not in it)."""


def record_observation(
    conn: Connection,
    *,
    event_id: int,
    medal: str,
    slot: int,
    country_id: int,
    source: str,
    raw_version_id: int,
    observed_at: datetime,
) -> Claim:
    """Store what ``source`` claimed. Idempotent per (slot, source, raw version)."""
    params = {
        "e": event_id,
        "m": medal,
        "s": slot,
        "c": country_id,
        "src": source,
        "v": raw_version_id,
        "at": observed_at,
    }
    conn.execute(
        text(
            """INSERT INTO source_observations
                   (event_id, medal, slot, country_id, source, raw_version_id, observed_at)
               VALUES (:e, :m, :s, :c, :src, :v, :at)
               ON CONFLICT (event_id, medal, slot, source, raw_version_id) DO NOTHING"""
        ),
        params,
    )
    row = conn.execute(
        text(
            """SELECT id, country_id, observed_at FROM source_observations
               WHERE event_id = :e AND medal = :m AND slot = :s AND source = :src
                 AND raw_version_id = :v"""
        ),
        params,
    ).one()
    return Claim(source, row.country_id, row.observed_at, row.id)


def latest_claims_of_other_sources(
    conn: Connection, *, event_id: int, medal: str, slot: int, source: str
) -> list[Claim]:
    """The most recent observation of every source except ``source`` for this slot."""
    rows = conn.execute(
        text(
            """SELECT DISTINCT ON (source) id, source, country_id, observed_at
               FROM source_observations
               WHERE event_id = :e AND medal = :m AND slot = :s AND source <> :src
               ORDER BY source, observed_at DESC, id DESC"""
        ),
        {"e": event_id, "m": medal, "s": slot, "src": source},
    )
    return [Claim(r.source, r.country_id, r.observed_at, r.id) for r in rows]


def has_current_placing(conn: Connection, *, event_id: int, medal: str, slot: int) -> bool:
    return (
        conn.execute(
            text(
                """SELECT 1 FROM placings
                   WHERE event_id = :e AND medal = :m AND slot = :s AND is_current"""
            ),
            {"e": event_id, "m": medal, "s": slot},
        ).first()
        is not None
    )


def open_conflict_id(conn: Connection, *, event_id: int, medal: str, slot: int) -> int | None:
    return conn.execute(
        text(
            """SELECT id FROM source_conflicts
               WHERE event_id = :e AND medal = :m AND slot = :s AND status = :open"""
        ),
        {"e": event_id, "m": medal, "s": slot, "open": OPEN},
    ).scalar_one_or_none()


def record_conflict(
    conn: Connection,
    *,
    event_id: int,
    medal: str,
    slot: int,
    against: Claim,
    new: Claim,
    rule: Rule,
    status: Status,
    now: datetime,
) -> int | None:
    """Record a decision about two disagreeing claims. Idempotent.

    The same pair of observations is never recorded twice, and a slot never has two open conflicts.
    Returns the new conflict id, or ``None`` when an equivalent row already exists.
    """
    if against.observation_id is None or new.observation_id is None:
        raise ConflictError("a conflict needs stored observations on both sides")
    if status.value == OPEN and open_conflict_id(conn, event_id=event_id, medal=medal, slot=slot):
        return None
    same = conn.execute(
        text(
            """SELECT 1 FROM source_conflicts
               WHERE observation_a_id = :a AND observation_b_id = :b AND policy_rule = :r"""
        ),
        {"a": against.observation_id, "b": new.observation_id, "r": rule.value},
    ).first()
    if same:
        return None
    return conn.execute(
        text(
            """INSERT INTO source_conflicts (event_id, medal, slot, observation_a_id, observation_b_id,
                                             policy_rule, status, created_at, resolved_at)
               VALUES (:e, :m, :s, :a, :b, :r, :st, :now, CASE WHEN :st = 'needs_review' THEN NULL
                                                               ELSE :now END)
               RETURNING id"""
        ),
        {
            "e": event_id,
            "m": medal,
            "s": slot,
            "a": against.observation_id,
            "b": new.observation_id,
            "r": rule.value,
            "st": status.value,
            "now": now,
        },
    ).scalar_one()


def close_open_conflict(
    conn: Connection, *, event_id: int, medal: str, slot: int, note: str, by: str, now: datetime
) -> None:
    conn.execute(
        text(
            """UPDATE source_conflicts
               SET status = 'resolved', resolution_note = :n, resolved_by = :by, resolved_at = :now
               WHERE event_id = :e AND medal = :m AND slot = :s AND status = :open"""
        ),
        {"e": event_id, "m": medal, "s": slot, "n": note, "by": by, "now": now, "open": OPEN},
    )


def refresh_disputed(conn: Connection, event_id: int) -> bool:
    """Set ``events.is_disputed`` from the open conflicts. Returns the new value."""
    disputed = conn.execute(
        text(
            """UPDATE events SET is_disputed = EXISTS (
                   SELECT 1 FROM source_conflicts c WHERE c.event_id = events.id AND c.status = :open)
               WHERE id = :e RETURNING is_disputed"""
        ),
        {"e": event_id, "open": OPEN},
    ).scalar_one()
    return bool(disputed)


def list_conflicts(conn: Connection, *, status: str | None = OPEN) -> list[dict]:
    rows = conn.execute(
        text(
            """SELECT c.id, e.name AS event, c.medal, c.slot, c.policy_rule, c.status, c.created_at,
                      a.source AS source_a, ca.name AS country_a,
                      b.source AS source_b, cb.name AS country_b
               FROM source_conflicts c
               JOIN events e ON e.id = c.event_id
               JOIN source_observations a ON a.id = c.observation_a_id
               JOIN source_observations b ON b.id = c.observation_b_id
               JOIN countries ca ON ca.id = a.country_id
               JOIN countries cb ON cb.id = b.country_id
               WHERE (CAST(:st AS text) IS NULL OR c.status = :st)
               ORDER BY c.id"""
        ),
        {"st": status},
    )
    return [dict(r._mapping) for r in rows]


def resolve_conflict(
    conn: Connection,
    conflict_id: int,
    *,
    accept_source: str,
    note: str,
    by: str,
    now: datetime,
) -> ChangeType:
    """The owner decides: make the placing show ``accept_source``'s claim and close the conflict."""
    if not note.strip():
        raise ConflictError("a resolution needs a note saying why")
    conflict = conn.execute(
        text(
            """SELECT c.id, c.event_id, c.medal, c.slot, c.status,
                      a.source AS source_a, a.country_id AS country_a, a.raw_version_id AS raw_a,
                      b.source AS source_b, b.country_id AS country_b, b.raw_version_id AS raw_b
               FROM source_conflicts c
               JOIN source_observations a ON a.id = c.observation_a_id
               JOIN source_observations b ON b.id = c.observation_b_id
               WHERE c.id = :id FOR UPDATE OF c"""
        ),
        {"id": conflict_id},
    ).one_or_none()
    if conflict is None:
        raise ConflictError(f"no conflict with id {conflict_id}")
    if conflict.status != OPEN:
        raise ConflictError(f"conflict {conflict_id} is {conflict.status}, not open")
    if accept_source == conflict.source_a:
        country_id, raw_version_id = conflict.country_a, conflict.raw_a
    elif accept_source == conflict.source_b:
        country_id, raw_version_id = conflict.country_b, conflict.raw_b
    else:
        raise ConflictError(
            f"conflict {conflict_id} is between {conflict.source_a!r} and {conflict.source_b!r}, "
            f"not {accept_source!r}"
        )

    current = conn.execute(
        text(
            """SELECT is_tie, result_date, entrant_id FROM placings
               WHERE event_id = :e AND medal = :m AND slot = :s AND is_current"""
        ),
        {"e": conflict.event_id, "m": conflict.medal, "s": conflict.slot},
    ).one_or_none()
    change = apply_placing(
        conn,
        event_id=conflict.event_id,
        medal=conflict.medal,
        slot=conflict.slot,
        country_id=country_id,
        entrant_id=current.entrant_id if current else None,
        raw_version_id=raw_version_id,
        source=accept_source,
        now=now,
        is_tie=current.is_tie if current else False,
        result_date=current.result_date if current else None,
        reason=f"conflict {conflict_id} resolved by {by}: {note}",
    )
    conn.execute(
        text(
            """UPDATE source_conflicts
               SET status = 'resolved', resolution_note = :n, resolved_by = :by, resolved_at = :now
               WHERE id = :id"""
        ),
        {"n": f"accepted {accept_source}: {note}", "by": by, "now": now, "id": conflict_id},
    )
    refresh_disputed(conn, conflict.event_id)
    return change
