"""Placing write procedure: create, no-op, reallocate or correct (docs/DATABASE.md section 6).

The caller owns the transaction. Everything here runs on the connection it is given, so closing the
old version and inserting the new one commit or roll back together. Rows are never deleted or
overwritten; the old version is closed and kept, and a placing_history row records the change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, text

ChangeType = Literal["created", "unchanged", "reallocated", "corrected"]

_SELECT_CURRENT = text(
    """
    SELECT id, country_id, entrant_id, is_tie
    FROM placings
    WHERE event_id = :event_id AND medal = :medal AND slot = :slot AND is_current
    FOR UPDATE
    """
)
_CLOSE = text("UPDATE placings SET is_current = false, valid_to = :now WHERE id = :id")
_INSERT = text(
    """
    INSERT INTO placings (event_id, medal, slot, country_id, entrant_id, is_tie,
                          valid_from, supersedes_id, raw_version_id, source)
    VALUES (:event_id, :medal, :slot, :country_id, :entrant_id, :is_tie,
            :now, :supersedes_id, :raw_version_id, :source)
    RETURNING id
    """
)
_HISTORY = text(
    """
    INSERT INTO placing_history (event_id, medal, slot, old_placing_id, new_placing_id,
                                 old_country_id, new_country_id, change_type, reason, source,
                                 run_id, changed_at)
    VALUES (:event_id, :medal, :slot, :old_id, :new_id, :old_country, :new_country,
            :change_type, :reason, :source, :run_id, :now)
    """
)


def apply_placing(
    conn: Connection,
    *,
    event_id: int,
    medal: str,
    slot: int,
    country_id: int,
    entrant_id: int | None,
    raw_version_id: int,
    source: str,
    now: datetime,
    run_id: int | None = None,
    is_tie: bool = False,
    reason: str | None = None,
) -> ChangeType:
    """Make (event, medal, slot) show the given country and entrant. Idempotent.

    Returns ``created`` (no current placing), ``unchanged`` (identical, nothing written),
    ``reallocated`` (different country) or ``corrected`` (same country, different entrant or tie flag).
    """
    key = {"event_id": event_id, "medal": medal, "slot": slot}
    current = conn.execute(_SELECT_CURRENT, key).one_or_none()

    if current is not None and (
        current.country_id == country_id
        and current.entrant_id == entrant_id
        and current.is_tie == is_tie
    ):
        return "unchanged"

    if current is not None:
        conn.execute(_CLOSE, {"id": current.id, "now": now})

    new_id = conn.execute(
        _INSERT,
        {
            **key,
            "country_id": country_id,
            "entrant_id": entrant_id,
            "is_tie": is_tie,
            "now": now,
            "supersedes_id": current.id if current is not None else None,
            "raw_version_id": raw_version_id,
            "source": source,
        },
    ).scalar_one()

    if current is None:
        change: ChangeType = "created"
    elif current.country_id != country_id:
        change = "reallocated"
    else:
        change = "corrected"

    conn.execute(
        _HISTORY,
        {
            **key,
            "old_id": current.id if current is not None else None,
            "new_id": new_id,
            "old_country": current.country_id if current is not None else None,
            "new_country": country_id,
            "change_type": change,
            "reason": reason,
            "source": source,
            "run_id": run_id,
            "now": now,
        },
    )
    return change
