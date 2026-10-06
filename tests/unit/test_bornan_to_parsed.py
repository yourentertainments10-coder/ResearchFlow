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


def test_two_placings_of_one_medal_are_marked_as_a_tie_and_single_ones_are_not():
    rows = capture_to_parsed(capture(), "asiad-2026")
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        counts[(r.external_key, r.medal)] = counts.get((r.external_key, r.medal), 0) + 1
    assert any(n > 1 for n in counts.values())
    for r in rows:
        assert (r.is_tie == "yes") == (counts[(r.external_key, r.medal)] > 1)


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


def _row(disc: str, event: str, medal: str, order: int, org: str, kind: str = "A") -> dict:
    return {
        "Medal": medal, "Order": order, "Org": org, "OrgDesc": org, "Reg": "1", "Type": kind,
        "DateRaw": "2026-10-01T10:00:00+09:00", "Name": "X", "Disc": disc, "DiscDesc": disc.title(),
        "Event": event, "EventDesc": "Final", "Bib": "1", "Gender": "M",
    }  # fmt: skip


def test_the_same_event_code_in_two_disciplines_is_two_events():
    """Portal event codes repeat across disciplines: a tie or a team event in one must not leak."""
    code = "M.INDIVID----------.FNL-"
    medals = {
        "AAA": [
            _row("AAA", code, "ME_GOLD", 1, "IND"),
            _row("AAA", code, "ME_GOLD", 2, "KOR", "T"),
        ],
        "BBB": [_row("BBB", code, "ME_GOLD", 1, "CHN"), _row("BBB", code, "ME_SILVER", 1, "JPN")],
    }
    rows = capture_to_parsed(json.dumps({"medals": medals}).encode(), "asiad-2026")
    by_disc = {}
    for r in rows:
        by_disc.setdefault(r.sport, []).append(r)
    assert {r.is_tie for r in by_disc["Aaa"]} == {"yes"}
    assert {r.participation for r in by_disc["Aaa"]} == {"Team"}
    assert {r.is_tie for r in by_disc["Bbb"]} == {"no"}
    assert {r.participation for r in by_disc["Bbb"]} == {"Individual"}
