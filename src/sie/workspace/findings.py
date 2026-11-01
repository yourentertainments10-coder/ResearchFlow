"""Plain-language findings. Rule based; no model. Every number is a figure (``figs``), so a finding
template holds no digits of its own. Correlation is described as association only."""

from __future__ import annotations

import math
from typing import Any

from sie.datasets.models import ProfileResult
from sie.workspace import figs
from sie.workspace.figs import Figure
from sie.workspace.models import AnalysisItem, Finding

MAX_PER_KIND = 3
STRENGTH = ((0.7, "strong"), (0.3, "moderate"), (0.0, "weak"))  # |r| thresholds, documented


def pretty_metric(metric: str) -> str:
    """``revenue:sum`` -> ``sum of revenue``; ``rows`` -> ``number of rows``."""
    if metric == "rows":
        return "number of rows"
    column, _, function = metric.rpartition(":")
    return f"{function} of {column}"


class _Builder:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(
        self,
        kind: str,
        severity: str,
        template: str,
        source: str,
        figures: list[Figure],
        values: dict[str, str] | None = None,
    ) -> None:
        by_id = {f.id: f for f in figures}
        vals = dict(values or {})
        fid = f"finding_{len(self.items) + 1}"
        self.items.append(
            Finding(
                id=fid,
                kind=kind,  # type: ignore[arg-type]
                severity=severity,  # type: ignore[arg-type]
                template=template,
                text=figs.render_template(template, by_id, vals),
                source=source,
                figures=[f.model_dump(mode="json") for f in figures],
                values=vals,
            )
        )


def quality_findings(profile: ProfileResult, file_sha256: str, b: _Builder) -> None:
    def ff(fid: str, value: Any, label: str, decimals: int = 0, unit: str = "") -> Figure:
        return figs.file_figure(
            fid, value, label=label, file_sha256=file_sha256, decimals=decimals, unit=unit
        )

    if profile.rows == 0:
        b.add(
            "quality",
            "warning",
            "The file has a header but no data rows, so nothing can be analysed.",
            "profile",
            [],
        )
        return
    if profile.duplicate_rows:
        b.add(
            "quality", "warning",
            "{F:dups} rows repeat an earlier row exactly. They were not removed.", "profile",
            [ff("dups", profile.duplicate_rows, "duplicate rows")],
        )  # fmt: skip
    gaps = sorted(
        (c for c in profile.column_profiles if c.nulls and c.type != "empty"),
        key=lambda c: (-c.nulls, c.name),
    )
    for c in gaps[:MAX_PER_KIND]:
        b.add(
            "quality", "warning" if c.null_share >= 0.1 else "info",
            "Column {V:col} has no value in {F:n} rows ({F:p} of all rows). Those rows are left out of calculations on it.",
            "profile",
            [ff("n", c.nulls, f"missing in {c.name}"), ff("p", c.null_share * 100, f"missing share of {c.name}", 1, "%")],
            {"col": c.name},
        )  # fmt: skip
    for name in profile.empty_columns[:MAX_PER_KIND]:
        b.add("quality", "warning", "Column {V:col} is empty.", "profile", [], {"col": name})
    for name in profile.constant_columns[:MAX_PER_KIND]:
        b.add(
            "quality",
            "info",
            "Column {V:col} has the same value in every row.",
            "profile",
            [],
            {"col": name},
        )
    for c in [c for c in profile.column_profiles if c.type == "mixed"][:MAX_PER_KIND]:
        b.add(
            "quality", "warning",
            "Column {V:col} mixes value types, so it is not used in calculations. {V:hint}", "profile", [],
            {"col": c.name, "hint": next((h for h in c.hints if h.startswith("mixed")), "")},
        )  # fmt: skip
    shown = 0
    for c in profile.column_profiles:
        for hint in c.hints:
            if hint.startswith("mixed types") or shown >= 5:
                continue
            b.add(
                "quality",
                "warning",
                "Column {V:col}: {V:hint}",
                "profile",
                [],
                {"col": c.name, "hint": hint},
            )
            shown += 1
    for note in profile.notes[:MAX_PER_KIND]:
        count = note.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            b.add(
                "quality", "info", "Loader note {V:code}: {F:n} occurrences.", "profile",
                [ff("n", count, "loader note count")], {"code": str(note.get("code", ""))},
            )  # fmt: skip


