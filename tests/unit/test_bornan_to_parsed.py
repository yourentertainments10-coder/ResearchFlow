"""Portal medal rows become shared ParsedResult rows without losing the rules the loader relies on."""

from __future__ import annotations

import json
from pathlib import Path

from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed

FIX = Path(__file__).parents[1] / "fixtures/sources/bornan"


def capture() -> bytes:
    medals = {
        d: json.loads((FIX / f"{d}_medals_discipline.json").read_text()) for d in ("SWM", "ARC")
    }
    return json.dumps({"medals": medals}).encode()


def test_source_name_is_the_official_source():
    assert SOURCE == "official"


def test_every_row_is_converted_and_numbered_from_one():
    rows = capture_to_parsed(capture(), "asiad-2026")
    assert [r.row_number for r in rows] == list(range(1, len(rows) + 1))
    assert {r.competition for r in rows} == {"asiad-2026"}


def test_no_athlete_name_is_copied():
    assert all(r.entrant == "" for r in capture_to_parsed(capture(), "asiad-2026"))


def test_two_golds_or_silvers_are_marked_as_a_tie_but_bronzes_are_not():
    rows = capture_to_parsed(capture(), "asiad-2026")
    tied = [r for r in rows if r.is_tie == "yes"]
    assert len(tied) >= 2 and all(r.medal in {"Gold", "Silver"} for r in tied)
    assert all(r.is_tie == "no" for r in rows if r.medal == "Bronze")


def test_team_events_are_marked_team_for_every_row_of_the_event():
    rows = capture_to_parsed(capture(), "asiad-2026")
    by_event: dict[str, set[str]] = {}
    for r in rows:
        by_event.setdefault(r.external_key, set()).add(r.participation)
    assert all(len(kinds) == 1 for kinds in by_event.values())  # never mixed within an event
    assert {"Team"} in by_event.values()


def test_event_code_date_and_gender_are_kept_for_the_loader():
    row = capture_to_parsed(capture(), "asiad-2026")[0]
    assert row.external_key and len(row.date) == 10
    assert row.gender in {"Men", "Women", "Mixed", "Open"}
    assert row.slot.isdigit()
