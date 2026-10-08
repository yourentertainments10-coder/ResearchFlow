"""Snapshots and change detection (docs/ANALYTICS_SPEC.md section 10, docs/DATABASE.md section 8).

Identity rules: the fingerprint is a SHA-256 of the current placings by natural key (no surrogate ids,
so rebuilding from raw data reproduces it) plus every event's status and disputed flag. A ``change``
snapshot is written only when the fingerprint or the analytics version differs from the latest
``change`` snapshot; one ``daily`` snapshot per competition-local day is written regardless. Snapshots
are never updated. Everything runs on the caller's connection; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Connection, text

from sie.analytics.metrics import ANALYTICS_VERSION, competition_rank

Key = tuple[int, int, str]  # country_id, sport_id, gender


@dataclass(frozen=True)
class SnapshotResult:
    fingerprint: str
    change_id: int | None  # None when nothing changed since the latest change snapshot
    daily_id: int | None  # None when today's daily snapshot already existed
    changes: int = 0


def fingerprint(conn: Connection, competition_id: int) -> str:
    """SHA-256 of a canonical text of the current placings and every event's state."""
    placings = conn.execute(
        text(
            """SELECT s.name AS sport, d.name AS discipline, e.name AS event, e.gender, p.medal,
                      p.slot, c.code
               FROM placings p
               JOIN events e ON e.id = p.event_id
               JOIN disciplines d ON d.id = e.discipline_id
               JOIN sports s ON s.id = d.sport_id
               JOIN countries c ON c.id = p.country_id
               WHERE e.competition_id = :c AND p.is_current"""
        ),
        {"c": competition_id},
    ).all()
    events = conn.execute(
        text(
            """SELECT s.name AS sport, d.name AS discipline, e.name AS event, e.gender, e.status,
                      e.is_disputed
               FROM events e
               JOIN disciplines d ON d.id = e.discipline_id
               JOIN sports s ON s.id = d.sport_id
               WHERE e.competition_id = :c"""
        ),
        {"c": competition_id},
    ).all()
    lines = sorted("P|" + "|".join(str(v) for v in row) for row in placings)
    lines += sorted("E|" + "|".join(str(v) for v in row) for row in events)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def diff_snapshots(
    previous: dict[Key, tuple[int, int, int]], current: dict[Key, tuple[int, int, int]]
) -> list[dict]:
    """Changes between two snapshots, each ``{change_type, country_id, sport_id, gender, detail}``.

    ``medals_gained`` and ``medals_lost`` are per country, sport and gender; ``new_sport`` marks a
    country's first medal in a sport; ``rank_change`` is a move in the country's podium rank.
    """
    out: list[dict] = []
    names = ("Gold", "Silver", "Bronze")
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key, (0, 0, 0))
        after = current.get(key, (0, 0, 0))
        deltas = [(n, a - b) for n, a, b in zip(names, after, before, strict=True) if a != b]
        for sign, change_type in ((1, "medals_gained"), (-1, "medals_lost")):
            part = [f"{n} {d:+d}" for n, d in deltas if d * sign > 0]
            if part:
                out.append(_change(change_type, key, ", ".join(part)))

    def per_sport(rows: dict[Key, tuple[int, int, int]]) -> dict[tuple[int, int], int]:
        totals: dict[tuple[int, int], int] = {}
        for (country, sport, _), medals in rows.items():
            totals[(country, sport)] = totals.get((country, sport), 0) + sum(medals)
        return totals

    before_sport, after_sport = per_sport(previous), per_sport(current)
    for country, sport in sorted(after_sport):
        if after_sport[(country, sport)] > 0 and before_sport.get((country, sport), 0) == 0:
            out.append(
                {
                    "change_type": "new_sport",
                    "country_id": country,
                    "sport_id": sport,
                    "gender": None,
                    "detail": "first medal in this sport",
                }
            )

    before_rank, after_rank = _ranks(previous), _ranks(current)
    for country in sorted(set(before_rank) & set(after_rank)):
        if before_rank[country] != after_rank[country]:
            out.append(
                {
                    "change_type": "rank_change",
                    "country_id": country,
                    "sport_id": None,
                    "gender": None,
                    "detail": f"podium rank {before_rank[country]} -> {after_rank[country]}",
                }
            )
    return out


