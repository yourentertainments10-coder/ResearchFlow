"""Track B: the profile reports types, nulls, hints and examples without coercing anything."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from sie.datasets import open_dataset, profile
from sie.datasets.models import STANDARD_LIMITATIONS, LoadOptions
from sie.datasets.profile import duplicate_row_count


def prof(text: str, **opts):
    options = LoadOptions(**opts) if opts else None
    ds = open_dataset(text.encode(), "t.csv", options)
    return profile(ds)


def col(result, name):
    return next(c for c in result.column_profiles if c.name == name)


def test_table_level_counts_and_limitations():
    result = prof("a,b,c\n1,x,\n2,x,\n2,x,\n")
    assert result.rows == 3 and result.columns == 3
    assert result.duplicate_rows == 1  # row 3 repeats row 2
    assert result.empty_columns == ["c"]
    assert result.constant_columns == ["b"]
    assert tuple(result.limitations) == STANDARD_LIMITATIONS


def test_integer_column_statistics():
    c = col(prof("n,k\n5,a\n-3,a\n,a\n10,a\n5,a\n"), "n")
    assert (c.type, c.non_null, c.nulls, c.distinct) == ("integer", 4, 1, 3)
    assert c.null_share == 0.2
    assert (c.min, c.max) == ("-3", "10")
    assert c.type_counts == {}


def test_decimal_dates_and_booleans():
    result = prof(
        "d,t,ok,f\n2026-03-01,2026-03-01T10:00:00,true,1.5\n2026-01-15,2026-01-01T09:30:00,FALSE,2\n"
    )
    assert (col(result, "d").type, col(result, "d").min, col(result, "d").max) == (
        "date",
        "2026-01-15",
        "2026-03-01",
    )
    assert (col(result, "t").type, col(result, "t").min) == ("datetime", "2026-01-01T09:30:00")
    assert col(result, "ok").type == "boolean" and col(result, "ok").min is None
    f = col(result, "f")
    assert (f.type, f.min, f.max) == ("decimal", "1.5", "2")


def test_text_column_has_no_min_max_and_distinct_counts():
    c = col(prof("t\nb\na\nb\n"), "t")
    assert (c.type, c.distinct, c.min, c.max) == ("text", 2, None, None)


def test_mixed_column_reports_counts_and_shares_and_is_not_coerced():
    c = col(prof("m\n1\n2\nx\n"), "m")
    assert c.type == "mixed" and c.type_counts == {"integer": 2, "text": 1}
    assert any("integer 66.7%" in h and "text 33.3%" in h for h in c.hints)
    assert c.min is None


def test_one_percent_text_is_still_mixed():
    rows = "\n".join(["1"] * 99 + ["x"])
    c = col(prof("m\n" + rows + "\n"), "m")
    assert c.type == "mixed" and any("integer 99.0%" in h for h in c.hints)


def test_identifier_comma_decimal_and_null_marker_hints():
    result = prof('id,price,flag\n007,"1,5",N/A\n010,"2,5",x\n')
    assert (
        any("identifier" in h for h in col(result, "id").hints) and col(result, "id").type == "text"
    )
    assert any("comma-decimal" in h for h in col(result, "price").hints)
    assert any("null marker" in h for h in col(result, "flag").hints)


def test_ambiguous_dates_stay_text_and_report_the_formats_they_would_fit():
    c = col(prof("d\n03/04/2026\n25/12/2026\n"), "d")
    assert c.type == "text"
    assert any(h == "date-like values are valid as: %d/%m/%Y" for h in c.hints)
    both = col(prof("d\n03/04/2026\n05/06/2026\n"), "d")
    assert any("%d/%m/%Y, %m/%d/%Y" in h for h in both.hints)
    none = col(prof("d\n13/13/2026\n"), "d")
    assert any("no single common date format" in h for h in none.hints)


def test_na_values_option_turns_markers_into_nulls():
    c = col(prof("a\n1\nN/A\n3\n", na_values=("N/A",)), "a")
    assert (c.type, c.nulls, c.non_null) == ("integer", 1, 2)


def test_examples_are_first_five_distinct_values_with_row_ids_cut_to_50_chars():
    long = "x" * 80
    text = "v\n" + "\n".join(["a", "a", "b", "c", "d", "e", "f", long]) + "\n"
    ds = open_dataset(text.encode(), "t.csv")
    examples = profile(ds).column_profiles[0].examples
    assert [e["value"] for e in examples] == ["a", "b", "c", "d", "e"]
    assert [e["row_id"] for e in examples] == ["1", "3", "4", "5", "6"]
    assert (
        len(
            profile(open_dataset(("v\n" + long + "\n").encode(), "t.csv"))
            .column_profiles[0]
            .examples[0]["value"]
        )
        == 50
    )


def test_no_examples_omits_values_entirely():
    ds = open_dataset(b"col\ns3cr3t-v4lue\n", "t.csv")
    result = profile(ds, examples=False)
    assert result.column_profiles[0].examples is None
    assert "s3cr3t-v4lue" not in str(result.to_dict())


def test_header_only_table_profiles_with_zero_rows():
    result = prof("a,b\n")
    assert result.rows == 0 and result.duplicate_rows == 0 and result.constant_columns == []
    assert col(result, "a").type == "empty" and col(result, "a").null_share == 0.0
    assert result.empty_columns == ["a", "b"]


def test_constant_requires_two_rows_and_no_nulls():
    assert prof("a\n1\n").constant_columns == []
    assert prof("a\n1\n1\n").constant_columns == ["a"]
    assert prof("a,b\n1,x\n,y\n").constant_columns == []


def test_duplicate_rows_use_exact_values_and_keep_true_and_one_apart():
    frame = pd.DataFrame(
        {"a": [True, 1, 1, None, None], "b": ["x", "x", "x", "y", "y"]}, dtype=object
    )
    assert duplicate_row_count(frame) == 2  # (1,x) twice, (None,y) twice; True differs from 1


def test_native_excel_and_json_types_are_profiled_as_they_are():
    ds = open_dataset(
        b'[{"a": 1, "b": 1.5, "c": true, "d": "x"}, {"a": 2, "b": null, "c": false, "d": "y"}]',
        "t.json",
    )
    kinds = {c.name: c.type for c in profile(ds).column_profiles}
    assert kinds == {"a": "integer", "b": "decimal", "c": "boolean", "d": "text"}
    from sie.datasets import from_frame

    frame = pd.DataFrame({"when": [dt.datetime(2026, 1, 2), dt.datetime(2026, 2, 1)]})
    c = profile(from_frame(frame, "x")).column_profiles[0]
    assert (c.type, c.min, c.max) == ("datetime", "2026-01-02T00:00:00", "2026-02-01T00:00:00")


def test_loader_notes_are_carried_into_the_profile():
    ds = open_dataset(b"a,a\n1,2\n", "t.csv", LoadOptions(rename_duplicates=True))
    assert any(n["code"] == "renamed_header" for n in profile(ds).notes)


def test_profile_is_deterministic_and_does_not_change_the_dataset():
    ds = open_dataset(b"a,b\n1,x\n2,y\n", "t.csv")
    before = ds.frame.copy()
    assert profile(ds).to_dict() == profile(ds).to_dict()
    assert ds.frame.equals(before)
