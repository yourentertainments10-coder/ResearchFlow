"""Track C: cleaning is a plan, applied in a fixed order, logged, and never silent."""

from __future__ import annotations

import pandas as pd
import pytest

from sie.datasets import open_dataset
from sie.datasets.clean import _number_pattern, clean, parse_date_value, parse_number_value
from sie.datasets.models import (
    CleanPlan,
    DateFormat,
    DedupSpec,
    LoadOptions,
    NumberFormat,
    OperationError,
    RejectedTooMany,
)


def ds_of(text: str, **opts):
    return open_dataset(text.encode(), "t.csv", LoadOptions(**opts) if opts else None)


def num(column: str, decimal: str = ".", thousands: str = ""):
    return NumberFormat(column=column, decimal=decimal, thousands=thousands)


def parse_one(text, decimal=".", thousands=""):
    fmt = num("c", decimal, thousands)
    return parse_number_value(text, fmt, _number_pattern(fmt))


# --- numbers -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "decimal", "thousands", "expected"),
    [
        ("5", ".", "", 5), ("-5", ".", "", -5), ("+5", ".", "", 5), ("1.5", ".", "", 1.5),
        ("1e3", ".", "", 1000.0), ("1.234,56", ",", ".", 1234.56), ("1,234.56", ".", ",", 1234.56),
        ("1 234", ".", " ", 1234), ("1'234'567", ".", "'", 1234567), ("0,5", ",", "", 0.5),
        ("1,234", ".", ",", 1234),
    ],
)  # fmt: skip
def test_number_accepts_only_the_stated_separators(text, decimal, thousands, expected):
    value, reason = parse_one(text, decimal, thousands)
    assert reason is None and value == expected
    assert isinstance(value, int) == isinstance(expected, int)


@pytest.mark.parametrize(
    ("text", "decimal", "thousands", "reason"),
    [
        ("abc", ".", "", "NOT_A_NUMBER"), ("$5", ".", "", "NOT_A_NUMBER"), ("5%", ".", "", "NOT_A_NUMBER"),
        ("-", ".", "", "NOT_A_NUMBER"), ("", ".", "", "NOT_A_NUMBER"), ("١٢٣", ".", "", "NOT_A_NUMBER"),
        ("1e3", ",", "", "NOT_A_NUMBER"), ("1,5", ".", "", "WRONG_SEPARATORS"),
        ("1.234,56", ".", ",", "WRONG_SEPARATORS"), ("1,23,456", ".", ",", "WRONG_SEPARATORS"),
        (".5", ".", "", "WRONG_SEPARATORS"), ("5.", ".", "", "WRONG_SEPARATORS"),
        ("1 2", ".", "", "WRONG_SEPARATORS"), ("--5", ".", "", "WRONG_SEPARATORS"),
        ("99999999999999999999", ".", "", "OUT_OF_RANGE"), ("1e999", ".", "", "OUT_OF_RANGE"),
    ],
)  # fmt: skip
def test_number_failures_have_specific_reasons(text, decimal, thousands, reason):
    value, got = parse_one(text, decimal, thousands)
    assert value is None and got == reason


def test_native_numbers_pass_and_other_native_types_fail():
    assert parse_one(7)[0] == 7 and parse_one(1.5)[0] == 1.5
    assert parse_one(True)[1] == "NOT_A_NUMBER" and parse_one(float("inf"))[1] == "OUT_OF_RANGE"
    assert parse_one(2**70)[1] == "OUT_OF_RANGE"


# --- dates ---------------------------------------------------------------------------------------


def test_date_parsing_uses_only_the_given_format():
    assert str(parse_date_value("03/04/2026", "%d/%m/%Y")[0].date()) == "2026-04-03"
    assert str(parse_date_value("03/04/2026", "%m/%d/%Y")[0].date()) == "2026-03-04"
    assert parse_date_value("2026-04-03", "%d/%m/%Y")[1] == "NOT_A_DATE"
    assert parse_date_value("30/02/2026", "%d/%m/%Y")[1] == "DATE_OUT_OF_RANGE"
    assert parse_date_value("31/13/2026", "%d/%m/%Y")[1] == "NOT_A_DATE"
    assert parse_date_value("01/01/1500", "%d/%m/%Y")[1] == "DATE_OUT_OF_RANGE"
    assert parse_date_value(5, "%d/%m/%Y")[1] == "NOT_A_DATE"
    assert parse_date_value(" 03/04/2026", "%d/%m/%Y")[1] == "NOT_A_DATE"  # trim is a separate step


