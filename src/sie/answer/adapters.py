"""Turn existing results into figures without typing a number by hand.

Duck-typed on purpose: this package imports nothing from the sports modules or from ``sie.datasets``.
Any table with ``.loc``-style access (a pandas DataFrame) and any result object with a
``provenance`` (``operation``, ``parameters``, ``input_sha256``, ``result_sha256``) works.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sie.answer.models import Figure, SourceKind


class FigureError(ValueError):
    """The value cannot become an exact figure (missing, not finite, not a number). Never filled in."""


def to_decimal(value: Any) -> Decimal:
    """Exact text of the value: ints as they are, floats at 15 significant digits (as ``sie.datasets``)."""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()  # numpy scalar
    if value is None or isinstance(value, bool):
        raise FigureError(f"{value!r} is not a number")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise FigureError("NaN and infinity are not figures")
        return Decimal(format(value, ".15g"))
    try:
        return Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001
        raise FigureError(f"{value!r} is not a number") from exc


def figure_from_value(
    fid: str,
    value: Any,
    *,
    label: str,
    source_kind: SourceKind,
    source_ref: str,
    decimals: int = 0,
    unit: str = "",
    provenance: Mapping[str, str] | None = None,
) -> Figure:
    return Figure(
        id=fid,
        value=to_decimal(value),
        decimals=decimals,
        unit=unit,
        label=label,
        source_kind=source_kind,
        source_ref=source_ref,
        provenance=dict(provenance or {}),
    )


def figure_from_cell(
    fid: str,
    table: Any,
    *,
    where: Mapping[str, Any],
    column: str,
    label: str,
    source_kind: SourceKind,
    source_ref: str,
    decimals: int = 0,
    unit: str = "",
    provenance: Mapping[str, str] | None = None,
) -> Figure:
    """The one cell of ``column`` in the one row of ``table`` that matches ``where``.

    No match, or more than one, is an error: the figure is never a guess.
    """
    mask = None
    for key, wanted in where.items():
        part = table[key] == wanted
        mask = part if mask is None else (mask & part)
    if mask is None:
        raise FigureError("where must name at least one column")
    rows = table[mask]
    if len(rows) != 1:
        raise FigureError(f"{len(rows)} rows match {dict(where)!r}; exactly one is needed")
    return figure_from_value(
        fid,
        rows.iloc[0][column],
        label=label,
        source_kind=source_kind,
        source_ref=source_ref,
        decimals=decimals,
        unit=unit,
        provenance=provenance,
    )


def operation_provenance(result: Any) -> tuple[str, dict[str, str]]:
    """``(source_ref, provenance)`` from an operation result: the result hash and what produced it."""
    p = result.provenance
    ref = p.result_sha256
    if not ref:
        raise FigureError("the operation result has no result hash")
    return ref, {
        "operation": str(p.operation),
        "input_sha256": str(p.input_sha256),
        "parameters": ",".join(f"{k}={p.parameters[k]}" for k in sorted(p.parameters)),
    }
