"""KPI cards. Every value is an exact figure taken from the profile or from the column summary."""

from __future__ import annotations

from typing import Any

from sie.datasets.models import ProfileResult
from sie.workspace import figs
from sie.workspace.models import Card


def profile_figures(profile: ProfileResult) -> dict[str, Any]:
    missing = sum(c.nulls for c in profile.column_profiles)
    cells = profile.rows * profile.columns
    return {
        "rows": profile.rows,
        "columns": profile.columns,
        "missing_cells": missing,
        "duplicate_rows": profile.duplicate_rows,
        "missing_share": (missing / cells * 100) if cells else 0.0,
    }


def build_cards(
    profile: ProfileResult,
    file_sha256: str,
    describe_item: Any | None,
    *,
    profile_verified: bool,
) -> list[Card]:
    """Four profile cards plus mean, minimum and maximum of the first numeric column.

    ``describe_item`` is the verified column-summary analysis (an ``AnalysisItem``) or ``None``."""
    f = profile_figures(profile)
    spec = [
        ("rows", "Rows", f["rows"], 0, "", "data rows in the file"),
        ("columns", "Columns", f["columns"], 0, "", "columns in the file"),
        ("missing_cells", "Missing cells", f["missing_cells"], 0, "", "cells with no value"),
        ("missing_share", "Missing share", f["missing_share"], 1, "%", "of all cells"),
        (
            "duplicate_rows",
            "Duplicate rows",
            f["duplicate_rows"],
            0,
            "",
            "repeat an earlier row; not removed",
        ),
    ]
    cards: list[Card] = []
    for cid, label, value, decimals, unit, detail in spec:
        fig = figs.file_figure(
            f"card_{cid}", value, label=label, file_sha256=file_sha256, decimals=decimals, unit=unit
        )
        cards.append(
            Card(
                f"card_{cid}",
                label,
                figs.show(fig),
                detail,
                "profile",
                fig.model_dump(mode="json"),
                profile_verified,
            )
        )
    if describe_item is not None and describe_item.result is not None:
        table = describe_item.result.table
        numeric = table[table["type"].isin(["integer", "decimal"]) & (table["count"] > 0)]
        if len(numeric):
            row = numeric.iloc[0]
            for key, label in (("mean", "Mean"), ("min", "Minimum"), ("max", "Maximum")):
                value = row[key]
                if value is None:
                    continue
                fig = figs.operation_figure(
                    f"card_{key}",
                    value,
                    label=f"{label} of {row['column']}",
                    result=describe_item.result,
                    decimals=figs.decimals_for(value),
                )
                cards.append(
                    Card(
                        f"card_{key}",
                        f"{label} of {row['column']}",
                        figs.show(fig),
                        "ignoring empty cells",
                        describe_item.planned.id,
                        fig.model_dump(mode="json"),
                        describe_item.verification.status == "verified",
                    )
                )
    return cards