# --- the plan: order, rejects, log ---------------------------------------------------------------


def test_trim_changes_only_the_ends_and_blank_becomes_null():
    ds = ds_of('a,b\n"  x  y ",1\n"   ",2\nz,3\n')
    result = clean(ds, CleanPlan(trim=["a"]))
    assert result.dataset.frame["a"].tolist() == ["x  y", None, "z"]
    assert result.log[0]["cells_changed"] == 2 and result.dataset.rows == 3
    assert ds.frame["a"].tolist()[0] == "  x  y "  # the input is never changed


def test_trim_all_and_unknown_column():
    result = clean(ds_of('a,b\n" 1 ", x \n'), CleanPlan(trim="all"))
    assert result.dataset.frame.loc[1].tolist() == ["1", "x"]
    with pytest.raises(OperationError, match="unknown column"):
        clean(ds_of("a\n1\n"), CleanPlan(trim=["nope"]))


def test_drop_empty_columns_only_drops_named_columns_that_are_entirely_null():
    ds = ds_of("a,b,c,d\n1,,,x\n2,,5,y\n")
    result = clean(ds, CleanPlan(drop_empty_columns=["b", "c"]))
    assert result.dataset.columns == ["a", "c", "d"]
    entry = result.log[0]
    assert entry["columns_dropped"] == ["b"] and entry["kept_not_empty"] == ["c"]


def test_parse_number_types_the_column_and_rejects_bad_rows_with_reasons():
    ds = ds_of(
        "id,amount\n1,10\n2,abc\n3,\n4,20\n5,1.5\n6,7\n7,8\n8,9\n9,10\n10,11\n11,12\n12,13\n"
    )
    result = clean(ds, CleanPlan(parse_number=[num("amount")], max_reject_share=0.5))
    frame = result.dataset.frame
    assert result.rejects.to_dict("records") == [
        {
            "row_id": 2,
            "operation": "parse_number",
            "column": "amount",
            "original_value": "abc",
            "reason": "NOT_A_NUMBER",
        }
    ]
    assert str(frame["amount"].dtype) == "float64"  # 1.5 makes the column decimal
    assert pd.isna(frame.loc[3, "amount"]) and frame.loc[5, "amount"] == 1.5  # empty stays null
    assert 2 not in frame.index.tolist()  # a rejected row is in the rejects table only
    entry = result.log[0]
    assert (entry["rows_before"], entry["rows_after"], entry["rows_rejected"]) == (12, 11, 1)
    assert entry["reasons"] == {"NOT_A_NUMBER": 1} and entry["cells_changed"] == 10


def test_all_integer_values_give_an_integer_column():
    result = clean(ds_of("a\n1\n2\n"), CleanPlan(parse_number=[num("a")]))
    assert str(result.dataset.frame["a"].dtype) == "Int64"


def test_european_numbers():
    result = clean(ds_of('a\n"1.234,56"\n"2,5"\n'), CleanPlan(parse_number=[num("a", ",", ".")]))
    assert result.dataset.frame["a"].tolist() == [1234.56, 2.5]


def test_parse_date_makes_a_datetime_column():
    ds = ds_of("d\n03/04/2026\n\n25/12/2026\n")
    result = clean(ds, CleanPlan(parse_date=[DateFormat(column="d", format="%d/%m/%Y")]))
    frame = result.dataset.frame
    assert str(frame["d"].dtype).startswith("datetime64")
    assert [str(x.date()) for x in frame["d"].dropna()] == ["2026-04-03", "2026-12-25"]


def test_date_format_with_time_zone_is_refused():
    with pytest.raises(OperationError, match="time zones"):
        clean(ds_of("d\n1\n"), CleanPlan(parse_date=[DateFormat(column="d", format="%Y-%m-%d %z")]))


def test_a_column_can_only_be_parsed_once():
    with pytest.raises(OperationError, match="once"):
        clean(
            ds_of("a\n1\n"),
            CleanPlan(parse_number=[num("a")], parse_date=[DateFormat(column="a", format="%Y")]),
        )


def test_first_failure_wins_and_a_row_is_never_in_two_tables():
    ds = ds_of("a,b\n1,2026-01-01\nx,notadate\n3,2026-01-02\n4,2026-01-03\n5,2026-01-04\n")
    plan = CleanPlan(
        parse_number=[num("a")],
        parse_date=[DateFormat(column="b", format="%Y-%m-%d")],
        max_reject_share=0.5,
    )
    result = clean(ds, plan)
    assert result.rejects["operation"].tolist() == ["parse_number"]  # number parsing runs first
    assert result.rejects["column"].tolist() == ["a"]


