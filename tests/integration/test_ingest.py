"""The single ingestion path, end to end on a real PostgreSQL (docs/ROADMAP.md Phase 2 acceptance).

Both sources, the portal capture and the manual CSV, go through ``run_ingest``; these tests cover the
shared behaviour once and the source-specific parts separately.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.config import Settings
from sie.pipeline.raw import read_raw
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.bornan import to_parsed
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
BORNAN = ROOT / "tests/fixtures/sources/bornan"
MANUAL = ROOT / "tests/fixtures/manual"


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
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def csv_source(name: str, settings: Settings) -> SourceInput:
    return SourceInput(
        source=manual.SOURCE,
        url=f"file://{name}",
        content=(MANUAL / name).read_bytes(),
        content_type="text/csv",
        extension="csv",
        parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
    )


def csv_text_source(url: str, content: bytes) -> SourceInput:
    return SourceInput(
        source=manual.SOURCE,
        url=url,
        content=content,
        content_type="text/csv",
        extension="csv",
        parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
    )


def capture_source(settings: Settings, mutate=None) -> SourceInput:
    medals = {
        d: json.loads((BORNAN / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    if mutate:
        mutate(medals)
    raw = json.dumps({"medals": medals}).encode()
    return SourceInput(
        source=to_parsed.SOURCE,
        url="capture:test.json",
        content=raw,
        content_type="application/json",
        extension="json",
        parse=lambda b: to_parsed.capture_to_parsed(b, settings.competition_id),
    )


def count(engine, sql: str, **params) -> int:
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


# --- portal capture (behaviour of the former load-capture loader, now on the shared path) -----------


def test_capture_loads_all_events_and_placings(seeded, settings):
    src = capture_source(settings)
    rows = to_parsed.capture_to_parsed(src.content, settings.competition_id)
    result = run_ingest(seeded, src, settings)
    assert result.status == "success", result.error
    assert result.rows_quarantined == 0
    assert result.placings == {"created": len(rows)}
    assert result.events == len({r.external_key for r in rows})
    assert count(seeded, "SELECT count(*) FROM v_medal_facts") == len(rows)
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_tie") >= 2  # the swimming gold tie


def test_capture_rerun_is_idempotent(seeded, settings):
    first = run_ingest(seeded, capture_source(settings), settings)
    again = run_ingest(seeded, capture_source(settings), settings)
    n = sum(first.placings.values())
    assert again.placings == {"unchanged": n} and again.raw_outcome == "unchanged"
    assert count(seeded, "SELECT count(*) FROM placings") == n
    assert count(seeded, "SELECT count(*) FROM placing_history") == n
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1
    assert count(seeded, "SELECT count(*) FROM events") == first.events


def test_capture_with_a_changed_country_is_a_reallocation(seeded, settings):
    first = run_ingest(seeded, capture_source(settings), settings)

    def swap(medals):
        row = medals["ARC"][0]
        row["Org"], row["OrgDesc"] = (
            ("KOR", "South Korea") if row["Org"] != "KOR" else ("JPN", "Japan")
        )

    changed = run_ingest(seeded, capture_source(settings, swap), settings)
    assert changed.placings["reallocated"] == 1 and changed.raw_outcome == "new_version"
    n = sum(first.placings.values())
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_current") == n
    assert count(seeded, "SELECT count(*) FROM placings WHERE NOT is_current") == 1
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 2


def test_capture_unknown_country_is_quarantined_and_the_rest_still_loads(seeded, settings):
    def unknown(medals):
        medals["ARC"][0]["Org"] = "ZZZ"

    result = run_ingest(seeded, capture_source(settings, unknown), settings)
    clean = to_parsed.capture_to_parsed(capture_source(settings).content, settings.competition_id)
    assert result.status == "success"
    assert result.rows_quarantined == 1
    assert sum(result.placings.values()) == len(clean) - 1
    with seeded.connect() as c:
        reason, payload = c.execute(text("SELECT reason, payload_json FROM quarantine")).one()
    assert reason == "UNKNOWN_COUNTRY" and payload["row"]["country"] == "ZZZ"


def test_capture_rows_carry_no_personal_data(seeded, settings):
    rows = to_parsed.capture_to_parsed(capture_source(settings).content, settings.competition_id)
    assert all(r.entrant == "" for r in rows)
    assert count(seeded, "SELECT count(*) FROM entrants") == 0


# --- manual CSV: the golden data set -------------------------------------------------------------


def test_golden_csv_loads_with_the_documented_semantics(seeded, settings):
    result = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    assert result.status == "success", result.error
    assert (result.events, result.rows_seen, result.rows_quarantined) == (4, 13, 0)
    assert result.placings == {"created": 13}
    with seeded.connect() as c:
        bronzes = c.execute(
            text(
                """SELECT count(*) FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.name = 'Men''s 57kg' AND p.medal = 'Bronze' AND p.is_current"""
            )
        ).scalar_one()
        tie = c.execute(
            text(
                """SELECT count(*) FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.name = 'Men''s 100m' AND p.medal = 'Gold' AND p.is_tie"""
            )
        ).scalar_one()
        participation = dict(c.execute(text("SELECT name, participation FROM events")).all())
        sources = {r[0] for r in c.execute(text("SELECT DISTINCT source FROM placings"))}
        note = c.execute(
            text("SELECT source_note FROM placings WHERE source_note IS NOT NULL LIMIT 1")
        ).scalar_one()
    assert bronzes == 2  # double bronze in boxing
    assert tie == 2  # the marked tie keeps two golds
    assert participation["Compound Women's Team"] == "Team"
    assert participation["Recurve Men's Individual"] == "Individual"
    assert sources == {"manual"}
    assert note in {"final", "https://example.org/archery"}


def test_golden_csv_twice_changes_nothing(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    before = {
        t: count(seeded, f"SELECT count(*) FROM {t}")
        for t in ("placings", "placing_history", "events", "raw_versions", "quarantine")
    }
    again = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    assert again.placings == {"unchanged": 13} and again.raw_outcome == "unchanged"
    after = {t: count(seeded, f"SELECT count(*) FROM {t}") for t in before}
    assert after == before


def test_a_reallocation_closes_the_old_placing_and_writes_one_history_row(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    changed = run_ingest(seeded, csv_source("golden_reallocated.csv", settings), settings)
    assert changed.placings["reallocated"] == 1
    assert changed.placings["unchanged"] == 12
    with seeded.connect() as c:
        closed = c.execute(
            text("SELECT count(*) FROM placings WHERE NOT is_current AND valid_to IS NOT NULL")
        ).scalar_one()
        history = (
            c.execute(
                text(
                    "SELECT old_country_id <> new_country_id FROM placing_history WHERE change_type = 'reallocated'"
                )
            )
            .scalars()
            .all()
        )
    assert closed == 1 and history == [True]
    assert count(seeded, "SELECT count(*) FROM placing_history") == 14  # 13 created + 1 reallocated


# --- quarantine --------------------------------------------------------------------------------------


def test_bad_rows_are_quarantined_with_reasons_and_valid_rows_still_load(seeded, settings):
    result = run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    assert result.status == "success", result.error
    assert result.placings == {"created": 13}
    assert result.rows_quarantined == 3
    with seeded.connect() as c:
        reasons = sorted(c.execute(text("SELECT reason FROM quarantine")).scalars())
        unresolved = c.execute(
            text("SELECT count(*) FROM quarantine WHERE NOT resolved")
        ).scalar_one()
    assert reasons == ["UNKNOWN_COUNTRY", "UNKNOWN_GENDER", "UNKNOWN_SPORT"]
    assert unresolved == 3
    assert count(seeded, "SELECT count(*) FROM events WHERE name = 'Mystery Cup'") == 0


def test_quarantine_is_not_duplicated_on_rerun(seeded, settings):
    run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    again = run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    assert again.rows_quarantined == 3  # still bad, and reported
    assert count(seeded, "SELECT count(*) FROM quarantine") == 3  # but stored once


def test_adding_an_alias_and_rerunning_resolves_the_quarantined_row(seeded, settings):
    run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    with seeded.begin() as c:
        india = c.execute(text("SELECT id FROM countries WHERE code = 'IND'")).scalar_one()
        c.execute(
            text("INSERT INTO country_aliases (alias_norm, country_id) VALUES ('zzz', :i)"),
            {"i": india},
        )
    again = run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    assert again.rows_quarantined == 2
    assert count(seeded, "SELECT count(*) FROM quarantine WHERE resolved") == 1
    assert count(seeded, "SELECT count(*) FROM placings WHERE is_current") == 14


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (
            "asiad-2026,Archery,,Recurve Men's Individual,Men,Gold,IND\n"
            "asiad-2026,Archery,,Recurve Men's Individual,Men,Gold,KOR\n",
            "MEDAL_COUNT_EXCEEDED",  # a second gold without a marked tie
        ),
        (
            "asiad-2026,Archery,,Compound Women's Team,Women,Gold,IND\n"
            "asiad-2026,Archery,,Compound Women's Team,Women,Silver,IND\n",
            "DUPLICATE_COUNTRY_IN_TEAM_EVENT",  # one country once per team event
        ),
        (
            "asiad-2026,Archery,,Recurve Men's Individual,Men,Bronze,IND\n"
            "asiad-2026,Archery,,Recurve Men's Individual,Men,Bronze,KOR\n",
            "MEDAL_COUNT_EXCEEDED",  # double bronze only in flagged sports
        ),
    ],
)
def test_validator_rules_quarantine_the_offending_row(seeded, settings, rows, reason):
    header = "competition,sport,discipline,event,gender,medal,country\n"
    result = run_ingest(
        seeded, csv_text_source("file://rules.csv", (header + rows).encode()), settings
    )
    assert result.status == "success", result.error
    assert result.rows_quarantined == 1
    assert count(seeded, "SELECT count(*) FROM quarantine WHERE reason = :r", r=reason) == 1
    assert count(seeded, "SELECT count(*) FROM placings") == 1  # the first row still loaded


# --- raw storage and versions --------------------------------------------------------------------------


def test_the_first_capture_is_stored_and_can_be_read_back(seeded, settings):
    src = csv_source("golden.csv", settings)
    result = run_ingest(seeded, src, settings)
    assert result.raw_outcome == "new_version"
    with seeded.connect() as c:
        backend, path = c.execute(text("SELECT storage_backend, path FROM raw_versions")).one()
        assert backend == "fs" and path.startswith("manual/")
        assert (settings.data_dir / "raw" / path).read_bytes() == src.content
        assert read_raw(c, result.raw_version_id, settings.data_dir / "raw") == src.content


def test_the_database_backend_keeps_the_bytes_in_raw_blobs(seeded, settings):
    settings = settings.model_copy(update={"raw_store_backend": "db"})
    src = csv_source("golden.csv", settings)
    result = run_ingest(seeded, src, settings)
    with seeded.connect() as c:
        backend, path = c.execute(text("SELECT storage_backend, path FROM raw_versions")).one()
        assert (backend, path) == ("db", None)
        assert read_raw(c, result.raw_version_id, settings.data_dir / "raw") == src.content
    assert not (settings.data_dir / "raw").exists()


def test_identical_content_is_not_stored_again_and_changed_content_is(seeded, settings):
    a = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    same = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    changed = run_ingest(seeded, csv_source("golden_reallocated.csv", settings), settings)
    assert (a.raw_outcome, same.raw_outcome, changed.raw_outcome) == (
        "new_version",
        "unchanged",
        "new_version",
    )
    assert same.raw_version_id == a.raw_version_id != changed.raw_version_id
    assert (
        count(seeded, "SELECT count(*) FROM raw_versions") == 2
    )  # two documents, one version each
    assert count(seeded, "SELECT count(*) FROM raw_documents") == 2


def test_content_that_reverts_to_an_older_version_moves_the_latest_pointer_back(seeded, settings):
    url = "file://same-name.csv"
    one = (MANUAL / "golden.csv").read_bytes()
    two = (MANUAL / "golden_reallocated.csv").read_bytes()
    v1 = run_ingest(seeded, csv_text_source(url, one), settings)
    v2 = run_ingest(seeded, csv_text_source(url, two), settings)
    back = run_ingest(seeded, csv_text_source(url, one), settings)
    assert (v1.raw_outcome, v2.raw_outcome, back.raw_outcome) == (
        "new_version",
        "new_version",
        "reverted",
    )
    assert back.raw_version_id == v1.raw_version_id
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 2  # no third row for the revert
    with seeded.connect() as c:
        latest = c.execute(text("SELECT latest_version_id FROM raw_documents")).scalar_one()
        outcomes = c.execute(text("SELECT outcome FROM raw_fetches ORDER BY id")).scalars().all()
    assert latest == v1.raw_version_id
    assert outcomes == ["new_version", "new_version", "reverted"]
    # the database follows the source: the reverted file puts the original country back
    assert (
        count(seeded, "SELECT count(*) FROM placing_history WHERE change_type = 'reallocated'") == 2
    )


def test_raw_versions_trace_back_to_the_run_and_the_placings(seeded, settings):
    result = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    with seeded.connect() as c:
        run = c.execute(
            text("SELECT run_id FROM raw_fetches WHERE version_id = :v"),
            {"v": result.raw_version_id},
        ).scalar_one()
        linked = c.execute(
            text("SELECT count(*) FROM placings WHERE raw_version_id = :v"),
            {"v": result.raw_version_id},
        ).scalar_one()
    assert run == result.run_id and linked == 13


def test_failed_processing_keeps_the_raw_input_and_records_the_failure(seeded, settings):
    def explode(_raw):
        raise ValueError("unexpected structure")

    src = SourceInput(
        source="manual",
        url="file://broken.csv",
        content=b"not,a,valid,file\n",
        content_type="text/csv",
        extension="csv",
        parse=explode,
    )
    result = run_ingest(seeded, src, settings)
    assert result.status == "failed" and "unexpected structure" in result.error
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1  # the input is not lost
    assert count(seeded, "SELECT count(*) FROM placings") == 0
    with seeded.connect() as c:
        status, error, finished = c.execute(
            text("SELECT status, error_summary, finished_at FROM ingest_runs")
        ).one()
    assert status == "failed" and "unexpected structure" in error and finished is not None


def test_a_csv_with_a_bad_structure_fails_the_run_but_keeps_the_file(seeded, settings):
    bad = b"competition,sport,event,medal,wrong\nasiad-2026,Archery,E,Gold,IND\n"
    result = run_ingest(seeded, csv_text_source("file://bad.csv", bad), settings)
    assert result.status == "failed" and "unknown columns" in result.error
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


def test_exceeding_the_official_event_total_fails_the_load_and_keeps_nothing(seeded, settings):
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = 1"))
    result = run_ingest(seeded, csv_source("golden.csv", settings), settings)
    assert result.status == "failed" and "EVENT_TOTAL_EXCEEDED" in result.error
    assert count(seeded, "SELECT count(*) FROM placings") == 0  # all or nothing
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1


# --- ingest_runs ---------------------------------------------------------------------------------------


def test_a_run_records_what_was_run_when_and_what_happened(seeded, settings):
    result = run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    with seeded.connect() as c:
        run = c.execute(text("SELECT * FROM ingest_runs")).one()._mapping
    assert run["id"] == result.run_id
    assert run["source"] == "manual" and run["status"] == "success"
    assert run["started_at"] <= run["finished_at"]
    assert (run["docs_fetched"], run["docs_changed"]) == (1, 1)
    assert (run["rows_loaded"], run["rows_quarantined"]) == (13, 3)
    assert run["error_summary"] is None


def test_an_unchanged_rerun_is_recorded_as_such(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    with seeded.connect() as c:
        rows = c.execute(text("SELECT docs_changed FROM ingest_runs ORDER BY id")).scalars().all()
    assert rows == [1, 0]


def test_an_unseeded_database_is_a_setup_error_not_a_failed_run(engine, settings):
    from sie.pipeline.load import LoadError

    with pytest.raises(LoadError, match="seed-reference"):
        run_ingest(engine, csv_source("golden.csv", settings), settings)
    assert count(engine, "SELECT count(*) FROM ingest_runs") == 0


# --- verification added before merge: exact acceptance results, failure recovery, side effects ----------

PODIUMS = """
    SELECT e.name, p.medal, p.slot, c.code, p.is_tie
    FROM placings p
    JOIN events e ON e.id = p.event_id
    JOIN countries c ON c.id = p.country_id
    WHERE p.is_current
    ORDER BY e.name, p.medal, p.slot
