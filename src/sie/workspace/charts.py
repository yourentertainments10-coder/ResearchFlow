"""Deterministic SVG charts from a result table. Pure functions, no JavaScript, no external fonts.

The same string is the on-screen chart and the exported file. Every number drawn is read from the
computed table; text is HTML-escaped; layout numbers are formatted with fixed precision, so the same
table always gives the same bytes.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any

import pandas as pd

WIDTH = 640
MAX_BARS = 25
MAX_POINTS = 400
LABEL_CHARS = 28

_STYLE = (
    "<style>.bg{fill:none}.t{fill:#1d1d1f;font:600 14px sans-serif}.l{fill:#1d1d1f;font:12px sans-serif}"
    ".m{fill:#5b5b61;font:11px sans-serif}.bar{fill:#2a6fdb}.ln{fill:none;stroke:#2a6fdb;stroke-width:2}"
    ".pt{fill:#2a6fdb}.ax{stroke:#8a8a90;stroke-width:1}"
    "@media (prefers-color-scheme:dark){.t,.l{fill:#f2f2f4}.m{fill:#b4b4ba}.bar,.pt{fill:#6aa3ff}"
    ".ln{stroke:#6aa3ff}.ax{stroke:#8a8a90}}</style>"
)


def display_number(value: Any) -> str:
    """Whole numbers as they are (with thousands commas); other numbers to at most 2 decimals."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return str(value)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, int | float) and not (isinstance(value, float) and math.isnan(value)):
        return float(value)
    return None


def _cut(text: Any) -> str:
    s = str(text)
    return s if len(s) <= LABEL_CHARS else s[: LABEL_CHARS - 1] + "…"


def _wrap(title: str, height: int, body: str, summary: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" '
        f'height="{height}" role="img" aria-label="{escape(summary, quote=True)}">{_STYLE}'
        f"<title>{escape(title)}</title>"
        f'<rect class="bg" width="{WIDTH}" height="{height}"/>'
        f'<text class="t" x="12" y="20">{escape(_cut_title(title))}</text>{body}</svg>'
    )


def _cut_title(title: str) -> str:
    return title if len(title) <= 80 else title[:79] + "…"


def _empty(title: str, note: str) -> str:
    return _wrap(
        title, 80, f'<text class="m" x="12" y="52">{escape(note)}</text>', f"{title}: {note}"
    )


def bar_chart(table: pd.DataFrame, x: str, y: str, title: str) -> str:
    """Horizontal bars, one per row, baseline at zero. Rows with no number are listed as 'no value'."""
    rows = [(r[x], _number(r[y])) for _, r in table.iterrows()]
    shown = rows[:MAX_BARS]
    values = [v for _, v in shown if v is not None]
    if not shown or not values:
        return _empty(title, "no values to chart")
    low, high = min(0.0, min(values)), max(0.0, max(values))
    span = (high - low) or 1.0
    left, right, top, step = 190, 90, 34, 24
    plot = WIDTH - left - right
    zero = left + plot * (0 - low) / span
    body = [
        f'<line class="ax" x1="{zero:.2f}" y1="{top - 4}" x2="{zero:.2f}" y2="{top + step * len(shown)}"/>'
    ]
    for i, (label, value) in enumerate(shown):
        y0 = top + i * step
        body.append(
            f'<text class="l" x="{left - 8}" y="{y0 + 14}" text-anchor="end">{escape(_cut(label))}</text>'
        )
        if value is None:
            body.append(f'<text class="m" x="{zero + 6:.2f}" y="{y0 + 14}">no value</text>')
            continue
        end = left + plot * (value - low) / span
        x0, w = min(zero, end), abs(end - zero)
        body.append(
            f'<rect class="bar" x="{x0:.2f}" y="{y0 + 2}" width="{max(w, 1):.2f}" height="{step - 8}"/>'
        )
        tx = end + 6 if value >= 0 else end - 6
        anchor = "start" if value >= 0 else "end"
        body.append(
            f'<text class="m" x="{tx:.2f}" y="{y0 + 14}" text-anchor="{anchor}">{escape(display_number(table.iloc[i][y]))}</text>'
        )
    note = ""
    if len(rows) > MAX_BARS:
        note = f'<text class="m" x="12" y="{top + step * len(shown) + 16}">first {MAX_BARS} of {len(rows)} rows shown</text>'
    height = top + step * len(shown) + (28 if note else 12)
    return _wrap(title, height, "".join(body) + note, f"Bar chart: {title}")


def line_chart(table: pd.DataFrame, x: str, y: str, title: str) -> str:
    """Line over an ordered x. A missing value breaks the line (it is not drawn as zero)."""
    if len(table) > MAX_POINTS:
        return _empty(
            title, f"{len(table)} points exceed the chart limit of {MAX_POINTS}; use the table"
        )
    labels = [str(v) for v in table[x].tolist()]
    values = [_number(v) for v in table[y].tolist()]
    present = [v for v in values if v is not None]
    if not present:
        return _empty(title, "no values to chart")
    low, high = min(present), max(present)
    if low >= 0:
        low = 0.0
    span = (high - low) or 1.0
    left, right, top, bottom = 56, 16, 34, 40
    height = 300
    pw, ph = WIDTH - left - right, height - top - bottom
    n = len(values)

    def px(i: int) -> float:
        return left + (pw * i / (n - 1) if n > 1 else pw / 2)

    def py(v: float) -> float:
        return top + ph * (1 - (v - low) / span)

    body = [
        f'<line class="ax" x1="{left}" y1="{top}" x2="{left}" y2="{top + ph}"/>',
        f'<line class="ax" x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}"/>',
        f'<text class="m" x="{left - 6}" y="{top + 4}" text-anchor="end">{escape(display_number(high))}</text>',
        f'<text class="m" x="{left - 6}" y="{top + ph + 4}" text-anchor="end">{escape(display_number(low))}</text>',
    ]
    path: list[str] = []
    for i, v in enumerate(values):
        if v is None:
            if path:
                body.append(f'<polyline class="ln" points="{" ".join(path)}"/>')
                path = []
            continue
        path.append(f"{px(i):.2f},{py(v):.2f}")
        body.append(
            f'<circle class="pt" cx="{px(i):.2f}" cy="{py(v):.2f}" r="3"><title>{escape(labels[i])}: {escape(display_number(table.iloc[i][y]))}</title></circle>'
        )
    if len(path) > 1:
        body.append(f'<polyline class="ln" points="{" ".join(path)}"/>')
    ticks = sorted({0, n - 1, *(round(k * (n - 1) / 4) for k in range(5))}) if n > 1 else [0]
    for i in ticks:
        anchor = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        body.append(
            f'<text class="m" x="{px(i):.2f}" y="{top + ph + 16}" text-anchor="{anchor}">{escape(_cut(labels[i]))}</text>'
        )
    return _wrap(title, height, "".join(body), f"Line chart: {title}")


def render_chart(
    kind: str, table: pd.DataFrame, x: str | None, y: str | None, title: str
) -> str | None:
    """SVG for a bar or line chart; ``None`` for a table-only analysis."""
    if kind == "bar" and x and y:
        return bar_chart(table, x, y, title)
    if kind == "line" and x and y:
        return line_chart(table, x, y, title)
    return None
