"""``sie analyze`` on a real PostgreSQL: the golden data set, snapshots and change detection.

The expected numbers are written out by hand from ``tests/fixtures/manual/golden.csv`` (13 medals in
4 events), not derived from the code under test.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.analytics.analyze import SPEC_TABLES, run_analysis, write_outputs
from sie.analytics.snapshots import fingerprint, latest_changes, rank_trajectory, take_snapshot
from sie.config import Settings
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "tests/fixtures/manual"
DAY1 = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 12:00 in Japan
DAY1_LATER = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)  # 18:00 the same Japanese day
DAY2 = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)

# country, gold, silver, bronze, total, points, podium rank (by hand from golden.csv)
GOLDEN_COUNTRIES = [
    ("IND", 2, 1, 1, 4, 9, 1),
    ("KOR", 1, 2, 1, 4, 8, 2),
    ("CHN", 1, 0, 2, 3, 5, 3),
    ("JPN", 1, 0, 1, 2, 4, 4),
]


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="fs",
    )


@pytest.fixture()
def golden(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    load(engine, settings, "golden.csv", DAY1)
    return engine


def load(engine, settings, name: str, now: datetime) -> None:
    src = SourceInput(
        source=manual.SOURCE,
        url=f"file://{name}",
        content=(MANUAL / name).read_bytes(),
        content_type="text/csv",
        extension="csv",
        parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
    )
    result = run_ingest(engine, src, settings, now=now)
    assert result.status == "success", result.error


def analyse(engine, settings, now, **kwargs):
    with engine.begin() as conn:
        analysis, result = run_analysis(conn, settings.competition_id, now=now, **kwargs)
    return analysis, result


def scalar(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def test_the_golden_data_set_gives_exactly_the_hand_computed_country_table(golden, settings):
    analysis, _ = analyse(golden, settings, DAY1)
    t = analysis.tables["country_summary"]
    got = list(
        zip(
            t["country_code"], t["Gold"], t["Silver"], t["Bronze"], t["Total"], t["Points"],
            t["podium_rank"], strict=True,
        )
    )  # fmt: skip
    assert [tuple(map(lambda v: v if isinstance(v, str) else int(v), r)) for r in got] == (
        GOLDEN_COUNTRIES
    )
    assert int(analysis.tables["country_sport"]["Total"].sum()) == 13


def test_women_in_the_golden_data_are_one_compound_team_gold(golden, settings):
    analysis, _ = analyse(golden, settings, DAY1)
    women = analysis.tables["women_country_summary"]
    assert women["country_code"].tolist() == ["IND"] and int(women["Gold"].iloc[0]) == 1


def test_every_spec_table_is_produced_and_written(golden, settings, tmp_path):
    analysis, _ = analyse(golden, settings, DAY1)
    assert set(SPEC_TABLES) <= set(analysis.tables)
    out = tmp_path / "out"
    written = write_outputs(analysis, out, excel=False)
    names = {p.name for p in written}
    assert {f"{t}.csv" for t in SPEC_TABLES} <= names and "manifest.json" in names
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["medals"] == 13 and manifest["countries"] == 4
    assert manifest["events_total"] == 4 and manifest["events_completed"] == 4
    assert (
        manifest["reconciliation"] == "not_run"
    )  # nothing to compare with: never reported as a pass
    assert manifest["analytics_version"] == "1" and manifest["partial_sports"] == []


def test_the_workbook_has_one_sheet_per_table(golden, settings, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    analysis, _ = analyse(golden, settings, DAY1)
    written = write_outputs(analysis, tmp_path / "x")
    book = openpyxl.load_workbook(next(p for p in written if p.suffix == ".xlsx"))
    assert set(SPEC_TABLES) <= set(book.sheetnames)


def test_snapshots_follow_the_identity_rules(golden, settings):
    _, first = analyse(golden, settings, DAY1)
    assert first.change_id is not None and first.daily_id is not None and first.changes == 0
    _, again = analyse(golden, settings, DAY1_LATER)  # same data, same Japanese day
    assert again.change_id is None and again.daily_id is None
    assert again.fingerprint == first.fingerprint
    assert scalar(golden, "SELECT count(*) FROM analytics_snapshots") == 2
    rows = scalar(
        golden, "SELECT count(*) FROM snapshot_rows WHERE snapshot_id = :s", s=first.change_id
    )
    assert rows == scalar(
        golden, "SELECT count(*) FROM snapshot_rows WHERE snapshot_id = :s", s=first.daily_id
    )
    assert (
        scalar(
            golden, "SELECT sum(total) FROM snapshot_rows WHERE snapshot_id = :s", s=first.change_id
        )
        == 13
    )

    _, next_day = analyse(golden, settings, DAY2)  # a new day: a daily snapshot, still no change
    assert next_day.change_id is None and next_day.daily_id is not None
    assert scalar(golden, "SELECT count(*) FROM analytics_snapshots WHERE kind = 'daily'") == 2


def test_a_snapshot_records_the_analytics_version_and_completion(golden, settings):
    analyse(golden, settings, DAY1)
    with golden.connect() as c:
        row = c.execute(text("SELECT * FROM analytics_snapshots WHERE kind = 'change'")).one()
    assert row.analytics_version == "1" and (row.events_completed, row.events_total) == (4, 4)
    assert row.disputed_events == 0 and str(row.local_date) == "2026-10-01"


def test_a_reallocation_is_detected_against_the_previous_snapshot(golden, settings):
    analyse(golden, settings, DAY1)
    load(golden, settings, "golden_reallocated.csv", DAY2)  # the recurve gold moves IND -> CHN
    _, second = analyse(golden, settings, DAY2)
    assert second.change_id is not None and second.changes > 0
    with golden.begin() as conn:
        changes = latest_changes(conn, _competition(conn))
        trajectory = rank_trajectory(conn, _competition(conn))
    found = {(r.change_type, r.country_code, r.detail) for r in changes.itertuples()}
    assert ("medals_lost", "IND", "Gold -1") in found
    assert ("medals_gained", "CHN", "Gold +1") in found
    assert ("rank_change", "IND", "podium rank 1 -> 3") in found
    assert ("rank_change", "CHN", "podium rank 3 -> 1") in found
    assert not any(c[1] in ("KOR", "JPN") and c[0] == "rank_change" for c in found)
    latest = trajectory[trajectory["snapshot_id"] == second.change_id].set_index("country_code")
    assert latest["podium_rank"].to_dict() == {"CHN": 1, "KOR": 2, "IND": 3, "JPN": 4}


def _competition(conn) -> int:
    return conn.execute(text("SELECT id FROM competitions WHERE code = 'asiad-2026'")).scalar_one()


def test_take_snapshot_is_safe_to_call_twice_in_one_transaction(golden):
    with golden.begin() as conn:
        competition = _competition(conn)
        a = take_snapshot(conn, competition, now=DAY1)
        b = take_snapshot(conn, competition, now=DAY1_LATER)
    assert a.change_id is not None and b.change_id is None and b.daily_id is None


def test_the_fingerprint_is_stable_and_changes_with_the_data(golden, settings):
    with golden.connect() as conn:
        a = fingerprint(conn, _competition(conn))
        assert a == fingerprint(conn, _competition(conn))
    load(golden, settings, "golden_reallocated.csv", DAY2)
    with golden.connect() as conn:
        assert fingerprint(conn, _competition(conn)) != a


def test_an_empty_competition_is_an_error_not_an_empty_report(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    with pytest.raises(ValueError, match="no medals"), engine.begin() as conn:
        run_analysis(conn, settings.competition_id, now=DAY1)
