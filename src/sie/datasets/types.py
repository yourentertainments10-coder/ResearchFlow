"""Value and column type inference (docs/DATASET_ANALYTICS_DESIGN.md section 11).

Conservative on purpose: anything ambiguous stays text with a hint. Nothing is guessed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

_INT = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_LEADING_ZERO = re.compile(r"^[+-]?0\d+$")
_DEC = re.compile(r"^[+-]?(0|[1-9]\d*)\.\d+([eE][+-]?\d+)?$|^[+-]?(0|[1-9]\d*)[eE][+-]?\d+$")
_COMMA_DEC = re.compile(r"^[+-]?\d{1,3}([.\s']\d{3})*,\d+$|^[+-]?\d+,\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_DATE_LIKE = re.compile(r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$")
NULL_LIKE = {"n/a", "na", "null", "none", "nil", "-", "--", "nan", "#n/a", "?"}


def is_null(value: Any, na_values: tuple[str, ...] = ()) -> bool:
    """Null is ``None``, NaN, the empty or blank string, or an explicit ``na_values`` marker."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == "" or stripped in na_values
    try:  # pandas NA / NaT without importing pandas at module load
        import pandas as pd

        return bool(value is pd.NA or value is pd.NaT)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class ValueType:
    name: str  # boolean integer decimal date datetime text
    hint: str | None = None


def value_type(value: Any) -> ValueType:
    """Type of one non-null value. Native types (Excel, JSON) are kept; strings follow the rules."""
    if isinstance(value, bool):
        return ValueType("boolean")
    if isinstance(value, int):
        return ValueType("integer")
    if isinstance(value, float):
        return ValueType("decimal")
    if isinstance(value, datetime):
        return ValueType("datetime")
    if isinstance(value, date):
        return ValueType("date")
    text = str(value).strip()
    low = text.lower()
    if low in ("true", "false"):
        return ValueType("boolean")
    if _INT.match(text):
        if INT64_MIN <= int(text) <= INT64_MAX:
            return ValueType("integer")
        return ValueType("text", "integer outside the 64-bit range")
    if _LEADING_ZERO.match(text):
        return ValueType("text", "looks like an identifier (leading zero)")
    if _DEC.match(text):
        return ValueType("decimal")
    if _COMMA_DEC.match(text):
        return ValueType("text", "possible comma-decimal locale; use clean --parse-number")
    if _DATE.match(text):
        try:
            date.fromisoformat(text)
            return ValueType("date")
        except ValueError:
            return ValueType("text", "date-like but not a valid calendar date")
    if _DATETIME.match(text):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return ValueType("datetime")
        except ValueError:
            return ValueType("text", "datetime-like but not valid")
    if _DATE_LIKE.match(text):
        return ValueType("text", "date-like, ambiguous format; use clean --parse-date")
    if low in NULL_LIKE:
        return ValueType("text", "possible null marker; use --na-values")
    return ValueType("text")


@dataclass
class ColumnType:
    type: str  # boolean integer decimal date datetime text mixed empty
    counts: dict[str, int]  # per value type among non-null values
    non_null: int
    nulls: int
    hints: list[str] = field(default_factory=list)


def infer_column(values: list[Any], na_values: tuple[str, ...] = ()) -> ColumnType:
    """Infer a column from its values. Integer with decimal widens to decimal, date with datetime to
    datetime; any other combination is ``mixed`` and nothing is coerced."""
    counts: Counter[str] = Counter()
    hints: list[str] = []
    nulls = 0
    for value in values:
        if is_null(value, na_values):
            nulls += 1
            continue
        vt = value_type(value)
        counts[vt.name] += 1
        if vt.hint and vt.hint not in hints:
            hints.append(vt.hint)
    non_null = sum(counts.values())
    kinds = set(counts)
    if not kinds:
        name = "empty"
    elif len(kinds) == 1:
        name = next(iter(kinds))
    elif kinds == {"integer", "decimal"}:
        name = "decimal"
    elif kinds == {"date", "datetime"}:
        name = "datetime"
    else:
        name = "mixed"
    return ColumnType(name, dict(counts), non_null, nulls, hints)


def to_typed(series, column_type: str, na_values: tuple[str, ...] = ()):  # noqa: ANN001, ANN201
    """Convert an object column to its inferred type, keeping its index. Nulls become missing values.

    Raises ``ValueError`` if a value does not fit (callers turn that into a specific error or a reject).
    """
    import pandas as pd

    values = [None if is_null(v, na_values) else v for v in series.tolist()]
    index = series.index
    if column_type == "integer":
        return pd.Series(
            pd.array([None if v is None else int(str(v).strip()) for v in values], dtype="Int64"),
            index=index,
        )
    if column_type == "decimal":
        return pd.Series(
            [float("nan") if v is None else float(str(v).strip()) for v in values],
            index=index,
            dtype="float64",
        )
    if column_type == "boolean":
        flags = [
            None if v is None else (v if isinstance(v, bool) else str(v).strip().lower() == "true")
            for v in values
        ]
        return pd.Series(pd.array(flags, dtype="boolean"), index=index)
    if column_type in ("date", "datetime"):
        text = [v.strip() if isinstance(v, str) else v for v in values]
        return pd.to_datetime(pd.Series(text, index=index, dtype=object), errors="raise")
    return pd.Series(values, index=index, dtype=object)
