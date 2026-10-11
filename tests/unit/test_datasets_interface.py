"""Shared interface of sie.datasets: canonical serialisation, type inference, specs, import rules."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pandas as pd
import pytest

from sie.datasets import list_operations, parse_spec
from sie.datasets.models import NumberFormat, OperationError, parse_metric
from sie.datasets.provenance import (
    canonical_json,
    canonical_table,
    format_float,
    table_sha256,
)
from sie.datasets.types import infer_column, is_null, to_typed, value_type

PKG = Path(__file__).resolve().parents[2] / "src" / "sie" / "datasets"


# --- canonical serialisation (design section 5) -----------------------------------------------------


def test_canonical_json_is_sorted_compact_and_nfc():
    assert canonical_json({"b": 1, "a": [True, None]}) == '{"a":[true,null],"b":1}'
    assert canonical_json({"k": "é"}) == canonical_json({"k": "é"})
    assert "é" in canonical_json({"k": "é"})  # not ASCII-escaped


@pytest.mark.parametrize(
    ("value", "text"),
    [(3.0, "3"), (0.1 + 0.2, "0.3"), (1e-7, "1e-07"), (123456789.123456789, "123456789.123457")],
)
def test_floats_use_fifteen_significant_digits(value, text):
    assert format_float(value) == text


def test_nan_and_infinity_become_null():
    assert format_float(math.nan) is None and format_float(math.inf) is None
    assert canonical_json({"x": float("nan")}) == '{"x":null}'


def test_table_hash_ignores_the_index_but_not_values_or_order():
    a = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    same = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]}, index=[10, 20])
    changed = pd.DataFrame({"x": [1, 3], "y": ["a", "b"]})
    reordered = a.iloc[::-1]
    assert table_sha256(a) == table_sha256(same)
    assert table_sha256(a) != table_sha256(changed)
    assert table_sha256(a) != table_sha256(reordered)
    assert canonical_table(a)["columns"] == ["x", "y"]


def test_timestamps_and_missing_values_are_canonical():
    frame = pd.DataFrame(
        {"d": pd.to_datetime(["2026-01-02", None]), "n": pd.array([1, None], dtype="Int64")}
    )
    assert canonical_table(frame)["rows"] == [["2026-01-02T00:00:00", 1], [None, None]]


# --- types (design section 11) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("12", "integer"), ("-250", "integer"), ("+7", "integer"), ("1.5", "decimal"), ("1e5", "decimal"),
        ("true", "boolean"), ("FALSE", "boolean"), ("2026-02-03", "date"),
        ("2026-02-03T10:00:00Z", "datetime"), ("abc", "text"),
        (12, "integer"), (1.5, "decimal"), (True, "boolean"),
    ],
)  # fmt: skip
def test_value_types(raw, kind):
    assert value_type(raw).name == kind


@pytest.mark.parametrize(
    ("raw", "hint"),
    [
        ("007", "identifier"), ("1,5", "comma-decimal"), ("1.234,5", "comma-decimal"),
        ("03/04/2026", "ambiguous"), ("N/A", "null marker"), ("2026-02-30", "valid calendar"),
        ("99999999999999999999", "64-bit"),
    ],
)  # fmt: skip
def test_ambiguous_values_stay_text_with_a_hint(raw, hint):
    vt = value_type(raw)
    assert vt.name == "text" and hint in (vt.hint or "")


def test_null_rules():
    assert is_null(None) and is_null("") and is_null("   ") and is_null(float("nan"))
    assert not is_null("N/A") and not is_null("0")
    assert is_null("N/A", ("N/A",))


def test_column_inference():
    assert infer_column(["1", "2", None]).type == "integer"
    assert infer_column(["1", "2.5"]).type == "decimal"  # integer widens to decimal
    assert infer_column(["2026-01-01", "2026-01-01T10:00:00"]).type == "datetime"
    assert infer_column([None, ""]).type == "empty"
    mixed = infer_column(["1", "x", "x"])
    assert mixed.type == "mixed" and mixed.counts == {"integer": 1, "text": 2}
    assert infer_column(["a", "007"]).hints  # identifier hint carried up


def test_to_typed_keeps_the_index_and_nulls():
    s = pd.Series(["1", None, "3"], index=[2, 3, 4], dtype=object)
    typed = to_typed(s, "integer")
    assert typed.index.tolist() == [2, 3, 4] and typed.isna().tolist() == [False, True, False]
    assert to_typed(s, "decimal").tolist()[0] == 1.0
    dates = to_typed(pd.Series(["2026-01-02", None], index=[5, 6], dtype=object), "date")
    assert str(dates.iloc[0].date()) == "2026-01-02" and pd.isna(dates.iloc[1])


# --- specs are data, never code (design section 4) ---------------------------------------------------


def test_the_registry_lists_six_operations_with_schemas():
    ops = list_operations()
    assert sorted(ops) == [
        "correlation",
        "describe",
        "group_aggregate",
        "time_trend",
        "top_n",
        "value_counts",
    ]
    assert all("properties" in schema for schema in ops.values())


def test_specs_reject_unknown_operations_extra_fields_and_bad_metrics():
    assert parse_spec({"op": "top_n", "by": "c", "metric": "x:sum"}).n == 10
    for bad in (
        {"op": "exec", "code": "1"},
        {"op": "top_n", "by": "c", "metric": "x:sum", "extra": 1},
        {"op": "top_n", "by": "c", "metric": "no-function"},
        {"op": "top_n", "by": "c", "metric": "x:sum", "n": 0},
        {"op": "group_aggregate", "by": [], "metrics": ["x:sum"]},
        {"op": "correlation", "columns": ["a"]},
        {},
    ):
        with pytest.raises(OperationError):
            parse_spec(bad)


def test_metric_text_is_data_only():
    assert parse_metric("a:b:sum") == ("a:b", "sum")
    for hostile in ("__import__('os').system('x'):sum", "x:sum; DROP TABLE t"):
        try:
            col, fn = parse_metric(hostile)
        except ValueError:
            continue
        assert fn == "sum" and col != ""  # treated as a column name, never executed


def test_number_format_rejects_equal_separators():
    with pytest.raises(ValueError):
        NumberFormat(column="a", decimal=",", thousands=",")


# --- the package is a library (design section 4) ---------------------------------------------------


FORBIDDEN = (
    "sie.cli",
    "sie.pipeline",
    "sie.analytics",
    "sie.db",
    "typer",
    "sqlalchemy",
    "sie.sources",
)


def test_the_package_imports_nothing_from_the_cli_pipeline_analytics_or_database():
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN), (
                    path.name,
                    name,
                )


def test_the_package_does_not_print_or_exit():
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"{path.name} prints"
            if isinstance(node, ast.Attribute) and node.attr == "exit":
                assert not (isinstance(node.value, ast.Name) and node.value.id == "sys"), path.name
