"""The metrics of docs/ANALYTICS_SPEC.md as pure functions ``f(df) -> DataFrame``.

Input is the analysis frame built from ``v_medal_facts`` (``analytics.facts.analysis_frame``): one row
per country medal with ``country_code``, ``country_name``, ``sport``, ``discipline_name``, ``event_id``,
``gender``, ``medal``. Nothing here touches the database or the clock, and every function works on any
subset of rows (for example women's events only, spec section 9), so a filtered frame gives the
"women-only version" of any table.

``unit`` is ``"sport"`` (the 49 official sports) or ``"discipline"`` (the source's 59). Division by zero
returns null, never zero (spec section 13). Formulas cite the spec section they implement. Changing
one needs a spec update and a decision record.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

ANALYTICS_VERSION = "1"  # version of ANALYTICS_SPEC used; recorded on every snapshot

MEDALS = ("Gold", "Silver", "Bronze")
GENDERS = ("Men", "Women", "Mixed", "Open")
POINTS: Mapping[str, int] = {"Gold": 3, "Silver": 2, "Bronze": 1}  # spec 3, configurable
HHI_CONCENTRATED = (
    0.25  # spec 5: above = concentrated; 0.10 to 0.25 = moderate; below = diversified
)
HHI_MODERATE = 0.10
MIN_RCA_MEDALS = 2  # spec 6: RCA cells with fewer medals are hidden
SMALL_SAMPLE = 3  # spec 11: fewer medals than this are shown but flagged
TIER_CORE, TIER_SECONDARY = 60, 90  # spec 8: cumulative percent of points
SPECIALISED_RCA, GOLD_HEAVY_RATIO, TAG_MIN_MEDALS = 1.5, 0.5, 3

_UNIT_COLUMN = {"sport": "sport", "discipline": "discipline_name"}


# --- helpers ---------------------------------------------------------------------------------------


def _divide(a: pd.Series, b: pd.Series) -> pd.Series:
    """``a / b`` with null where ``b`` is zero."""
    return a / b.where(b != 0)


def _names(df: pd.DataFrame) -> dict[str, str]:
    return dict(zip(df["country_code"], df["country_name"], strict=True))


def _with_country(out: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    out.insert(1, "country", out["country_code"].map(_names(df)))
    return out


def _counts(df: pd.DataFrame, keys: list[str], weights: Mapping[str, int] = POINTS) -> pd.DataFrame:
    """Gold, Silver, Bronze, Total and Points per ``keys`` (spec 3)."""
    if df.empty:
        return pd.DataFrame(columns=[*keys, *MEDALS, "Total", "Points"])
    g = df.groupby([*keys, "medal"]).size().unstack(fill_value=0)
    for m in MEDALS:
        if m not in g.columns:
            g[m] = 0
    g = g[list(MEDALS)].astype(int).rename_axis(columns=None)
    g["Total"] = g.sum(axis=1)
    g["Points"] = sum(g[m] * weights[m] for m in MEDALS)
    return g.reset_index()


def competition_rank(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
    """Standard competition ranking (1, 2, 2, 4): 1 + the rows strictly better on ``columns``."""
    keys = [tuple(row) for row in frame[columns].itertuples(index=False)]
    return pd.Series([1 + sum(other > k for other in keys) for k in keys], index=frame.index)


def _unit_counts(df: pd.DataFrame, unit: str, extra: list[str] | None = None) -> pd.DataFrame:
    column = _UNIT_COLUMN[unit]
    out = _counts(df, ["country_code", column, *(extra or [])])
    return out.rename(columns={column: unit})


# --- spec 3: base metrics --------------------------------------------------------------------------


def country_summary(df: pd.DataFrame, weights: Mapping[str, int] = POINTS) -> pd.DataFrame:
    """Per country: G, S, B, Total, Points, gold ratio and three ranks (spec 3).

    ``podium_rank`` sorts by gold, then silver, then bronze; ``total_rank`` by Total and
    ``points_rank`` by Points alone. Equal values share a rank.
    """
    c = _counts(df, ["country_code"], weights)
    c["gold_ratio"] = _divide(c["Gold"], c["Total"])
    c["podium_rank"] = competition_rank(c, ["Gold", "Silver", "Bronze"])
    c["total_rank"] = competition_rank(c, ["Total"])
    c["points_rank"] = competition_rank(c, ["Points"])
    c = _with_country(c, df)
    return c.sort_values(["podium_rank", "country_code"]).reset_index(drop=True)


def country_sport(df: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Country x unit with the shares of spec 4.

    ``share_of_country`` = T_cs / T_c; ``market_share`` = T_cs / T_s; ``gold_market_share`` =
    G_cs / G_s; ``gold_ratio`` = G_cs / T_cs.
    """
    cs = _unit_counts(df, unit)
    if cs.empty:
        return cs.assign(share_of_country=[], market_share=[], gold_market_share=[], gold_ratio=[])
    cs["share_of_country"] = cs["Total"] / cs.groupby("country_code")["Total"].transform("sum")
    cs["market_share"] = cs["Total"] / cs.groupby(unit)["Total"].transform("sum")
    cs["gold_market_share"] = _divide(cs["Gold"], cs.groupby(unit)["Gold"].transform("sum"))
    cs["gold_ratio"] = _divide(cs["Gold"], cs["Total"])
    cs = _with_country(cs, df)
    return cs.sort_values(
        ["country_code", "Total", unit], ascending=[True, False, True]
    ).reset_index(drop=True)


