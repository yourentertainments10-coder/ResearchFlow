"""Independent check of a cleaning run: the steps are re-done with plain pandas and the two cleaned
tables are compared cell by cell.

This path shares no code with ``sie.workspace.cleaning`` (that one loops over values and reuses the
engine's ``clean``): numbers here come from ``pandas.to_numeric`` and Series methods, duplicates from
``DataFrame.duplicated``, fences from ``Series.quantile``. Any difference (rows, columns, a cell, a log
count) makes the cleaning ``unverified``, and the workspace then refuses to export.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from sie.datasets.models import Dataset
from sie.workspace.cleaning import (
    CleaningStep,
    DropColumn,
    DropMissingRows,
    FillMissing,
    FlagOutliers,
    RemoveDuplicates,
    RemoveOutliers,
    TextToDate,
    TextToNumber,
)
from sie.workspace.models import Verification

REL_TOL = 1e-9


def _blank(value: Any, na: tuple[str, ...]) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip() in na
    return bool(value is pd.NA or value is pd.NaT)


def _isnull(series: pd.Series, na: tuple[str, ...]) -> pd.Series:
    return pd.Series([_blank(v, na) for v in series.tolist()], index=series.index)


def _numeric(series: pd.Series, na: tuple[str, ...]) -> pd.Series:
    nulls = _isnull(series, na)
    text = series.where(~nulls, None).map(lambda v: v.strip() if isinstance(v, str) else v)
    return pd.to_numeric(pd.Series(text.tolist(), index=series.index, dtype=object))


def _text_number(series: pd.Series, decimal: str, thousands: str) -> pd.Series:
    def conv(v: Any) -> Any:
        if not isinstance(v, str):
            return v
        s = v
        if thousands:
            s = s.replace(thousands, "")
        return s.replace(decimal, ".") if decimal != "." else s

    return pd.to_numeric(
        pd.Series([conv(v) for v in series.tolist()], index=series.index, dtype=object)
    )


def _expected(frame: pd.DataFrame, step: CleaningStep, na: tuple[str, ...]) -> pd.DataFrame:
    if isinstance(step, FillMissing):
        nulls = _isnull(frame[step.column], na)
        if not nulls.any():
            return frame
        present = frame[step.column][~nulls]
        out = frame.copy()
        if step.method in ("median", "mean"):
            numbers = _numeric(frame[step.column], na).dropna()
            fill = numbers.median() if step.method == "median" else numbers.mean()
        elif step.method == "mode":
            counts = present.astype(str).value_counts()
            top = sorted(counts[counts == counts.max()].index)[0]
            fill = present[present.astype(str) == top].iloc[0]
        else:
            fill = step.value
            if not all(isinstance(v, str) for v in present.tolist()):
                fill = float(step.value)  # type: ignore[arg-type]
        out[step.column] = [
            fill if n else v
            for v, n in zip(frame[step.column].tolist(), nulls.tolist(), strict=True)
        ]
        return out
    if isinstance(step, DropMissingRows):
        gone = pd.Series(False, index=frame.index)
        for c in step.columns:
            gone |= _isnull(frame[c], na)
        return frame[~gone]
    if isinstance(step, DropColumn):
        return frame.drop(columns=[step.column])
    if isinstance(step, RemoveDuplicates):
        return frame[
            ~frame.astype(str)
            .where(~frame.isna(), "\0null")
            .duplicated(subset=step.subset, keep="first")
        ]
    if isinstance(step, TextToNumber):
        out = frame.copy()
        out[step.column] = _text_number(frame[step.column], step.decimal, step.thousands)
        return out
    if isinstance(step, TextToDate):
        out = frame.copy()
        nulls = _isnull(frame[step.column], na)
        texts = pd.Series(
            [
                None if n else v
                for v, n in zip(frame[step.column].tolist(), nulls.tolist(), strict=True)
            ],
            index=frame.index,
            dtype=object,
        )
        out[step.column] = pd.to_datetime(texts, format=step.format, errors="raise")
        return out
    if isinstance(step, FlagOutliers):
        return frame
    assert isinstance(step, RemoveOutliers)
    numbers = _numeric(frame[step.column], na)
    clean = numbers.dropna()
    if len(clean) < 8:
        return frame
    q1, q3 = clean.quantile(0.25), clean.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return frame
    low, high = q1 - step.k * iqr, q3 + step.k * iqr
    return frame[~((numbers < low) | (numbers > high))]


def _same(a: Any, b: Any, na: tuple[str, ...]) -> bool:
    a_null, b_null = _blank(a, na), _blank(b, na)
    if a_null or b_null:
        return a_null and b_null
    if hasattr(a, "item"):
        a = a.item()
    if hasattr(b, "item"):
        b = b.item()
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=1e-12)
    if isinstance(a, str) and isinstance(b, int | float):
        a, b = b, a
    if isinstance(b, str) and isinstance(a, int | float):
        try:
            return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=1e-12)
        except ValueError:
            return False
    return a == b


def verify_cleaning(
    original: Dataset, steps: list[CleaningStep], cleaned: Dataset, entries: list[dict[str, Any]]
) -> Verification:
    """Re-do ``steps`` on ``original`` independently and compare with ``cleaned`` and the log."""
    if not steps:
        return Verification("verified", 0)
    na = tuple(original.options.na_values)
    mismatches: list[str] = []
    checks = 0
    try:
        frame = original.frame.sort_index(kind="stable").copy()
        for step, entry in zip(steps, entries, strict=True):
            before = len(frame)
            frame = _expected(frame, step, na)
            checks += 1
            if entry["rows_before"] != before or entry["rows_after"] != len(frame):
                mismatches.append(
                    f"{step.op}: rows {entry['rows_before']}->{entry['rows_after']} in the log, "
                    f"{before}->{len(frame)} recomputed"
                )
    except Exception as exc:  # noqa: BLE001 - any failure of the second path means "not verified"
        return Verification(
            "unverified", checks, [], f"the independent cleaning check failed: {exc}"[:300]
        )
    got = cleaned.frame.sort_index(kind="stable")
    checks += 1
    if [str(c) for c in got.columns] != [str(c) for c in frame.columns]:
        mismatches.append("columns differ after cleaning")
    elif list(got.index) != list(frame.index):
        mismatches.append("the kept rows differ after cleaning")
    else:
        for name in got.columns:
            checks += 1
            bad = next(
                (
                    int(i)
                    for i, a, b in zip(
                        got.index, got[name].tolist(), frame[name].tolist(), strict=True
                    )
                    if not _same(a, b, na)
                ),
                None,
            )
            if bad is not None:
                mismatches.append(
                    f"column {name}: cell of row {bad} differs from the recomputed one"
                )
    if mismatches:
        return Verification("unverified", checks, mismatches, "; ".join(mismatches[:3]))
    return Verification("verified", checks)
