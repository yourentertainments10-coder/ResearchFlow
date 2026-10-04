"""Build the Excel and HTML report from the official standings feed."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from sie.analytics.standings import concentration, gender_leaders, gender_totals, summary, to_frame

SOURCE = "Official results portal, ALL/medals/standings (AG2026)"


def build(records: list[dict], out_dir: Path, captured: str = "2026-10-04") -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = to_frame(records)
    s = summary(df)
    c = concentration(s)
    gt = gender_totals(df)
    leaders = gender_leaders(df)

    xlsx = out_dir / "asian_games_2026_country_analysis.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        s.to_excel(w, sheet_name="Country summary", index=False)
        df.to_excel(w, sheet_name="Country x medal x gender", index=False)
        gt.to_excel(w, sheet_name="Gender totals", index=False)
        pd.DataFrame(list(c.items()), columns=["metric", "value"]).to_excel(
            w, sheet_name="Concentration", index=False
        )
        for g, t in leaders.items():
            t.to_excel(w, sheet_name=f"Top {g}", index=False)
        pd.DataFrame(
            {
                "note": [
                    f"Source: {SOURCE}",
                    f"Captured: {captured}",
                    "Event-level (country x sport) analysis needs the per-sport feeds; not in this file.",
                ]
            }
        ).to_excel(w, sheet_name="About", index=False)

    mx = int(s["Total"].max())
    rows = "".join(
        f"<tr><td>{r.Rank}</td><td>{html.escape(r.country)} ({r.code})</td><td>{r.Gold}</td><td>{r.Silver}</td>"
        f"<td>{r.Bronze}</td><td><b>{r.Total}</b></td><td class=bar><i style='width:{r.Total / mx * 100:.0f}%'></i></td>"
        f"<td>{r.share}</td><td>{r.gshare}</td><td>{r.Mixed}</td><td>{r.Women_pct}</td></tr>"
        for r in s.rename(
            columns={"Share_%": "share", "Gold_share_%": "gshare", "Women_%": "Women_pct"}
        ).itertuples()
    )
    gt_rows = "".join(
        f"<tr><td>{r.gender}</td><td>{r.Gold}</td><td>{r.Silver}</td><td>{r.Bronze}</td><td>{r.Total}</td><td>{r.share}%</td></tr>"
        for r in gt.rename(columns={"Share_%": "share"}).itertuples()
    )
    cards = "".join(
        f"<div class=card><span>{k.replace('_', ' ')}</span><b>{v}</b></div>" for k, v in c.items()
    )
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Asian Games 2026 Medal Analysis</title>
<style>:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--ac:#2b6cb0}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#eee;--mut:#9aa;--line:#2c3036;--ac:#63a4e8}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}}
.card{{border:1px solid var(--line);border-radius:8px;padding:8px 12px}}.card span{{display:block;color:var(--mut);font-size:12px;text-transform:capitalize}}.card b{{font-size:20px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right}}th:nth-child(2),td:nth-child(2){{text-align:left}}
.bar{{width:18%}}.bar i{{display:block;height:10px;background:var(--ac);border-radius:3px}}.wrap{{overflow-x:auto}}small{{color:var(--mut)}}</style>
<h1>Asian Games 2026 (Aichi-Nagoya): country medal analysis</h1>
<p><small>Source: {SOURCE}. Captured {captured}. All figures computed by arithmetic and cross-checked against the feed's own totals (gender split = total, G+S+B = total).</small></p>
<div class=cards>{cards}</div>
<h2>Medals by gender of event</h2><div class=wrap><table><tr><th>Event gender<th>Gold<th>Silver<th>Bronze<th>Total<th>Share</tr>{gt_rows}</table></div>
<h2>Country table</h2><div class=wrap><table><tr><th>#<th>Country<th>G<th>S<th>B<th>Total<th><th>Share %<th>Gold share %<th>Mixed<th>Women %</tr>{rows}</table></div>
<p><small>Concentration: HHI of 1000-1800 is moderately concentrated; higher means a few countries dominate. Event-level (country x sport) analysis follows once per-sport feeds are loaded.</small></p></html>"""
    page_path = out_dir / "asian_games_2026_country_analysis.html"
    page_path.write_text(page, encoding="utf-8")
    return {"xlsx": xlsx, "html": page_path}