def _change(change_type: str, key: Key, detail: str) -> dict:
    return {
        "change_type": change_type,
        "country_id": key[0],
        "sport_id": key[1],
        "gender": key[2],
        "detail": detail,
    }


def _ranks(rows: dict[Key, tuple[int, int, int]]) -> dict[int, int]:
    totals: dict[int, list[int]] = {}
    for (country, _, _), medals in rows.items():
        acc = totals.setdefault(country, [0, 0, 0])
        for i, n in enumerate(medals):
            acc[i] += n
    if not totals:
        return {}
    frame = pd.DataFrame([(c, *m) for c, m in sorted(totals.items())], columns=["c", "g", "s", "b"])
    frame["rank"] = competition_rank(frame, ["g", "s", "b"])
    return {int(c): int(r) for c, r in zip(frame["c"], frame["rank"], strict=True)}


def _rows_of(conn: Connection, snapshot_id: int) -> dict[Key, tuple[int, int, int]]:
    rows = conn.execute(
        text(
            """SELECT country_id, sport_id, gender, sum(gold) AS g, sum(silver) AS s, sum(bronze) AS b
               FROM snapshot_rows WHERE snapshot_id = :s GROUP BY country_id, sport_id, gender"""
        ),
        {"s": snapshot_id},
    )
    return {(r.country_id, r.sport_id, r.gender): (int(r.g), int(r.s), int(r.b)) for r in rows}


def _insert_snapshot(
    conn: Connection,
    *,
    competition_id: int,
    kind: str,
    fp: str,
    now: datetime,
    local_date,
    run_id: int | None,
    version: str,
    note: str | None,
) -> int | None:
    counts = conn.execute(
        text(
            """SELECT count(*) FILTER (WHERE status IN ('completed', 'amended')) AS done,
                      count(*) AS total, count(*) FILTER (WHERE is_disputed) AS disputed
               FROM events WHERE competition_id = :c"""
        ),
        {"c": competition_id},
    ).one()
    sid = conn.execute(
        text(
            """INSERT INTO analytics_snapshots
                   (competition_id, created_at, as_of, local_date, run_id, kind, data_fingerprint,
                    analytics_version, events_completed, events_total, disputed_events, note)
               VALUES (:c, :now, :now, :d, :run, :kind, :fp, :v, :done, :total, :disputed, :note)
               ON CONFLICT (competition_id, local_date) WHERE kind = 'daily' DO NOTHING
               RETURNING id"""
        ),
        {
            "c": competition_id,
            "now": now,
            "d": local_date,
            "run": run_id,
            "kind": kind,
            "fp": fp,
            "v": version,
            "done": counts.done,
            "total": counts.total,
            "disputed": counts.disputed,
            "note": note,
        },
    ).scalar_one_or_none()
    if sid is None:
        return None
    conn.execute(
        text(
            """INSERT INTO snapshot_rows (snapshot_id, country_id, sport_id, discipline_id, gender,
                                          gold, silver, bronze, total, points_321)
               SELECT :s, c.id, sp.id, d.id, f.gender,
                      count(*) FILTER (WHERE f.medal = 'Gold'),
                      count(*) FILTER (WHERE f.medal = 'Silver'),
                      count(*) FILTER (WHERE f.medal = 'Bronze'),
                      count(*),
                      3 * count(*) FILTER (WHERE f.medal = 'Gold')
                        + 2 * count(*) FILTER (WHERE f.medal = 'Silver')
                        + count(*) FILTER (WHERE f.medal = 'Bronze')
               FROM v_medal_facts f
               JOIN countries c ON c.code = f.country_code
               JOIN sports sp ON sp.name = f.sport
               JOIN disciplines d ON d.sport_id = sp.id AND d.name = f.discipline
               WHERE f.competition_id = :c
               GROUP BY c.id, sp.id, d.id, f.gender"""
        ),
        {"s": sid, "c": competition_id},
    )
    return sid


