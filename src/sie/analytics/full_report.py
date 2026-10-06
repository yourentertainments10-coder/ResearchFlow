"""Full event-level report: load a portal capture, validate, analyse, write HTML + Excel + CSV."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from sie.analytics.events import (
    country_dependence,
    country_sport,
    country_timeline,
    medal_timeline,
    reconcile,
    reconcile_official_table,
    specialisation,
    sport_concentration,
)
from sie.analytics.facts import analysis_frame, country_gender_frame
from sie.analytics.standings import concentration, gender_totals, summary

PLACING_COLUMNS = [
    "discipline", "discipline_name", "sport", "event_id", "event_name", "gender", "medal", "slot",
    "country_code", "country_name", "source_country", "date",
]  # fmt: skip
SOURCE = "Official results portal (medals/discipline and medals/standings per discipline, AG2026)"


def load(facts: pd.DataFrame, reference: Path, disc_list: Path, all_standings: Path) -> dict:
    """Analysis frame from ``v_medal_facts`` plus the official tables used only as references.

    ``reference`` is the portal capture file: its per-discipline standings and capture time are the
    independent check, not the source of any medal count.
    """
    cap = json.loads(reference.read_text())
    disciplines = json.loads(disc_list.read_text())
    df = analysis_frame(facts, {d["DiscDesc"]: d["Disc"] for d in disciplines})
    official_events = Counter(d["Disc"] for d in disciplines for _ in d["Events"])
    mine = df.groupby("discipline")["event_id"].nunique().to_dict()
    checks = {
        "rows": len(df),
        "reconcile_mismatches": len(reconcile(df, cap["standings"])),
        "official_table_mismatches": len(
            reconcile_official_table(df, json.loads(all_standings.read_text()))
        ),
        "events_in_rows": int(df["event_id"].nunique()),
        "events_official": sum(official_events.values()),
        "disciplines_event_count_mismatch": sum(
            1
            for k in set(official_events) | set(mine)
            if official_events.get(k, 0) != mine.get(k, 0)
        ),
    }
    all_rec = json.loads(all_standings.read_text())
    off = {r["Org"]: r["Count"]["total"]["total"] for r in all_rec}
    ours = df.groupby("country_code").size().to_dict()
    checks["country_total_diffs"] = sum(
        1 for k in set(off) | set(ours) if off.get(k, 0) != ours.get(k, 0)
    )
    return {
        "df": df,
        "captured": cap["at"],
        "checks": checks,
        "all_records": all_rec,
        "standings": cap["standings"],
    }


def _tbl(frame: pd.DataFrame, limit: int | None = None) -> str:
    f = frame.head(limit) if limit else frame
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in f.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>"
        for r in f.itertuples(index=False)
    )
    return f"<div class=wrap><table><tr>{head}</tr>{body}</table></div>"


GENDER_CATEGORIES = ("Men", "Women", "Mixed", "Open")


def gender_tables(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Medals by country x gender and by sport x gender. Open stays its own category (ADR-019);
    only the reconciliation against the official table folds it into that table's Mixed bucket."""

    def table(keys: list[str]) -> pd.DataFrame:
        t = df.groupby([*keys, "gender"]).size().unstack(fill_value=0)
        for g in GENDER_CATEGORIES:
            if g not in t:
                t[g] = 0
        t = t[[*GENDER_CATEGORIES]]
        t["Total"] = t.sum(axis=1)
        t["Women_%"] = (t["Women"] / t["Total"] * 100).round(1)
        return t.sort_values("Total", ascending=False).reset_index()

    cg = table(["country_code"])
    sg = table(["sport"])
    return cg, sg


