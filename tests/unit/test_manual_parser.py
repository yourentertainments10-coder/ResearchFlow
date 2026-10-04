"""The manual CSV parser: strict about structure, never guesses (docs/DATA_PIPELINE.md section 9)."""

from __future__ import annotations

import pytest

from sie.sources.manual.parser import CsvFormatError, parse_manual_csv

HEADER = "competition,sport,event,medal,country"
LIMITS = {"max_rows": 100, "max_cell_chars": 50}


def parse(text: str, **overrides):
    return parse_manual_csv(text.encode("utf-8"), **{**LIMITS, **overrides})


def test_parses_rows_and_keeps_the_source_text_untouched():
    rows = parse(f"{HEADER}\nasiad-2026, Archery ,Recurve Men's Individual,gold,India\n")
    assert len(rows) == 1
    r = rows[0]
    assert (r.competition, r.sport, r.event, r.medal, r.country) == (
        "asiad-2026",
        "Archery",  # trimmed, nothing else changed
        "Recurve Men's Individual",
        "gold",  # not title-cased here: the normaliser decides
        "India",
    )
    assert r.row_number == 2  # the header is line 1


def test_column_order_does_not_matter_and_optional_columns_map_across():
    rows = parse(
        "country,medal,event,sport,competition,athlete_or_team,date,is_tie,slot,participation\n"
        "IND,Gold,Compound Team,Archery,asiad-2026,India,2026-09-30,no,1,Team\n"
    )
    r = rows[0]
    assert (r.entrant, r.date, r.is_tie, r.slot, r.participation) == (
        "India",
        "2026-09-30",
        "no",
        "1",
        "Team",
    )


def test_excel_byte_order_mark_and_blank_lines_are_tolerated():
    content = ("﻿" + f"{HEADER}\n\nasiad-2026,Archery,E,Gold,IND\n,,,,\n").encode()
    rows = parse_manual_csv(content, **LIMITS)
    assert [r.row_number for r in rows] == [3]


def test_quoted_commas_survive():
    rows = parse(f'{HEADER}\nasiad-2026,Archery,"Men\'s, Team",Gold,"Hong Kong, China"\n')
    assert rows[0].country == "Hong Kong, China"
    assert rows[0].event == "Men's, Team"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        (f"{HEADER},nonsense\nasiad-2026,Archery,E,Gold,IND,x\n", "unknown columns"),
        ("competition,sport,event,medal\nasiad-2026,Archery,E,Gold\n", "missing required"),
        (f"{HEADER},country\nasiad-2026,Archery,E,Gold,IND,IND\n", "duplicate columns"),
        (f"{HEADER}\nasiad-2026,Archery,E,Gold\n", "4 cells but the header has 5"),
        (f"{HEADER}\nasiad-2026,Archery,E,Gold,IND,extra\n", "6 cells but the header has 5"),
    ],
)
def test_structural_problems_refuse_the_whole_file(text, message):
    with pytest.raises(CsvFormatError, match=message):
        parse(text)


def test_too_many_rows_and_oversized_cells_are_refused():
    body = "".join(f"asiad-2026,Archery,E{i},Gold,IND\n" for i in range(3))
    with pytest.raises(CsvFormatError, match="more than 2 data rows"):
        parse(f"{HEADER}\n{body}", max_rows=2)
    with pytest.raises(CsvFormatError, match="longer than 50"):
        parse(f"{HEADER}\nasiad-2026,Archery,{'x' * 51},Gold,IND\n")


def test_invalid_utf8_is_refused():
    with pytest.raises(CsvFormatError, match="UTF-8"):
        parse_manual_csv(b"competition,sport,event,medal,country\n\xff\xfe\n", **LIMITS)
