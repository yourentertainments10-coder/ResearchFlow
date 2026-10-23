"""Turn short text forms (as typed on a command line or sent by a planner) into validated requests.

Pure functions, no I/O. Every failure is an ``OperationError`` so callers report it as an invalid request.
The forms are deliberately simple; a column name that contains the separator character must use a JSON
spec instead (``sie calc-spec`` or the library).
"""

from __future__ import annotations

import json
from typing import Any

from sie.datasets.models import (
    OPERATION_MODELS,
    CleanPlan,
    DateFormat,
    NumberFormat,
    OperationError,
    parse_spec,
)

# the CLI arguments each operation accepts (a name outside this set is a usage error, never ignored)
OPERATION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "describe": ("columns",),
    "group_aggregate": ("by", "metrics", "sort"),
    "top_n": ("by", "metric", "n", "order"),
    "time_trend": ("date_column", "metric", "freq", "start", "end"),
    "correlation": ("columns", "method"),
    "value_counts": ("column", "top", "normalize"),
}
_LIST_ARGUMENTS = {"columns", "by", "metrics"}


def split_list(text: str | None) -> list[str] | None:
    """``"a,b"`` -> ``["a", "b"]``; ``None`` stays ``None``; empty items are an error."""
    if text is None:
        return None
    items = [item.strip() for item in text.split(",")]
    if not text.strip() or any(not item for item in items):
        raise OperationError(f"expected a comma-separated list of names, got {text!r}")
    return items


def parse_number_spec(text: str) -> NumberFormat:
    """``COL:DECIMAL:THOUSANDS``, for example ``amount:,:.`` for ``1.234,56``."""
    parts = text.rsplit(":", 2)
    if len(parts) != 3 or not parts[0]:
        raise OperationError(f"parse-number needs COL:DECIMAL:THOUSANDS, got {text!r}")
    try:
        return NumberFormat(column=parts[0], decimal=parts[1], thousands=parts[2])  # type: ignore[arg-type]
    except ValueError as exc:
        raise OperationError(f"invalid parse-number {text!r}: {exc}") from exc


def parse_date_spec(text: str) -> DateFormat:
    """``COL:FORMAT`` with a strptime format, split at the first colon."""
    column, _, fmt = text.partition(":")
    if not column or not fmt:
        raise OperationError(f"parse-date needs COL:FORMAT, got {text!r}")
    return DateFormat(column=column, format=fmt)


def build_plan(
    *,
    trim: bool = False,
    trim_columns: list[str] | None = None,
    drop_empty_columns: list[str] | None = None,
    parse_number: list[str] | None = None,
    parse_date: list[str] | None = None,
    drop_duplicates: bool = False,
    subset: list[str] | None = None,
    max_reject_share: float = 0.10,
) -> CleanPlan | None:
    """A ``CleanPlan`` from the command-line forms, or ``None`` when nothing was asked for."""
    if subset and not drop_duplicates:
        raise OperationError("--subset only applies together with --drop-duplicates")
    if trim and trim_columns:
        raise OperationError("use either --trim (all text columns) or --trim-columns, not both")
    asked = (
        trim
        or bool(trim_columns)
        or bool(drop_empty_columns)
        or bool(parse_number)
        or bool(parse_date)
        or drop_duplicates
    )
    if not asked:
        return None
    try:
        return CleanPlan(
            trim="all" if trim else (trim_columns or None),
            drop_empty_columns=drop_empty_columns or [],
            parse_number=[parse_number_spec(t) for t in parse_number or []],
            parse_date=[parse_date_spec(t) for t in parse_date or []],
            drop_duplicates={"subset": subset} if drop_duplicates else None,  # type: ignore[arg-type]
            max_reject_share=max_reject_share,
        )
    except ValueError as exc:
        if isinstance(exc, OperationError):
            raise
        raise OperationError(f"invalid cleaning request: {exc}") from exc


def build_spec(op: str, arguments: dict[str, Any]) -> Any:
    """An operation spec from the operation name and the CLI arguments that were given.

    ``arguments`` maps names to values; ``None`` means "not given". A given argument that the
    operation does not accept is an error rather than being ignored."""
    if op not in OPERATION_MODELS:
        raise OperationError(f"unknown operation {op!r}; use one of {sorted(OPERATION_MODELS)}")
    allowed = OPERATION_ARGUMENTS[op]
    given = {k: v for k, v in arguments.items() if v is not None}
    stray = sorted(set(given) - set(allowed))
    if stray:
        raise OperationError(f"{op} does not take {stray}; it takes {list(allowed)}")
    data: dict[str, Any] = {"op": op}
    for name, value in given.items():
        if name in _LIST_ARGUMENTS and isinstance(value, str):
            value = split_list(value)
        data[name] = value
    if op == "top_n" and "by" in data and isinstance(data["by"], list):
        if len(data["by"]) != 1:
            raise OperationError("top_n ranks by exactly one column")
        data["by"] = data["by"][0]
    return parse_spec(data)


def parse_spec_document(text: str) -> list[Any]:
    """A JSON document holding one spec, a list of specs, or ``{"operations": [...]}``."""
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise OperationError(f"the spec file is not valid JSON: {exc}") from exc
    if isinstance(document, dict) and "operations" in document and "op" not in document:
        document = document["operations"]
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list) or not document:
        raise OperationError(
            "the spec file must hold an operation, a list of operations or {'operations': [...]}"
        )
    if not all(isinstance(item, dict) for item in document):
        raise OperationError("every operation must be a JSON object")
    return [parse_spec(item) for item in document]
