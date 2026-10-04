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


def country_sport(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["country_code", "discipline_name", "medal"]).size().unstack(fill_value=0)
    for m in ("Gold", "Silver", "Bronze"):
        if m not in g:
            g[m] = 0
    g = g[["Gold", "Silver", "Bronze"]]
    g["Total"] = g.sum(axis=1)
    return g.reset_index().rename(columns={"discipline_name": "sport"})


def sport_concentration(cs: pd.DataFrame) -> pd.DataFrame:
    """Per sport: medals, countries with a medal, HHI of country shares, top-country share."""
    rows = []
    for sport, x in cs.groupby("sport"):
        sh = x["Total"] / x["Total"].sum()
        top = x.loc[x["Total"].idxmax()]
        rows.append((sport, int(x["Total"].sum()), len(x), round(float((sh**2).sum() * 10000)),
                     top.country_code, round(float(sh.max() * 100), 1)))  # fmt: skip
    return pd.DataFrame(
        rows, columns=["sport", "medals", "countries", "HHI", "leader", "leader_share_%"]
    )


def specialisation(cs: pd.DataFrame) -> pd.DataFrame:
    """Share of each country's medals in each sport, and location quotient vs all medals in that sport.

    LQ = (country's share of its medals in sport) / (sport's share of all medals). LQ > 1: leans on it.
    """
    tot_c = cs.groupby("country_code")["Total"].transform("sum")
    tot_s = cs["Total"].sum()
    sport_share = cs.groupby("sport")["Total"].transform("sum") / tot_s
    out = cs.copy()
    out["share_of_country_%"] = (out["Total"] / tot_c * 100).round(1)
    out["LQ"] = ((out["Total"] / tot_c) / sport_share).round(2)
    return out.sort_values(["country_code", "Total"], ascending=[True, False])


def country_dependence(cs: pd.DataFrame) -> pd.DataFrame:
    """Per country: medals, sports with a medal, HHI across sports, biggest sport share."""
    rows = []
    for c, x in cs.groupby("country_code"):
        sh = x["Total"] / x["Total"].sum()
        top = x.loc[x["Total"].idxmax()]
        rows.append((c, int(x["Total"].sum()), len(x), round(float((sh**2).sum() * 10000)),
                     top.sport, round(float(sh.max() * 100), 1)))  # fmt: skip
    return pd.DataFrame(
        rows, columns=["country", "medals", "sports", "HHI", "top_sport", "top_sport_share_%"]
    ).sort_values("medals", ascending=False)
