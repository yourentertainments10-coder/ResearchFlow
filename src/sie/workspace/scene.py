"""A chart as a list of drawing primitives (shared by the PNG and PDF exports).

It is laid out exactly like the SVG in ``charts.py`` (same 640-unit width, same numbers) and every
number drawn is read from the computed table, so a picture never shows a figure that is not in it.
Pure functions; the same table always gives the same scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from sie.workspace.charts import (
    LABEL_CHARS,
    MAX_BARS,
    MAX_POINTS,
    WIDTH,
    _number,
    display_number,
)

INK = (29, 29, 31)
MUTED = (91, 91, 97)
AXIS = (138, 138, 144)
BLUE = (42, 111, 219)


@dataclass
class Scene:
    width: int
    height: int
    items: list[tuple[Any, ...]] = field(default_factory=list)
    # ("rect", x, y, w, h, rgb) ("line", x1, y1, x2, y2, rgb, width) ("poly", [(x, y)...], rgb, width)
    # ("dot", cx, cy, r, rgb) ("text", x, y, str, size, anchor, rgb)   size: "t" title, "l" label


def _cut(text: Any) -> str:
    s = str(text)
    return s if len(s) <= LABEL_CHARS else s[: LABEL_CHARS - 1] + "..."


def _title(title: str) -> str:
    return title if len(title) <= 80 else title[:79] + "..."


def _base(title: str, height: int) -> Scene:
    sc = Scene(WIDTH, height)
    sc.items.append(("text", 12, 20, _title(title), "t", "start", INK))
    return sc


def _empty(title: str, note: str) -> Scene:
    sc = _base(title, 80)
    sc.items.append(("text", 12, 52, note, "l", "start", MUTED))
    return sc


def bar_scene(table: pd.DataFrame, x: str, y: str, title: str) -> Scene:
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
    height = top + step * len(shown) + (28 if len(rows) > MAX_BARS else 12)
    sc = _base(title, height)
    sc.items.append(("line", zero, top - 4, zero, top + step * len(shown), AXIS, 1))
    for i, (label, value) in enumerate(shown):
        y0 = top + i * step
        sc.items.append(("text", left - 8, y0 + 14, _cut(label), "l", "end", INK))
        if value is None:
            sc.items.append(("text", zero + 6, y0 + 14, "no value", "l", "start", MUTED))
            continue
        end = left + plot * (value - low) / span
        x0, w = min(zero, end), abs(end - zero)
        sc.items.append(("rect", x0, y0 + 2, max(w, 1), step - 8, BLUE))
        tx = end + 6 if value >= 0 else end - 6
        anchor = "start" if value >= 0 else "end"
        sc.items.append(("text", tx, y0 + 14, display_number(table.iloc[i][y]), "l", anchor, MUTED))
    if len(rows) > MAX_BARS:
        sc.items.append(
            (
                "text",
                12,
                top + step * len(shown) + 16,
                f"first {MAX_BARS} of {len(rows)} rows shown",
                "l",
                "start",
                MUTED,
            )
        )
    return sc


def line_scene(table: pd.DataFrame, x: str, y: str, title: str) -> Scene:
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
    left, right, top, bottom, height = 56, 16, 34, 40, 300
    pw, ph = WIDTH - left - right, height - top - bottom
    n = len(values)

    def px(i: int) -> float:
        return left + (pw * i / (n - 1) if n > 1 else pw / 2)

    def py(v: float) -> float:
        return top + ph * (1 - (v - low) / span)

    sc = _base(title, height)
    sc.items += [
        ("line", left, top, left, top + ph, AXIS, 1),
        ("line", left, top + ph, left + pw, top + ph, AXIS, 1),
        ("text", left - 6, top + 4, display_number(high), "l", "end", MUTED),
        ("text", left - 6, top + ph + 4, display_number(low), "l", "end", MUTED),
    ]
    path: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        if v is None:
            if len(path) > 1:
                sc.items.append(("poly", path, BLUE, 2))
            path = []
            continue
        path.append((px(i), py(v)))
        sc.items.append(("dot", px(i), py(v), 3, BLUE))
    if len(path) > 1:
        sc.items.append(("poly", path, BLUE, 2))
    ticks = sorted({0, n - 1, *(round(k * (n - 1) / 4) for k in range(5))}) if n > 1 else [0]
    for i in ticks:
        anchor = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        sc.items.append(("text", px(i), top + ph + 16, _cut(labels[i]), "l", anchor, MUTED))
    return sc


def chart_scene(
    kind: str, table: pd.DataFrame, x: str | None, y: str | None, title: str
) -> Scene | None:
    """The scene for a bar or line chart; ``None`` for a table-only analysis."""
    if kind == "bar" and x and y:
        return bar_scene(table, x, y, title)
    if kind == "line" and x and y:
        return line_scene(table, x, y, title)
    return None
