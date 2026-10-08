"""Invariants every set of analytics tables must satisfy (docs/ANALYTICS_SPEC.md section 13).

``check_invariants`` returns the failures as text; ``assert_invariants`` raises. A table set that breaks
one is never written or snapshotted.
"""

from __future__ import annotations

import math

import pandas as pd

TOLERANCE = 1e-9


class InvariantError(ValueError):
    """The analytics tables contradict each other or the medal facts."""


def check_invariants(
    df: pd.DataFrame,
    *,
    country_summary: pd.DataFrame,
    country_sport: pd.DataFrame,
    country_sport_gender: pd.DataFrame,
    sport_summary: pd.DataFrame,
    gender_summary: pd.DataFrame,
    concentration: pd.DataFrame,
    unit: str = "sport",
) -> list[str]:
    failures: list[str] = []
    n = len(df)

    for name, frame in (
        ("country_summary", country_summary),
        ("country_sport", country_sport),
        ("country_sport_gender", country_sport_gender),
        ("sport_summary", sport_summary),
        ("gender_summary", gender_summary),
    ):
        if int(frame["Total"].sum()) != n:
            failures.append(f"{name}: medals sum to {int(frame['Total'].sum())}, facts have {n}")

    for name, frame in (("country_summary", country_summary), ("country_sport", country_sport)):
        points = frame["Gold"] * 3 + frame["Silver"] * 2 + frame["Bronze"]
        if not (points == frame["Points"]).all():
            failures.append(f"{name}: Points differ from 3G + 2S + B")
        if not (frame["Gold"] + frame["Silver"] + frame["Bronze"] == frame["Total"]).all():
            failures.append(f"{name}: Total differs from G + S + B")

    shares = country_sport.groupby("country_code")["share_of_country"].sum()
    off = [c for c, s in shares.items() if abs(s - 1) > TOLERANCE]
    if off:
        failures.append(f"shares per country do not sum to 1: {off[:5]}")

    by_gender = country_sport_gender.groupby(["country_code", unit])["Total"].sum()
    by_all = country_sport.set_index(["country_code", unit])["Total"]
    if not by_gender.sort_index().equals(by_all.sort_index()):
        failures.append("sum over genders differs from the All value per country and unit")

    per_country = country_summary.set_index("country_code")["Total"].sort_index()
    if not gender_summary.set_index("country_code")["Total"].sort_index().equals(per_country):
        failures.append("gender_summary totals differ from country_summary")

    for r in concentration.itertuples(index=False):
        units = getattr(r, f"{unit}s_with_medal")
        if not (1 / units - TOLERANCE <= r.hhi <= 1 + TOLERANCE) or math.isnan(r.hhi):
            failures.append(f"{r.country_code}: HHI {r.hhi} outside 1/N..1")
    return failures


def assert_invariants(df: pd.DataFrame, **tables: pd.DataFrame) -> None:
    failures = check_invariants(df, **tables)
    if failures:
        raise InvariantError("; ".join(failures))