def build(data: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df, checks = data["df"], data["checks"]
    if (
        checks["reconcile_mismatches"]
        or checks["official_table_mismatches"]
        or checks["country_total_diffs"]
    ):
        raise ValueError(f"data does not reconcile: {checks}")
    cs = country_sport(df)
    official_view = country_gender_frame(df, fold_open=True)  # the official table's 3 buckets
    country = summary(official_view)
    conc = concentration(country)
    sport_c = sport_concentration(cs).sort_values("medals", ascending=False)
    dep = country_dependence(cs)
    spec = specialisation(cs)
    spec_top = spec[spec["Total"] >= 5].sort_values("LQ", ascending=False).head(25)
    names = dict(zip(df["country_code"], df["country_name"], strict=True))
    for f in (dep, spec_top, cs):
        f.insert(1, "name", f.iloc[:, 0].map(names)) if "name" not in f else None
    cg, sg = gender_tables(df)
    sport_c = sport_c.merge(
        df.groupby("sport")["event_id"].nunique().rename("events").reset_index(), on="sport"
    )
    # The source's own 59 disciplines are kept next to the 49 official sports (ADR-020).
    csd = country_sport(df, "discipline")
    csd.insert(1, "name", csd["country_code"].map(names))
    disc_c = sport_concentration(csd, "discipline").sort_values("medals", ascending=False)
    disc_c = disc_c.merge(
        df.groupby("discipline_name")["event_id"]
        .nunique()
        .rename("events")
        .reset_index()
        .rename(columns={"discipline_name": "discipline"}),
        on="discipline",
    )
    timeline = medal_timeline(df)
    checks["undated_medals"] = int((df["date"] == "").sum())
    checks["timeline_total"] = int(timeline["Total_cum"].iloc[-1]) if len(timeline) else 0
    if checks["timeline_total"] != len(df):
        raise ValueError(f"timeline does not end at the country table total: {checks}")
    gt = gender_totals(official_view)
    sweeps = (
        df.groupby(["sport", "event_id", "country_code"]).size().reset_index(name="n").query("n>=2")
        .groupby("country_code").size().sort_values(ascending=False).head(10).rename("podium_sweeps_or_double_podiums").reset_index()
    )  # fmt: skip

    xlsx = out_dir / "asian_games_2026_full_analysis.xlsx"
    sheets = {
        "Checks": pd.DataFrame(list(checks.items()), columns=["check", "value"]),
        "Country": country, "Country x sport": cs, "Sport concentration": sport_c,
        "Country dependence": dep, "Specialisation LQ": spec, "Country x gender": cg,
        "Sport x gender": sg, "Country x discipline": csd, "Discipline concentration": disc_c,
        "Medal timeline": timeline, "Country timeline": country_timeline(df), "Gender totals": gt, "Concentration": pd.DataFrame(list(conc.items()), columns=["metric", "value"]),
        "Placings": df[PLACING_COLUMNS],
    }  # fmt: skip
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        for n, f in sheets.items():
            f.to_excel(w, sheet_name=n[:31], index=False)
    names = df.drop_duplicates("discipline").set_index("discipline")["discipline_name"].to_dict()
    ours = df.groupby("discipline").size().to_dict()
    validation = [
        {
            "code": code,
            "sport": names[code],
            "ours": int(ours[code]),
            "official": int(sum(r["Count"]["total"]["total"] for r in recs)),
        }
        for code, recs in sorted(data["standings"].items(), key=lambda kv: names[kv[0]])
    ]
    meta = {
        "captured_at": data["captured"],
        "checks": {k: int(v) for k, v in checks.items()},
        "validation": validation,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    df[PLACING_COLUMNS].to_csv(out_dir / "placings.csv", index=False)

    cards = "".join(
        f"<div class=card><span>{k.replace('_', ' ')}</span><b>{v}</b></div>"
        for k, v in {**{"medal events": checks["events_in_rows"], "country medals": checks["rows"], "sports": df["sport"].nunique(), "disciplines": df["discipline"].nunique()}, **conc}.items()
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
<p><small>Source: {SOURCE}. Captured {data["captured"]}. Validation: {ok}; {checks["events_in_rows"]} events found vs {checks["events_official"]} on the official programme ({checks["disciplines_event_count_mismatch"]} disciplines differ); {checks["country_total_diffs"]} country total differences.</small></p>
<div class=cards>{cards}</div>
<h2>Country medal table</h2>{_tbl(country[["Rank", "country", "Gold", "Silver", "Bronze", "Total", "Share_%", "Gold_rate_%", "Women_%", "Points_321"]])}
<h2>How dependent is each country on a few sports? (top 20 by medals)</h2><small>HHI across sports (10000 = all medals in one sport).</small>{_tbl(dep, 20)}
<h2>Where each country over-performs (location quotient, at least 5 medals)</h2><small>LQ above 1: the sport weighs more in the country's haul than in the Games overall.</small>{_tbl(spec_top)}
<h2>Sport concentration: who dominates each sport</h2>{_tbl(sport_c)}
<h2>Women's, men's and mixed medals by country</h2><small>Gender is the event's gender. Open events (esports, equestrian, one artistic swimming and one taekwondo event) have their own column here; the official medal table counts them under Mixed.</small>{_tbl(cg, 25)}
<h2>Women's share by sport</h2>{_tbl(sg)}
<h2>Disciplines (the source's finer split of the 49 sports)</h2>{_tbl(disc_c)}
<h2>Countries with most events where they took two or more podium places</h2>{_tbl(sweeps)}
</html>"""  # noqa: E501
    page_path = out_dir / "asian_games_2026_full_analysis.html"
    page_path.write_text(page, encoding="utf-8")
    return {"xlsx": xlsx, "html": page_path, "csv": out_dir / "placings.csv"}


def main() -> None:  # python -m sie.analytics.full_report CAPTURE (official standings reference)
    import sys

    from sie.analytics.facts import competition_id, load_medal_facts
    from sie.config import get_settings
    from sie.db.session import make_engine

    settings = get_settings()
    root = Path(__file__).resolve().parents[3]
    fx = root / "tests/fixtures/sources/bornan"
    with make_engine(settings).connect() as conn:  # medals come from the database, never the file
        facts = load_medal_facts(conn, competition_id(conn, settings.competition_id))
    out = build(
        load(
            facts,
            Path(sys.argv[1]),
            fx / "ALL_disc_data.trimmed.json",
            fx / "ALL_medals_standings.decoded.json",
        ),
        root / "reports",
    )
    print(out)


if __name__ == "__main__":
    main()
