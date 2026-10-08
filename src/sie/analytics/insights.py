"""Rule-based insight sentences (docs/ANALYTICS_SPEC.md section 12). No LLM, no free text.

Every sentence is a template filled from the metric tables, and carries the numbers it used in
``evidence`` so a later explanation step can lock them. A country with fewer than 3 medals gets no
sentence (small sample, spec 11).
"""

from __future__ import annotations

import json

import pandas as pd

from sie.analytics.metrics import SMALL_SAMPLE, SPECIALISED_RCA, TAG_MIN_MEDALS

SPECIALISATIONS_PER_COUNTRY = 2
COLUMNS = ["country_code", "country", "kind", "text", "evidence"]


def build_insights(
    *,
    country_sport: pd.DataFrame,
    concentration: pd.DataFrame,
    rca: pd.DataFrame,
    gender_summary: pd.DataFrame,
    women_country_sport: pd.DataFrame,
    unit: str = "sport",
) -> pd.DataFrame:
    rows: list[dict] = []
    top_unit = country_sport.sort_values(
        ["country_code", "Total", unit], ascending=[True, False, True]
    ).drop_duplicates("country_code")
    top = top_unit.set_index("country_code")
    for c in concentration.itertuples(index=False):
        if c.Total < SMALL_SAMPLE:
            continue
        t = top.loc[c.country_code]
        share = t["share_of_country"]
        text = (
            f"{c.country}: {share:.0%} of medals come from {t[unit]}; "
            f"effective number of {unit}s is {c.effective_units:.1f} ({c.label})."
        )
        rows.append(
            _row(
                c,
                "dependence",
                text,
                {unit: t[unit], "share": share, "effective": c.effective_units, "label": c.label},
            )
        )

    names = dict(zip(concentration["country_code"], concentration["country"], strict=True))
    ok = rca[(rca["Total"] >= TAG_MIN_MEDALS) & (rca["rca"] >= SPECIALISED_RCA)]
    for code, x in ok.sort_values(
        ["country_code", "rca", unit], ascending=[True, False, True]
    ).groupby("country_code"):
        for r in x.head(SPECIALISATIONS_PER_COUNTRY).itertuples(index=False):
            text = (
                f"{names[code]} specialises in {getattr(r, unit)} (RCA {r.rca:.1f}); "
                f"{r.Gold} golds from {r.Total} medals."
            )
            rows.append(
                {
                    "country_code": code,
                    "country": names[code],
                    "kind": "specialisation",
                    "text": text,
                    "evidence": json.dumps(
                        {
                            unit: getattr(r, unit),
                            "rca": r.rca,
                            "gold": int(r.Gold),
                            "total": int(r.Total),
                        },
                        sort_keys=True,
                        default=float,
                    ),
                }
            )

    led = (
        women_country_sport.sort_values(
            ["country_code", "Points", "Total", unit], ascending=[True, False, False, True]
        )
        .drop_duplicates("country_code")
        .set_index("country_code")[unit]
    )
    for g in gender_summary.itertuples(index=False):
        if g.Total < SMALL_SAMPLE or g.Women_medals == 0 or g.country_code not in led.index:
            continue
        led_by = led[g.country_code]
        text = f"{g.country}'s women contribute {g.women_share:.0%} of medals, led by {led_by}."
        rows.append(
            {
                "country_code": g.country_code,
                "country": g.country,
                "kind": "women",
                "text": text,
                "evidence": json.dumps(
                    {"women_share": g.women_share, "led_by": led_by, "women": int(g.Women_medals)},
                    sort_keys=True,
                    default=float,
                ),
            }
        )
    out = pd.DataFrame(rows, columns=COLUMNS)
    return out.sort_values(["country_code", "kind", "text"]).reset_index(drop=True)


def _row(c, kind: str, text: str, evidence: dict) -> dict:
    return {
        "country_code": c.country_code,
        "country": c.country,
        "kind": kind,
        "text": text,
        "evidence": json.dumps(evidence, sort_keys=True, default=float),
    }
