"""Track D: the six operations. See design section 6.

Every number comes from the table's rows through code in this module. A request is data (a spec
model or a dict), never code. Text is never coerced to a number; nulls are excluded and counted;
sums use exact arithmetic (integers) or ``math.fsum`` (decimals) so row order cannot change a result;
every ordering is stated and ends in a total order, so ties cannot make a result depend on input order.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from sie.datasets.models import (
    NULL_LABEL,
    OTHER_LABEL,
    STANDARD_LIMITATIONS,
    CleanResult,
    Correlation,
    Dataset,
    Describe,
    GroupAggregate,
    OperationError,
    OperationResult,
    Provenance,
    TimeTrend,
    TopN,
    ValueCounts,
    parse_metric,
    parse_spec,
)
from sie.datasets.provenance import table_sha256
from sie.datasets.types import infer_column, is_null, to_typed

MAX_PERIODS = 100_000
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_NUMERIC = ("integer", "decimal")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- typed columns -------------------------------------------------------------------------------


@dataclass
class _Col:
    name: str
    kind: str  # integer decimal boolean date datetime text mixed empty
    values: list[Any]  # python values in row order, None for null


def _is_midnight(values: list[Any]) -> bool:
    return all(
        v is None
        or (
            v.hour == 0
            and v.minute == 0
            and v.second == 0
            and v.microsecond == 0
            and v.nanosecond == 0
        )
        for v in values
    )


def _load_column(ds: Dataset, name: str) -> _Col:
    frame = ds.frame
    known = [str(c) for c in frame.columns]
    if name not in known:
        raise OperationError(f"unknown column {name!r}; columns are {known}")
    series = frame[name]
    na_values = tuple(ds.options.na_values)
    if pd.api.types.is_bool_dtype(series.dtype):
        return _Col(name, "boolean", [None if pd.isna(v) else bool(v) for v in series.tolist()])
    if pd.api.types.is_integer_dtype(series.dtype):
        return _Col(name, "integer", [None if pd.isna(v) else int(v) for v in series.tolist()])
    if pd.api.types.is_float_dtype(series.dtype):
        return _Col(name, "decimal", [None if pd.isna(v) else float(v) for v in series.tolist()])
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        values = [None if pd.isna(v) else v for v in series.tolist()]
        return _Col(name, "date" if _is_midnight(values) else "datetime", values)
    raw = series.tolist()
    info = infer_column(raw, na_values)
    if info.type in ("integer", "decimal", "boolean", "date", "datetime"):
        try:
            typed = to_typed(series, info.type, na_values)
        except (ValueError, TypeError, OverflowError) as exc:
            raise OperationError(
                f"column {name!r} could not be read as {info.type}: {exc}"
            ) from exc
        converted = _load_column(Dataset(frame=typed.to_frame(name), source=ds.source), name)
        return _Col(name, info.type, converted.values)
    return _Col(name, info.type, [None if is_null(v, na_values) else v for v in raw])


def _need_numeric(col: _Col, function: str) -> None:
    if col.kind not in (*_NUMERIC, "empty"):
        raise OperationError(
            f"column {col.name!r} is {col.kind}; {function!r} needs numbers and text is never "
            "coerced (type the column first, for example clean --parse-number)"
        )


def _fmt(value: Any, kind: str) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if kind == "date" else value.isoformat()
    return value


def _tag(value: Any) -> str:
    if isinstance(value, bool):
        return "b"
    if isinstance(value, int | float):
        return "n"
    if isinstance(value, pd.Timestamp):
        return "d"
    return "s"


def _gkey(value: Any) -> Any:
    return ("null",) if value is None else (_tag(value), value)


def _sortkey(value: Any) -> tuple[Any, ...]:
    """Total order: booleans, numbers, timestamps, then text in code-point order; null last."""
    if value is None:
        return (9, 0)
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, int | float):
        return (1, value)
    if isinstance(value, pd.Timestamp):
        return (2, int(value.value))
    return (3, str(value))


def _label(value: Any, kind: str) -> Any:
    return NULL_LABEL if value is None else _fmt(value, kind)


# --- aggregation ---------------------------------------------------------------------------------


def _aggregate(function: str, values: list[Any], col: _Col) -> tuple[Any, str | None]:
    """One metric over the non-null ``values`` of a group. Returns ``(value, warning)``."""
    if function == "count":
        return len(values), None
    if function == "distinct":
        return len({_gkey(v) for v in values}), None
    if not values:
        return None, f"{function} of {col.name!r}: no non-null values, result is null"
    if function in ("min", "max"):
        if col.kind == "mixed":
            raise OperationError(f"column {col.name!r} has mixed types; {function} is not defined")
        pick = min if function == "min" else max
        return _fmt(pick(values, key=_sortkey), col.kind), None
    _need_numeric(col, function)
    n = len(values)
    if function == "sum":
        if col.kind == "integer":
            total = sum(values)
            if not _INT64_MIN <= total <= _INT64_MAX:
                raise OperationError(f"sum of {col.name!r} overflows 64-bit integers")
            return total, None
        try:
            return math.fsum(values), None
        except OverflowError:
            return None, f"sum of {col.name!r} overflows; result is null"
    if function == "mean":
        if col.kind == "integer":
            return sum(values) / n, None
        try:
            return math.fsum(values) / n, None
        except OverflowError:
            return None, f"mean of {col.name!r} overflows; result is null"
    if function == "median":
        ordered = sorted(values)
        mid = n // 2
        if n % 2:
            return float(ordered[mid]) if col.kind == "decimal" else ordered[mid], None
        return (ordered[mid - 1] + ordered[mid]) / 2, None
    raise OperationError(f"unknown function {function!r}")


def _table(columns: list[str], rows: list[list[Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns, dtype=object)


def _check_unique(names: list[str], what: str) -> None:
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise OperationError(f"{what}: repeated {repeated}")


def _collision_warning(cols: list[_Col]) -> list[str]:
    out = []
    for col in cols:
        hit = [label for label in (NULL_LABEL, OTHER_LABEL) if label in col.values]
        if hit:
            out.append(
                f"column {col.name!r} contains the text {hit}, which is also used as a label"
            )
    return out


# --- percentiles and correlation -----------------------------------------------------------------


def _percentile(ordered: list[float], q: float) -> float:
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _std(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = math.fsum(values) / n
    return math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (n - 1))


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        average = (start + end) / 2 + 1
        for position in range(start, end + 1):
            ranks[order[position]] = average
        start = end + 1
    return ranks


# --- operations ----------------------------------------------------------------------------------


@dataclass
class _Run:
    ds: Dataset
    cache: dict[str, _Col]
    warnings: list[str]
    excluded: dict[str, int]
    rows_used: int
    extra: dict[str, Any]

    def col(self, name: str) -> _Col:
        if name not in self.cache:
            self.cache[name] = _load_column(self.ds, name)
        return self.cache[name]

    def warn(self, message: str | None) -> None:
        if message and message not in self.warnings:
            self.warnings.append(message)


def _op_describe(spec: Describe, run: _Run) -> pd.DataFrame:
    all_names = [str(c) for c in run.ds.frame.columns]
    wanted = spec.columns if spec.columns is not None else all_names
    _check_unique(list(wanted), "describe columns")
    for name in wanted:
        run.col(name)  # validates the name
    names = [n for n in all_names if n in set(wanted)]
    columns = [
        "column", "type", "count", "nulls", "distinct", "min", "max", "mean", "median",
        "std", "p25", "p75", "min_length", "max_length", "mean_length",
    ]  # fmt: skip
    rows: list[list[Any]] = []
    for name in names:
        col = run.col(name)
        present = [v for v in col.values if v is not None]
        row: dict[str, Any] = dict.fromkeys(columns)
        row.update(
            column=name,
            type=col.kind,
            count=len(present),
            nulls=len(col.values) - len(present),
            distinct=len({_gkey(v) for v in present}),
        )
        if present and col.kind != "mixed":
            ordered = sorted(present, key=_sortkey)
            row["min"], row["max"] = _fmt(ordered[0], col.kind), _fmt(ordered[-1], col.kind)
        if present and col.kind in _NUMERIC:
            floats = sorted(float(v) for v in present)
            row["mean"], _ = _aggregate("mean", present, col)
            row["median"], _ = _aggregate("median", present, col)
            row["std"] = _std(floats)
            row["p25"], row["p75"] = _percentile(floats, 0.25), _percentile(floats, 0.75)
            if len(present) < 2:
                run.warn(f"std of {name!r}: fewer than 2 values, result is null")
        if present and col.kind == "text":
            lengths = [len(str(v)) for v in present]
            row["min_length"], row["max_length"] = min(lengths), max(lengths)
            row["mean_length"] = sum(lengths) / len(lengths)
        rows.append([row[c] for c in columns])
    run.rows_used = len(run.ds.frame)
    run.excluded = {}
    run.extra.update(
        percentile_method="linear interpolation", std_ddof=1, order="file column order"
    )
    return _table(columns, rows)


def _group(run: _Run, by: list[str]) -> tuple[list[_Col], dict[Any, list[int]]]:
    _check_unique(by, "by")
    key_cols = [run.col(name) for name in by]
    groups: dict[Any, list[int]] = {}
    for position in range(len(run.ds.frame)):
        key = tuple(_gkey(col.values[position]) for col in key_cols)
        groups.setdefault(key, []).append(position)
    return key_cols, groups


def _metric_values(
    run: _Run, label: str, positions: list[int], *, warn: bool = True
) -> tuple[Any, _Col]:
    column, function = parse_metric(label)
    col = run.col(column)
    values = [col.values[p] for p in positions if col.values[p] is not None]
    value, warning = _aggregate(function, values, col)
    if warn:
        run.warn(warning)
    return value, col


def _note_metric_nulls(run: _Run, labels: list[str]) -> None:
    for label in labels:
        column, _ = parse_metric(label)
        nulls = sum(1 for v in run.col(column).values if v is None)
        if nulls:
            run.excluded[column] = nulls


def _grouped_rows(
    run: _Run, by: list[str], labels: list[str]
) -> tuple[list[str], list[list[Any]], list[tuple[Any, ...]]]:
    key_cols, groups = _group(run, by)
    columns = [*by, "rows", *labels]
    _check_unique(columns, "output columns")
    for label in labels:
        _metric_values(
            run, label, [], warn=False
        )  # validates only; an empty probe is not a finding
    rows: list[list[Any]] = []
    sort_keys: list[tuple[Any, ...]] = []
    for positions in groups.values():
        first = positions[0]
        keys = [col.values[first] for col in key_cols]
        row: list[Any] = [_label(k, c.kind) for k, c in zip(keys, key_cols, strict=True)]
        row.append(len(positions))
        for label in labels:
            value, _ = _metric_values(run, label, positions)
            row.append(value)
        rows.append(row)
        sort_keys.append(tuple(_sortkey(k) for k in keys))
    run.warnings.extend(w for w in _collision_warning(key_cols) if w not in run.warnings)
    run.rows_used = len(run.ds.frame)
    _note_metric_nulls(run, labels)
    return columns, rows, sort_keys


def _metric_order(value: Any, descending: bool) -> tuple[Any, ...]:
    """Null last in either direction; numbers by value (negated when descending)."""
    if value is None:
        return (1, 0)
    key = _sortkey(value)
    if descending and key[0] == 1:
        return (0, -key[1])
    return (0, key)


def _sorted_by_metric(
    rows: list[list[Any]], keys: list[tuple[Any, ...]], index: int, descending: bool
) -> list[int]:
    def order(i: int) -> tuple[Any, ...]:
        value = rows[i][index]
        if descending and isinstance(value, str):
            # text metrics (min/max of text) descending: invert code points
            return (0, tuple(-ord(ch) for ch in value) + (1,), keys[i])
        return (*_metric_order(value, descending), keys[i])

    return sorted(range(len(rows)), key=order)


def _op_group_aggregate(spec: GroupAggregate, run: _Run) -> pd.DataFrame:
    _check_unique(list(spec.metrics), "metrics")
    columns, rows, keys = _grouped_rows(run, list(spec.by), list(spec.metrics))
    if spec.sort is not None:
        if spec.sort not in spec.metrics:
            raise OperationError(
                f"sort {spec.sort!r} must be one of the metrics {list(spec.metrics)}"
            )
        index = columns.index(spec.sort)
        order = _sorted_by_metric(rows, keys, index, True)
        run.extra["order"] = f"{spec.sort} descending, ties by group keys ascending (null last)"
    else:
        order = sorted(range(len(rows)), key=lambda i: keys[i])
        run.extra["order"] = (
            "group keys ascending (booleans, numbers, dates, text by code point; null last)"
        )
    return _table(columns, [rows[i] for i in order])


def _op_top_n(spec: TopN, run: _Run) -> pd.DataFrame:
    columns, rows, keys = _grouped_rows(run, [spec.by], [spec.metric])
    descending = spec.order == "desc"
    order = _sorted_by_metric(rows, keys, columns.index(spec.metric), descending)
    metric_at = columns.index(spec.metric)
    ranked: list[list[Any]] = []
    rank = 0
    previous: Any = object()
    for position, i in enumerate(order, start=1):
        value = rows[i][metric_at]
        if position == 1 or value != previous:
            rank = position
        previous = value
        ranked.append([rank, *rows[i]])
    cutoff = ranked[: spec.n]
    tied = False
    if len(ranked) > spec.n and cutoff:
        edge = cutoff[-1][1 + metric_at]
        extra_rows = [r for r in ranked[spec.n :] if r[1 + metric_at] == edge]
        if extra_rows:
            tied = True
            cutoff = cutoff + extra_rows
            run.warn(
                f"groups tie with the value at the cutoff, so {len(cutoff)} rows are returned instead of {spec.n}"
            )
    run.extra.update(
        tied_at_cutoff=tied,
        rows_returned=len(cutoff),
        order=f"{spec.metric} {spec.order}, ties by group key ascending; equal values share a rank (minimum method)",
    )
    return _table(["rank", *columns], cutoff)


def _period_index(value: date, freq: str) -> int:
    if freq == "day":
        return value.toordinal()
    if freq == "month":
        return value.year * 12 + value.month - 1
    if freq == "quarter":
        return value.year * 4 + (value.month - 1) // 3
    return value.year


def _period_label(index: int, freq: str) -> str:
    if freq == "day":
        return date.fromordinal(index).isoformat()
    if freq == "month":
        return f"{index // 12:04d}-{index % 12 + 1:02d}"
    if freq == "quarter":
        return f"{index // 4:04d}-Q{index % 4 + 1}"
    return f"{index:04d}"


def _parse_bound(text: str | None, name: str) -> date | None:
    if text is None:
        return None
    if not _ISO_DATE.match(text):
        raise OperationError(f"{name} must be an ISO date YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise OperationError(f"{name} is not a valid calendar date: {text!r}") from exc


def _op_time_trend(spec: TimeTrend, run: _Run) -> pd.DataFrame:
    when = run.col(spec.date_column)
    if when.kind not in ("date", "datetime"):
        raise OperationError(
            f"column {when.name!r} is {when.kind}; time_trend needs real dates (ISO 8601, or typed with "
            "clean --parse-date). Ambiguous text is never guessed"
        )
    start, end = _parse_bound(spec.start, "start"), _parse_bound(spec.end, "end")
    if start and end and start > end:
        raise OperationError("start is after end")
    metric_col = None
    function = ""
    if spec.metric != "rows":
        column, function = parse_metric(spec.metric)
        metric_col = run.col(column)
        _aggregate(function, [], metric_col)  # validate before work
    buckets: dict[int, list[int]] = {}
    null_dates = 0
    for position, stamp in enumerate(when.values):
        if stamp is None:
            null_dates += 1
            continue
        day = stamp.date()
        if (start and day < start) or (end and day > end):
            continue
        buckets.setdefault(_period_index(day, spec.freq), []).append(position)
    if null_dates:
        run.excluded[when.name] = null_dates
    run.rows_used = sum(len(p) for p in buckets.values())
    if start or end:
        first = _period_index(start, spec.freq) if start else (min(buckets) if buckets else None)
        last = _period_index(end, spec.freq) if end else (max(buckets) if buckets else None)
    else:
        first, last = (min(buckets), max(buckets)) if buckets else (None, None)
    columns = ["period", "rows"] + ([spec.metric] if metric_col else [])
    if first is None or last is None:
        return _table(columns, [])
    if last - first + 1 > MAX_PERIODS:
        raise OperationError(
            f"{last - first + 1} periods exceed the limit of {MAX_PERIODS}; use a coarser freq"
        )
    rows: list[list[Any]] = []
    metric_nulls = 0
    for index in range(first, last + 1):
        positions = buckets.get(index, [])
        row: list[Any] = [_period_label(index, spec.freq), len(positions)]
        if metric_col is not None:
            values = [metric_col.values[p] for p in positions if metric_col.values[p] is not None]
            metric_nulls += len(positions) - len(values)
            value, warning = _aggregate(function, values, metric_col) if positions else (None, None)
            run.warn(warning)
            row.append(value)
        rows.append(row)
    if metric_col is not None and metric_nulls:
        run.excluded[metric_col.name] = metric_nulls
    run.extra["order"] = (
        "period ascending; periods without rows are listed with rows = 0 and a null metric"
    )
    return _table(columns, rows)


def _op_correlation(spec: Correlation, run: _Run) -> pd.DataFrame:
    _check_unique(list(spec.columns), "columns")
    all_names = [str(c) for c in run.ds.frame.columns]
    for name in spec.columns:
        _need_numeric(run.col(name), "correlation")
    names = [n for n in all_names if n in set(spec.columns)]
    cols = [run.col(n) for n in names]
    rows: list[list[Any]] = []
    for i, a in enumerate(cols):
        for b in cols[i + 1 :]:
            pairs = [
                (float(x), float(y))
                for x, y in zip(a.values, b.values, strict=True)
                if x is not None and y is not None
            ]
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            coefficient: float | None = None
            if len(pairs) < 3:
                run.warn(
                    f"{a.name!r} and {b.name!r}: fewer than 3 usable pairs, coefficient is null"
                )
            else:
                if spec.method == "spearman":
                    xs, ys = _ranks(xs), _ranks(ys)
                coefficient = _pearson(xs, ys)
                if coefficient is None:
                    run.warn(
                        f"{a.name!r} and {b.name!r}: a column is constant, coefficient is null"
                    )
            rows.append([a.name, b.name, coefficient, len(pairs)])
    for col in cols:
        nulls = sum(1 for v in col.values if v is None)
        if nulls:
            run.excluded[col.name] = nulls
    run.rows_used = sum(
        1 for p in range(len(run.ds.frame)) if sum(c.values[p] is not None for c in cols) >= 2
    )
    run.extra.update(
        method=spec.method,
        order="upper triangle in file column order",
        rows_used_definition="rows with at least two non-null selected values; each pair uses n_used",
    )
    return _table(["a", "b", "coefficient", "n_used"], rows)


def _op_value_counts(spec: ValueCounts, run: _Run) -> pd.DataFrame:
    col = run.col(spec.column)
    counts: dict[Any, list[Any]] = {}
    for value in col.values:
        slot = counts.setdefault(_gkey(value), [value, 0])
        slot[1] += 1
    ordered = sorted(counts.values(), key=lambda item: (-item[1], _sortkey(item[0])))
    total = len(col.values)
    shown = ordered[: spec.top]
    rest = ordered[spec.top :]
    rows = [[_label(v, col.kind), c] for v, c in shown]
    if rest:
        rows.append([OTHER_LABEL, sum(c for _, c in rest)])
    columns = ["value", "count"]
    if spec.normalize:
        columns.append("share")
        rows = [[*r, (r[1] / total) if total else None] for r in rows]
    run.warnings.extend(w for w in _collision_warning([col]) if w not in run.warnings)
    run.rows_used = total
    run.extra.update(
        distinct_values=len(ordered),
        order="count descending, then value (numbers, dates, text by code point; null last)",
    )
    return _table(columns, rows)


_DISPATCH = {
    "describe": _op_describe,
    "group_aggregate": _op_group_aggregate,
    "top_n": _op_top_n,
    "time_trend": _op_time_trend,
    "correlation": _op_correlation,
    "value_counts": _op_value_counts,
}


def run_operation(
    ds: Dataset, spec: object, *, cleaning: CleanResult | None = None
) -> OperationResult:
    """Run one operation. ``spec`` is a spec model or a dict (validated with ``parse_spec``).
    ``cleaning`` only feeds the provenance counts. Raises ``OperationError`` for invalid requests."""
    if isinstance(spec, dict):
        spec = parse_spec(spec)
    handler = _DISPATCH.get(getattr(spec, "op", ""))
    if handler is None:
        raise OperationError(f"not an operation spec: {type(spec).__name__}")
    run = _Run(ds=ds, cache={}, warnings=[], excluded={}, rows_used=0, extra={})
    table = handler(spec, run)  # type: ignore[operator, arg-type]
    if run.rows_used == 0:
        run.warn("no usable rows: the table is empty or every row was excluded")
    provenance = Provenance(
        operation=spec.op,  # type: ignore[attr-defined]
        parameters=spec.model_dump(mode="json"),  # type: ignore[attr-defined]
        input_sha256=ds.source.sha256,
        rows_in=len(ds.frame),
        rows_used=run.rows_used,
        rows_excluded_null=dict(sorted(run.excluded.items())),
        cleaning=(
            {
                "rows_in": cleaning.rows_in,
                "rows_rejected": len(cleaning.rejects),
                "rows_duplicate": len(cleaning.duplicates),
            }
            if cleaning
            else {}
        ),
        result_sha256=table_sha256(table),
    )
    run.extra["input_table_sha256"] = table_sha256(ds.frame)
    return OperationResult(
        table=table,
        provenance=provenance,
        warnings=run.warnings,
        limitations=list(STANDARD_LIMITATIONS),
        extra=run.extra,
    )


__all__ = ["run_operation", "parse_spec"]
