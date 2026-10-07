"""Conflict policy on a real PostgreSQL: observations, conflicts, the disputed flag, resolution.

The official capture is loaded first, then a manual claim that disagrees with it. The clock is passed
to ``run_ingest`` so freshness is deterministic.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.config import Settings
from sie.db.conflicts import ConflictError, list_conflicts, resolve_conflict
from sie.pipeline.runner import SourceInput, run_ingest
from sie.reference import seed_reference
from sie.sources.bornan import to_parsed
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
BORNAN = ROOT / "tests/fixtures/sources/bornan"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


def official(settings, country=None):
    """The ARC/SWM capture; ``country`` re-awards the first archery medal."""
    medals = {
        d: json.loads((BORNAN / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    if country:
        medals["ARC"][0]["Org"], medals["ARC"][0]["OrgDesc"] = country
    return SourceInput(
        source=to_parsed.SOURCE,
        url="capture:test.json",
        content=json.dumps({"medals": medals}).encode(),
        content_type="application/json",
        extension="json",
        parse=lambda b: to_parsed.capture_to_parsed(b, settings.competition_id),
    )


def manual_claim(engine, country_code: str, name: str = "claim.csv") -> SourceInput:
    """A manual CSV that gives the first archery gold (as loaded from the capture) to ``country_code``."""
    with engine.connect() as c:
        fact = c.execute(
            text(
                """SELECT sport, discipline, event, gender FROM v_medal_facts
                   WHERE medal = 'Gold' AND discipline = 'Archery' AND country_code = 'KOR'
                   ORDER BY event_id LIMIT 1"""
            )
        ).one()
    header = "competition,sport,discipline,event,gender,medal,country,athlete_or_team,date"
    discipline = "" if fact.discipline == fact.sport else fact.discipline
    line = f"asiad-2026,{fact.sport},{discipline},{fact.event},{fact.gender},Gold,{country_code},,"
    return SourceInput(
        source=manual.SOURCE,
        url=f"file://{name}",
        content=f"{header}\n{line}\n".encode(),
        content_type="text/csv",
        extension="csv",
        parse=lambda raw: manual.parse_manual_csv(raw, max_rows=100, max_cell_chars=200),
    )


def scalar(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


GOLD_COUNTRY = """SELECT country_code FROM v_medal_facts WHERE event_id = :e AND medal = 'Gold'
                  AND slot = 1"""


def open_conflicts(engine) -> list[dict]:
    with engine.connect() as c:
        return list_conflicts(c)


def first_event(engine) -> int:
    return scalar(
        engine,
        """SELECT event_id FROM v_medal_facts WHERE medal = 'Gold' AND discipline = 'Archery'
           AND country_code = 'KOR' ORDER BY event_id LIMIT 1""",
    )


def load_disagreement(seeded, settings):
    first = run_ingest(seeded, official(settings), settings, now=T0)
    assert first.status == "success", first.error
    event = first_event(seeded)
    claim = run_ingest(seeded, manual_claim(seeded, "JPN"), settings, now=T0 + timedelta(minutes=5))
    assert claim.status == "success", claim.error
    return event


def test_official_alone_creates_observations_and_no_conflict(seeded, settings):
    run_ingest(seeded, official(settings), settings, now=T0)
    n = scalar(seeded, "SELECT count(*) FROM placings")
    assert scalar(seeded, "SELECT count(*) FROM source_observations") == n
    assert scalar(seeded, "SELECT count(*) FROM source_conflicts") == 0
    assert scalar(seeded, "SELECT count(*) FROM events WHERE is_disputed") == 0


def test_rerunning_the_official_source_adds_no_observation_and_changes_nothing(seeded, settings):
    run_ingest(seeded, official(settings), settings, now=T0)
    n = scalar(seeded, "SELECT count(*) FROM source_observations")
    again = run_ingest(seeded, official(settings), settings, now=T0 + timedelta(minutes=1))
    assert again.placings == {"unchanged": scalar(seeded, "SELECT count(*) FROM placings")}
    assert scalar(seeded, "SELECT count(*) FROM source_observations") == n


def test_a_disagreeing_manual_claim_is_held_and_the_event_is_disputed(seeded, settings):
    event = load_disagreement(seeded, settings)
    assert scalar(seeded, GOLD_COUNTRY, e=event) == "KOR"  # the accepted value did not move
    assert (
        scalar(seeded, "SELECT count(*) FROM placing_history WHERE change_type <> 'created'") == 0
    )
    (conflict,) = open_conflicts(seeded)
    assert conflict["policy_rule"] == "official_stale" and conflict["status"] == "needs_review"
    assert {conflict["source_a"], conflict["source_b"]} == {"official", "manual"}
    assert scalar(seeded, "SELECT is_disputed FROM events WHERE id = :e", e=event) is True
    assert scalar(seeded, "SELECT bool_or(is_disputed) FROM reporting.medal_facts") is True


def test_repeating_the_disagreeing_claim_does_not_duplicate_conflicts(seeded, settings):
    load_disagreement(seeded, settings)
    run_ingest(seeded, manual_claim(seeded, "JPN"), settings, now=T0 + timedelta(minutes=10))
    assert scalar(seeded, "SELECT count(*) FROM source_conflicts") == 1
    assert scalar(seeded, "SELECT count(*) FROM source_observations WHERE source = 'manual'") == 1


def test_an_official_refetch_that_still_disagrees_keeps_the_conflict_open(seeded, settings):
    event = load_disagreement(seeded, settings)
    run_ingest(seeded, official(settings), settings, now=T0 + timedelta(minutes=15))
    assert scalar(seeded, GOLD_COUNTRY, e=event) == "KOR"
    assert (
        scalar(seeded, "SELECT count(*) FROM source_conflicts WHERE status = 'needs_review'") == 1
    )
    assert scalar(seeded, "SELECT is_disputed FROM events WHERE id = :e", e=event) is True


def test_an_official_refetch_that_agrees_resolves_the_conflict_with_history(seeded, settings):
    event = load_disagreement(seeded, settings)
    changed = run_ingest(
        seeded, official(settings, ("JPN", "Japan")), settings, now=T0 + timedelta(minutes=15)
    )
    assert changed.placings["reallocated"] == 1
    assert scalar(seeded, GOLD_COUNTRY, e=event) == "JPN"
    assert (
        scalar(seeded, "SELECT count(*) FROM source_conflicts WHERE status = 'needs_review'") == 0
    )
    assert scalar(seeded, "SELECT status FROM source_conflicts") == "resolved"
    assert scalar(seeded, "SELECT is_disputed FROM events WHERE id = :e", e=event) is False
    assert (
        scalar(seeded, "SELECT count(*) FROM placing_history WHERE change_type = 'reallocated'")
        == 1
    )


def test_resolving_in_favour_of_manual_applies_it_and_stores_who_and_why(seeded, settings):
    event = load_disagreement(seeded, settings)
    (conflict,) = open_conflicts(seeded)
    with seeded.begin() as conn:
        change = resolve_conflict(
            conn,
            conflict["id"],
            accept_source="manual",
            note="confirmed on the federation site",
            by="owner",
            now=T0 + timedelta(hours=1),
        )
    assert change == "reallocated"
    assert scalar(seeded, GOLD_COUNTRY, e=event) == "JPN"
    with seeded.connect() as c:
        row = c.execute(text("SELECT * FROM source_conflicts")).one()
    assert row.status == "resolved" and row.resolved_by == "owner"
    assert "federation site" in row.resolution_note and row.resolved_at is not None
    assert scalar(seeded, "SELECT is_disputed FROM events WHERE id = :e", e=event) is False
    assert scalar(seeded, "SELECT reason FROM placing_history WHERE change_type = 'reallocated'")


def test_resolving_in_favour_of_official_keeps_the_value_and_clears_the_flag(seeded, settings):
    event = load_disagreement(seeded, settings)
    (conflict,) = open_conflicts(seeded)
    with seeded.begin() as conn:
        change = resolve_conflict(
            conn,
            conflict["id"],
            accept_source="official",
            note="official is right",
            by="owner",
            now=T0 + timedelta(hours=1),
        )
    assert change == "unchanged"
    assert scalar(seeded, GOLD_COUNTRY, e=event) == "KOR"
    assert scalar(seeded, "SELECT is_disputed FROM events WHERE id = :e", e=event) is False


def test_resolution_errors_are_specific(seeded, settings):
    load_disagreement(seeded, settings)
    (conflict,) = open_conflicts(seeded)
    now = T0 + timedelta(hours=1)
    with seeded.begin() as conn:
        with pytest.raises(ConflictError, match="note"):
            resolve_conflict(
                conn, conflict["id"], accept_source="manual", note=" ", by="o", now=now
            )
        with pytest.raises(ConflictError, match="not 'scraper'"):
            resolve_conflict(
                conn, conflict["id"], accept_source="scraper", note="x", by="o", now=now
            )
        with pytest.raises(ConflictError, match="no conflict"):
            resolve_conflict(conn, 99999, accept_source="manual", note="x", by="o", now=now)
    with seeded.begin() as conn:
        resolve_conflict(conn, conflict["id"], accept_source="manual", note="x", by="o", now=now)
    with seeded.begin() as conn, pytest.raises(ConflictError, match="not open"):
        resolve_conflict(conn, conflict["id"], accept_source="manual", note="x", by="o", now=now)
