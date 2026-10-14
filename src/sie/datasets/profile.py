"""Track B: column and table profile. See design section 11.

Nothing is coerced and nothing is guessed: ambiguous values stay text with a hint, and the profile says
which formats a date-like column would be valid under instead of choosing one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from sie.datasets.models import (
    STANDARD_LIMITATIONS,
    ColumnProfile,
    Dataset,
    ProfileResult,
)
from sie.datasets.provenance import format_float
from sie.datasets.types import ColumnType, infer_column, is_null, to_typed, value_type

EXAMPLE_COUNT = 5
EXAMPLE_CHARS = 50
_DATE_HINT = "date-like, ambiguous format"
# formats a date-like column is tested against, in this fixed order
CANDIDATE_DATE_FORMATS = (
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%m.%d.%Y",
    "%d/%m/%y",
    "%m/%d/%y",
)


def _key(value: Any) -> tuple[str, Any]:
    """Equality key that keeps ``True`` and ``1`` apart and never fails on unhashable values."""
    try:
        hash(value)
    except TypeError:
        return (type(value).__name__, repr(value))
    return (type(value).__name__, value)


def _cut(value: Any) -> str:
    text = str(value)
    return text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS]


def _format_extreme(value: Any, kind: str) -> str:
    if kind == "integer":
        return str(int(value))
    if kind == "decimal":
        formatted = format_float(float(value))
        return formatted if formatted is not None else str(value)
    if kind == "date":
        return pd.Timestamp(value).date().isoformat()
    return pd.Timestamp(value).isoformat()


def _min_max(values: list[Any], kind: str) -> tuple[str | None, str | None, str | None]:
    """Min and max of a typed column as text. Returns ``(min, max, hint)``."""
    if kind not in ("integer", "decimal", "date", "datetime"):
        return None, None, None
    present = pd.Series([v for v in values if v is not None], dtype=object)
    if present.empty:
        return None, None, None
    try:
        typed = to_typed(present, kind)
        typed = typed.dropna()
        if typed.empty:
            return None, None, None
        return _format_extreme(typed.min(), kind), _format_extreme(typed.max(), kind), None
    except (ValueError, TypeError, OverflowError):
        return (
            None,
            None,
            "min and max unavailable (values could not be compared, for example mixed time zones)",
        )


def _date_format_hint(values: list[str]) -> str:
    fitting = []
    for fmt in CANDIDATE_DATE_FORMATS:
        try:
            for text in values:
                datetime.strptime(text, fmt)
        except ValueError:
            continue
        fitting.append(fmt)
    if fitting:
        return "date-like values are valid as: " + ", ".join(fitting)
    return "no single common date format fits all date-like values"


def _column_profile(
    name: str, values: list[Any], row_ids: list[int], na_values: tuple[str, ...], examples: bool
) -> ColumnProfile:
    info: ColumnType = infer_column(values, na_values)
    rows = len(values)
    seen: dict[tuple[str, Any], int] = {}
    first_rows: list[tuple[int, Any]] = []
    ambiguous_dates: list[str] = []
    for row_id, value in zip(row_ids, values, strict=True):
        if is_null(value, na_values):
            continue
        key = _key(value)
        if key not in seen:
            seen[key] = row_id
            if len(first_rows) < EXAMPLE_COUNT:
                first_rows.append((row_id, value))
        if isinstance(value, str) and (value_type(value).hint or "").startswith(_DATE_HINT):
            ambiguous_dates.append(value.strip())

    hints = list(info.hints)
    if ambiguous_dates:
        hints.append(_date_format_hint(sorted(set(ambiguous_dates))))
    if info.type == "mixed":
        total = info.non_null
        parts = ", ".join(
            f"{kind} {count / total * 100:.1f}%" for kind, count in sorted(info.counts.items())
        )
        hints.append(f"mixed types, nothing is coerced: {parts}")
    low, high, extreme_hint = _min_max(
        [None if is_null(v, na_values) else v for v in values], info.type
    )
    if extreme_hint:
        hints.append(extreme_hint)
    return ColumnProfile(
        name=name,
        type=info.type,  # type: ignore[arg-type]
        non_null=info.non_null,
        nulls=info.nulls,
        null_share=round(info.nulls / rows, 6) if rows else 0.0,
        distinct=len(seen),
        type_counts=dict(sorted(info.counts.items())) if info.type == "mixed" else {},
        min=low,
        max=high,
        examples=(
            [{"row_id": str(rid), "value": _cut(val)} for rid, val in first_rows]
            if examples
            else None
        ),
        hints=hints,
    )


def _is_missing(value: Any) -> bool:
    return not isinstance(value, str) and is_null(value)


def duplicate_row_count(frame: pd.DataFrame) -> int:
    """Rows identical in every column to an earlier row (``True`` and ``1`` are different values)."""
    seen: set[tuple[tuple[str, Any], ...]] = set()
    duplicates = 0
    for row in frame.itertuples(index=False, name=None):
        key = tuple(("null", None) if _is_missing(v) else _key(v) for v in row)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def profile(ds: Dataset, *, examples: bool = True) -> ProfileResult:
    """Types, nulls, distinct counts, min and max, duplicates, constants, hints and examples.

    ``constant_columns`` lists columns with one distinct value and no nulls in a table of at least
    two rows. ``duplicate_rows`` counts rows identical in every column to an earlier row. Null follows
    the dataset's options (empty string, ``None`` and any ``na_values`` marker).
    """
    frame = ds.frame
    row_ids = [int(i) for i in frame.index]
    na_values = tuple(ds.options.na_values)
    columns = [
        _column_profile(str(name), frame[name].tolist(), row_ids, na_values, examples)
        for name in frame.columns
    ]
    return ProfileResult(
        rows=len(frame),
        columns=len(columns),
        duplicate_rows=duplicate_row_count(frame),
        empty_columns=[c.name for c in columns if c.type == "empty"],
        constant_columns=[
            c.name for c in columns if len(frame) >= 2 and c.distinct == 1 and c.nulls == 0
        ],
        column_profiles=columns,
        notes=[dict(n) for n in ds.notes],
        limitations=list(STANDARD_LIMITATIONS),
    )
