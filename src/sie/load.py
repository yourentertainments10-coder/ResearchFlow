"""Load parsed placings into PostgreSQL (docs/DATA_PIPELINE.md, load stage). Idempotent.

The raw capture is registered as a raw document/version (sha256), every placing goes through
``apply_placing`` so reruns write nothing and later changes become versioned reallocations.
Countries must already exist (seeded reference data); they are never auto-created. The portal's own
discipline list is the sport list for this competition, so each discipline gets a sport of the same name.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, text

from sie.db.placings import apply_placing
from sie.sources.bornan.parse_medals import ParsedPlacing, parse_medal_rows

COMPETITION = "asiad-2026"
SOURCE = "official"


class LoadError(ValueError):
    """Input cannot be loaded without guessing."""


@dataclass
class LoadResult:
    events: int
    placings: dict[str, int]
    raw_version_id: int
    raw_unchanged: bool


def parse_capture(path: Path) -> tuple[list[ParsedPlacing], bytes]:
    raw = path.read_bytes()
    cap = json.loads(raw)
    return [p for rows in cap["medals"].values() for p in parse_medal_rows(rows)], raw


def _raw_version(conn: Connection, raw: bytes, name: str, now: datetime) -> tuple[int, bool]:
    sha = hashlib.sha256(raw).hexdigest()
    doc = conn.execute(
        text(
            """INSERT INTO raw_documents (source, url, first_seen_at) VALUES (:s, :u, :n)
               ON CONFLICT (source, url) DO UPDATE SET source = EXCLUDED.source RETURNING id"""
        ),
        {"s": SOURCE, "u": f"capture:{name}", "n": now},
    ).scalar_one()
    found = conn.execute(
        text("SELECT id FROM raw_versions WHERE document_id = :d AND sha256 = :h"),
        {"d": doc, "h": sha},
    ).scalar_one_or_none()
    if found:
        return found, True
    no = conn.execute(
        text("SELECT coalesce(max(version_no), 0) + 1 FROM raw_versions WHERE document_id = :d"),
        {"d": doc},
    ).scalar_one()
    vid = conn.execute(
        text(
            """INSERT INTO raw_versions (document_id, version_no, sha256, path, size_bytes, content_type, first_fetched_at)
               VALUES (:d, :no, :h, :p, :sz, 'application/json', :n) RETURNING id"""
        ),
        {"d": doc, "no": no, "h": sha, "p": f"data/raw/{name}", "sz": len(raw), "n": now},
    ).scalar_one()
    conn.execute(
        text("UPDATE raw_documents SET latest_version_id = :v WHERE id = :d"), {"v": vid, "d": doc}
    )
    return vid, False


def _discipline_id(conn: Connection, code: str, name: str, cache: dict[str, int]) -> int:
    if code not in cache:
        sport = conn.execute(
            text(
                "INSERT INTO sports (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
        cache[code] = conn.execute(
            text(
                """INSERT INTO disciplines (sport_id, name) VALUES (:s, :n)
                   ON CONFLICT (sport_id, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"""
            ),
            {"s": sport, "n": name},
        ).scalar_one()
    return cache[code]


def load_placings(
    conn: Connection,
    placings: list[ParsedPlacing],
    raw: bytes,
    name: str,
    now: datetime | None = None,
) -> LoadResult:
    now = now or datetime.now(UTC)
    comp = conn.execute(
        text("SELECT id FROM competitions WHERE code = :c"), {"c": COMPETITION}
    ).scalar_one_or_none()
    if comp is None:
        raise LoadError(f"competition {COMPETITION!r} missing: run `sie seed-reference` first")
    countries = {r.code: r.id for r in conn.execute(text("SELECT code, id FROM countries"))}
    missing = sorted({p.country_code for p in placings} - set(countries))
    if missing:
        raise LoadError(
            f"unknown country codes (add them to data/reference/countries.csv): {missing}"
        )
    rv, unchanged = _raw_version(conn, raw, name, now)

    by_event: dict[tuple[str, str], list[ParsedPlacing]] = {}
    for p in placings:
        by_event.setdefault((p.discipline, p.event_code), []).append(p)
    disc_cache: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for (disc, code), rows in by_event.items():
        first = rows[0]
        participation = "Team" if any(r.entrant_type == "T" for r in rows) else "Individual"
        date = max(r.awarded_at for r in rows)[:10] or None
        did = _discipline_id(conn, disc, first.discipline_name, disc_cache)
        event_id = conn.execute(
            text(
                """INSERT INTO events (competition_id, discipline_id, name, name_raw, gender, participation,
                                       event_date, status, external_key)
                   VALUES (:c, :d, :n, :n, :g, :p, :dt, 'completed', :k)
                   ON CONFLICT (competition_id, discipline_id, name, gender)
                   DO UPDATE SET external_key = EXCLUDED.external_key
                   RETURNING id, external_key"""
            ),
            {
                "c": comp,
                "d": did,
                "n": first.event_name,
                "g": first.gender,
                "p": participation,
                "dt": date,
                "k": code,
            },
        ).one()
        # Two different portal events with the same name would silently merge: refuse.
        if event_id.external_key != code:
            raise LoadError(f"event name collision in {disc}: {first.event_name!r}")
        medal_counts = Counter(r.medal for r in rows)
        for r in rows:
            counts[
                apply_placing(
                    conn,
                    event_id=event_id.id,
                    medal=r.medal,
                    slot=r.slot,
                    country_id=countries[r.country_code],
                    entrant_id=None,
                    raw_version_id=rv,
                    source=SOURCE,
                    now=now,
                    is_tie=r.medal != "Bronze" and medal_counts[r.medal] > 1,
                )  # fmt: skip
            ] += 1
    return LoadResult(len(by_event), dict(counts), rv, unchanged)