def test_order_is_fixed_whatever_the_plan_lists_first():
    ds = ds_of('a\n" 5 "\n5\n')
    plan = CleanPlan(drop_duplicates=DedupSpec(), parse_number=[num("a")], trim=["a"])
    result = clean(ds, plan)
    assert result.plan["order"] == ["trim", "parse_number", "drop_duplicates"]
    assert [e["operation"] for e in result.log] == ["trim", "parse_number", "drop_duplicates"]
    # trim and parse come before the duplicate comparison, so " 5 " and "5" are one value
    assert result.dataset.rows == 1 and result.duplicates.to_dict("records") == [
        {"row_id": 2, "duplicate_of": 1}
    ]


def test_drop_duplicates_keeps_the_lowest_row_id_and_supports_a_subset():
    ds = ds_of("k,v\na,1\nb,2\na,1\na,9\nb,2\n")
    full = clean(ds, CleanPlan(drop_duplicates=DedupSpec()))
    assert full.dataset.frame.index.tolist() == [1, 2, 4]
    assert full.duplicates.to_dict("records") == [
        {"row_id": 3, "duplicate_of": 1},
        {"row_id": 5, "duplicate_of": 2},
    ]
    subset = clean(ds, CleanPlan(drop_duplicates=DedupSpec(subset=["k"])))
    assert subset.dataset.frame.index.tolist() == [1, 2]
    assert [d["duplicate_of"] for d in subset.duplicates.to_dict("records")] == [1, 1, 2]


def test_nulls_count_as_equal_for_duplicates_and_true_is_not_one():
    frame = pd.DataFrame({"a": [None, None, True, 1]}, dtype=object)
    from sie.datasets import from_frame

    result = clean(from_frame(frame, "x"), CleanPlan(drop_duplicates=DedupSpec()))
    assert result.dataset.frame.index.tolist() == [1, 3, 4]


def test_reject_threshold_stops_the_run_and_equal_share_passes():
    ds = ds_of("a\n1\nx\n3\n4\n5\n6\n7\n8\n9\ny\n")  # 2 of 10 rejected = 20%
    with pytest.raises(RejectedTooMany, match="wrong format"):
        clean(ds, CleanPlan(parse_number=[num("a")]))  # default 10%
    assert clean(ds, CleanPlan(parse_number=[num("a")], max_reject_share=0.2)).dataset.rows == 8


def test_invariant_rows_in_equals_cleaned_plus_rejected_plus_duplicate():
    ds = ds_of("a\n1\n1\nx\n2\n2\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n")
    result = clean(
        ds, CleanPlan(parse_number=[num("a")], drop_duplicates=DedupSpec(), max_reject_share=0.2)
    )
    summary = result.to_dict()
    assert (
        summary["rows_in"]
        == summary["rows_cleaned"] + summary["rows_rejected"] + summary["rows_duplicate"]
    )
    assert (summary["rows_rejected"], summary["rows_duplicate"]) == (1, 3)
    assert result.log[1]["rows_before"] == result.log[0]["rows_after"]


def test_an_empty_plan_changes_nothing_and_the_input_is_untouched():
    ds = ds_of("a,b\n1,x\n2,y\n")
    before = ds.frame.copy()
    result = clean(ds, CleanPlan())
    assert result.log == [] and result.dataset.frame.equals(before) and ds.frame.equals(before)
    assert result.plan["order"] == [] and result.rejects.empty and result.duplicates.empty


def test_cleaning_is_deterministic():
    ds = ds_of("a\n 1 \n1\nx\n" + "2\n" * 20)
    plan = CleanPlan(
        trim="all", parse_number=[num("a")], drop_duplicates=DedupSpec(), max_reject_share=0.5
    )
    first, second = clean(ds, plan), clean(ds, plan)
    assert first.to_dict() == second.to_dict() and first.dataset.frame.equals(second.dataset.frame)
    assert first.rejects.equals(second.rejects)


def test_na_values_are_treated_as_null_and_not_rejected():
    ds = ds_of("a\n1\nN/A\n3\n", na_values=("N/A",))
    result = clean(ds, CleanPlan(parse_number=[num("a")]))
    assert result.rejects.empty and pd.isna(result.dataset.frame.loc[2, "a"])
