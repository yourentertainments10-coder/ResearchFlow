"""A self-contained HTML report of a session: no script, no external file, every text escaped.

Only verified analyses appear with their numbers. An analysis that failed its independent check is named
in a separate section with the reason and no data, matching what the directory writer exports.
Deterministic: no clock, no environment; the same session gives the same bytes.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

import pandas as pd

from sie.workspace.charts import display_number
from sie.workspace.models import AnalysisItem
from sie.workspace.session import Session

REPORT_ROWS = 500  # table rows shown per analysis in the report; the CSV export has all of them

_CSS = (
    ":root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--mut:#59636e;--line:#d0d7de;--card:#f6f8fa}"
    "@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#9198a1;"
    "--line:#30363d;--card:#161b22}}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}"
    "main{max-width:60rem;margin:0 auto;padding:1rem}"
    "section{border:1px solid var(--line);border-radius:8px;background:var(--card);"
    "padding:1rem;margin:1rem 0}"
    "h1,h2,h3{line-height:1.2}table{border-collapse:collapse;width:100%;font-size:.9rem}"
    "th,td{border-bottom:1px solid var(--line);padding:.25rem .5rem;text-align:left}"
    "td.n{text-align:right;font-variant-numeric:tabular-nums}"
    "pre{overflow:auto;background:var(--bg);border:1px solid var(--line);padding:.5rem}"
    ".mut{color:var(--mut)}.warn{font-weight:600}img{max-width:100%;height:auto}"
    "@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}"
)


def svg_data_uri(svg: str) -> str:
    """An SVG as an ``<img>``-safe data URI (an image cannot run script)."""
    import base64

    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _cell(value: Any) -> tuple[str, bool]:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return "", False
    if isinstance(value, bool):
        return str(value), False
    if isinstance(value, int | float):
        return display_number(value), True
    return str(value), False


def table_html(table: pd.DataFrame, limit: int = REPORT_ROWS) -> str:
    head = "".join(f"<th>{escape(str(c))}</th>" for c in table.columns)
    body = []
    for _, row in table.head(limit).iterrows():
        cells = []
        for value in row:
            text, numeric = _cell(value)
            cells.append(
                f'<td class="n">{escape(text)}</td>' if numeric else f"<td>{escape(text)}</td>"
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    note = (
        f'<p class="mut">First {limit} of {len(table)} rows; the CSV export has all of them.</p>'
        if len(table) > limit
        else ""
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>{note}"


def _analysis_section(item: AnalysisItem, request: str | None) -> str:
    p = item.planned
    parts = [f"<h3>{escape(p.title)}</h3>"]
    if request:
        parts.append(f'<p class="mut">Request: {escape(request)}</p>')
    parts.append(
        f'<p class="mut">Verified by an independent calculation ({item.verification.checks} checks). '
        f"Result hash <code>{escape(item.result.provenance.result_sha256)}</code>.</p>"
    )
    if item.svg:
        parts.append(f'<img alt="{escape(p.title, quote=True)}" src="{svg_data_uri(item.svg)}">')
    parts.append(table_html(item.result.table))
    spec = json.dumps(p.spec.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False)
    parts.append(
        f"<details><summary>Definition (operation spec)</summary><pre>{escape(spec)}</pre></details>"
    )
    for warning in item.result.warnings:
        parts.append(f'<p class="warn">Warning: {escape(str(warning))}</p>')
    return "<section>" + "".join(parts) + "</section>"


def build_report(session: Session) -> str:
    ws = session.workspace
    requests = {
        s.analysis_id: s.request for s in session.steps if s.analysis_id and s.kind == "plan"
    }
    shown = [*ws.items, *session.items]
    good = [i for i in shown if i.exportable]
    bad = [i for i in shown if not i.exportable]
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
        "style-src 'unsafe-inline'\">",
        f"<title>Analysis report: {escape(ws.source.name)}</title><style>{_CSS}</style></head><body><main>",
        f'<h1>Analysis report</h1><p class="mut">Source file {escape(ws.source.name)}, '
        f"SHA-256 <code>{escape(ws.source.sha256)}</code>. {ws.profile.rows} rows, "
        f"{ws.profile.columns} columns.</p>",
    ]
    if ws.cards:
        out.append("<section><h2>Key figures</h2><table><tbody>")
        out += [
            f'<tr><th>{escape(c.label)}</th><td class="n">{escape(c.value)}</td>'
            f'<td class="mut">{escape(c.detail)}</td></tr>'
            for c in ws.cards
            if c.verified
        ]
        out.append("</tbody></table></section>")
    if ws.findings:
        out.append("<section><h2>Findings</h2><ul>")
        out += [f"<li>{escape(f.text)}</li>" for f in ws.findings]
        out.append("</ul></section>")
    out.append("<h2>Analyses</h2>")
    out += [_analysis_section(i, requests.get(i.planned.id)) for i in good]
    if bad:
        out.append("<section><h2>Not included (failed verification or could not run)</h2><ul>")
        for i in bad:
            why = (
                i.error
                or i.verification.reason
                or "; ".join(i.verification.mismatches[:3])
                or "not verified"
            )
            out.append(f"<li>{escape(i.planned.title)}: {escape(why)}</li>")
        out.append("</ul></section>")
    if session.steps:
        out.append("<section><h2>Request history</h2><ol>")
        for s in session.steps:
            out.append(f"<li>{escape(s.request)} &rarr; {escape(s.kind)}: {escape(s.message)}</li>")
        out.append("</ol></section>")
    out.append("<section><h2>Limitations</h2><ul>")
    out += [f"<li>{escape(t)}</li>" for t in ws.limitations]
    out.append("</ul></section></main></body></html>")
    return "\n".join(out) + "\n"
