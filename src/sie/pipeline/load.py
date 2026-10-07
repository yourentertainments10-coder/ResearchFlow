"""The one place where validated rows reach the database (docs/DATA_PIPELINE.md, load stage).

Every source (portal capture, manual CSV, later adapters) ends here. Events are created from the
reference-resolved rows, placings go through ``apply_placing`` (idempotent, reallocation-safe), and
rejected rows are written to ``quarantine``. The caller owns the transaction.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, text

from sie.db import conflicts as db
from sie.db.placings import apply_placing
from sie.pipeline.conflicts import ConflictPolicy, Rule, decide
from sie.pipeline.models import CompetitionRef, NormalisedResult, Reason, Rejection
from sie.pipeline.validate import Placed


class LoadError(ValueError):
    """The rows cannot be loaded without guessing; nothing from this load is kept."""


@dataclass
class LoadSummary:
    events: int = 0
    placings: dict[str, int] = field(default_factory=dict)
    quarantined: int = 0
    conflicts: dict[str, int] = field(default_factory=dict)  # policy rule -> rows decided
    held: int = 0  # claims not applied because the conflict policy held them back
    refetch_official: bool = False  # the policy asks for an immediate re-fetch of the official source


def load_placed(
    conn: Connection,
    placed: list[Placed],
    competition: CompetitionRef,
    *,
    raw_version_id: int,
    run_id: int,
    source: str,
    now: datetime,
    policy: ConflictPolicy | None = None,
) -> LoadSummary:
    """Load validated rows. With a ``policy`` every claim is also recorded as an observation and
    checked against the other sources' latest claims (docs/DATA_PIPELINE.md section 7)."""
    by_event: dict[tuple[int, str, str], list[Placed]] = defaultdict(list)
    for item in placed:
        by_event[item.row.event_key].append(item)

    counts: Counter[str] = Counter()
    conflict_counts: Counter[str] = Counter()
    held = 0
    summary_refetch = False
    for items in by_event.values():
        event_id = _upsert_event(conn, competition, [i.row for i in items])
        for item in items:
            decision = None
            if policy is not None:
                decision = _decide(conn, event_id, item, raw_version_id, source, now, policy)
                if decision.rule is not None:
                    conflict_counts[decision.rule.value] += 1
                summary_refetch |= decision.refetch_official
            if decision is not None and not decision.apply:
                held += 1
                continue
            change = apply_placing(
                conn,
                event_id=event_id,
                medal=item.row.medal,
                slot=item.slot,
                country_id=item.row.country_id,
                entrant_id=None,
                raw_version_id=raw_version_id,
                source=source,
                now=now,
                run_id=run_id,
                is_tie=item.row.is_tie,
                result_date=item.row.event_date,
                source_country=item.row.parsed.country_label or item.row.parsed.country or None,
            )
            _record_source_note(conn, event_id, item, change)
            counts[change] += 1
        if policy is not None:
            db.refresh_disputed(conn, event_id)

    _check_event_total(conn, competition)
    return LoadSummary(
        events=len(by_event),
        placings=dict(counts),
        conflicts=dict(conflict_counts),
        held=held,
        refetch_official=summary_refetch,
    )


def _decide(conn, event_id, item, raw_version_id, source, now, policy):
    """Record the observation, ask the policy, and write the conflict row it asks for."""
    medal, slot = item.row.medal, item.slot
    new = db.record_observation(
        conn,
        event_id=event_id,
        medal=medal,
        slot=slot,
        country_id=item.row.country_id,
        source=source,
        raw_version_id=raw_version_id,
        observed_at=now,
    )
    others = db.latest_claims_of_other_sources(
        conn, event_id=event_id, medal=medal, slot=slot, source=source
    )
    open_id = db.open_conflict_id(conn, event_id=event_id, medal=medal, slot=slot)
    decision = decide(
        new,
        others,
        has_accepted=db.has_current_placing(conn, event_id=event_id, medal=medal, slot=slot),
        open_conflict=open_id is not None,
        policy=policy,
        now=now,
    )
    if decision.rule is Rule.AGREEMENT:
        db.close_open_conflict(
            conn,
            event_id=event_id,
            medal=medal,
            slot=slot,
            note="the sources agree again",
            by="pipeline",
            now=now,
        )
    elif decision.rule is not None and decision.against is not None and decision.status:
        db.record_conflict(
            conn,
            event_id=event_id,
            medal=medal,
            slot=slot,
            against=decision.against,
            new=new,
            rule=decision.rule,
            status=decision.status,
            now=now,
        )
    return decision


