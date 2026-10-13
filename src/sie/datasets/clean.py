"""Track C: an explicit, logged cleaning plan. See design section 7.

Cleaning only happens when it is in the plan, in a fixed order (trim, drop_empty_columns,
parse_number, parse_date, drop_duplicates), on a copy. A value that does not parse is never turned
into null: its whole row goes to the rejects table with the first failure. A run that rejects more than
``max_reject_share`` of the rows raises and the caller publishes nothing.
"""

from __future__ import annotations

import math
import numbers
import re
from dataclasses import replace
from datetime import date, datetime
from typing import Any

import pandas as pd

from sie.datasets.models import (
    CleanPlan,
    CleanResult,
    Dataset,
    DateFormat,
    IntegrityError,
    NumberFormat,
    OperationError,
    RejectedTooMany,
)
from sie.datasets.provenance import normalise_scalar
from sie.datasets.types import is_null

ORDER = ("trim", "drop_empty_columns", "parse_number", "parse_date", "drop_duplicates")
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_DIGITS = "0123456789"
MIN_YEAR, MAX_YEAR = 1678, 2261  # the same on every pandas version, so results never depend on it
REJECT_COLUMNS = ["row_id", "operation", "column", "original_value", "reason"]


# --- numbers -------------------------------------------------------------------------------------


def _number_pattern(fmt: NumberFormat) -> re.Pattern[str]:
    decimal = re.escape(fmt.decimal)
    if fmt.thousands:
        thousands = re.escape(fmt.thousands)
        integer = rf"(?:\d{{1,3}}(?:{thousands}\d{{3}})+|\d+)"
    else:
        integer = r"\d+"
    exponent = r"(?:[eE][+-]?\d+)?" if fmt.decimal == "." and not fmt.thousands else ""
    return re.compile(rf"^[+-]?{integer}(?:{decimal}\d+)?{exponent}$", re.ASCII)


def parse_number_value(
    value: Any, fmt: NumberFormat, pattern: re.Pattern[str]
) -> tuple[Any, str | None]:
    """Return ``(number, None)`` or ``(None, reason)``. Integers stay ``int``; anything with a decimal
    part or exponent is ``float``. Nothing is guessed: only the stated separators are accepted."""
    if isinstance(value, bool):
        return None, "NOT_A_NUMBER"
    if isinstance(value, numbers.Integral):
        number = int(value)
        return (number, None) if _INT64_MIN <= number <= _INT64_MAX else (None, "OUT_OF_RANGE")
    if isinstance(value, numbers.Real):
        number_f = float(value)
        return (number_f, None) if math.isfinite(number_f) else (None, "OUT_OF_RANGE")
    if not isinstance(value, str):
        return None, "NOT_A_NUMBER"
    plain = fmt.decimal == "." and not fmt.thousands
    allowed = set(_DIGITS + "+-.,' ") | (set("eE") if plain else set())
    if (
        not value
        or any(ch not in allowed for ch in value)
        or not any(ch in _DIGITS for ch in value)
    ):
        return None, "NOT_A_NUMBER"
    if not pattern.match(value):
        return None, "WRONG_SEPARATORS"
    text = value.replace(fmt.thousands, "") if fmt.thousands else value
    text = text.replace(fmt.decimal, ".")
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text)
        return (number, None) if _INT64_MIN <= number <= _INT64_MAX else (None, "OUT_OF_RANGE")
    number_f = float(text)
    return (number_f, None) if math.isfinite(number_f) else (None, "OUT_OF_RANGE")


# --- dates ---------------------------------------------------------------------------------------


def parse_date_value(value: Any, fmt: str) -> tuple[Any, str | None]:
    """Return ``(Timestamp, None)`` or ``(None, reason)``. Only the stated strptime format is tried."""
    if isinstance(value, datetime | date):
        if not MIN_YEAR <= value.year <= MAX_YEAR:
            return None, "DATE_OUT_OF_RANGE"
        try:
            return pd.Timestamp(value), None
        except (ValueError, OverflowError, pd.errors.OutOfBoundsDatetime):
            return None, "DATE_OUT_OF_RANGE"
    if not isinstance(value, str):
        return None, "NOT_A_DATE"
    try:
        parsed = datetime.strptime(value, fmt)  # noqa: DTZ007 - naive on purpose; time zones are refused earlier
    except ValueError as exc:
        return None, "DATE_OUT_OF_RANGE" if "out of range" in str(exc) else "NOT_A_DATE"
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return None, "DATE_OUT_OF_RANGE"
    try:
        return pd.Timestamp(parsed), None
    except (ValueError, OverflowError, pd.errors.OutOfBoundsDatetime):
        return None, "DATE_OUT_OF_RANGE"


