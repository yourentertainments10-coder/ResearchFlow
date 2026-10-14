"""Track D: the six operations, with expected values worked out by hand (see comments)."""

from __future__ import annotations

import pandas as pd
import pytest

from sie.datasets import from_frame, open_dataset, run_operation
from sie.datasets.clean import clean
from sie.datasets.models import (
    STANDARD_LIMITATIONS,
    CleanPlan,
    DateFormat,
    LoadOptions,
    NumberFormat,
    OperationError,
)
from sie.datasets.provenance import table_sha256

# row_id region product amount qty day
#   1   north  A       10     1   2026-01-05
#   2   north  B       20     2   2026-01-20
#   3   south  A        5     -   2026-02-03
#   4   south  A       15     4   2026-03-10
#   5   (null) B      100     5   -
SALES = (
    "region,product,amount,qty,day\n"
    "north,A,10,1,2026-01-05\n"
    "north,B,20,2,2026-01-20\n"
    "south,A,5,,2026-02-03\n"
    "south,A,15,4,2026-03-10\n"
    ",B,100,5,\n"
)


def ds(text: str = SALES, **opts):
    return open_dataset(text.encode(), "t.csv", LoadOptions(**opts) if opts else None)


def run(spec: dict, text: str = SALES, **opts):
    return run_operation(ds(text, **opts), spec)


def rows(result) -> list[list]:
    return result.table.values.tolist()


# --- describe ------------------------------------------------------------------------------------


def test_describe_numeric_columns():
    result = run({"op": "describe", "columns": ["amount", "qty"]})
    amount, qty = result.table.to_dict("records")
    # amount 5,10,15,20,100: mean 30, median 15, sample std sqrt(6250/4), p25 = 10, p75 = 20
    assert amount["column"] == "amount" and amount["type"] == "integer"
    assert (amount["count"], amount["nulls"], amount["distinct"]) == (5, 0, 5)
    assert (amount["min"], amount["max"], amount["mean"], amount["median"]) == (5, 100, 30.0, 15)
    assert amount["std"] == pytest.approx(39.528470752104745)
    assert (amount["p25"], amount["p75"]) == (10.0, 20.0)
    # qty 1,2,4,5 (one null): mean 3, median 3, std sqrt(10/3), p25 1.75, p75 4.25
    assert (qty["count"], qty["nulls"], qty["mean"], qty["median"]) == (4, 1, 3.0, 3.0)
    assert qty["std"] == pytest.approx(1.8257418583505538)
    assert (qty["p25"], qty["p75"]) == (1.75, 4.25)
    assert result.extra["std_ddof"] == 1 and "linear" in result.extra["percentile_method"]


def test_describe_text_has_length_statistics_and_keeps_file_column_order():
    result = run({"op": "describe", "columns": ["product", "region"]})
    assert result.table["column"].tolist() == ["region", "product"]  # file order, not request order
    region = result.table.iloc[0]
    assert (region["count"], region["nulls"], region["distinct"]) == (4, 1, 2)
    assert (region["min"], region["max"]) == ("north", "south")
    assert (region["min_length"], region["max_length"], region["mean_length"]) == (5, 5, 5.0)
    assert region["mean"] is None


def test_describe_one_value_gives_null_std_and_a_warning():
    result = run({"op": "describe"}, "a\n7\n")
    assert result.table.iloc[0]["std"] is None
    assert any("fewer than 2" in w for w in result.warnings)


def test_describe_dates():
    row = run({"op": "describe", "columns": ["day"]}).table.iloc[0]
    assert (row["type"], row["min"], row["max"], row["nulls"]) == (
        "date",
        "2026-01-05",
        "2026-03-10",
        1,
    )


# --- group_aggregate -----------------------------------------------------------------------------


def test_group_aggregate_sums_means_counts_and_keeps_the_null_group():
    result = run(
        {
            "op": "group_aggregate",
            "by": ["region"],
            "metrics": ["amount:sum", "qty:mean", "qty:count"],
        }
    )
    assert list(result.table.columns) == ["region", "rows", "amount:sum", "qty:mean", "qty:count"]
    assert rows(result) == [
        ["north", 2, 30, 1.5, 2],
        ["south", 2, 20, 4.0, 1],  # the null qty is excluded, not counted as zero
        ["(null)", 1, 100, 5.0, 1],  # null keys are a group of their own, listed last
    ]
    assert result.provenance.rows_excluded_null == {"qty": 1}
    assert result.provenance.rows_in == 5 and result.provenance.rows_used == 5