"""

# Written out by hand, not derived from the CSV: this is the acceptance result for golden.csv.
GOLDEN_EXPECTED = sorted(
    [
        ("Recurve Men's Individual", "Gold", 1, "IND", False),
        ("Recurve Men's Individual", "Silver", 1, "KOR", False),
        ("Recurve Men's Individual", "Bronze", 1, "CHN", False),
        ("Compound Women's Team", "Gold", 1, "IND", False),
        ("Compound Women's Team", "Silver", 1, "KOR", False),
        ("Compound Women's Team", "Bronze", 1, "CHN", False),
        ("Men's 57kg", "Gold", 1, "CHN", False),
        ("Men's 57kg", "Silver", 1, "IND", False),
        ("Men's 57kg", "Bronze", 1, "KOR", False),
        ("Men's 57kg", "Bronze", 2, "JPN", False),
        ("Men's 100m", "Gold", 1, "JPN", True),
        ("Men's 100m", "Gold", 2, "KOR", True),
        ("Men's 100m", "Bronze", 1, "IND", False),
    ]
)


def podiums(engine):
    with engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(text(PODIUMS)))


def test_golden_csv_produces_exactly_the_expected_podiums(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    assert podiums(seeded) == GOLDEN_EXPECTED
    # and the reporting view the analytics will read agrees with the placings
    assert count(seeded, "SELECT count(*) FROM v_medal_facts") == len(GOLDEN_EXPECTED)


def test_golden_reallocation_changes_exactly_one_placing_and_keeps_the_old_one(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    run_ingest(seeded, csv_source("golden_reallocated.csv", settings), settings)
    expected = [
        ("Recurve Men's Individual", "Gold", 1, "CHN", False)
        if row[:4] == ("Recurve Men's Individual", "Gold", 1, "IND")
        else row
        for row in GOLDEN_EXPECTED
    ]
    assert podiums(seeded) == sorted(expected)
    with seeded.connect() as c:
        old = c.execute(
            text(
                """SELECT c.code, p.is_current, p.valid_to IS NOT NULL AS closed, p.id
                   FROM placings p JOIN countries c ON c.id = p.country_id
                   JOIN events e ON e.id = p.event_id
                   WHERE e.name = 'Recurve Men''s Individual' AND p.medal = 'Gold' AND NOT p.is_current"""
            )
        ).one()
        new = c.execute(
            text(
                """SELECT p.supersedes_id FROM placings p JOIN events e ON e.id = p.event_id
                   WHERE e.name = 'Recurve Men''s Individual' AND p.medal = 'Gold' AND p.is_current"""
            )
        ).scalar_one()
        history = c.execute(
            text(
                """SELECT oc.code, nc.code FROM placing_history h
                   JOIN countries oc ON oc.id = h.old_country_id
                   JOIN countries nc ON nc.id = h.new_country_id
                   WHERE h.change_type = 'reallocated'"""
            )
        ).all()
    assert (old.code, old.is_current, old.closed) == ("IND", False, True)
    assert new == old.id  # the new version points back at the one it replaced
    assert [tuple(r) for r in history] == [("IND", "CHN")]


@pytest.mark.parametrize("backend", ["fs", "db"])
def test_a_failed_load_leaves_no_partial_data_and_a_rerun_recovers(seeded, settings, backend):
    settings = settings.model_copy(update={"raw_store_backend": backend})
    raw_dir = settings.data_dir / "raw"
    src = csv_source("golden.csv", settings)
    with seeded.begin() as c:
        c.execute(text("UPDATE competitions SET official_event_total = 1"))  # forces a late failure

    failed = run_ingest(seeded, src, settings)
    assert failed.status == "failed"
    for table in ("placings", "placing_history", "events", "quarantine"):
        assert count(seeded, f"SELECT count(*) FROM {table}") == 0, table  # nothing half-loaded
    with seeded.connect() as c:  # the input and the failure are both on record
        assert read_raw(c, failed.raw_version_id, raw_dir) == src.content
        status, error = c.execute(text("SELECT status, error_summary FROM ingest_runs")).one()
    assert status == "failed" and error

    with seeded.begin() as c:  # the owner fixes the cause and runs the same file again
        c.execute(text("UPDATE competitions SET official_event_total = NULL"))
    ok = run_ingest(seeded, src, settings)
    assert ok.status == "success", ok.error
    assert ok.raw_outcome == "unchanged" and ok.raw_version_id == failed.raw_version_id
    assert podiums(seeded) == GOLDEN_EXPECTED
    assert count(seeded, "SELECT count(*) FROM raw_versions") == 1  # no second copy of the input
    with seeded.connect() as c:
        statuses = c.execute(text("SELECT status FROM ingest_runs ORDER BY id")).scalars().all()
    assert statuses == ["failed", "success"]


def test_a_rerun_adds_only_its_own_run_and_fetch_records(seeded, settings):
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    tables = (
        "placings",
        "placing_history",
        "events",
        "raw_documents",
        "raw_versions",
        "raw_blobs",
        "quarantine",
        "entrants",
    )
    before = {t: count(seeded, f"SELECT count(*) FROM {t}") for t in tables}
    runs, fetches = (
        count(seeded, f"SELECT count(*) FROM {t}") for t in ("ingest_runs", "raw_fetches")
    )
    run_ingest(seeded, csv_source("golden.csv", settings), settings)
    assert {t: count(seeded, f"SELECT count(*) FROM {t}") for t in tables} == before
    assert count(seeded, "SELECT count(*) FROM ingest_runs") == runs + 1  # the run itself is logged
    assert count(seeded, "SELECT count(*) FROM raw_fetches") == fetches + 1
    assert podiums(seeded) == GOLDEN_EXPECTED


def test_run_counts_match_what_was_loaded_and_quarantined(seeded, settings):
    result = run_ingest(seeded, csv_source("golden_with_bad_rows.csv", settings), settings)
    placed = count(seeded, "SELECT count(*) FROM placings WHERE is_current")
    quarantined = count(seeded, "SELECT count(*) FROM quarantine WHERE NOT resolved")
    assert (result.rows_seen, placed, quarantined) == (16, 13, 3)
    assert result.rows_seen == placed + quarantined + result.duplicates_skipped