# --- helpers -------------------------------------------------------------------------------------


def _require_columns(frame: pd.DataFrame, names: list[str], where: str) -> None:
    known = {str(c) for c in frame.columns}
    missing = [n for n in names if n not in known]
    if missing:
        raise OperationError(f"{where}: unknown column(s) {missing}; columns are {sorted(known)}")


def _cell_key(value: Any) -> tuple[str, Any]:
    if value is None or (not isinstance(value, str) and is_null(value)):
        return ("null", None)
    if hasattr(value, "item") and not isinstance(value, pd.Timestamp):
        value = value.item()
    return (type(value).__name__, normalise_scalar(value))


def _log(name: str, parameters: Any, before: int, after: int, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "operation": name,
        "parameters": parameters,
        "rows_before": before,
        "rows_after": after,
        "rows_rejected": 0,
        "rows_duplicate": 0,
        "cells_changed": 0,
    }
    entry.update(extra)
    return entry


def _typed_number_column(index: pd.Index, values: list[Any], accepted: list[Any]) -> pd.Series:
    integral = all(isinstance(v, int) for v in accepted)
    if integral:
        return pd.Series(pd.array(values, dtype="Int64"), index=index)
    return pd.Series(
        [float("nan") if v is None else float(v) for v in values], index=index, dtype="float64"
    )


# --- the plan ------------------------------------------------------------------------------------


def _validate(frame: pd.DataFrame, plan: CleanPlan) -> None:
    if isinstance(plan.trim, list):
        _require_columns(frame, plan.trim, "trim")
    _require_columns(frame, plan.drop_empty_columns, "drop_empty_columns")
    _require_columns(frame, [f.column for f in plan.parse_number], "parse_number")
    _require_columns(frame, [f.column for f in plan.parse_date], "parse_date")
    if plan.drop_duplicates and plan.drop_duplicates.subset:
        _require_columns(frame, plan.drop_duplicates.subset, "drop_duplicates")
    parsed = [f.column for f in plan.parse_number] + [f.column for f in plan.parse_date]
    repeated = sorted({c for c in parsed if parsed.count(c) > 1})
    if repeated:
        raise OperationError(f"a column can be parsed once: {repeated}")
    for date_format in plan.parse_date:
        if "%z" in date_format.format or "%Z" in date_format.format:
            raise OperationError("time zones in parse_date formats are not supported")