def test_group_aggregate_sort_by_metric_descending_with_key_tiebreak():
    result = run(
        {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum"], "sort": "amount:sum"}
    )
    assert [r[0] for r in rows(result)] == ["(null)", "north", "south"]  # 100, 30, 20
    tie = run(
        {
            "op": "group_aggregate",
            "by": ["region"],
            "metrics": ["rows_x:count"],
            "sort": "rows_x:count",
        },
        "region,rows_x\nb,1\na,1\n",
    )
    assert [r[0] for r in rows(tie)] == ["a", "b"]  # equal metrics: group key ascending


def test_group_aggregate_two_keys_min_max_median_distinct():
    result = run(
        {
            "op": "group_aggregate",
            "by": ["region", "product"],
            "metrics": ["amount:min", "amount:max", "amount:median", "day:max", "product:distinct"],
        }
    )
    assert rows(result) == [
        ["north", "A", 1, 10, 10, 10, "2026-01-05", 1],
        ["north", "B", 1, 20, 20, 20, "2026-01-20", 1],
        ["south", "A", 2, 5, 15, 10.0, "2026-03-10", 1],  # median of 5 and 15
        ["(null)", "B", 1, 100, 100, 100, None, 1],  # no non-null day: null, not an error
    ]
    assert any("day" in w and "no non-null" in w for w in result.warnings)


def test_group_aggregate_is_independent_of_input_row_order():
    spec = {"op": "group_aggregate", "by": ["product"], "metrics": ["amount:sum", "qty:mean"]}
    lines = SALES.strip().split("\n")
    shuffled = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
    a, b = run(spec), run(spec, shuffled)
    assert table_sha256(a.table) == table_sha256(b.table) == a.provenance.result_sha256


def test_float_sums_do_not_depend_on_row_order():
    values = [0.1, 0.2, 0.3, 1e16, -1e16, 0.7] * 5
    forward = "x,v\n" + "".join(f"a,{v!r}\n" for v in values)
    backward = "x,v\n" + "".join(f"a,{v!r}\n" for v in reversed(values))
    spec = {"op": "group_aggregate", "by": ["x"], "metrics": ["v:sum"]}
    assert rows(run(spec, forward)) == rows(run(spec, backward))


def test_text_is_never_coerced_to_a_number():
    with pytest.raises(OperationError) as info:
        run({"op": "group_aggregate", "by": ["region"], "metrics": ["product:sum"]})
    assert (
        "product" in str(info.value)
        and "text" in str(info.value)
        and "parse-number" in str(info.value)
    )
    with pytest.raises(OperationError, match="mixed"):
        run({"op": "group_aggregate", "by": ["a"], "metrics": ["m:min"]}, "a,m\nx,1\nx,abc\n")
    with pytest.raises(OperationError, match="needs numbers"):
        run({"op": "group_aggregate", "by": ["a"], "metrics": ["m:sum"]}, "a,m\nx,1\nx,abc\n")
    # count and distinct work on any type
    ok = run(
        {"op": "group_aggregate", "by": ["a"], "metrics": ["m:count", "m:distinct"]},
        "a,m\nx,1\nx,abc\n",
    )
    assert rows(ok) == [["x", 2, 2, 2]]


def test_numbers_that_look_like_ids_stay_text_and_cannot_be_summed():
    with pytest.raises(OperationError, match="text"):
        run({"op": "group_aggregate", "by": ["k"], "metrics": ["id:sum"]}, "k,id\na,007\na,008\n")


def test_unknown_columns_and_bad_requests_are_errors_that_list_the_columns():
    with pytest.raises(OperationError, match="unknown column 'nope'.*region"):
        run({"op": "group_aggregate", "by": ["nope"], "metrics": ["amount:sum"]})
    with pytest.raises(OperationError, match="unknown column"):
        run(
            {"op": "group_aggregate", "by": ["region"], "metrics": ["Amount:sum"]}
        )  # exact, no case folding
    with pytest.raises(OperationError, match="repeated"):
        run({"op": "group_aggregate", "by": ["region", "region"], "metrics": ["amount:sum"]})
    with pytest.raises(OperationError, match="sort"):
        run(
            {
                "op": "group_aggregate",
                "by": ["region"],
                "metrics": ["amount:sum"],
                "sort": "qty:sum",
            }
        )
    with pytest.raises(OperationError):
        run({"op": "group_aggregate", "by": ["region"], "metrics": ["amount:total"]})


def test_integer_sums_that_overflow_int64_are_an_error_not_a_wraparound():
    frame = pd.DataFrame({"g": ["a"] * 3, "v": [2**62] * 3})
    with pytest.raises(OperationError, match="overflow"):
        run_operation(
            from_frame(frame, "x"), {"op": "group_aggregate", "by": ["g"], "metrics": ["v:sum"]}
        )


def test_empty_table_gives_an_empty_result_and_a_warning_not_an_error():
    result = run({"op": "group_aggregate", "by": ["a"], "metrics": ["b:sum"]}, "a,b\n")
    assert len(result.table) == 0 and result.provenance.rows_used == 0
    assert any("no usable rows" in w for w in result.warnings)


def test_labels_that_collide_with_data_are_flagged():
    result = run({"op": "value_counts", "column": "a"}, "a\n(null)\n\n")
    assert any("(null)" in w for w in result.warnings)


def test_na_values_are_nulls_for_metrics_and_group_keys():
    result = run(
        {"op": "group_aggregate", "by": ["g"], "metrics": ["v:sum"]},
        "g,v\na,1\nN/A,2\na,N/A\n",
        na_values=("N/A",),
    )
    assert rows(result) == [["a", 2, 1], ["(null)", 1, 2]]


# --- top_n ---------------------------------------------------------------------------------------


def test_top_n_ranks_with_the_minimum_method():
    result = run({"op": "top_n", "by": "product", "metric": "amount:sum", "n": 1})
    # A = 10 + 5 + 15 = 30, B = 20 + 100 = 120
    assert list(result.table.columns) == ["rank", "product", "rows", "amount:sum"]
    assert rows(result) == [[1, "B", 2, 120]]
    assert result.extra["tied_at_cutoff"] is False and result.extra["rows_returned"] == 1


def test_top_n_includes_every_group_tied_at_the_cutoff():
    result = run({"op": "top_n", "by": "region", "metric": "amount:count", "n": 1})
    # north has 2 amounts, south 2, (null) 1: the first two tie at the cutoff, so both are returned
    assert rows(result) == [[1, "north", 2, 2], [1, "south", 2, 2]]
    assert result.extra["tied_at_cutoff"] is True and result.extra["rows_returned"] == 2
    assert any("instead of 1" in w for w in result.warnings)


def test_top_n_ascending_and_ranks_share_values():
    result = run({"op": "top_n", "by": "region", "metric": "amount:count", "n": 10, "order": "asc"})
    assert [(r[0], r[1]) for r in rows(result)] == [(1, "(null)"), (2, "north"), (2, "south")]


def test_top_n_validation():
    for bad in ({"n": 0}, {"n": 1001}, {"order": "up"}):
        with pytest.raises(OperationError):
            run({"op": "top_n", "by": "region", "metric": "amount:sum", **bad})


# --- time_trend ----------------------------------------------------------------------------------


def test_time_trend_by_month_counts_rows_and_excludes_null_dates():
    result = run(
        {"op": "time_trend", "date_column": "day", "metric": "amount:sum", "freq": "month"}
    )
    assert list(result.table.columns) == ["period", "rows", "amount:sum"]
    assert rows(result) == [["2026-01", 2, 30], ["2026-02", 1, 5], ["2026-03", 1, 15]]
    assert result.provenance.rows_excluded_null == {"day": 1}
    assert result.provenance.rows_used == 4


def test_time_trend_lists_empty_periods_with_zero_rows_and_a_null_metric():
    text = "d,v\n2026-01-10,4\n2026-04-02,6\n"
    result = run({"op": "time_trend", "date_column": "d", "metric": "v:sum", "freq": "month"}, text)
    assert rows(result) == [
        ["2026-01", 1, 4],
        ["2026-02", 0, None],  # not filled with zero, not interpolated
        ["2026-03", 0, None],
        ["2026-04", 1, 6],
    ]
    bounded = run(
        {
            "op": "time_trend",
            "date_column": "d",
            "freq": "month",
            "start": "2025-12-01",
            "end": "2026-05-31",
        },
        text,
    )
    assert [r[0] for r in rows(bounded)][0] == "2025-12" and rows(bounded)[-1] == ["2026-05", 0]
    assert [r[1] for r in rows(bounded)] == [0, 1, 0, 0, 1, 0]


def test_time_trend_quarter_year_and_day():
    assert rows(run({"op": "time_trend", "date_column": "day", "freq": "quarter"})) == [
        ["2026-Q1", 4]
    ]
    assert rows(run({"op": "time_trend", "date_column": "day", "freq": "year"})) == [["2026", 4]]
    daily = run(
        {"op": "time_trend", "date_column": "d", "freq": "day"}, "d\n2026-02-27\n2026-03-01\n"
    )
    assert [r[0] for r in rows(daily)] == ["2026-02-27", "2026-02-28", "2026-03-01"]


def test_time_trend_start_and_end_are_inclusive_and_validated():
    result = run(
        {
            "op": "time_trend",
            "date_column": "day",
            "freq": "day",
            "start": "2026-01-20",
            "end": "2026-02-03",
        }
    )
    assert [r for r in rows(result) if r[1]] == [["2026-01-20", 1], ["2026-02-03", 1]]
    for bad in (
        {"start": "2026-13-01"},
        {"start": "01/02/2026"},
        {"start": "2026-03-01", "end": "2026-01-01"},
    ):
        with pytest.raises(OperationError):
            run({"op": "time_trend", "date_column": "day", **bad})


def test_time_trend_needs_real_dates_and_never_guesses():
    with pytest.raises(OperationError, match="real dates"):
        run({"op": "time_trend", "date_column": "d"}, "d\n03/04/2026\n")
    with pytest.raises(OperationError, match="real dates"):
        run({"op": "time_trend", "date_column": "region"})


def test_time_trend_refuses_an_absurd_number_of_periods():
    text = "d\n0001-01-01\n9999-12-31\n"
    with pytest.raises(OperationError, match="periods exceed"):
        run({"op": "time_trend", "date_column": "d", "freq": "day"}, text)


# --- correlation ---------------------------------------------------------------------------------


def test_pearson_uses_pairwise_complete_rows():
    result = run({"op": "correlation", "columns": ["qty", "amount"]})
    # file column order, upper triangle: (amount, qty). Row 3 has no qty, so n_used = 4
    assert rows(result)[0][:2] == ["amount", "qty"] and rows(result)[0][3] == 4
    assert rows(result)[0][2] == pytest.approx(0.7483314773547884)


def test_spearman_uses_average_ranks():
    result = run({"op": "correlation", "columns": ["amount", "qty"], "method": "spearman"})
    assert rows(result)[0][2] == pytest.approx(0.8)  # ranks (1,3,2,4) against (1,2,3,4)
    tied = run(
        {"op": "correlation", "columns": ["a", "b"], "method": "spearman"}, "a,b\n1,1\n1,2\n2,3\n"
    )
    # ranks of a: 1.5, 1.5, 3; ranks of b: 1, 2, 3
    assert rows(tied)[0][2] == pytest.approx(0.8660254037844387)


def test_correlation_null_cases_warn_instead_of_failing():
    few = run({"op": "correlation", "columns": ["a", "b"]}, "a,b\n1,2\n2,3\n")
    assert rows(few)[0][2] is None and any("fewer than 3" in w for w in few.warnings)
    constant = run({"op": "correlation", "columns": ["a", "b"]}, "a,b\n1,5\n1,6\n1,7\n")
    assert rows(constant)[0][2] is None and any("constant" in w for w in constant.warnings)


def test_correlation_needs_numeric_columns_and_a_valid_pair():
    with pytest.raises(OperationError, match="text"):
        run({"op": "correlation", "columns": ["amount", "region"]})
    with pytest.raises(OperationError):
        run({"op": "correlation", "columns": ["amount"]})
    with pytest.raises(OperationError, match="repeated"):
        run({"op": "correlation", "columns": ["amount", "amount"]})


def test_three_columns_give_three_pairs_in_file_order():
    result = run({"op": "correlation", "columns": ["qty", "amount", "qty"][:2] + []})
    assert len(result.table) == 1
    three = run(
        {"op": "correlation", "columns": ["a", "b", "c"]}, "a,b,c\n1,2,9\n2,4,7\n3,7,8\n4,8,1\n"
    )
    assert [(r[0], r[1]) for r in rows(three)] == [("a", "b"), ("a", "c"), ("b", "c")]


# --- value_counts --------------------------------------------------------------------------------


def test_value_counts_sorted_by_count_then_value_with_a_null_row():
    result = run({"op": "value_counts", "column": "region"})
    assert rows(result) == [["north", 2], ["south", 2], ["(null)", 1]]
    assert result.provenance.rows_used == 5 and result.extra["distinct_values"] == 3


def test_value_counts_other_row_keeps_totals_adding_up_and_normalize():
    result = run({"op": "value_counts", "column": "region", "top": 1, "normalize": True})
    assert rows(result) == [["north", 2, 0.4], ["(other)", 3, 0.6]]
    assert sum(r[1] for r in rows(result)) == 5


def test_value_counts_compares_exactly_case_and_spacing_matter():
    result = run({"op": "value_counts", "column": "v"}, 'v\na\nA\n"a "\na\n')
    assert dict(map(tuple, rows(result))) == {"a": 2, "A": 1, "a ": 1}


def test_value_counts_validation():
    for bad in ({"top": 0}, {"top": 1001}):
        with pytest.raises(OperationError):
            run({"op": "value_counts", "column": "region", **bad})


# --- specs are data, results are auditable -----------------------------------------------------------


def test_hostile_specs_are_rejected_as_data_never_executed():
    for spec in (
        {"op": "exec", "code": "__import__('os').system('x')"},
        {"op": "top_n", "by": "region", "metric": "amount:sum", "extra": 1},
        {
            "op": "group_aggregate",
            "by": ["region"],
            "metrics": ["__import__('os').system('x'):sum"],
        },
        {},
    ):
        with pytest.raises(OperationError):
            run(spec)
    with pytest.raises(OperationError, match="not an operation spec"):
        run_operation(ds(), object())


def test_provenance_traces_the_result_to_the_input_and_parameters():
    dataset = ds()
    spec = {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum"]}
    result = run_operation(dataset, spec)
    prov = result.provenance
    assert prov.operation == "group_aggregate" and prov.parameters["by"] == ["region"]
    assert prov.input_sha256 == dataset.source.sha256
    assert prov.result_sha256 == table_sha256(result.table)
    assert result.extra["input_table_sha256"] == table_sha256(dataset.frame)
    assert tuple(result.limitations) == STANDARD_LIMITATIONS
    assert result.to_dict()["provenance"]["rows_in"] == 5
    assert run_operation(dataset, spec).provenance.result_sha256 == prov.result_sha256


def test_the_dataset_is_not_changed_by_an_operation():
    dataset = ds()
    before = dataset.frame.copy()
    run_operation(dataset, {"op": "describe"})
    run_operation(dataset, {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum"]})
    assert dataset.frame.equals(before)


def test_cleaning_counts_flow_into_the_provenance_and_typed_columns_work():
    raw = ds(
        'g,amount,day\na,"1.234,5",03/04/2026\na,"2.000,5",04/04/2026\na,"2.000,5",04/04/2026\n'
    )
    plan = CleanPlan(
        parse_number=[NumberFormat(column="amount", decimal=",", thousands=".")],
        parse_date=[DateFormat(column="day", format="%d/%m/%Y")],
        drop_duplicates={},
    )
    cleaned = clean(raw, plan)
    result = run_operation(
        cleaned.dataset,
        {"op": "group_aggregate", "by": ["g"], "metrics": ["amount:sum", "day:max"]},
        cleaning=cleaned,
    )
    assert rows(result) == [["a", 2, 3235.0, "2026-04-04"]]
    assert result.provenance.cleaning == {"rows_in": 3, "rows_rejected": 0, "rows_duplicate": 1}
    trend = run_operation(
        cleaned.dataset, {"op": "time_trend", "date_column": "day", "freq": "day"}
    )
    assert [r for r in rows(trend)] == [["2026-04-03", 1], ["2026-04-04", 1]]
