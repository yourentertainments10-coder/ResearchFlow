"""``sie analyze``: every table of docs/ANALYTICS_SPEC.md section 12 from ``v_medal_facts``.

``build_tables`` is pure over frames; ``run_analysis`` reads the database, checks the invariants, takes
the snapshots (spec 10) and writes CSV, Excel and a manifest. Data-quality context (spec 11) goes into
the manifest and into the reconciliation, quarantine and changes tables. Nothing is estimated: a
reconciliation that was not run says ``not_run``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import Connection, text

from sie.analytics import metrics
from sie.analytics.facts import competition_id, load_medal_facts
from sie.analytics.insights import build_insights
from sie.analytics.invariants import assert_invariants
from sie.analytics.snapshots import (
    SnapshotResult,
    latest_changes,
    rank_trajectory,
    take_snapshot,
)

SPEC_TABLES = (
    "country_summary", "country_sport", "country_sport_gender", "sport_summary", "gender_summary",
    "concentration", "rca", "conversion", "tiers", "changes", "reconciliation", "quarantine",
)  # fmt: skip
RECONCILIATION_COLUMNS = [
    "country_code", "gold_db", "silver_db", "bronze_db", "gold_official", "silver_official",
    "bronze_official", "matches", "status", "note",
]  # fmt: skip
QUARANTINE_COLUMNS = ["id", "run_id", "reason", "row_number", "created_at", "resolved"]


@dataclass
class Analysis:
    tables: dict[str, pd.DataFrame]
    manifest: dict


def frame_from_facts(facts: pd.DataFrame) -> pd.DataFrame:
    """Facts under the column names the metric functions use."""
    return pd.DataFrame(
        {
            "country_code": facts["country_code"],
            "country_name": facts["country"],
            "sport": facts["sport"],
            "discipline_name": facts["discipline"],
            "event_id": facts["event_id"],
            "gender": facts["gender"],
            "medal": facts["medal"],
        }
    )


def official_records(path: Path) -> list[dict]:
    """The official all-country table from a capture (``all_standings``) or the bare list."""
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["all_standings"] if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError(f"{path} has no official all-country table")
    return records


def reconcile_with_official(df: pd.DataFrame, records: list[dict]) -> pd.DataFrame:
    """Per country: medals from the facts vs the official table (any gender bucket)."""
    mine = df.groupby(["country_code", "medal"]).size()
    keys = {"Gold": "ME_GOLD", "Silver": "ME_SILVER", "Bronze": "ME_BRONZE"}
    official = {
        rec["Org"]: {m: sum(rec["Count"][k].get(g, 0) for g in "MWX") for m, k in keys.items()}
        for rec in records
    }
    rows = []
    for code in sorted(set(official) | set(df["country_code"])):
        db = {m: int(mine.get((code, m), 0)) for m in keys}
        off = official.get(code)
        matches = off is not None and all(db[m] == off[m] for m in keys)
        rows.append(
            {
                "country_code": code,
                "gold_db": db["Gold"], "silver_db": db["Silver"], "bronze_db": db["Bronze"],
                "gold_official": off["Gold"] if off else None,
                "silver_official": off["Silver"] if off else None,
                "bronze_official": off["Bronze"] if off else None,
                "matches": matches,
                "status": "match" if matches else "mismatch",
                "note": None if off else "country missing from the official table",
            }
        )  # fmt: skip
    return pd.DataFrame(rows, columns=RECONCILIATION_COLUMNS)


def not_run_reconciliation() -> pd.DataFrame:
    row = dict.fromkeys(RECONCILIATION_COLUMNS)
    row["status"] = "not_run"
    row["note"] = "no official table was supplied (use --official) and none is stored for this run"
    return pd.DataFrame([row], columns=RECONCILIATION_COLUMNS)


def reconciliation_status(table: pd.DataFrame) -> str:
    statuses = set(table["status"])
    if statuses == {"not_run"}:
        return "not_run"
    return "reconciled" if statuses == {"match"} else "mismatch"


def build_tables(
    df: pd.DataFrame,
    events: pd.DataFrame,
    *,
    reconciliation: pd.DataFrame,
    quarantine: pd.DataFrame,
    changes: pd.DataFrame,
    trajectory: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """All output tables, in a fixed order. Pure."""
    sport_done = metrics.unit_completion(events, "sport")
    disc_done = metrics.unit_completion(events, "discipline")
    women = df[df["gender"] == "Women"]

    country_summary = metrics.country_summary(df)
    country_sport = metrics.country_sport(df)
    csg = metrics.country_sport_gender(df)
    sport_summary = metrics.sport_summary(df, "sport", sport_done)
    gender_summary = metrics.gender_summary(df)
    conc = metrics.concentration(df)
    rca = metrics.rca(df)
    assert_invariants(
        df,
        country_summary=country_summary,
        country_sport=country_sport,
        country_sport_gender=csg,
        sport_summary=sport_summary,
        gender_summary=gender_summary,
        concentration=conc,
    )
    women_cs = metrics.country_sport(women)
    tables = {
        "country_summary": country_summary,
        "country_sport": country_sport,
        "country_sport_gender": csg,
        "sport_summary": sport_summary,
        "gender_summary": gender_summary,
        "concentration": conc,
        "rca": rca,
        "conversion": metrics.conversion(df, "sport", sport_done),
        "tiers": metrics.tiers(df, "sport", sport_done),
        "changes": changes,
        "reconciliation": reconciliation,
        "quarantine": quarantine,
        "country_discipline": metrics.country_sport(df, "discipline"),
        "discipline_summary": metrics.sport_summary(df, "discipline", disc_done),
        "women_country_summary": metrics.country_summary(women),
        "women_concentration": metrics.concentration(women),
        "women_rca": metrics.rca(women),
        "insights": build_insights(
            country_sport=country_sport,
            concentration=conc,
            rca=rca,
            gender_summary=gender_summary,
            women_country_sport=women_cs,
        ),
        "rank_trajectory": trajectory,
    }
    return tables


def _quarantine(conn: Connection, competition: int) -> pd.DataFrame:
    rows = conn.execute(
        text(
            """SELECT q.id, q.run_id, q.reason, q.payload_json ->> 'row_number' AS row_number,
                      q.created_at, q.resolved
               FROM quarantine q JOIN ingest_runs r ON r.id = q.run_id
               WHERE r.competition_id = :c ORDER BY q.id"""
        ),
        {"c": competition},
    ).all()
    return pd.DataFrame(rows, columns=QUARANTINE_COLUMNS)


def _stored_reconciliation(conn: Connection, competition: int) -> pd.DataFrame | None:
    rows = conn.execute(
        text(
            """SELECT c.code AS country_code, x.gold_db, x.silver_db, x.bronze_db, x.gold_official,
                      x.silver_official, x.bronze_official, x.matches, x.note
               FROM reconciliation_results x
               JOIN countries c ON c.id = x.country_id
               WHERE x.run_id = (SELECT max(x2.run_id) FROM reconciliation_results x2
                                 JOIN ingest_runs r ON r.id = x2.run_id
                                 WHERE r.competition_id = :c)
               ORDER BY c.code"""
        ),
        {"c": competition},
    ).all()
    if not rows:
        return None
    frame = pd.DataFrame(rows, columns=[c for c in RECONCILIATION_COLUMNS if c != "status"])
    frame["status"] = frame["matches"].map({True: "match", False: "mismatch"})
    return frame[RECONCILIATION_COLUMNS]


def run_analysis(
    conn: Connection,
    code: str,
    *,
    now: datetime,
    official: Path | None = None,
    snapshot: bool = True,
) -> tuple[Analysis, SnapshotResult | None]:
    """Read the facts, check, optionally snapshot, and return every table plus the manifest."""
    competition = competition_id(conn, code)
    facts = load_medal_facts(conn, competition)
    if facts.empty:
        raise ValueError(f"competition {code!r} has no medals yet: nothing to analyse")
    df = frame_from_facts(facts)
    events = pd.DataFrame(
        conn.execute(
            text(
                """SELECT sport, discipline, status FROM reporting.events
                   WHERE competition_id = :c ORDER BY event_id"""
            ),
            {"c": competition},
        ).all(),
        columns=["sport", "discipline", "status"],
    )
    if official is not None:
        reconciliation = reconcile_with_official(df, official_records(official))
    else:
        reconciliation = _stored_reconciliation(conn, competition)
        if reconciliation is None:
            reconciliation = not_run_reconciliation()

    result = take_snapshot(conn, competition, now=now) if snapshot else None
    tables = build_tables(
        df,
        events,
        reconciliation=reconciliation,
        quarantine=_quarantine(conn, competition),
        changes=latest_changes(conn, competition),
        trajectory=rank_trajectory(conn, competition),
    )
    completed = int(events["status"].isin(["completed", "amended"]).sum())
    sport_done = metrics.unit_completion(events, "sport")
    manifest = {
        "competition": code,
        "analytics_version": metrics.ANALYTICS_VERSION,
        "generated_at": now.isoformat(),
        "medals": len(df),
        "countries": int(df["country_code"].nunique()),
        "sports": int(df["sport"].nunique()),
        "disciplines": int(df["discipline_name"].nunique()),
        "events_total": len(events),
        "events_completed": completed,
        "partial_sports": sorted(sport_done.loc[sport_done["partial"], "sport"]),
        "disputed_events": int(facts.drop_duplicates("event_id")["is_disputed"].sum()),
        "reconciliation": reconciliation_status(reconciliation),
        "quarantined_unresolved": int((~tables["quarantine"]["resolved"].astype(bool)).sum()),
        "snapshot": None
        if result is None
        else {
            "fingerprint": result.fingerprint,
            "change_snapshot_id": result.change_id,
            "daily_snapshot_id": result.daily_id,
            "changes": result.changes,
        },
        "thresholds": {
            "hhi_concentrated": metrics.HHI_CONCENTRATED,
            "hhi_moderate": metrics.HHI_MODERATE,
            "small_sample_medals": metrics.SMALL_SAMPLE,
        },
        "tables": {name: len(frame) for name, frame in tables.items()},
    }
    return Analysis(tables, manifest), result


def _excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Excel cannot store timezone-aware times: write them as naive UTC (the CSVs keep the offset)."""
    out = frame.copy()
    for column in out.columns:
        values = out[column].dropna()
        if len(values) and getattr(values.iloc[0], "tzinfo", None) is not None:
            out[column] = pd.to_datetime(out[column], utc=True).dt.tz_localize(None)
    return out


def write_outputs(analysis: Analysis, out_dir: Path, *, excel: bool = True) -> list[Path]:
    """CSV per table, one workbook, and the manifest (written last)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in analysis.tables.items():
        path = out_dir / f"{name}.csv"
        frame.to_csv(path, index=False)
        written.append(path)
    if excel:
        try:
            import openpyxl  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Excel output needs openpyxl (the 'reports' extra); install it or use --no-excel"
            ) from exc
        workbook = out_dir / "analysis.xlsx"
        with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
            for name, frame in analysis.tables.items():
                _excel_safe(frame).to_excel(writer, sheet_name=name[:31], index=False)
        written.append(workbook)
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps(analysis.manifest, indent=2, default=str), encoding="utf-8")
    written.append(manifest)
    return written
