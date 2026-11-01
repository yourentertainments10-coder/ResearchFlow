"""Choose the starter analyses from a profile. Fixed rules, so the same profile gives the same plan.

The plan holds ``OperationSpec`` objects only; every number is computed later by ``sie.datasets``.
Rules (documented in docs/CSV_JOURNEY_DESIGN.md):
* describe: always, as the first analysis.
* numeric column: integer or decimal, not constant, and not identifier-like (an integer column whose
  values are all distinct, or 90% distinct with an id-like name such as id, index, key, code; or a
  profile hint saying so).
* category column: text with 2 to 50 distinct values and at most half as many distinct values as rows.
  Boolean columns are not planned automatically.
* date column: type date or datetime.
At most ``MAX_ANALYSES`` analyses; at most 2 category columns and 2 numeric columns are used.
"""

from __future__ import annotations

from datetime import date

from sie.datasets.models import (
    Correlation,
    Describe,
    ProfileResult,
    TimeTrend,
    TopN,
    ValueCounts,
)
from sie.workspace.models import PlannedAnalysis

MAX_ANALYSES = 8
MAX_CATEGORIES = 2
MAX_NUMERIC = 2
MAX_CORRELATION_COLUMNS = 6
TOP_N = 10
CATEGORY_MAX_DISTINCT = 50


_ID_WORDS = {"id", "index", "key", "no", "number", "code", "uuid", "idx"}


def _id_name(name: str) -> bool:
    words = "".join(ch if ch.isalnum() else " " for ch in name.lower()).split()
    return bool(set(words) & _ID_WORDS) or name.lower().endswith("id")


def _numeric(profile: ProfileResult) -> list[str]:
    out = []
    for c in profile.column_profiles:
        if c.type not in ("integer", "decimal") or c.name in profile.constant_columns:
            continue
        identifier_like = (
            (c.type == "integer" and c.non_null >= 3 and c.distinct == c.non_null)
            or (c.type == "integer" and _id_name(c.name) and c.distinct >= 0.9 * c.non_null)
            or any("identifier" in h.lower() for h in c.hints)
        )
        if not identifier_like:
            out.append(c.name)
    return out


def _categories(profile: ProfileResult) -> list[str]:
    return [
        c.name
        for c in profile.column_profiles
        if c.type == "text"
        and 2 <= c.distinct <= CATEGORY_MAX_DISTINCT
        and c.distinct * 2 <= c.non_null
    ]


def _dates(profile: ProfileResult) -> list[str]:
    return [c.name for c in profile.column_profiles if c.type in ("date", "datetime")]


def choose_frequency(low: str | None, high: str | None) -> str:
    """Calendar frequency from the span of the date column: up to 62 days -> day, up to about 3 years ->
    month, up to 10 years -> quarter, else year. Unreadable bounds give month."""
    try:
        span = (date.fromisoformat(str(high)[:10]) - date.fromisoformat(str(low)[:10])).days
    except (TypeError, ValueError):
        return "month"
    if span <= 62:
        return "day"
    if span <= 1100:
        return "month"
    if span <= 3650:
        return "quarter"
    return "year"


def plan_analyses(profile: ProfileResult) -> list[PlannedAnalysis]:
    if profile.rows == 0 or profile.columns == 0:
        return []
    by_name = {c.name: c for c in profile.column_profiles}
    numeric = _numeric(profile)[:MAX_NUMERIC]
    categories = _categories(profile)[:MAX_CATEGORIES]
    dates = _dates(profile)
    plan: list[PlannedAnalysis] = [
        PlannedAnalysis("describe", "Column summary", Describe(), "table")
    ]

    def add(title: str, spec: object, chart: str, x: str | None, y: str | None) -> None:
        if len(plan) < MAX_ANALYSES:
            plan.append(PlannedAnalysis(f"a{len(plan)}", title, spec, chart, x, y))  # type: ignore[arg-type]

    for cat in categories:
        add(
            f"Number of rows by {cat}",
            ValueCounts(column=cat, top=15, normalize=True),
            "bar",
            "value",
            "count",
        )
    if numeric:
        for cat in categories:
            metric = f"{numeric[0]}:sum"
            add(
                f"Top {TOP_N} {cat} by sum of {numeric[0]}",
                TopN(by=cat, metric=metric, n=TOP_N),
                "bar",
                cat,
                metric,
            )
    if dates:
        when = by_name[dates[0]]
        freq = choose_frequency(when.min, when.max)
        metric = f"{numeric[0]}:sum" if numeric else "rows"
        what = f"sum of {numeric[0]}" if numeric else "number of rows"
        add(
            f"{what.capitalize()} by {freq} ({dates[0]})",
            TimeTrend(date_column=dates[0], metric=metric, freq=freq),  # type: ignore[arg-type]
            "line",
            "period",
            metric,
        )
    corr_cols = _numeric(profile)[:MAX_CORRELATION_COLUMNS]
    if len(corr_cols) >= 2:
        add(
            "Correlation between numeric columns",
            Correlation(columns=corr_cols),
            "table",
            None,
            None,
        )
    return plan
