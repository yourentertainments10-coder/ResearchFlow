"""Event-level analytics: country x sport x gender, specialisation, per-sport concentration.

Input is a list of ParsedPlacing. Reconciliation against the official standings is explicit and
raises on any difference.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from sie.sources.bornan.parse_medals import ParsedPlacing


def placings_frame(placings: list[ParsedPlacing]) -> pd.DataFrame:
    df = pd.DataFrame([p.__dict__ for p in placings])
    df["event_key"] = df["event_code"].str.split(".").str[:2].str.join(".")
    # Parsed portal rows only know the discipline; the official sport comes from the database facts.
    df["sport"] = df["discipline_name"]
    return df


def reconcile(df: pd.DataFrame, standings: dict[str, list[dict[str, Any]]]) -> pd.DataFrame:
    """Medal rows vs official per-discipline standings. Returns mismatches (empty = reconciled)."""
    medal_key = {"Gold": "ME_GOLD", "Silver": "ME_SILVER", "Bronze": "ME_BRONZE"}
    # The official table counts Open events under Mixed (verified, SOURCE_DISCOVERY section 11).
    gender_key = {"Men": "M", "Women": "W", "Mixed": "X"}
    df = df.assign(gender=df["gender"].replace({"Open": "Mixed"}))
    mine = df.groupby(["discipline", "country_code", "medal", "gender"]).size()
    bad = []
    for disc, recs in standings.items():
        official = {}
        for rec in recs:
            for m, mk in medal_key.items():
                for g, gk in gender_key.items():
                    n = rec["Count"][mk].get(gk, 0)
                    if n:
                        official[(disc, rec["Org"], m, g)] = n
        for k in set(official) | {k for k in mine.index if k[0] == disc}:
            a, b = int(mine.get(k, 0)), official.get(k, 0)
            if a != b:
                bad.append((*k, a, b))
    return pd.DataFrame(
        bad, columns=["discipline", "country", "medal", "gender", "from_rows", "official"]
    )


# The unit a medal table is cut by. ``sport`` is the official sport (49, ADR-020); ``discipline`` is the
# source's own finer split (59), kept so no discipline-level information is lost.
_UNIT_COLUMN = {"sport": "sport", "discipline": "discipline_name"}


def reconcile_official_table(df: pd.DataFrame, records: list[dict[str, Any]]) -> pd.DataFrame:
    """Country x medal x gender counts vs the official all-disciplines table. Empty = reconciled.

    ``records`` is the portal's ALL standings (one record per country, counts split M / W / X). Only
    this comparison folds Open into Mixed, because the official table has no Open column; our own
    tables keep Open apart (ADR-019). Compared in both directions: a country missing on either side
    is a mismatch.
    """
    medal_key = {"Gold": "ME_GOLD", "Silver": "ME_SILVER", "Bronze": "ME_BRONZE"}
    gender_key = {"Men": "M", "Women": "W", "Mixed": "X"}
    mine = (
        df.assign(gender=df["gender"].replace({"Open": "Mixed"}))
        .groupby(["country_code", "medal", "gender"])
        .size()
    )
    official = {
        (rec["Org"], m, g): rec["Count"][mk].get(gk, 0)
        for rec in records
        for m, mk in medal_key.items()
        for g, gk in gender_key.items()
    }
    bad = [
        (*k, int(mine.get(k, 0)), official.get(k, 0))
        for k in sorted(set(official) | set(mine.index))
        if int(mine.get(k, 0)) != official.get(k, 0)
    ]
    return pd.DataFrame(bad, columns=["country", "medal", "gender", "from_rows", "official"])


def country_sport(df: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Medals per country and per ``unit`` ('sport' by default, or 'discipline'); the unit column
    carries the unit's name."""
    column = _UNIT_COLUMN[unit]
    g = df.groupby(["country_code", column, "medal"]).size().unstack(fill_value=0)
    for m in ("Gold", "Silver", "Bronze"):
        if m not in g:
            g[m] = 0
    g = g[["Gold", "Silver", "Bronze"]]
    g["Total"] = g.sum(axis=1)
    return g.reset_index().rename(columns={column: unit})


def sport_concentration(cs: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Per sport (or discipline): medals, countries with a medal, HHI of country shares, top share."""
    rows = []
    for sport, x in cs.groupby(unit):
        sh = x["Total"] / x["Total"].sum()
        top = x.loc[x["Total"].idxmax()]
        rows.append((sport, int(x["Total"].sum()), len(x), round(float((sh**2).sum() * 10000)),
                     top.country_code, round(float(sh.max() * 100), 1)))  # fmt: skip
    return pd.DataFrame(
        rows, columns=[unit, "medals", "countries", "HHI", "leader", "leader_share_%"]
    )


def specialisation(cs: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Share of each country's medals in each sport, and location quotient vs all medals in that sport.

    LQ = (country's share of its medals in sport) / (sport's share of all medals). LQ > 1: leans on it.
    """
    tot_c = cs.groupby("country_code")["Total"].transform("sum")
    tot_s = cs["Total"].sum()
    sport_share = cs.groupby(unit)["Total"].transform("sum") / tot_s
    out = cs.copy()
    out["share_of_country_%"] = (out["Total"] / tot_c * 100).round(1)
    out["LQ"] = ((out["Total"] / tot_c) / sport_share).round(2)
    return out.sort_values(["country_code", "Total"], ascending=[True, False])


def country_dependence(cs: pd.DataFrame, unit: str = "sport") -> pd.DataFrame:
    """Per country: medals, sports with a medal, HHI across sports, biggest sport share."""
    rows = []
    for c, x in cs.groupby("country_code"):
        sh = x["Total"] / x["Total"].sum()
        top = x.loc[x["Total"].idxmax()]
        rows.append((c, int(x["Total"].sum()), len(x), round(float((sh**2).sum() * 10000)),
                     top[unit], round(float(sh.max() * 100), 1)))  # fmt: skip
    return pd.DataFrame(
        rows, columns=["country", "medals", f"{unit}s", "HHI", f"top_{unit}", f"top_{unit}_share_%"]
    ).sort_values("medals", ascending=False)


UNDATED = "undated"  # sorts after every ISO date


def _day_label(df: pd.DataFrame) -> pd.Series:
    return df["date"].where(df["date"] != "", UNDATED)


def medal_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Medals decided per day and cumulative, from each medal's own result date (ADR-025).

    A medal whose source gave no date is counted on a final ``undated`` row, never on a guessed day, so
    the last cumulative row always equals the country medal table's total.
    """
    per_day = df.assign(day=_day_label(df)).groupby(["day", "medal"]).size().unstack(fill_value=0)
    for m in ("Gold", "Silver", "Bronze"):
        if m not in per_day:
            per_day[m] = 0
    per_day = per_day[["Gold", "Silver", "Bronze"]].sort_index()
    per_day["Total"] = per_day.sum(axis=1)
    for m in ("Gold", "Silver", "Bronze", "Total"):
        per_day[f"{m}_cum"] = per_day[m].cumsum()
    return per_day.reset_index().rename(columns={"day": "date"})


def country_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative medals per country per day, carried forward over days the country won nothing."""
    labelled = df.assign(day=_day_label(df))
    days = sorted(labelled["day"].unique())
    per = (
        labelled.groupby(["country_code", "day"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=days, fill_value=0)
    )
    out = per.cumsum(axis=1).stack().rename("cumulative").reset_index()
    return out.rename(columns={"day": "date"})