def clean(ds: Dataset, plan: CleanPlan) -> CleanResult:
    """Apply the plan in the fixed order. Never changes ``ds``. Raises ``RejectedTooMany`` over the
    plan's ``max_reject_share`` and ``OperationError`` for an unknown column."""
    _validate(ds.frame, plan)
    na_values = tuple(ds.options.na_values)
    frame = ds.frame.sort_index(kind="stable").copy()
    rows_in = len(frame)
    log: list[dict[str, Any]] = []
    used: list[str] = []
    rejects: list[tuple[int, str, str, Any, str]] = []
    duplicates: list[tuple[int, int]] = []

    # 1. trim ------------------------------------------------------------------------------------
    if plan.trim is not None:
        columns = [str(c) for c in frame.columns] if plan.trim == "all" else list(plan.trim)
        changed = 0
        before = len(frame)
        for name in columns:
            new_values = []
            for value in frame[name].tolist():
                if isinstance(value, str):
                    stripped = value.strip()
                    if stripped != value:
                        changed += 1
                    new_values.append(stripped if stripped != "" else None)
                else:
                    new_values.append(value)
            frame[name] = pd.Series(new_values, index=frame.index, dtype=object)
        used.append("trim")
        log.append(_log("trim", columns, before, len(frame), cells_changed=changed))

    # 2. drop_empty_columns ----------------------------------------------------------------------
    if plan.drop_empty_columns:
        dropped, kept = [], []
        for name in plan.drop_empty_columns:
            if all(is_null(v, na_values) for v in frame[name].tolist()):
                dropped.append(name)
            else:
                kept.append(name)
        frame = frame.drop(columns=dropped)
        used.append("drop_empty_columns")
        log.append(
            _log(
                "drop_empty_columns",
                list(plan.drop_empty_columns),
                len(frame),
                len(frame),
                columns_dropped=dropped,
                kept_not_empty=kept,
            )
        )

    # 3. parse_number and 4. parse_date ----------------------------------------------------------
    for op_name, formats in (("parse_number", plan.parse_number), ("parse_date", plan.parse_date)):
        if not formats:
            continue
        before = len(frame)
        failed: dict[int, tuple[str, Any, str]] = {}  # row_id -> (column, original, reason)
        reasons: dict[str, int] = {}
        changed = 0
        for fmt in formats:
            parsed: list[Any] = []
            accepted: list[Any] = []
            pattern = _number_pattern(fmt) if isinstance(fmt, NumberFormat) else None
            for row_id, value in zip(frame.index.tolist(), frame[fmt.column].tolist(), strict=True):
                if row_id in failed or is_null(value, na_values):
                    parsed.append(None)
                    continue
                if isinstance(fmt, NumberFormat):
                    assert pattern is not None
                    result, reason = parse_number_value(value, fmt, pattern)
                else:
                    assert isinstance(fmt, DateFormat)
                    result, reason = parse_date_value(value, fmt.format)
                if reason:
                    failed[int(row_id)] = (fmt.column, value, reason)
                    reasons[reason] = reasons.get(reason, 0) + 1
                    parsed.append(None)
                    continue
                parsed.append(result)
                accepted.append(result)
                if isinstance(value, str):
                    changed += 1
            if isinstance(fmt, NumberFormat):
                frame[fmt.column] = _typed_number_column(frame.index, parsed, accepted)
            else:
                frame[fmt.column] = pd.Series(
                    pd.to_datetime(pd.Series(parsed, index=frame.index, dtype=object)),
                    index=frame.index,
                )
        for row_id in sorted(failed):
            column, original, reason = failed[row_id]
            rejects.append((row_id, op_name, column, original, reason))
        frame = frame.drop(index=list(failed))
        used.append(op_name)
        log.append(
            _log(
                op_name,
                [f.model_dump(mode="json") for f in formats],
                before,
                len(frame),
                rows_rejected=len(failed),
                cells_changed=changed,
                reasons=dict(sorted(reasons.items())),
            )
        )

    # 5. drop_duplicates -------------------------------------------------------------------------
    if plan.drop_duplicates is not None:
        before = len(frame)
        subset = (
            list(plan.drop_duplicates.subset)
            if plan.drop_duplicates.subset
            else [str(c) for c in frame.columns]
        )
        first_seen: dict[tuple[tuple[str, Any], ...], int] = {}
        drop_ids: list[int] = []
        columns_values = [frame[name].tolist() for name in subset]
        for position, row_id in enumerate(frame.index.tolist()):
            key = tuple(_cell_key(col[position]) for col in columns_values)
            if key in first_seen:
                duplicates.append((int(row_id), first_seen[key]))
                drop_ids.append(row_id)
            else:
                first_seen[key] = int(row_id)
        frame = frame.drop(index=drop_ids)
        used.append("drop_duplicates")
        log.append(
            _log(
                "drop_duplicates",
                subset,
                before,
                len(frame),
                rows_duplicate=len(drop_ids),
                kept="lowest row_id",
            )
        )

    # invariants (checked at run time; a violation aborts the run) -------------------------------
    previous = rows_in
    for entry in log:
        if entry["rows_before"] != previous:
            raise IntegrityError(
                f"{entry['operation']}: rows_before {entry['rows_before']} != {previous}"
            )
        if (
            entry["rows_before"] - entry["rows_rejected"] - entry["rows_duplicate"]
            != entry["rows_after"]
        ):
            raise IntegrityError(f"{entry['operation']}: row counts do not add up")
        previous = entry["rows_after"]
    if rows_in != len(frame) + len(rejects) + len(duplicates):
        raise IntegrityError("rows_in != rows_cleaned + rows_rejected + rows_duplicate")
    if {r[0] for r in rejects} & set(frame.index.tolist()) or {d[0] for d in duplicates} & {
        r[0] for r in rejects
    }:
        raise IntegrityError("a row appears in more than one output")

    if rows_in and len(rejects) / rows_in > plan.max_reject_share:
        raise RejectedTooMany(
            f"{len(rejects)} of {rows_in} rows were rejected "
            f"({len(rejects) / rows_in:.1%}), more than the allowed {plan.max_reject_share:.1%}; "
            "this usually means the wrong format was given. Nothing is published."
        )

    plan_record = plan.model_dump(mode="json")
    plan_record["order"] = [name for name in ORDER if name in used]
    reject_frame = pd.DataFrame(
        [
            (rid, op, col, None if val is None else str(val), reason)
            for rid, op, col, val, reason in rejects
        ],
        columns=REJECT_COLUMNS,
        dtype=object,
    )
    duplicate_frame = pd.DataFrame(
        sorted(duplicates), columns=["row_id", "duplicate_of"], dtype=object
    )
    cleaned = replace(ds, frame=frame, notes=[dict(n) for n in ds.notes])
    return CleanResult(
        dataset=cleaned,
        rejects=reject_frame.sort_values("row_id", kind="stable").reset_index(drop=True),
        duplicates=duplicate_frame,
        log=log,
        plan=plan_record,
        rows_in=rows_in,
    )
