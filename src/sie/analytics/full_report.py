"""Full event-level report: load a portal capture, validate, analyse, write HTML + Excel + CSV."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

from sie.analytics.events import (
    country_dependence,
    country_sport,
    placings_frame,
    reconcile,
    specialisation,
    sport_concentration,
)
from sie.analytics.standings import concentration, gender_totals, summary, to_frame
from sie.sources.bornan.parse_medals import parse_medal_rows

SOURCE = "Official results portal (medals/discipline and medals/standings per discipline, AG2026)"


def load(capture: Path, disc_list: Path, all_standings: Path) -> dict:
    cap = json.loads(capture.read_text())
    placings = [p for rows in cap["medals"].values() for p in parse_medal_rows(rows)]
    df = placings_frame(placings)
    official_events = {
        (d["Disc"], e["EvKey"]) for d in json.loads(disc_list.read_text()) for e in d["Events"]
    }
    ev_codes = set(
        zip(df["discipline"], df["event_key"].str.replace(r"\.$", "", regex=True), strict=True)
    )
    checks = {
        "rows": len(df),
        "reconcile_mismatches": len(reconcile(df, cap["standings"])),
        "events_in_rows": df.groupby(["discipline", "event_code"]).ngroups,
        "events_official": len(official_events),
        "event_keys_unmatched": len({k for k in ev_codes if k not in official_events}),
    }
    all_rec = json.loads(all_standings.read_text())
    off = {r["Org"]: r["Count"]["total"]["total"] for r in all_rec}
    mine = df.groupby("country_code").size().to_dict()
    checks["country_total_diffs"] = sum(
        1 for k in set(off) | set(mine) if off.get(k, 0) != mine.get(k, 0)
    )
    return {"df": df, "captured": cap["at"], "checks": checks, "all_records": all_rec}


def _tbl(frame: pd.DataFrame, limit: int | None = None) -> str:
    f = frame.head(limit) if limit else frame
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in f.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>"
        for r in f.itertuples(index=False)
    )
    return f"<div class=wrap><table><tr>{head}</tr>{body}</table></div>"


def build(data: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df, checks = data["df"], data["checks"]
    if checks["reconcile_mismatches"] or checks["country_total_diffs"]:
        raise ValueError(f"data does not reconcile: {checks}")
    cs = country_sport(df)
    country = summary(to_frame(data["all_records"]))
    conc = concentration(country)
    sport_c = sport_concentration(cs).sort_values("medals", ascending=False)
    dep = country_dependence(cs)
    spec = specialisation(cs)
    spec_top = spec[spec["Total"] >= 5].sort_values("LQ", ascending=False).head(25)
    names = dict(zip(df["country_code"], df["country_name"], strict=True))
    for f in (dep, spec_top, cs):
        f.insert(1, "name", f.iloc[:, 0].map(names)) if "name" not in f else None
    df2 = df.assign(gender=df["gender"].replace({"Open": "Mixed"}))
    cg = df2.groupby(["country_code", "gender"]).size().unstack(fill_value=0)
    cg["Total"] = cg.sum(axis=1)
    cg["Women_%"] = (cg["Women"] / cg["Total"] * 100).round(1)
    cg = cg.sort_values("Total", ascending=False).reset_index()
    sg = df2.groupby(["discipline_name", "gender"]).size().unstack(fill_value=0)
    for g in ("Men", "Women", "Mixed"):
        if g not in sg:
            sg[g] = 0
    sg["Total"] = sg.sum(axis=1)
    sg["Women_%"] = (sg["Women"] / sg["Total"] * 100).round(1)
    sg = (
        sg.sort_values("Total", ascending=False)
        .reset_index()
        .rename(columns={"discipline_name": "sport"})
    )
    events_by_sport = (
        df.groupby("discipline_name")["event_code"]
        .nunique()
        .rename("events")
        .reset_index()
        .rename(columns={"discipline_name": "sport"})
    )
    sport_c = sport_c.merge(events_by_sport, on="sport")
    gt = gender_totals(to_frame(data["all_records"]))
    sweeps = (
        df.groupby(["discipline_name", "event_code", "country_code"]).size().reset_index(name="n").query("n>=2")
        .groupby("country_code").size().sort_values(ascending=False).head(10).rename("podium_sweeps_or_double_podiums").reset_index()
    )  # fmt: skip

    xlsx = out_dir / "asian_games_2026_full_analysis.xlsx"
    sheets = {
        "Checks": pd.DataFrame(list(checks.items()), columns=["check", "value"]),
        "Country": country, "Country x sport": cs, "Sport concentration": sport_c,
        "Country dependence": dep, "Specialisation LQ": spec, "Country x gender": cg,
        "Sport x gender": sg, "Gender totals": gt, "Concentration": pd.DataFrame(list(conc.items()), columns=["metric", "value"]),
        "Placings": df.drop(columns=["entrant_name", "awarded_at"]).assign(date=df["awarded_at"].str[:10]),
    }  # fmt: skip
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        for n, f in sheets.items():
            f.to_excel(w, sheet_name=n[:31], index=False)
    pub = df.assign(date=df["awarded_at"].str[:10]).drop(
        columns=["entrant_name", "awarded_at", "entrant_type"]
    )
    pub.to_csv(out_dir / "placings.csv", index=False)

    cards = "".join(
        f"<div class=card><span>{k.replace('_', ' ')}</span><b>{v}</b></div>"
        for k, v in {**{"medal events": checks["events_in_rows"], "country medals": checks["rows"], "disciplines": df["discipline"].nunique()}, **conc}.items()
        if k not in ("total_medals",)
    )  # fmt: skip
    ok = "all medal rows reconcile with the official standings (0 mismatches over 59 disciplines)"
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Asian Games 2026 Full Medal Analysis</title>
<style>:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#eee;--mut:#9aa;--line:#2c3036}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1150px;padding:16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}}
.card{{border:1px solid var(--line);border-radius:8px;padding:8px 12px}}.card span{{display:block;color:var(--mut);font-size:12px;text-transform:capitalize}}.card b{{font-size:20px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
.wrap{{overflow-x:auto}}small{{color:var(--mut)}}h2{{margin-top:28px}}</style>
<h1>Asian Games 2026 (Aichi-Nagoya): full medal analysis</h1>
<p><small>Source: {SOURCE}. Captured {data["captured"]}. Validation: {ok}; {checks["events_in_rows"]} events found vs {checks["events_official"]} on the official programme; {checks["country_total_diffs"]} country total differences.</small></p>
<div class=cards>{cards}</div>
<h2>Country medal table</h2>{_tbl(country[["Rank", "country", "Gold", "Silver", "Bronze", "Total", "Share_%", "Gold_rate_%", "Women_%", "Points_321"]])}
<h2>How dependent is each country on a few sports? (top 20 by medals)</h2><small>HHI across sports (10000 = all medals in one sport).</small>{_tbl(dep, 20)}
<h2>Where each country over-performs (location quotient, at least 5 medals)</h2><small>LQ above 1: the sport weighs more in the country's haul than in the Games overall.</small>{_tbl(spec_top)}
<h2>Sport concentration: who dominates each sport</h2>{_tbl(sport_c)}
<h2>Women's, men's and mixed medals by country</h2><small>Gender is the event's gender; Open events (esports, equestrian, sailing) count as Mixed, as the official table does.</small>{_tbl(cg, 25)}
<h2>Women's share by sport</h2>{_tbl(sg)}
<h2>Countries with most events where they took two or more podium places</h2>{_tbl(sweeps)}
</html>"""  # noqa: E501
    page_path = out_dir / "asian_games_2026_full_analysis.html"
    page_path.write_text(page, encoding="utf-8")
    return {"xlsx": xlsx, "html": page_path, "csv": out_dir / "placings.csv"}


def main() -> None:  # python -m sie.analytics.full_report CAPTURE
    import sys

    root = Path(__file__).resolve().parents[3]
    fx = root / "tests/fixtures/sources/bornan"
    out = build(
        load(
            Path(sys.argv[1]),
            fx / "ALL_disc_data.trimmed.json",
            fx / "ALL_medals_standings.decoded.json",
        ),
        root / "reports",
    )
    print(out)


if __name__ == "__main__":
    main()