def _upsert_event(
    conn: Connection, competition: CompetitionRef, rows: list[NormalisedResult]
) -> int:
    first = rows[0]
    dates = [r.event_date for r in rows if r.event_date]
    external_key = next((r.external_key for r in rows if r.external_key), None)
    found = conn.execute(
        text(
            """INSERT INTO events (competition_id, discipline_id, name, name_raw, gender, participation,
                                   event_date, status, external_key)
               VALUES (:c, :d, :n, :raw, :g, :p, :dt, 'completed', :k)
               ON CONFLICT (competition_id, discipline_id, name, gender)
               DO UPDATE SET external_key = coalesce(events.external_key, EXCLUDED.external_key)
               RETURNING id, external_key"""
        ),
        {
            "c": competition.id,
            "d": first.discipline_id,
            "n": first.event_name,
            "raw": first.event_name_raw,
            "g": first.gender,
            "p": first.participation,
            "dt": max(dates) if dates else None,
            "k": external_key,
        },
    ).one()
    # Two different source events with the same name would silently merge into one: refuse.
    if external_key is not None and found.external_key != external_key:
        raise LoadError(
            f"event name collision: {first.event_name!r} is already event {found.external_key!r}, "
            f"this source calls it {external_key!r}"
        )
    return found.id


def _record_source_note(conn: Connection, event_id: int, item: Placed, change: str) -> None:
    """Keep the per-row source URL or note on the current placing (manual imports)."""
    note = item.row.source_note or item.row.source_url
    if not note or change == "unchanged":
        return
    conn.execute(
        text(
            """UPDATE placings SET source_note = :n
               WHERE event_id = :e AND medal = :m AND slot = :s AND is_current"""
        ),
        {"n": note, "e": event_id, "m": item.row.medal, "s": item.slot},
    )


def _check_event_total(conn: Connection, competition: CompetitionRef) -> None:
    if competition.official_event_total is None:
        return
    total = conn.execute(
        text("SELECT count(*) FROM events WHERE competition_id = :c"), {"c": competition.id}
    ).scalar_one()
    if total > competition.official_event_total:
        raise LoadError(
            f"{Reason.EVENT_TOTAL_EXCEEDED}: {total} events exceeds the official total of "
            f"{competition.official_event_total}"
        )


def write_quarantine(
    conn: Connection,
    rejections: list[Rejection],
    *,
    run_id: int,
    raw_version_id: int,
    now: datetime,
) -> int:
    """Store rejected rows with their reason. Idempotent per raw version.

    A problem that is already in quarantine for this raw version is not stored twice. Rows that were
    quarantined by an earlier run of the same raw version and no longer fail (the owner added an alias
    and re-ran) are marked resolved, never deleted.
    """
    existing = {
        (r.reason, str(r.payload_json.get("row_number"))): r.id
        for r in conn.execute(
            text(
                """SELECT id, reason, payload_json FROM quarantine
                   WHERE raw_version_id = :v AND NOT resolved"""
            ),
            {"v": raw_version_id},
        )
    }
    current = {(str(r.reason), str(r.row_number)) for r in rejections}
    inserted = 0
    for rejection in rejections:
        if (str(rejection.reason), str(rejection.row_number)) in existing:
            continue
        conn.execute(
            text(
                """INSERT INTO quarantine (run_id, raw_version_id, reason, payload_json, created_at)
                   VALUES (:r, :v, :reason, CAST(:p AS jsonb), :n)"""
            ),
            {
                "r": run_id,
                "v": raw_version_id,
                "reason": str(rejection.reason),
                "p": _json(rejection.payload()),
                "n": now,
            },
        )
        inserted += 1
    stale = [qid for key, qid in existing.items() if key not in current]
    if stale:
        conn.execute(
            text("UPDATE quarantine SET resolved = true WHERE id = ANY(:ids)"), {"ids": stale}
        )
    return inserted


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