def country_sport_gender(df: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Country x unit x gender (grain 3 of the spec). Open stays its own category (ADR-019)."""
    out = _unit_counts(df, unit, ["gender"])
    if out.empty:
        return out
    out = _with_country(out, df)
    return out.sort_values(["country_code", unit, "gender"]).reset_index(drop=True)


def sport_summary(
    df: pd.DataFrame, unit: str = "sport", completion: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Per unit, all countries (grain 5): medals, countries, events, concentration and gender split.

    ``hhi`` is the Herfindahl index of the countries' shares of this unit's medals (0 to 1).
    ``completion`` (from ``unit_completion``) adds ``events_total``, ``events_completed`` and the
    partial-data flag of spec 11.
    """
    column = _UNIT_COLUMN[unit]
    totals = _counts(df, [column]).rename(columns={column: unit})
    if totals.empty:
        return totals
    by_country = _unit_counts(df, unit)
    extra = by_country.groupby(unit).agg(countries=("country_code", "nunique"))
    leaders = (
        by_country.sort_values(["Total", "country_code"], ascending=[False, True])
        .drop_duplicates(unit)
        .set_index(unit)["country_code"]
    )
    extra["leader"] = leaders
    unit_total = by_country.groupby(unit)["Total"].sum()
    shares = by_country["Total"] / by_country[unit].map(unit_total)
    extra["hhi"] = (shares**2).groupby(by_country[unit]).sum()
    extra["leader_share"] = by_country.groupby(unit)["Total"].max() / unit_total
    events = df.groupby(column)["event_id"].nunique().rename("events_with_medals")
    out = totals.merge(extra.reset_index(), on=unit).merge(
        events.reset_index().rename(columns={column: unit}), on=unit
    )
    gender = df.groupby([column, "gender"]).size().unstack(fill_value=0)
    for g in GENDERS:
        if g not in gender.columns:
            gender[g] = 0
    gender = gender[list(GENDERS)].rename(columns=lambda g: f"{g}_medals")
    out = out.merge(gender.reset_index().rename(columns={column: unit}), on=unit)
    out["women_share"] = _divide(out["Women_medals"], out["Total"])
    if completion is not None:
        out = out.merge(completion, on=unit, how="left")
    return out.sort_values(["Total", unit], ascending=[False, True]).reset_index(drop=True)


def unit_completion(events: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Events per unit and how many are completed (spec 7 and 11).

    ``events`` has one row per event with ``sport``, ``discipline`` and ``status``. ``events_total`` is
    the events in the database, which is the official total only once every event has been loaded;
    ``partial`` is true while ``events_completed < events_total``.
    """
    done = events["status"].isin(["completed", "amended"])
    out = (
        events.assign(done=done)
        .groupby(unit)
        .agg(events_total=("status", "size"), events_completed=("done", "sum"))
        .reset_index()
    )
    out["events_completed"] = out["events_completed"].astype(int)
    out["completed_share"] = _divide(out["events_completed"], out["events_total"])
    out["partial"] = out["events_completed"] < out["events_total"]
    return out


# --- spec 4, 9: gender --------------------------------------------------------------------------------


def gender_summary(df: pd.DataFrame, weights: Mapping[str, int] = POINTS) -> pd.DataFrame:
    """Per country: medals and points by gender category side by side (spec 9).

    ``women_share`` = women / all medals (Mixed and Open stay in the denominator);
    ``women_share_excl_mixed`` = women / (men + women). ``gender_gap`` = women points / (women + men
    points), 0.5 is parity, shown only where men's and women's medals together are at least 3.
    Mixed and Open are never added to Women or Men.
    """
    t = _counts(df, ["country_code", "gender"], weights)
    if t.empty:
        return t
    wide = {}
    for label, column in (("medals", "Total"), ("points", "Points")):
        w = t.pivot(index="country_code", columns="gender", values=column).fillna(0).astype(int)
        for g in GENDERS:
            if g not in w.columns:
                w[g] = 0
        wide[label] = w[list(GENDERS)].rename(columns=lambda g, lab=label: f"{g}_{lab}")
    out = pd.concat([wide["medals"], wide["points"]], axis=1)
    out["Total"] = out[[f"{g}_medals" for g in GENDERS]].sum(axis=1)
    out["women_share"] = _divide(out["Women_medals"], out["Total"])
    out["women_share_excl_mixed"] = _divide(
        out["Women_medals"], out["Women_medals"] + out["Men_medals"]
    )
    gap = _divide(out["Women_points"], out["Women_points"] + out["Men_points"])
    out["gender_gap"] = gap.where(out["Women_medals"] + out["Men_medals"] >= SMALL_SAMPLE)
    out = out.reset_index()
    out = _with_country(out, df)
    return out.sort_values(["Total", "country_code"], ascending=[False, True]).reset_index(
        drop=True
    )


# --- spec 5: concentration --------------------------------------------------------------------------


def hhi_label(hhi: float) -> str:
    if math.isnan(hhi):
        return ""
    if hhi > HHI_CONCENTRATED:
        return "concentrated"
    if hhi >= HHI_MODERATE:
        return "moderate"
    return "diversified"


def concentration(df: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Per country: HHI, effective number of units, top-1 and top-3 share, coverage, entropy (spec 5).

    HHI = sum of squared shares of the country's medals by unit (range 1/N to 1); effective number =
    1 / HHI. Labels use the thresholds in this module (0.25 and 0.10), stated in the UI.
    """
    cs = _unit_counts(df, unit)
    rows = []
    for code, x in cs.groupby("country_code"):
        total = int(x["Total"].sum())
        shares = (x["Total"] / total).sort_values(ascending=False)
        hhi = float((shares**2).sum())
        rows.append(
            {
                "country_code": code,
                "Total": total,
                f"{unit}s_with_medal": int((x["Total"] >= 1).sum()),
                f"{unit}s_with_gold": int((x["Gold"] >= 1).sum()),
                "hhi": hhi,
                "effective_units": 1 / hhi,
                "top1_share": float(shares.iloc[0]),
                "top3_share": float(shares.iloc[:3].sum()),
                "entropy": float(-(shares * shares.map(math.log)).sum()),
                "label": hhi_label(hhi),
                "small_sample": total < SMALL_SAMPLE,
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = _with_country(out, df)
    return out.sort_values(["Total", "country_code"], ascending=[False, True]).reset_index(
        drop=True
    )


# --- spec 6: specialisation ---------------------------------------------------------------------------


def rca(df: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Revealed comparative advantage: ``(T_cs / T_c) / (T_s / T_all)`` (spec 6).

    Cells with fewer than 2 medals have a null ``rca`` (hidden); cells below 3 medals are flagged
    ``small_sample``. RCA above 1 means the country draws more of its medals from the unit than the
    average country does.
    """
    cs = _unit_counts(df, unit)
    if cs.empty:
        return cs.assign(share_of_country=[], unit_share_of_all=[], rca=[], small_sample=[])
    t_all = cs["Total"].sum()
    out = cs[["country_code", unit, "Gold", "Total"]].copy()
    out["share_of_country"] = out["Total"] / out.groupby("country_code")["Total"].transform("sum")
    out["unit_share_of_all"] = out.groupby(unit)["Total"].transform("sum") / t_all
    out["rca"] = (out["share_of_country"] / out["unit_share_of_all"]).where(
        out["Total"] >= MIN_RCA_MEDALS
    )
    out["small_sample"] = out["Total"] < SMALL_SAMPLE
    out = _with_country(out, df)
    return out.sort_values(
        ["country_code", "Total", unit], ascending=[True, False, True]
    ).reset_index(drop=True)


# --- spec 7: opportunity ------------------------------------------------------------------------------


def conversion(
    df: pd.DataFrame, unit: str = "sport", completion: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Event conversion: medals and golds per completed event of the unit (spec 7).

    Not "efficiency": that needs entries, which v1 does not have. ``completed_events`` comes from
    ``completion`` when given, otherwise it is the events that produced a medal. ``podium_sweeps`` is
    the events where the country took two or more podium places.
    """
    column = _UNIT_COLUMN[unit]
    cs = _unit_counts(df, unit)
    if cs.empty:
        return cs
    seen = df.groupby(column)["event_id"].nunique().rename("completed_events").reset_index()
    out = cs[["country_code", unit, "Gold", "Total"]].merge(
        seen.rename(columns={column: unit}), on=unit
    )
    if completion is not None:
        out = out.drop(columns="completed_events").merge(
            completion.rename(columns={"events_completed": "completed_events"})[
                [unit, "completed_events", "events_total", "completed_share", "partial"]
            ],
            on=unit,
            how="left",
        )
    out["event_conversion"] = _divide(out["Total"], out["completed_events"])
    out["gold_conversion"] = _divide(out["Gold"], out["completed_events"])
    per_event = df.groupby([column, "country_code", "event_id"]).size().reset_index(name="n")
    sweeps = (
        per_event[per_event["n"] >= 2]
        .groupby([column, "country_code"])
        .size()
        .rename("podium_sweeps")
        .reset_index()
        .rename(columns={column: unit})
    )
    out = out.merge(sweeps, on=["country_code", unit], how="left")
    out["podium_sweeps"] = out["podium_sweeps"].fillna(0).astype(int)
    out = _with_country(out, df)
    return out.sort_values(
        ["country_code", "Total", unit], ascending=[True, False, True]
    ).reset_index(drop=True)


# --- spec 8: tiers ------------------------------------------------------------------------------------


def tiers(
    df: pd.DataFrame, unit: str = "sport", completion: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Pareto strength tiers per country (spec 8), descriptive and never called "weak" on its own.

    A unit is Core while the points before it are under 60 percent of the country's points,
    Secondary under 90 percent, Tail after that. ``No medals`` rows list every unit where another
    country medalled and this one did not, with ``unit_events`` (the unit's size) beside them.
    ``specialised``: RCA >= 1.5 and at least 3 medals; ``gold_heavy``: gold ratio >= 0.5 and at least
    3 medals.
    """
    cs = _unit_counts(df, unit)
    if cs.empty:
        return cs
    rca_table = rca(df, unit).set_index(["country_code", unit])["rca"]
    column = _UNIT_COLUMN[unit]
    events = (
        completion.set_index(unit)["events_total"]
        if completion is not None
        else df.groupby(column)["event_id"].nunique().rename_axis(unit)
    )
    rows = []
    all_units = sorted(cs[unit].unique())
    for code, x in cs.groupby("country_code"):
        x = x.sort_values(["Points", unit], ascending=[False, True])
        total_points = int(x["Points"].sum())
        before = 0
        for r in x.itertuples(index=False):
            if total_points == 0:
                tier = "Tail"
            elif before * 100 < TIER_CORE * total_points:
                tier = "Core"
            elif before * 100 < TIER_SECONDARY * total_points:
                tier = "Secondary"
            else:
                tier = "Tail"
            before += int(r.Points)
            value = getattr(r, unit)
            rca_value = rca_table.get((code, value))
            enough = r.Total >= TAG_MIN_MEDALS
            rows.append(
                {
                    "country_code": code,
                    unit: value,
                    "Gold": r.Gold,
                    "Silver": r.Silver,
                    "Bronze": r.Bronze,
                    "Total": r.Total,
                    "Points": r.Points,
                    "points_share": r.Points / total_points if total_points else float("nan"),
                    "cumulative_points_share": before / total_points
                    if total_points
                    else float("nan"),
                    "tier": tier,
                    "specialised": bool(
                        enough and rca_value is not None and rca_value >= SPECIALISED_RCA
                    ),
                    "gold_heavy": bool(enough and r.Gold / r.Total >= GOLD_HEAVY_RATIO),
                    "unit_events": int(events.get(value, 0)),
                }
            )
        held = set(x[unit])
        for value in all_units:
            if value not in held:
                rows.append(
                    {
                        "country_code": code,
                        unit: value,
                        "Gold": 0,
                        "Silver": 0,
                        "Bronze": 0,
                        "Total": 0,
                        "Points": 0,
                        "points_share": 0.0,
                        "cumulative_points_share": float("nan"),
                        "tier": "No medals",
                        "specialised": False,
                        "gold_heavy": False,
                        "unit_events": int(events.get(value, 0)),
                    }
                )
    out = pd.DataFrame(rows)
    out = _with_country(out, df)
    order = {"Core": 0, "Secondary": 1, "Tail": 2, "No medals": 3}
    out["_o"] = out["tier"].map(order)
    out = out.sort_values(
        ["country_code", "_o", "Points", unit], ascending=[True, True, False, True]
    )
    return out.drop(columns="_o").reset_index(drop=True)
