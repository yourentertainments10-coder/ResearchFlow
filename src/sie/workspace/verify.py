"""Independent recomputation of every number the workspace shows (docs/CSV_JOURNEY_DESIGN.md, slice 1).

This module does **not** use the calculation code of ``sie.datasets``. It reads the same loaded table and recomputes with
plain pandas, using the documented semantics on purpose:

* null = ``None``, NaN, an empty or blank string, or one of the dataset's ``na_values`` markers;
* text is never turned into a number unless every non-null value parses (a column the profile calls
  numeric); nulls are excluded from a metric and a group with no values has a null metric;
* a null group key is its own group, labelled ``(null)``;
* sums, means and medians are compared with a relative tolerance of 1e-9 (the engine adds floats with
  ``math.fsum``, pandas does not, so the last digits may differ); integers and counts must match exactly.

Anything this module cannot check is ``unverified`` (never silently passed) and is not exported.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from sie.datasets.models import (
    Correlation,
    Dataset,
    Describe,
    GroupAggregate,
    TimeTrend,
    TopN,
    ValueCounts,
    parse_metric,
)
from sie.workspace.models import Verification

NULL_LABEL = "(null)"
OTHER_LABEL = "(other)"
REL_TOL = 1e-9
ABS_TOL = 1e-12
MAX_GROUPS_CHECKED = 5000


class NotVerifiable(Exception):
    """The independent path does not cover this request; the item is marked unverified."""


# --- independent typing ---------------------------------------------------------------------------


def _blank(value: Any, na: tuple[str, ...]) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str):
        text = value.strip()
        return text == "" or text in na
    return bool(value is pd.NA or value is pd.NaT)


def _clean(series: pd.Series, na: tuple[str, ...]) -> pd.Series:
    """Values with every null as ``None`` (a list, so pandas cannot turn None into NaN)."""
    return pd.Series(
        [None if _blank(v, na) else v for v in series.tolist()], index=series.index, dtype=object
    )


def _numbers(series: pd.Series, na: tuple[str, ...]) -> pd.Series:
    cleaned = _clean(series, na)
    try:
        return pd.to_numeric(cleaned.map(lambda v: v.strip() if isinstance(v, str) else v))
    except (ValueError, TypeError) as exc:
        raise NotVerifiable(f"column {series.name!r} is not numeric") from exc


def _dates(series: pd.Series, na: tuple[str, ...]) -> pd.Series:
    cleaned = _clean(series, na)
    try:
        return pd.to_datetime(
            cleaned.map(lambda v: v.strip() if isinstance(v, str) else v), format="ISO8601"
        )
    except (ValueError, TypeError) as exc:
        raise NotVerifiable(f"column {series.name!r} is not an ISO date column") from exc


def _same(a: Any, b: Any) -> bool:
    a_null = a is None or (isinstance(a, float) and math.isnan(a))
    b_null = b is None or (isinstance(b, float) and math.isnan(b))
    if a_null or b_null:
        return a_null and b_null
    if hasattr(a, "item"):
        a = a.item()
    if hasattr(b, "item"):
        b = b.item()
    if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool):
        return a == b
    try:
        return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=ABS_TOL)
    except (TypeError, ValueError):
        return a == b


class _Check:
    def __init__(self) -> None:
        self.checks = 0
        self.mismatches: list[str] = []

    def eq(self, label: str, got: Any, want: Any) -> None:
        self.checks += 1
        if not _same(got, want):
            self.mismatches.append(f"{label}: result {got!r}, independent {want!r}")

    def true(self, label: str, ok: bool) -> None:
        self.checks += 1
        if not ok:
            self.mismatches.append(label)

    def done(self) -> Verification:
        if self.checks == 0:
            return Verification("unverified", 0, [], "nothing could be checked")
        return Verification(
            "verified" if not self.mismatches else "unverified", self.checks, self.mismatches[:20]
        )


# --- grouped metrics ------------------------------------------------------------------------------


def _metric_series(ds: Dataset, label: str, na: tuple[str, ...]) -> tuple[pd.Series, str]:
    column, function = parse_metric(label)
    if column not in ds.frame.columns:
        raise NotVerifiable(f"unknown column {column!r}")
    series = ds.frame[column]
    if function in ("sum", "mean", "median"):
        return _numbers(series, na), function
    return _clean(series, na), function


def _agg(group: Any, function: str) -> pd.Series:
    if function == "sum":
        return group.sum(min_count=1)
    if function == "mean":
        return group.mean()
    if function == "median":
        return group.median()
    if function == "count":
        return group.count()
    if function == "distinct":
        return group.nunique()
    if function in ("min", "max"):
        try:
            return getattr(group, function)()
        except TypeError as exc:
            raise NotVerifiable(f"{function} over mixed values") from exc
    raise NotVerifiable(f"function {function!r}")


def _keys(ds: Dataset, by: list[str], na: tuple[str, ...]) -> list[pd.Series]:
    out = []
    for name in by:
        if name not in ds.frame.columns:
            raise NotVerifiable(f"unknown column {name!r}")
        cleaned = _clean(ds.frame[name], na)
        out.append(
            pd.Series(
                [NULL_LABEL if v is None else v for v in cleaned.tolist()],
                index=cleaned.index,
                dtype=object,
            )
        )
    return out


def _expected_groups(
    ds: Dataset, by: list[str], metrics: list[str], na: tuple[str, ...]
) -> dict[tuple[Any, ...], dict[str, Any]]:
    keys = _keys(ds, by, na)
    frame = pd.DataFrame({f"k{i}": k for i, k in enumerate(keys)})
    kcols = list(frame.columns)
    for m in metrics:
        series, _ = _metric_series(ds, m, na)
        frame[m] = series
    frame["__n"] = 1
    grouped = frame.groupby(kcols, sort=False, dropna=False)
    sizes = grouped["__n"].sum()
    metric_values = {m: _agg(grouped[m], parse_metric(m)[1]) for m in metrics}
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key in sizes.index:
        tup = key if isinstance(key, tuple) else (key,)
        entry: dict[str, Any] = {"rows": int(sizes.loc[key])}
        for m in metrics:
            entry[m] = metric_values[m].loc[key]
        out[tuple(tup)] = entry
    return out


def _group_rows(table: pd.DataFrame, by: list[str]) -> dict[tuple[Any, ...], pd.Series]:
    out = {}
    for _, row in table.iterrows():
        out[tuple(row[b] for b in by)] = row
    return out


def _verify_groups(
    ds: Dataset, table: pd.DataFrame, by: list[str], metrics: list[str], c: _Check
) -> dict[tuple[Any, ...], dict[str, Any]]:
    na = tuple(ds.options.na_values)
    expected = _expected_groups(ds, by, metrics, na)
    got = _group_rows(table, by)
    c.true("a group is listed twice", len(got) == len(table))
    for key, row in got.items():
        want = expected.get(key)
        if want is None:
            c.true(f"group {key!r} does not exist in the data", False)
            continue
        c.eq(f"rows of group {key!r}", row["rows"], want["rows"])
        for m in metrics:
            c.eq(f"{m} of group {key!r}", row[m], want[m])
    return expected


# --- per operation --------------------------------------------------------------------------------


def _group_aggregate(ds: Dataset, spec: GroupAggregate, table: pd.DataFrame) -> Verification:
    c = _Check()
    expected = _verify_groups(ds, table, list(spec.by), list(spec.metrics), c)
    c.eq("number of groups", len(table), len(expected))
    return c.done()


def _top_n(ds: Dataset, spec: TopN, table: pd.DataFrame) -> Verification:
    c = _Check()
    expected = _verify_groups(ds, table, [spec.by], [spec.metric], c)
    desc = spec.order == "desc"

    def better(a: Any, b: Any) -> bool:  # a ranks strictly ahead of b; null ranks last
        a_null = a is None or (isinstance(a, float) and math.isnan(a))
        b_null = b is None or (isinstance(b, float) and math.isnan(b))
        if a_null or b_null:
            return (not a_null) and b_null
        return float(a) > float(b) if desc else float(a) < float(b)

    if len(expected) > MAX_GROUPS_CHECKED:
        raise NotVerifiable(f"more than {MAX_GROUPS_CHECKED} groups; ranking not re-checked")
    values = {k: v[spec.metric] for k, v in expected.items()}
    # the groups that must be listed: the first n, plus any tied with the n-th
    ordered = sorted(values.items(), key=lambda kv: sum(better(o, kv[1]) for o in values.values()))
    if ordered:
        edge = ordered[min(spec.n, len(ordered)) - 1][1]
        must = {
            k
            for k, v in ordered
            if sum(better(o, v) for o in values.values()) < spec.n or _same(v, edge)
        }
        c.true(
            "a top group is missing or an extra group is listed",
            {(r,) for r in table[spec.by]} == must,
        )
    for _, row in table.iterrows():
        key = (row[spec.by],)
        if key in values:
            rank = 1 + sum(better(o, values[key]) for o in values.values())
            c.eq(f"rank of {key[0]!r}", row["rank"], rank)
    return c.done()


def _value_counts(ds: Dataset, spec: ValueCounts, table: pd.DataFrame) -> Verification:
    c = _Check()
    na = tuple(ds.options.na_values)
    if spec.column not in ds.frame.columns:
        raise NotVerifiable(f"unknown column {spec.column!r}")
    cleaned = _clean(ds.frame[spec.column], na)
    if any(v is not None and not isinstance(v, str) for v in cleaned.tolist()):
        raise NotVerifiable("value_counts check covers text columns only")
    labelled = pd.Series([NULL_LABEL if v is None else v for v in cleaned.tolist()], dtype=object)
    counts = labelled.value_counts(dropna=False)
    total = int(len(cleaned))
    shown = table[table["value"] != OTHER_LABEL]
    other = table[table["value"] == OTHER_LABEL]
    for _, row in shown.iterrows():
        c.eq(f"count of {row['value']!r}", row["count"], int(counts.get(row["value"], -1)))
    c.eq("total of all counts", int(table["count"].sum()), total)
    if "share" in table.columns:
        for _, row in table.iterrows():
            c.eq(
                f"share of {row['value']!r}",
                row["share"],
                (int(row["count"]) / total) if total else None,
            )
    shown_labels = set(shown["value"])
    rest = [int(n) for label, n in counts.items() if label not in shown_labels]
    if len(other):
        c.eq("count of (other)", other.iloc[0]["count"], sum(rest))
        c.true(
            "a larger value is hidden in (other)",
            not rest or int(shown["count"].min()) >= max(rest),
        )
        c.eq("number of shown values", len(shown), spec.top)
    else:
        c.true("a value is missing from the table", not rest)
    return c.done()


_FREQ = {"day": "D", "month": "M", "quarter": "Q", "year": "Y"}


def _label(period: pd.Period, freq: str) -> str:
    if freq == "quarter":
        return f"{period.year:04d}-Q{period.quarter}"
    return str(period)


def _time_trend(ds: Dataset, spec: TimeTrend, table: pd.DataFrame) -> Verification:
    c = _Check()
    na = tuple(ds.options.na_values)
    if spec.date_column not in ds.frame.columns:
        raise NotVerifiable(f"unknown column {spec.date_column!r}")
    stamps = _dates(ds.frame[spec.date_column], na)
    frame = pd.DataFrame({"t": stamps})
    if spec.metric != "rows":
        series, function = _metric_series(ds, spec.metric, na)
        frame["m"] = series
    frame = frame[frame["t"].notna()]
    if spec.start:
        frame = frame[frame["t"].dt.date >= pd.Timestamp(spec.start).date()]
    if spec.end:
        frame = frame[frame["t"].dt.date <= pd.Timestamp(spec.end).date()]
    frame = frame.assign(p=frame["t"].dt.to_period(_FREQ[spec.freq]))
    if frame.empty:
        c.eq("rows in table", len(table), 0)
        return c.done()
    first = pd.Period(spec.start, freq=_FREQ[spec.freq]) if spec.start else frame["p"].min()
    last = pd.Period(spec.end, freq=_FREQ[spec.freq]) if spec.end else frame["p"].max()
    periods = pd.period_range(first, last, freq=_FREQ[spec.freq])
    c.eq("number of periods", len(table), len(periods))
    by_period = frame.groupby("p", sort=False)
    sizes = by_period.size()
    metric = _agg(by_period["m"], function) if spec.metric != "rows" else None
    for i, period in enumerate(periods):
        if i >= len(table):
            break
        row = table.iloc[i]
        c.true(f"label of period {i + 1}", row["period"] == _label(period, spec.freq))
        n = int(sizes.get(period, 0))
        c.eq(f"rows in {row['period']}", row["rows"], n)
        if metric is not None:
            c.eq(
                f"{spec.metric} in {row['period']}",
                row[spec.metric],
                metric.get(period) if n else None,
            )
    return c.done()


def _correlation(ds: Dataset, spec: Correlation, table: pd.DataFrame) -> Verification:
    c = _Check()
    na = tuple(ds.options.na_values)
    names = [n for n in ds.frame.columns if n in set(spec.columns)]
    data = pd.DataFrame({n: _numbers(ds.frame[n], na) for n in names})
    expected = {}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            both = data[[a, b]].dropna()
            n = len(both)
            r = None
            if n >= 3:
                r = both[a].corr(both[b], method=spec.method)
                if r is None or (isinstance(r, float) and math.isnan(r)):
                    r = None
            expected[(a, b)] = (r, n)
    c.eq("number of pairs", len(table), len(expected))
    for _, row in table.iterrows():
        want = expected.get((row["a"], row["b"]))
        if want is None:
            c.true(f"unknown pair {row['a']!r}, {row['b']!r}", False)
            continue
        c.eq(f"n_used of {row['a']}/{row['b']}", row["n_used"], want[1])
        if want[0] is None or row["coefficient"] is None:
            c.eq(f"coefficient of {row['a']}/{row['b']}", row["coefficient"], want[0])
        else:
            c.true(
                f"coefficient of {row['a']}/{row['b']}: {row['coefficient']!r} vs {want[0]!r}",
                abs(float(row["coefficient"]) - float(want[0])) <= 1e-9,
            )
    return c.done()


def _describe(ds: Dataset, spec: Describe, table: pd.DataFrame) -> Verification:
    c = _Check()
    na = tuple(ds.options.na_values)
    for _, row in table.iterrows():
        name = row["column"]
        cleaned = _clean(ds.frame[name], na)
        present = int(cleaned.notna().sum())
        c.eq(f"count of {name}", row["count"], present)
        c.eq(f"nulls of {name}", row["nulls"], len(cleaned) - present)
        if row["type"] in ("integer", "decimal"):
            nums = _numbers(ds.frame[name], na).dropna()
            if len(nums):
                c.eq(f"mean of {name}", row["mean"], nums.mean())
                c.eq(f"median of {name}", row["median"], nums.median())
                c.eq(f"min of {name}", row["min"], nums.min())
                c.eq(f"max of {name}", row["max"], nums.max())
    return c.done()


_CHECKS = {
    "describe": _describe,
    "group_aggregate": _group_aggregate,
    "top_n": _top_n,
    "time_trend": _time_trend,
    "correlation": _correlation,
    "value_counts": _value_counts,
}


def verify_result(ds: Dataset, spec: Any, table: pd.DataFrame) -> Verification:
    """Recompute ``table`` independently from ``ds`` and compare. Never raises for data problems."""
    check = _CHECKS.get(getattr(spec, "op", ""))
    if check is None:
        return Verification(
            "unverified", 0, [], f"no independent check for {getattr(spec, 'op', '?')}"
        )
    try:
        return check(ds, spec, table)  # type: ignore[operator]
    except NotVerifiable as exc:
        return Verification("unverified", 0, [], str(exc))
    except Exception as exc:  # noqa: BLE001 - a crash in the checker must never pass a result
        return Verification(
            "unverified", 0, [], f"independent check failed to run: {type(exc).__name__}"
        )


def verify_profile_figures(ds: Dataset, figures: dict[str, Any]) -> Verification:
    """Re-count rows, columns, blank cells and duplicate rows without ``sie.datasets.profile``."""
    c = _Check()
    na = tuple(ds.options.na_values)
    frame = ds.frame
    c.eq("rows", figures["rows"], len(frame))
    c.eq("columns", figures["columns"], frame.shape[1])
    blanks = int(sum(_clean(frame[col], na).isna().sum() for col in frame.columns))
    c.eq("missing cells", figures["missing_cells"], blanks)
    cells = len(frame) * frame.shape[1]
    c.eq("missing share", figures["missing_share"], (blanks / cells * 100) if cells else 0.0)
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    cleaned = [_clean(frame[col], na).tolist() for col in frame.columns]
    for position in range(len(frame)):
        key = tuple(
            "\x00null" if col[position] is None else (type(col[position]).__name__, col[position])
            for col in cleaned
        )
        duplicates += key in seen
        seen.add(key)
    c.eq("duplicate rows", figures["duplicate_rows"], duplicates)
    return c.done()
