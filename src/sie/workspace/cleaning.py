"""Data cleaning for the workspace: typed, logged steps on a copy. The loaded data is never altered.

A step is data (a small pydantic model), never code. ``apply_steps`` runs steps in the order given on a
copy of the dataset and returns the cleaned dataset plus one log entry per step. Nothing is applied by
itself: ``suggest`` only proposes steps from the profile, and a person has to approve each one.

Rules kept here
  * a value that cannot be handled stops the step with an error; it is never turned into a null quietly
    (text to number reuses the engine's strict ``clean`` with no rejects allowed);
  * filling uses only the stated method (median, mean, mode or a constant you give); the filled value is
    in the log;
  * outliers are only flagged (counts and bounds) by ``flag_outliers``; rows go only through an explicit
    ``remove_outliers`` step;
  * the log holds counts and values as numbers; ``describe`` turns them into text through
    ``sie.workspace.figs`` so every displayed number is an exact figure.

Deterministic: no clock, no randomness, no environment.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Annotated, Any, Literal, get_args

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from sie.datasets import CleanPlan, clean
from sie.datasets.models import (
    ColumnProfile,
    Dataset,
    DatasetError,
    DateFormat,
    DedupSpec,
    NumberFormat,
    OperationError,
    ProfileResult,
)
from sie.datasets.types import is_null
from sie.workspace import figs

DROP_COLUMN_SHARE = 0.5  # suggest dropping a column that is at least this empty
OUTLIER_K = 1.5  # the usual Tukey fence: Q1 - k*IQR .. Q3 + k*IQR
MIN_OUTLIER_VALUES = 8  # fewer numbers than this: no outlier talk
_COL = Annotated[str, Field(min_length=1, max_length=200)]


class _Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FillMissing(_Step):
    op: Literal["fill_missing"] = "fill_missing"
    column: _COL
    method: Literal["median", "mean", "mode", "constant"]
    value: Annotated[str, Field(max_length=200)] | None = None  # only for method == "constant"


class DropMissingRows(_Step):
    op: Literal["drop_missing_rows"] = "drop_missing_rows"
    columns: list[_COL] = Field(min_length=1, max_length=50)


class DropColumn(_Step):
    op: Literal["drop_column"] = "drop_column"
    column: _COL


class RemoveDuplicates(_Step):
    op: Literal["remove_duplicates"] = "remove_duplicates"
    subset: list[_COL] | None = None  # None = every column


class TextToNumber(_Step):
    op: Literal["text_to_number"] = "text_to_number"
    column: _COL
    decimal: Literal[".", ","] = "."
    thousands: Literal["", ",", ".", " ", "'"] = ""


DateFormatName = Literal[
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
]
DATE_FORMATS: tuple[str, ...] = get_args(DateFormatName)
DATE_FORMAT_WORDS = {  # how each format reads, for people; a fixed list, so nothing is guessed
    "%d/%m/%Y": "day/month/year, like 31/12/2025",
    "%m/%d/%Y": "month/day/year, like 12/31/2025",
    "%d-%m-%Y": "day-month-year, like 31-12-2025",
    "%m-%d-%Y": "month-day-year, like 12-31-2025",
    "%d.%m.%Y": "day.month.year, like 31.12.2025",
    "%Y/%m/%d": "year/month/day, like 2025/12/31",
    "%Y.%m.%d": "year.month.day, like 2025.12.31",
    "%d %b %Y": "day, short month name, year, like 31 Dec 2025",
    "%d %B %Y": "day, month name, year, like 31 December 2025",
    "%b %d, %Y": "short month name, day, year, like Dec 31, 2025",
    "%B %d, %Y": "month name, day, year, like December 31, 2025",
    "%d-%b-%Y": "day-short month name-year, like 31-Dec-2025",
}
MAX_DISTINCT_DATE_CHECK = 50_000


class TextToDate(_Step):
    """Text in one stated date format to real dates. Strict: a value that does not fit stops the step."""

    op: Literal["text_to_date"] = "text_to_date"
    column: _COL
    format: DateFormatName


class FlagOutliers(_Step):
    op: Literal["flag_outliers"] = "flag_outliers"
    column: _COL
    k: float = Field(default=OUTLIER_K, gt=0, le=10)


class RemoveOutliers(_Step):
    op: Literal["remove_outliers"] = "remove_outliers"
    column: _COL
    k: float = Field(default=OUTLIER_K, gt=0, le=10)


FilterTest = Literal["equals", "not_equals", "in", "not_in", "between"]
MAX_FILTER_VALUES = 50


class FilterRows(_Step):
    """Keep or drop rows by one column. ``equals``/``in`` keep the rows whose value is one of ``values``
    (rows with no value are dropped); ``not_equals``/``not_in`` drop those rows (rows with no value are
    kept, since an empty cell is not that value); ``between`` keeps rows whose number or date lies in
    ``low``..``high``, both ends included and either end optional (dates are written YYYY-MM-DD; rows with
    no value are dropped). Text is matched exactly, capitals included."""

    op: Literal["filter_rows"] = "filter_rows"
    column: _COL
    test: FilterTest
    values: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = None
    low: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    high: Annotated[str, Field(min_length=1, max_length=40)] | None = None

    @model_validator(mode="after")
    def _shape(self) -> FilterRows:
        if self.test == "between":
            if self.values is not None:
                raise ValueError("between takes low and high, not values")
            if self.low is None and self.high is None:
                raise ValueError("between needs low, high or both")
        else:
            if self.low is not None or self.high is not None:
                raise ValueError(f"{self.test} takes values, not low and high")
            if not self.values or len(self.values) > MAX_FILTER_VALUES:
                raise ValueError(f"{self.test} needs 1 to {MAX_FILTER_VALUES} values")
            if self.test in ("equals", "not_equals") and len(self.values) != 1:
                raise ValueError(f"{self.test} takes exactly one value; use in / not_in for more")
        return self


CleaningStep = (
    FillMissing
    | DropMissingRows
    | DropColumn
    | RemoveDuplicates
    | TextToNumber
    | TextToDate
    | FlagOutliers
    | RemoveOutliers
    | FilterRows
)
_ADAPTER: TypeAdapter[CleaningStep] = TypeAdapter(
    Annotated[CleaningStep, Field(discriminator="op")]
)


def parse_step(obj: object) -> CleaningStep:
    """A step from untrusted data (a request body or a recorded history). Raises ``OperationError``."""
    try:
        return _ADAPTER.validate_python(obj)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise OperationError(f"not a cleaning step: {where}: {first['msg']}"[:300]) from exc


@dataclass
class CleaningOutcome:
    dataset: Dataset
    entries: list[dict[str, Any]] = field(default_factory=list)


# -- numbers -------------------------------------------------------------------------------------


def _require(frame: pd.DataFrame, columns: list[str], where: str) -> None:
    known = {str(c) for c in frame.columns}
    missing = [c for c in columns if c not in known]
    if missing:
        raise OperationError(f"{where}: unknown column(s) {missing}; columns are {sorted(known)}")


def _null(value: Any, ds: Dataset) -> bool:
    return is_null(value, tuple(ds.options.na_values))


def _numeric_values(ds: Dataset, column: str) -> list[float | None]:
    """Every value of the column as a float, None for a null. Raises ``OperationError`` when a value is
    text that is not a plain number (use text to number first)."""
    out: list[float | None] = []
    for value in ds.frame[column].tolist():
        if _null(value, ds):
            out.append(None)
            continue
        if isinstance(value, bool):
            raise OperationError(f"{column} holds true/false values, not numbers")
        if isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                raise OperationError(
                    f"{column} has a value that is not a number ({value[:30]!r}); "
                    "convert the column with 'text to number' first"
                ) from None
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise OperationError(f"{column} has a value that is not a number") from None
        if not math.isfinite(number):
            raise OperationError(f"{column} has a value that is not a finite number")
        out.append(number)
    return out


def _text(number: float) -> str:
    return str(int(number)) if number == int(number) and abs(number) < 1e15 else repr(number)


def _quartiles(values: list[float]) -> tuple[float, float]:
    s = pd.Series(values, dtype="float64")
    return float(s.quantile(0.25)), float(s.quantile(0.75))


def fences(values: list[float], k: float) -> tuple[float, float] | None:
    """``(low, high)`` of the Tukey rule, or None when there are too few values or no spread."""
    if len(values) < MIN_OUTLIER_VALUES:
        return None
    q1, q3 = _quartiles(values)
    iqr = q3 - q1
    if iqr == 0:
        return None
    return q1 - k * iqr, q3 + k * iqr


def _entry(step: CleaningStep, before: int, after: int, **facts: Any) -> dict[str, Any]:
    return {
        "op": step.op,
        "parameters": step.model_dump(mode="json"),
        "rows_before": before,
        "rows_after": after,
        **facts,
    }


# -- the steps -----------------------------------------------------------------------------------


def _fill(ds: Dataset, step: FillMissing) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "fill_missing")
    column = step.column
    values = ds.frame[column].tolist()
    missing = [i for i, v in enumerate(values) if _null(v, ds)]
    rows = len(values)
    if not missing:
        return ds, _entry(
            step, rows, rows, cells_filled=0, filled_with=None, note="nothing missing"
        )
    present = [v for v in values if not _null(v, ds)]
    strings = all(isinstance(v, str) for v in present)
    fill: Any
    if step.method in ("median", "mean"):
        numbers = [n for n in _numeric_values(ds, column) if n is not None]
        if not numbers:
            raise OperationError(f"{column} has no values to take the {step.method} of")
        series = pd.Series(numbers, dtype="float64")
        fill_number = float(series.median() if step.method == "median" else series.mean())
        fill = _text(fill_number) if strings else fill_number
        shown: Any = fill_number
    elif step.method == "mode":
        if not present:
            raise OperationError(f"{column} has no values to take the mode of")
        counts: dict[Any, int] = {}
        for v in present:
            counts[v] = counts.get(v, 0) + 1
        best = max(counts.values())
        fill = sorted((v for v, n in counts.items() if n == best), key=lambda x: (str(x), repr(x)))[
            0
        ]
        shown = fill
    else:
        if step.value is None or step.value == "":
            raise OperationError("fill_missing with a constant needs a value")
        try:  # a column of numbers only takes a number: a word would make it a mixed column
            _numeric_values(ds, column)
            numeric_column = True
        except OperationError:
            numeric_column = False
        if numeric_column:
            try:
                number = float(step.value)
            except ValueError:
                raise OperationError(
                    f"{column} is numeric; {step.value!r} is not a number"
                ) from None
            if not math.isfinite(number):
                raise OperationError("the constant must be a finite number")
            fill = _text(number) if strings else number
        else:
            fill = step.value
        shown = fill
    new = list(values)
    for i in missing:
        new[i] = fill
    frame = ds.frame.copy()
    if strings or not present:
        frame[column] = pd.Series(new, index=frame.index, dtype=object)
    else:
        floats = [float("nan") if _null(v, ds) else float(v) for v in new]
        integral = all(float(f) == int(f) for f in floats if not math.isnan(f))
        if integral and all(isinstance(v, int) or hasattr(v, "dtype") for v in present):
            frame[column] = pd.Series(
                pd.array([int(f) for f in floats], dtype="Int64"), index=frame.index
            )
        else:
            frame[column] = pd.Series(floats, index=frame.index, dtype="float64")
    return replace(ds, frame=frame), _entry(
        step, rows, rows, cells_filled=len(missing), filled_with=shown
    )


def _drop_missing_rows(ds: Dataset, step: DropMissingRows) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, step.columns, "drop_missing_rows")
    keep = []
    for position in range(len(ds.frame)):
        row_missing = any(_null(ds.frame[c].iloc[position], ds) for c in step.columns)
        keep.append(not row_missing)
    frame = ds.frame[pd.Series(keep, index=ds.frame.index)]
    return replace(ds, frame=frame), _entry(step, len(ds.frame), len(frame))


def _drop_column(ds: Dataset, step: DropColumn) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "drop_column")
    if len(ds.frame.columns) == 1:
        raise OperationError("the last column cannot be dropped")
    empty = sum(1 for v in ds.frame[step.column].tolist() if _null(v, ds))
    frame = ds.frame.drop(columns=[step.column])
    return replace(ds, frame=frame), _entry(
        step, len(frame), len(frame), values_empty=empty, values_total=len(frame)
    )


def _remove_duplicates(ds: Dataset, step: RemoveDuplicates) -> tuple[Dataset, dict[str, Any]]:
    if step.subset:
        _require(ds.frame, step.subset, "remove_duplicates")
    result = clean(ds, CleanPlan(drop_duplicates=DedupSpec(subset=step.subset)))
    removed = len(ds.frame) - len(result.dataset.frame)
    return result.dataset, _entry(
        step,
        len(ds.frame),
        len(result.dataset.frame),
        rows_removed=removed,
        kept="first occurrence",
    )


def _text_to_number(ds: Dataset, step: TextToNumber) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "text_to_number")
    plan = CleanPlan(
        parse_number=[
            NumberFormat(column=step.column, decimal=step.decimal, thousands=step.thousands)
        ],
        max_reject_share=0.0,
    )
    try:
        result = clean(ds, plan)
    except DatasetError as exc:
        raise OperationError(
            f"{step.column} cannot be converted to numbers with these separators: nothing was changed. "
            f"{str(exc)[:160]}"
        ) from exc
    log = next(e for e in result.log if e["operation"] == "parse_number")
    return result.dataset, _entry(
        step, len(ds.frame), len(result.dataset.frame), cells_converted=int(log["cells_changed"])
    )


def _text_to_date(ds: Dataset, step: TextToDate) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "text_to_date")
    values = [v for v in ds.frame[step.column].tolist() if not _null(v, ds)]
    if not values or not all(isinstance(v, str) for v in values):
        raise OperationError(
            f"{step.column} is not a text column with values to read as dates: nothing was changed."
        )
    plan = CleanPlan(
        parse_date=[DateFormat(column=step.column, format=step.format)], max_reject_share=0.0
    )
    try:
        result = clean(ds, plan)
    except DatasetError as exc:
        raise OperationError(
            f"{step.column} cannot all be read as dates written {DATE_FORMAT_WORDS[step.format]}: "
            f"nothing was changed. {str(exc)[:160]}"
        ) from exc
    log = next(e for e in result.log if e["operation"] == "parse_date")
    return result.dataset, _entry(
        step, len(ds.frame), len(result.dataset.frame), cells_converted=int(log["cells_changed"])
    )


def _outlier_rows(
    ds: Dataset, column: str, k: float
) -> tuple[list[int], tuple[float, float] | None]:
    numbers = _numeric_values(ds, column)
    fence = fences([n for n in numbers if n is not None], k)
    if fence is None:
        return [], None
    low, high = fence
    hits = [i for i, n in enumerate(numbers) if n is not None and (n < low or n > high)]
    return hits, fence


def _flag(ds: Dataset, step: FlagOutliers) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "flag_outliers")
    hits, fence = _outlier_rows(ds, step.column, step.k)
    row_ids = [int(ds.frame.index[i]) for i in hits]
    return ds, _entry(
        step,
        len(ds.frame),
        len(ds.frame),
        outliers=len(hits),
        low=None if fence is None else fence[0],
        high=None if fence is None else fence[1],
        row_ids=row_ids[:20],
        changed=False,
    )


def _remove_outliers(ds: Dataset, step: RemoveOutliers) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "remove_outliers")
    hits, fence = _outlier_rows(ds, step.column, step.k)
    drop = set(hits)
    frame = ds.frame[pd.Series([i not in drop for i in range(len(ds.frame))], index=ds.frame.index)]
    return replace(ds, frame=frame), _entry(
        step,
        len(ds.frame),
        len(frame),
        rows_removed=len(hits),
        low=None if fence is None else fence[0],
        high=None if fence is None else fence[1],
    )


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING_ZERO = re.compile(r"^0\d")


def column_kind(ds: Dataset, column: str) -> Literal["date", "number", "text"]:
    """How a filter reads the column. Numbers and ISO dates written as text count as numbers and dates
    (a CSV is read as text until it is converted); a value with a leading zero is an identifier, so the
    column is text."""
    present = [v for v in ds.frame[column].tolist() if not _null(v, ds)]
    if any(isinstance(v, datetime | date | pd.Timestamp) for v in present):
        return "date"
    if present and all(isinstance(v, str) for v in present):
        texts = [v.strip() for v in present]
        if all(_ISO_DAY.match(t) for t in texts):
            try:
                for t in set(texts):
                    date.fromisoformat(t)
                return "date"
            except ValueError:
                return "text"
        try:
            numeric = all(math.isfinite(float(t)) for t in texts)
        except ValueError:
            numeric = False
        if numeric and not any(_LEADING_ZERO.match(t) for t in texts):
            return "number"
        return "text"
    if present and not any(isinstance(v, str | bool) for v in present):
        return "number"
    return "text"


def _iso_date(text: str, what: str) -> date:
    try:
        if len(text) != 10:
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise OperationError(
            f"{what} must be a date written YYYY-MM-DD, not {text[:30]!r}"
        ) from None


def _plain_number(text: str, what: str) -> float:
    try:
        x = float(text)
    except ValueError:
        raise OperationError(f"{what} must be a number, not {text[:30]!r}") from None
    if not math.isfinite(x):
        raise OperationError(f"{what} must be a finite number")
    return x


def _filter(ds: Dataset, step: FilterRows) -> tuple[Dataset, dict[str, Any]]:
    _require(ds.frame, [step.column], "filter_rows")
    kind = column_kind(ds, step.column)
    cells = ds.frame[step.column].tolist()
    empty = [_null(v, ds) for v in cells]
    if step.test == "between":
        if kind == "text":
            raise OperationError(
                f"{step.column} holds text, so a range does not apply; "
                "convert it to numbers or dates first"
            )
        parse = _iso_date if kind == "date" else _plain_number
        low = None if step.low is None else parse(step.low, "low")
        high = None if step.high is None else parse(step.high, "high")
        if low is not None and high is not None and low > high:
            raise OperationError("low is above high, so no row could match")
        keep = []
        for v, gone in zip(cells, empty, strict=True):
            if gone:
                keep.append(False)
                continue
            x = (
                pd.Timestamp(str(v).strip() if isinstance(v, str) else v).date()
                if kind == "date"
                else float(v)
            )
            keep.append((low is None or x >= low) and (high is None or x <= high))
    else:
        assert step.values is not None
        if kind == "date":
            wanted: set[Any] = {_iso_date(v, "a value") for v in step.values}
            hit = [
                (not g)
                and pd.Timestamp(str(v).strip() if isinstance(v, str) else v).date() in wanted
                for v, g in zip(cells, empty, strict=True)
            ]
        elif kind == "number":
            numbers = {_plain_number(v, "a value") for v in step.values}
            hit = [(not g) and float(v) in numbers for v, g in zip(cells, empty, strict=True)]
        else:
            texts = set(step.values)
            hit = [(not g) and str(v) in texts for v, g in zip(cells, empty, strict=True)]
        if step.test in ("equals", "in"):
            keep = hit
        else:
            keep = [g or not h for h, g in zip(hit, empty, strict=True)]
    if not any(keep):
        raise OperationError("no row would remain, so nothing was changed")
    frame = ds.frame[pd.Series(keep, index=ds.frame.index)]
    dropped_empty = sum(1 for k, g in zip(keep, empty, strict=True) if g and not k)
    return replace(ds, frame=frame), _entry(
        step,
        len(ds.frame),
        len(frame),
        rows_removed=len(ds.frame) - len(frame),
        rows_without_value_removed=dropped_empty,
        kind=kind,
    )


_RUN = {
    "fill_missing": _fill,
    "drop_missing_rows": _drop_missing_rows,
    "drop_column": _drop_column,
    "remove_duplicates": _remove_duplicates,
    "text_to_number": _text_to_number,
    "text_to_date": _text_to_date,
    "flag_outliers": _flag,
    "remove_outliers": _remove_outliers,
    "filter_rows": _filter,
}


def apply_steps(ds: Dataset, steps: list[CleaningStep]) -> CleaningOutcome:
    """Run the steps in order on a copy. ``ds`` is never changed. Raises ``OperationError`` (or another
    ``DatasetError``) and returns nothing partial when a step cannot run."""
    current = replace(ds, frame=ds.frame.copy())
    entries: list[dict[str, Any]] = []
    for step in steps:
        current, entry = _RUN[step.op](current, step)  # type: ignore[operator]
        entries.append(entry)
    return CleaningOutcome(current, entries)


# -- text for people -----------------------------------------------------------------------------


def _n(value: Any, source: str, label: str) -> str:
    """A count or value as an exact figure."""
    return figs.show(
        figs.file_figure(
            "c", value, label=label, file_sha256=source, decimals=figs.decimals_for(value)
        )
    )


def describe(entry: dict[str, Any], source_sha256: str) -> str:
    """One sentence for a log entry. Every number goes through ``sie.answer`` formatting."""
    p = entry["parameters"]
    op = entry["op"]

    def n(v: Any) -> str:
        return _n(v, source_sha256, op)

    if op == "fill_missing":
        count = entry["cells_filled"]
        if not count:
            return f"{p['column']} had no missing values, so nothing was filled."
        how = {
            "median": "the median",
            "mean": "the mean",
            "mode": "the most frequent value",
            "constant": "the value you gave",
        }[p["method"]]
        value = entry["filled_with"]
        shown = (
            n(value) if isinstance(value, int | float) and not isinstance(value, bool) else value
        )
        noun = "value was" if count == 1 else "values were"
        return f"{n(count)} missing {p['column']} {noun} filled with {how} ({shown})."
    if op == "drop_missing_rows":
        removed = entry["rows_before"] - entry["rows_after"]
        cols = ", ".join(p["columns"])
        noun = "row" if removed == 1 else "rows"
        verb = "was" if removed == 1 else "were"
        return (
            f"{n(removed)} {noun} with a missing value in {cols} {verb} removed; "
            f"{n(entry['rows_after'])} rows remain."
        )
    if op == "drop_column":
        return (
            f"Column {p['column']} was removed ({n(entry['values_empty'])} of "
            f"{n(entry['values_total'])} values were empty)."
        )
    if op == "remove_duplicates":
        k = entry["rows_removed"]
        noun = "duplicate row was" if k == 1 else "duplicate rows were"
        return f"{n(k)} {noun} removed (the first occurrence of each is kept)."
    if op == "text_to_number":
        k = entry["cells_converted"]
        noun = "value was" if k == 1 else "values were"
        return f"{n(k)} {p['column']} {noun} converted from text to numbers."
    if op == "text_to_date":
        k = entry["cells_converted"]
        noun = "value was" if k == 1 else "values were"
        return (
            f"{n(k)} {p['column']} {noun} converted from text to dates "
            f"(written {DATE_FORMAT_WORDS[p['format']]})."
        )
    if op == "flag_outliers":
        if entry["low"] is None:
            return f"{p['column']}: no outlier check was possible (too few numbers or no spread). Nothing was changed."
        k = entry["outliers"]
        noun = "value lies" if k == 1 else "values lie"
        return (
            f"{n(k)} {p['column']} {noun} outside {n(entry['low'])} to "
            f"{n(entry['high'])} (the interquartile-range rule). They were flagged only; nothing was changed."
        )
    if op == "filter_rows":
        kept, removed = entry["rows_after"], entry["rows_removed"]
        what = _filter_words(p)
        text = (
            f"Kept the {n(kept)} rows where {what}; {n(removed)} "
            f"{'row was' if removed == 1 else 'rows were'} removed."
        )
        if entry["rows_without_value_removed"]:
            text += (
                f" {n(entry['rows_without_value_removed'])} of them had no {p['column']} value "
                "at all."
            )
        return text
    if op == "remove_outliers":
        if entry["low"] is None:
            return f"{p['column']}: no outlier check was possible, so no rows were removed."
        k = entry["rows_removed"]
        noun = "row" if k == 1 else "rows"
        verb = "was" if k == 1 else "were"
        return (
            f"{n(k)} {noun} with {p['column']} outside {n(entry['low'])} to "
            f"{n(entry['high'])} {verb} removed."
        )
    return op


def _filter_words(p: dict[str, Any]) -> str:
    """The test of a filter step in words (parameters only; no computed numbers)."""
    col = p["column"]
    if p["test"] == "between":
        if p["low"] is not None and p["high"] is not None:
            return f"{col} is from {p['low']} to {p['high']}"
        return (
            f"{col} is {'at least ' + p['low'] if p['low'] is not None else 'at most ' + p['high']}"
        )
    values = ", ".join(p["values"])
    return {
        "equals": f"{col} is {values}",
        "in": f"{col} is one of {values}",
        "not_equals": f"{col} is not {values}",
        "not_in": f"{col} is none of {values}",
    }[p["test"]]


def step_title(step: CleaningStep) -> str:
    """A short label for a step (no numbers except parameters the person gave)."""
    if isinstance(step, FillMissing):
        what = step.method if step.method != "constant" else f"the value {step.value}"
        return f"Fill missing {step.column} with {what}"
    if isinstance(step, DropMissingRows):
        return "Remove rows missing " + ", ".join(step.columns)
    if isinstance(step, DropColumn):
        return f"Remove column {step.column}"
    if isinstance(step, RemoveDuplicates):
        return "Remove duplicate rows"
    if isinstance(step, TextToNumber):
        return f"Convert {step.column} to numbers"
    if isinstance(step, TextToDate):
        return f"Convert {step.column} to dates ({DATE_FORMAT_WORDS[step.format]})"
    if isinstance(step, FlagOutliers):
        return f"Flag outliers in {step.column}"
    if isinstance(step, FilterRows):
        return f"Keep rows where {_filter_words(step.model_dump(mode='json'))}"
    return f"Remove outliers in {step.column}"


# -- suggestions ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    id: str
    label: str
    reason: str
    step: CleaningStep

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "reason": self.reason,
            "step": self.step.model_dump(mode="json"),
        }


def _percent(part: int, whole: int, source: str) -> str:
    value = part * 100 / whole if whole else 0
    decimals = 1 if 0 < value < 1 else 0  # "0%" for 2 of 891 would be wrong: show 0.2%
    return figs.show(
        figs.file_figure(
            "pct", value, label="share", file_sha256=source, decimals=decimals, unit="%"
        )
    )


def _plain_numbers(ds: Dataset, column: str) -> str | None:
    """The separators that make every value of a text column a number, or None."""
    values = [v for v in ds.frame[column].tolist() if not _null(v, ds)]
    if not values or not all(isinstance(v, str) for v in values):
        return None
    if any(
        len(v.strip()) > 1 and v.strip().startswith("0") and v.strip()[1:2].isdigit()
        for v in values
    ):
        return None  # leading zeros: an identifier (zip code, phone), not a quantity
    for decimal, thousands in ((".", ""), (".", ","), (",", ".")):
        try:
            clean(
                ds,
                CleanPlan(
                    parse_number=[
                        NumberFormat(column=column, decimal=decimal, thousands=thousands)
                    ],
                    max_reject_share=0.0,
                ),
            )
        except DatasetError:
            continue
        return f"{decimal}|{thousands}"
    return None


def _date_formats_that_fit(ds: Dataset, column: str) -> list[str]:
    """The listed formats under which every filled value of a text column reads as a date."""
    values = [v for v in ds.frame[column].tolist() if not _null(v, ds)]
    if not values or not all(isinstance(v, str) for v in values):
        return []
    distinct = list(dict.fromkeys(values))
    if len(distinct) > MAX_DISTINCT_DATE_CHECK:
        return []
    fits = []
    for fmt in DATE_FORMATS:
        try:
            for v in distinct:
                datetime.strptime(v, fmt)  # noqa: DTZ007 - dates only, no time zone
        except ValueError:
            continue
        fits.append(fmt)
    return fits


def suggest(ds: Dataset, profile: ProfileResult, source_sha256: str) -> list[Suggestion]:
    """Steps worth considering, from the profile. Proposals only; nothing here changes the data."""
    out: list[Suggestion] = []
    rows = profile.rows
    n = lambda v: _n(v, source_sha256, "suggestion")  # noqa: E731
    if profile.duplicate_rows:
        out.append(
            Suggestion(
                "remove_duplicates",
                "Remove duplicate rows",
                f"{n(profile.duplicate_rows)} "
                + ("row repeats" if profile.duplicate_rows == 1 else "rows repeat")
                + " an earlier row exactly.",
                RemoveDuplicates(),
            )
        )
    cols: list[ColumnProfile] = profile.column_profiles
    for c in cols:
        if c.type == "text" and c.non_null:
            seps = _plain_numbers(ds, c.name)
            if seps:
                decimal, thousands = seps.split("|")
                out.append(
                    Suggestion(
                        f"text_to_number:{c.name}",
                        f"Convert {c.name} to numbers",
                        f"Every filled {c.name} value reads as a number but is stored as text.",
                        TextToNumber(column=c.name, decimal=decimal, thousands=thousands),  # type: ignore[arg-type]
                    )
                )
    for c in cols:
        if c.type != "text" or not c.non_null:
            continue
        fits = _date_formats_that_fit(ds, c.name)
        for fmt in fits:
            reason = (
                f"Every filled {c.name} value reads as a date written {DATE_FORMAT_WORDS[fmt]}."
            )
            if len(fits) > 1:
                reason += (
                    " More than one order fits every value, so nothing is guessed: pick the one that"
                    " matches how your data was written."
                )
            out.append(
                Suggestion(
                    f"text_to_date:{c.name}:{fmt}",
                    f"Convert {c.name} to dates ({DATE_FORMAT_WORDS[fmt].split(',')[0]})",
                    reason,
                    TextToDate(column=c.name, format=fmt),  # type: ignore[arg-type]
                )
            )
    for c in cols:
        if not c.nulls or not rows:
            continue
        if c.null_share >= DROP_COLUMN_SHARE:
            out.append(
                Suggestion(
                    f"drop_column:{c.name}",
                    f"Remove column {c.name}",
                    f"{c.name} is {_percent(c.nulls, rows, source_sha256)} empty "
                    f"({n(c.nulls)} of {n(rows)} values), too little to use.",
                    DropColumn(column=c.name),
                )
            )
        elif c.type in ("integer", "decimal"):
            out.append(
                Suggestion(
                    f"fill_missing:{c.name}",
                    f"Fill missing {c.name} with the median",
                    f"{c.name} is {_percent(c.nulls, rows, source_sha256)} empty "
                    f"({n(c.nulls)} of {n(rows)} values); the median is not pulled by extreme values.",
                    FillMissing(column=c.name, method="median"),
                )
            )
        elif c.type == "text":
            out.append(
                Suggestion(
                    f"fill_missing:{c.name}",
                    f"Fill missing {c.name} with the most frequent value",
                    f"{c.name} is {_percent(c.nulls, rows, source_sha256)} empty "
                    f"({n(c.nulls)} of {n(rows)} values).",
                    FillMissing(column=c.name, method="mode"),
                )
            )
    for c in cols:
        if c.type in ("integer", "decimal"):
            try:
                hits, fence = _outlier_rows(ds, c.name, OUTLIER_K)
            except OperationError:
                continue
            if hits and fence is not None:
                out.append(
                    Suggestion(
                        f"flag_outliers:{c.name}",
                        f"Flag outliers in {c.name}",
                        f"{n(len(hits))} {c.name} "
                        + ("value lies" if len(hits) == 1 else "values lie")
                        + " far from the rest "
                        f"(outside {n(fence[0])} to {n(fence[1])}). Flagging changes nothing.",
                        FlagOutliers(column=c.name),
                    )
                )
    return out
