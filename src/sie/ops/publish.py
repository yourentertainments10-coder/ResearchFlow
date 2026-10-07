"""``sie publish``: a static export bundle of the current results (ROADMAP Phase 6, ADR-016/ADR-030).

The bundle is what may leave the database: it is read only from the ``reporting`` schema (no raw
documents, quarantine, run logs or paths, docs/DATABASE.md section 6), so a hosted copy can never leak
pipeline internals. It is data, not a page: ``medals.csv`` (one row per current medal placing),
``events.csv`` and a ``manifest.json`` that says what was exported, when the data was last good and the
SHA-256 of every file. The dashboard HTML is built separately (``python -m sie.dashboard``).

Deterministic: the same database content gives byte-identical ``medals.csv`` and ``events.csv``
(fixed column order, fixed row order, ``\\n`` line ends). Only the manifest carries the export time.
Files are written to a temporary name and renamed; the manifest is renamed last, so a reader that finds
a manifest finds a complete bundle.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

BUNDLE_FORMAT = 1
MEDAL_COLUMNS = (
    "country_code", "country", "sport", "discipline", "event", "gender", "participation",
    "medal", "slot", "is_tie", "entrant", "event_date", "is_disputed",
)  # fmt: skip
EVENT_COLUMNS = (
    "sport",
    "discipline",
    "event",
    "gender",
    "participation",
    "event_date",
    "status",
    "is_disputed",
)

_MEDALS_SQL = """
SELECT m.country_code, m.country, m.sport, m.discipline, m.event, m.gender, m.participation,
       m.medal, m.slot, m.is_tie, m.entrant, m.event_date, m.is_disputed
FROM reporting.medal_facts m JOIN public.competitions c ON c.id = m.competition_id
WHERE c.code = :code
ORDER BY m.sport, m.discipline, m.event, m.gender,
         CASE m.medal WHEN 'Gold' THEN 1 WHEN 'Silver' THEN 2 ELSE 3 END, m.slot, m.country_code
"""
_EVENTS_SQL = """
SELECT e.sport, e.discipline, e.event, e.gender, e.participation, e.event_date, e.status, e.is_disputed
FROM reporting.events e JOIN public.competitions c ON c.id = e.competition_id
WHERE c.code = :code
ORDER BY e.sport, e.discipline, e.event, e.gender
"""


class PublishError(RuntimeError):
    """Nothing was published, and the output directory is unchanged."""


@dataclass(frozen=True)
class PublishResult:
    out_dir: Path
    manifest: dict[str, Any]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _csv(columns: tuple[str, ...], rows: list[Any]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue().encode("utf-8")


def build_bundle(
    engine: Engine, competition: str, now: datetime
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Return ({file name: bytes}, manifest). Pure with respect to the filesystem."""
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        comp = conn.execute(
            text("SELECT name, timezone, official_event_total FROM competitions WHERE code = :c"),
            {"c": competition},
        ).one_or_none()
        if comp is None:
            raise PublishError(f"unknown competition {competition!r}")
        medals = conn.execute(text(_MEDALS_SQL), {"code": competition}).all()
        events = conn.execute(text(_EVENTS_SQL), {"code": competition}).all()
        good = conn.execute(
            text(
                """SELECT max(r.finished_at) FROM ingest_runs r
                   JOIN competitions c ON c.id = r.competition_id
                   WHERE c.code = :c AND r.status = 'success'"""
            ),
            {"c": competition},
        ).scalar_one()
    if not medals:
        raise PublishError(f"no current medal placings for {competition!r}; nothing to publish")

    files = {"medals.csv": _csv(MEDAL_COLUMNS, medals), "events.csv": _csv(EVENT_COLUMNS, events)}
    disputed = {(m.sport, m.discipline, m.event, m.gender) for m in medals if m.is_disputed}
    manifest = {
        "format": BUNDLE_FORMAT,
        "competition": {"code": competition, "name": comp.name, "timezone": comp.timezone},
        "exported_at": now.isoformat(),
        "data_as_of": good.isoformat() if good else None,
        "counts": {
            "medal_rows": len(medals),
            "events_with_medals": len({(m.sport, m.discipline, m.event, m.gender) for m in medals}),
            "events": len(events),
            "official_event_total": comp.official_event_total,
            "disputed_events": len(disputed),
        },
        "data_fingerprint": hashlib.sha256(files["medals.csv"] + files["events.csv"]).hexdigest(),
        "files": {
            name: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
            for name, b in files.items()
        },
    }
    return files, manifest


def publish(engine: Engine, competition: str, out_dir: Path, now: datetime) -> PublishResult:
    files, manifest = build_bundle(engine, competition, now)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    pending: list[tuple[Path, Path]] = []
    try:
        for name, content in {**files, "manifest.json": manifest_bytes}.items():  # manifest last
            final = out_dir / name
            tmp = out_dir / f".{name}.tmp"
            tmp.write_bytes(content)
            pending.append((tmp, final))
        for tmp, final in pending:
            os.replace(tmp, final)
    finally:
        for tmp, _ in pending:
            tmp.unlink(missing_ok=True)
    return PublishResult(out_dir, manifest)


def verify_bundle(out_dir: Path) -> list[str]:
    """Problems found in an existing bundle (empty list = intact). Used by tests and by consumers."""
    try:
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest.json unreadable: {exc}"]
    problems = []
    for name, info in manifest.get("files", {}).items():
        path = out_dir / name
        if not path.exists():
            problems.append(f"{name} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
            problems.append(f"{name} does not match its recorded hash")
    return problems
