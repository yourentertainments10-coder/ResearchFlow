"""Locators: validation, CSV and JSON resolution, deterministic number parsing, hostile input."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from sie.evidence.locators import (
    LocatorInvalid,
    NotLocated,
    TimeBudgetExceeded,
    locate_csv,
    locate_json,
    parse_number,
    validate_locator,
)

CSV = b'country,gold,note\nIndia,6,first\nJapan,8,"two\nlines"\n'


def cell(row, column="gold", **extra):
    return {"row_id": row, "column": column, "delimiter": ",", **extra}


def test_csv_cell_is_found_by_row_and_header():
    assert locate_csv(CSV, cell(1)).text == "6"
    assert locate_csv(CSV, cell(2, "country")).text == "Japan"
    assert locate_csv(CSV, cell(2, "note")).text == "two\nlines"  # a quoted newline is one cell


def test_csv_by_index_and_bom_and_unicode_normalisation():
    data = "﻿name,v\ncafé,1\n".encode()  # BOM, and e + combining accent
    assert locate_csv(data, {"row_id": 1, "column_index": 0, "delimiter": ","}).text == "café"


def test_csv_delimiter_is_never_sniffed():
    semi = b"a;b\n1;2\n"
    with pytest.raises(NotLocated) as err:  # with ',' the header is the single column 'a;b'
        locate_csv(semi, cell(1, "b"))
    assert err.value.code == "column_not_found"
    assert locate_csv(semi, {"row_id": 1, "column": "b", "delimiter": ";"}).text == "2"


@pytest.mark.parametrize(
    ("data", "loc", "code"),
    [
        (CSV, cell(9), "row_not_found"),
        (CSV, cell(1, "missing"), "column_not_found"),
        (b"a,a\n1,2\n", cell(1, "a"), "ambiguous_column"),
        (b"a,b\n1\n", cell(1, "b"), "ragged_row"),
        (b"a,b\n1,\x002\n", cell(1, "b"), "nul_byte"),
        (b"\xff\xfe,b\n1,2\n", cell(1, "b"), "undecodable"),
        (b'a,b\n"1,2\n', cell(1, "b"), "malformed_csv"),
        (
            b"a\n" + b"x" * 40_000 + b"\n",
            {"row_id": 1, "column": "a", "delimiter": ","},
            "cell_too_long",
        ),
    ],
)
def test_csv_failures_name_their_reason(data, loc, code):
    with pytest.raises(NotLocated) as err:
        locate_csv(data, loc)
    assert err.value.code == code


def test_duplicate_headers_need_an_index_that_carries_that_header():
    data = b"a,a\n1,2\n"
    assert (
        locate_csv(data, {"row_id": 1, "column": "a", "column_index": 1, "delimiter": ","}).text
        == "2"
    )
    with pytest.raises(NotLocated) as err:
        locate_csv(data, {"row_id": 1, "column": "zzz", "column_index": 1, "delimiter": ","})
    assert err.value.code == "column_not_found"


def test_csv_stops_at_the_time_budget():
    data = b"a\n" + b"1\n" * 25_000
    with pytest.raises(TimeBudgetExceeded):
        locate_csv(
            data, {"row_id": 24_000, "column": "a", "delimiter": ","}, deadline=time.monotonic() - 1
        )


def test_the_row_cap_bounds_the_work_because_a_row_beyond_it_cannot_be_asked_for():
    from sie.evidence.locators import MAX_ROWS

    with pytest.raises(LocatorInvalid):
        locate_csv(CSV, cell(MAX_ROWS + 1))


def test_too_many_columns_is_refused():
    wide = ",".join(f"c{i}" for i in range(501)).encode() + b"\n" + b",".join([b"1"] * 501) + b"\n"
    with pytest.raises(NotLocated) as err:
        locate_csv(wide, {"row_id": 1, "column": "c0", "delimiter": ","})
    assert err.value.code == "too_many_columns"


JSON = b'[{"name":"India","gold":6,"gdp":1.50,"big":1e2,"ok":true,"none":null,"a/b":"slash","t~":"tilde"}]'


@pytest.mark.parametrize(
    ("pointer", "expected"),
    [
        ("/0/name", "India"),
        ("/0/gold", "6"),
        ("/0/gdp", "1.50"),  # the literal, not the float 1.5
        ("/0/big", "1e2"),  # not silently 100.0
        ("/0/a~1b", "slash"),
        ("/0/t~0", "tilde"),
    ],
)
def test_json_pointer_returns_the_literal_text(pointer, expected):
    assert locate_json(JSON, {"pointer": pointer}).text == expected


@pytest.mark.parametrize(
    ("data", "pointer", "code"),
    [
        (JSON, "/0/ok", "value_not_comparable"),
        (JSON, "/0/none", "value_not_comparable"),
        (JSON, "/0", "value_not_comparable"),
        (JSON, "/1/name", "pointer_not_found"),
        (JSON, "/0/missing", "pointer_not_found"),
        (JSON, "/0/name/x", "pointer_not_found"),
        (JSON, "/00/name", "pointer_not_found"),
        (b'{"a":1,"a":2}', "/a", "duplicate_key"),
        (b'{"a":NaN}', "/a", "invalid_number"),
        (b'{"a":Infinity}', "/a", "invalid_number"),
        (b'{"a":', "/a", "malformed_json"),
        (b"\xff", "/a", "undecodable"),
        (b"[" * 100_000, "/0", "too_deep"),
    ],
)
def test_json_failures_name_their_reason(data, pointer, code):
    with pytest.raises(NotLocated) as err:
        locate_json(data, {"pointer": pointer})
    assert err.value.code == code


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("1234.50", None, Decimal("1234.50")),
        ("-7", None, Decimal("-7")),
        ("1,234", None, None),  # not a number unless a thousands separator is declared
        ("1,234", {"thousands_separator": ","}, Decimal("1234")),
        ("1.234,50", {"decimal_separator": ",", "thousands_separator": "."}, Decimal("1234.50")),
        ("12,5", {"decimal_separator": ","}, Decimal("12.5")),
        ("5%", None, None),
        ("(5)", None, None),
        ("$5", None, None),
        ("1e3", None, None),
        ("", None, None),
        ("007", None, None),
    ],
)
def test_number_parsing_is_deterministic_and_never_guesses(text, fmt, expected):
    assert parse_number(text, fmt) == expected


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        ("table_cell", {"column": "a", "delimiter": ","}),  # no row
        ("table_cell", {"row_id": 0, "column": "a", "delimiter": ","}),
        ("table_cell", {"row_id": 1, "delimiter": ","}),  # no column
        ("table_cell", {"row_id": 1, "column": "a"}),  # delimiter is required, never sniffed
        ("table_cell", {"row_id": 1, "column": "a", "delimiter": ",,"}),
        ("table_cell", {"row_id": 1, "column": "a", "delimiter": ",", "encoding": "latin-1"}),
        ("table_cell", {"row_id": True, "column": "a", "delimiter": ","}),
        ("json_pointer", {"pointer": "name"}),
        ("xlsx_cell", {"sheet": "S", "cell": "7B"}),
        ("xlsx_cell", {"sheet": "S", "cell": "B7", "epoch": 1999}),
        ("char_range", {"start": 5, "end": 5}),
        ("page", {"page": 0}),
        ("none", {"x": 1}),
        ("nonsense", {}),
    ],
)
def test_malformed_locators_are_refused(kind, locator):
    with pytest.raises(LocatorInvalid):
        validate_locator(kind, locator)


def test_well_formed_locators_pass():
    validate_locator("table_cell", cell(3, number_format={"decimal_separator": ","}))
    validate_locator("json_pointer", {"pointer": "/a/0"})
    validate_locator("xlsx_cell", {"sheet": "Sheet1", "cell": "B7", "epoch": 1904})
    validate_locator("char_range", {"start": 0, "end": 10})
    validate_locator("page", {"page": 3})
    validate_locator("none", {})
