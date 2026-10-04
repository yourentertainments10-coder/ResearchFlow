"""Country x medal x gender analytics from the official standings feed (ALL/medals/standings).

Pure functions over decoded records. Every number is derived by arithmetic and checked against the
feed's own totals; a mismatch raises instead of being smoothed over.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

MEDALS = {"ME_GOLD": "Gold", "ME_SILVER": "Silver", "ME_BRONZE": "Bronze"}
GENDERS = {"M": "Men", "W": "Women", "X": "Mixed"}


class StandingsError(ValueError):
    """The feed contradicts itself."""


def to_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Long frame: one row per country x medal x gender, validated against the feed's totals."""
    rows = []
    for rec in records:
        count = rec["Count"]
        for mkey, medal in MEDALS.items():
            cell = count[mkey]
            if sum(cell[g] for g in GENDERS) != cell["total"]:
                raise StandingsError(f"{rec['Org']} {medal}: gender split != total")
            for g, gname in GENDERS.items():
                rows.append((rec["Org"], rec["OrgDesc"], medal, gname, int(cell[g])))
        if sum(count[m]["total"] for m in MEDALS) != count["total"]["total"]:
            raise StandingsError(f"{rec['Org']}: G+S+B != total")
    return pd.DataFrame(rows, columns=["code", "country", "medal", "gender", "n"])


def summary(df: pd.DataFrame) -> pd.DataFrame:
    p = df.pivot_table(index=["code", "country"], columns="medal", values="n", aggfunc="sum")
    p = p[["Gold", "Silver", "Bronze"]].astype(int)
    p["Total"] = p.sum(axis=1)
    g = df.pivot_table(index=["code", "country"], columns="gender", values="n", aggfunc="sum")
    p = p.join(g[["Men", "Women", "Mixed"]].astype(int))
    p["Share_%"] = (p["Total"] / p["Total"].sum() * 100).round(2)
    p["Gold_share_%"] = (p["Gold"] / p["Gold"].sum() * 100).round(2)
    p["Women_%"] = (p["Women"] / p["Total"] * 100).round(1)
    p["Men_%"] = (p["Men"] / p["Total"] * 100).round(1)
    p["Mixed_%"] = (p["Mixed"] / p["Total"] * 100).round(1)
    p["Gold_rate_%"] = (p["Gold"] / p["Total"] * 100).round(1)
    p["Points_321"] = p["Gold"] * 3 + p["Silver"] * 2 + p["Bronze"]
    out = p.sort_values(["Gold", "Silver", "Bronze"], ascending=False).reset_index()
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out


def concentration(s: pd.DataFrame) -> dict[str, float]:
    """HHI of medal totals (0-10000), top-N shares and the number of countries with a medal."""
    sh = s["Total"] / s["Total"].sum()
    gs = s["Gold"] / s["Gold"].sum()
    return {
        "countries": int(len(s)),
        "total_medals": int(s["Total"].sum()),
        "hhi_total": round(float((sh**2).sum() * 10000), 1),
        "hhi_gold": round(float((gs**2).sum() * 10000), 1),
        "top1_share_%": round(float(sh.max() * 100), 1),
        "top3_share_%": round(float(sh.nlargest(3).sum() * 100), 1),
        "top5_share_%": round(float(sh.nlargest(5).sum() * 100), 1),
        "top10_share_%": round(float(sh.nlargest(10).sum() * 100), 1),
    }


def gender_totals(df: pd.DataFrame) -> pd.DataFrame:
    t = df.groupby(["gender", "medal"])["n"].sum().unstack()[["Gold", "Silver", "Bronze"]]
    t["Total"] = t.sum(axis=1)
    t["Share_%"] = (t["Total"] / t["Total"].sum() * 100).round(1)
    return t.reset_index()


def gender_leaders(df: pd.DataFrame, top: int = 5) -> dict[str, pd.DataFrame]:
    out = {}
    for g in GENDERS.values():
        x = df[df.gender == g].pivot_table(index=["code", "country"], columns="medal", values="n", aggfunc="sum")
        x = x[["Gold", "Silver", "Bronze"]].astype(int)
        x["Total"] = x.sum(axis=1)
        out[g] = x.sort_values(["Gold", "Silver", "Bronze"], ascending=False).head(top).reset_index()
    return out