def take_snapshot(
    conn: Connection,
    competition_id: int,
    *,
    now: datetime,
    run_id: int | None = None,
    version: str = ANALYTICS_VERSION,
) -> SnapshotResult:
    """Write the ``change`` and ``daily`` snapshots the identity rules call for. Idempotent."""
    timezone = conn.execute(
        text("SELECT timezone FROM competitions WHERE id = :c"), {"c": competition_id}
    ).scalar_one()
    local_date = now.astimezone(ZoneInfo(timezone)).date()
    fp = fingerprint(conn, competition_id)
    latest = conn.execute(
        text(
            """SELECT id, data_fingerprint, analytics_version FROM analytics_snapshots
               WHERE competition_id = :c AND kind = 'change' ORDER BY id DESC LIMIT 1"""
        ),
        {"c": competition_id},
    ).one_or_none()

    change_id = None
    changes = 0
    if latest is None or latest.data_fingerprint != fp or latest.analytics_version != version:
        change_id = _insert_snapshot(
            conn, competition_id=competition_id, kind="change", fp=fp, now=now,
            local_date=local_date, run_id=run_id, version=version,
            note="first snapshot" if latest is None else None,
        )  # fmt: skip
        if latest is not None and change_id is not None:
            found = diff_snapshots(_rows_of(conn, latest.id), _rows_of(conn, change_id))
            for item in found:
                conn.execute(
                    text(
                        """INSERT INTO snapshot_changes
                               (snapshot_id, change_type, country_id, sport_id, gender, detail)
                           VALUES (:s, :t, :c, :sp, :g, :d)"""
                    ),
                    {
                        "s": change_id,
                        "t": item["change_type"],
                        "c": item["country_id"],
                        "sp": item["sport_id"],
                        "g": item["gender"],
                        "d": item["detail"],
                    },
                )
            changes = len(found)

    exists = conn.execute(
        text(
            """SELECT 1 FROM analytics_snapshots
               WHERE competition_id = :c AND kind = 'daily' AND local_date = :d"""
        ),
        {"c": competition_id, "d": local_date},
    ).first()
    daily_id = None
    if not exists:
        daily_id = _insert_snapshot(
            conn, competition_id=competition_id, kind="daily", fp=fp, now=now,
            local_date=local_date, run_id=run_id, version=version, note=None,
        )  # fmt: skip
    return SnapshotResult(fp, change_id, daily_id, changes)


def latest_changes(conn: Connection, competition_id: int) -> pd.DataFrame:
    """What changed in the latest ``change`` snapshot compared with the one before it (spec 10).

    Empty when the latest snapshot is the first one or only flags changed (nothing to compare).
    """
    rows = conn.execute(
        text(
            """SELECT a.id AS snapshot_id, a.as_of, ch.change_type, c.code AS country_code,
                      s.name AS sport, ch.gender, ch.detail
               FROM analytics_snapshots a
               JOIN snapshot_changes ch ON ch.snapshot_id = a.id
               LEFT JOIN countries c ON c.id = ch.country_id
               LEFT JOIN sports s ON s.id = ch.sport_id
               WHERE a.id = (SELECT max(id) FROM analytics_snapshots
                             WHERE competition_id = :c AND kind = 'change')
               ORDER BY ch.change_type, c.code, s.name, ch.gender"""
        ),
        {"c": competition_id},
    ).all()
    return pd.DataFrame(
        rows,
        columns=[
            "snapshot_id",
            "as_of",
            "change_type",
            "country_code",
            "sport",
            "gender",
            "detail",
        ],
    )


def rank_trajectory(conn: Connection, competition_id: int) -> pd.DataFrame:
    """Podium rank of every country at every snapshot (spec 10)."""
    rows = conn.execute(
        text(
            """SELECT a.id AS snapshot_id, a.kind, a.as_of, c.code AS country_code,
                      sum(r.gold) AS gold, sum(r.silver) AS silver, sum(r.bronze) AS bronze
               FROM analytics_snapshots a
               JOIN snapshot_rows r ON r.snapshot_id = a.id
               JOIN countries c ON c.id = r.country_id
               WHERE a.competition_id = :c
               GROUP BY a.id, a.kind, a.as_of, c.code
               ORDER BY a.id, c.code"""
        ),
        {"c": competition_id},
    ).all()
    frame = pd.DataFrame(
        rows, columns=["snapshot_id", "kind", "as_of", "country_code", "gold", "silver", "bronze"]
    )
    if frame.empty:
        return frame.assign(podium_rank=[])
    parts = []
    for _, part in frame.groupby("snapshot_id", sort=True):
        part = part.copy()
        part["podium_rank"] = competition_rank(part, ["gold", "silver", "bronze"])
        parts.append(part)
    return pd.concat(parts, ignore_index=True)