def _value_counts(item: AnalysisItem, b: _Builder) -> None:
    table = item.result.table
    shown = table[table["value"] != "(other)"]
    if shown.empty:
        return
    top = shown.iloc[0]
    col = item.planned.spec.column
    figures = [
        figs.operation_figure(
            "n", top["count"], label=f"rows with the most common {col}", result=item.result
        ),
    ]
    template = "The most common {V:col} is {V:val}, in {F:n} rows"
    if "share" in table.columns and top["share"] is not None:
        figures.append(
            figs.operation_figure(
                "p",
                top["share"] * 100,
                label="share of rows",
                result=item.result,
                decimals=1,
                unit="%",
            )
        )
        template += " ({F:p} of all rows)"
    b.add(
        "result",
        "info",
        template + ".",
        item.planned.id,
        figures,
        {"col": col, "val": str(top["value"])},
    )


def _top_n(item: AnalysisItem, b: _Builder) -> None:
    table, spec = item.result.table, item.planned.spec
    if table.empty or table.iloc[0][spec.metric] is None:
        return
    top = table.iloc[0]
    value = top[spec.metric]
    fig = figs.operation_figure(
        "v", value, label=spec.metric, result=item.result, decimals=figs.decimals_for(value)
    )
    b.add(
        "result", "info", "The highest {V:by} is {V:key}, with {V:metric} of {F:v}.", item.planned.id, [fig],
        {"by": spec.by, "key": str(top[spec.by]), "metric": pretty_metric(spec.metric)},
    )  # fmt: skip
    if item.result.extra.get("tied_at_cutoff"):
        b.add(
            "result",
            "warning",
            "Groups tie at the cutoff, so more groups than requested are listed.",
            item.planned.id,
            [],
        )


def _time_trend(item: AnalysisItem, b: _Builder) -> None:
    table, spec = item.result.table, item.planned.spec
    metric = spec.metric if spec.metric != "rows" else "rows"
    live = table[table[metric].notna()] if len(table) else table
    if len(live) >= 2:
        first, last = live.iloc[0], live.iloc[-1]
        figures = [
            figs.operation_figure(
                "a",
                first[metric],
                label=f"{metric} in {first['period']}",
                result=item.result,
                decimals=figs.decimals_for(first[metric]),
            ),
            figs.operation_figure(
                "z",
                last[metric],
                label=f"{metric} in {last['period']}",
                result=item.result,
                decimals=figs.decimals_for(last[metric]),
            ),
        ]
        b.add(
            "result", "info", "The {V:metric} was {F:a} in {V:p1} and {F:z} in {V:p2}. This describes the values, not a cause.",
            item.planned.id, figures, {"metric": pretty_metric(metric), "p1": str(first["period"]), "p2": str(last["period"])},
        )  # fmt: skip
    empty = int((table["rows"] == 0).sum()) if len(table) else 0
    if empty:
        b.add(
            "result", "warning", "{F:k} periods in the range have no rows; they are shown as gaps, not as zero.",
            item.planned.id, [figs.operation_figure("k", empty, label="periods without rows", result=item.result)],
        )  # fmt: skip


def _correlation(item: AnalysisItem, b: _Builder) -> None:
    table = item.result.table
    usable = [
        (r, abs(float(r["coefficient"])))
        for _, r in table.iterrows()
        if r["coefficient"] is not None and not math.isnan(float(r["coefficient"]))
    ]
    if not usable:
        return
    row, size = max(usable, key=lambda t: (t[1], str(t[0]["a"]), str(t[0]["b"])))
    strength = next(word for limit, word in STRENGTH if size >= limit)
    direction = "positive" if float(row["coefficient"]) >= 0 else "negative"
    b.add(
        "result", "info",
        "{V:a} and {V:b} have a {V:strength} {V:direction} correlation of {F:r} over {F:n} rows. Correlation shows association, not cause.",
        item.planned.id,
        [
            figs.operation_figure("r", row["coefficient"], label="correlation coefficient", result=item.result, decimals=2),
            figs.operation_figure("n", row["n_used"], label="rows used", result=item.result),
        ],
        {"a": str(row["a"]), "b": str(row["b"]), "strength": strength, "direction": direction},
    )  # fmt: skip


_RESULT = {
    "value_counts": _value_counts,
    "top_n": _top_n,
    "time_trend": _time_trend,
    "correlation": _correlation,
}


def build_findings(
    profile: ProfileResult, file_sha256: str, items: list[AnalysisItem]
) -> list[Finding]:
    b = _Builder()
    quality_findings(profile, file_sha256, b)
    for item in items:
        if item.result is None:
            b.add(
                "quality",
                "warning",
                "Analysis {V:title} could not be run: {V:why}",
                item.planned.id,
                [],
                {"title": item.planned.title, "why": item.error},
            )
            continue
        if item.verification.status != "verified":
            b.add(
                "quality",
                "warning",
                "Analysis {V:title} could not be verified independently and is not reported.",
                item.planned.id,
                [],
                {"title": item.planned.title},
            )
            continue
        handler = _RESULT.get(item.planned.spec.op)
        if handler:
            handler(item, b)
    return b.items
