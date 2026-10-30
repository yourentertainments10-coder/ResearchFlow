"""The only place the workspace touches ``sie.answer``: exact figures and their display.

Every number shown to a person (cards, findings) is a ``Figure`` formatted by
``sie.answer.numbers.format_figure`` (half-up rounding, comma thousands, fixed decimals), so text
templates contain no digits of their own.
"""

from __future__ import annotations

import re
from typing import Any

from sie.answer.adapters import FigureError, figure_from_value, operation_provenance
from sie.answer.models import Figure, SourceKind
from sie.answer.numbers import format_figure

__all__ = [
    "Figure",
    "FigureError",
    "decimals_for",
    "file_figure",
    "operation_figure",
    "render_template",
    "show",
]

_TOKEN = re.compile(r"\{([FV]):([A-Za-z][A-Za-z0-9_.-]*)\}")


def decimals_for(value: Any) -> int:
    """0 for whole numbers, else 2."""
    if hasattr(value, "item"):
        value = value.item()
    return 0 if isinstance(value, int) or (isinstance(value, float) and value == int(value)) else 2


def file_figure(
    fid: str, value: Any, *, label: str, file_sha256: str, decimals: int = 0, unit: str = ""
) -> Figure:
    return figure_from_value(
        fid,
        value,
        label=label,
        source_kind=SourceKind.USER_FILE,
        source_ref=file_sha256,
        decimals=decimals,
        unit=unit,
    )


def operation_figure(
    fid: str, value: Any, *, label: str, result: Any, decimals: int = 0, unit: str = ""
) -> Figure:
    ref, provenance = operation_provenance(result)
    return figure_from_value(
        fid,
        value,
        label=label,
        source_kind=SourceKind.OPERATION,
        source_ref=ref,
        decimals=decimals,
        unit=unit,
        provenance=provenance,
    )


def show(figure: Figure) -> str:
    return format_figure(figure)


def render_template(template: str, figures: dict[str, Figure], values: dict[str, str]) -> str:
    """Replace ``{F:id}`` by the formatted figure and ``{V:key}`` by the data value. Unknown ids fail."""

    def sub(match: re.Match[str]) -> str:
        kind, key = match.groups()
        if kind == "F":
            return format_figure(figures[key])
        return values[key]

    return _TOKEN.sub(sub, template)


def template_digits(template: str) -> list[str]:
    """Digit characters left in a template once placeholders are removed (must be none)."""
    return re.findall(r"\d", _TOKEN.sub("", template))
