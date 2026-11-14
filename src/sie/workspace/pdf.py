"""The report as a PDF, with no PDF library: plain text in the built-in Courier fonts (so line widths are
exact and tables line up), charts drawn as vector shapes from the same scene as the PNG, and a hand-
written file structure (objects, cross-reference table, zlib streams). Deterministic: the same session
gives the same bytes (no dates, no random ids). Only analyses that passed their independent check are
included, exactly as in the HTML report. Characters outside ASCII are shown as '?' (accents are dropped).
"""

from __future__ import annotations

import textwrap
import unicodedata
import zlib
from typing import TYPE_CHECKING, Any

from sie.workspace.charts import display_number
from sie.workspace.scene import Scene, chart_scene

if TYPE_CHECKING:
    from sie.workspace.session import Session

PAGE_W, PAGE_H = 595, 842
MARGIN = 48
FONT = 9.0
LEAD = 12.0
CHAR_W = 0.6  # Courier: every character is 0.6 of the font size wide
LINE_CHARS = int((PAGE_W - 2 * MARGIN) / (CHAR_W * FONT))
TABLE_ROWS = 25
CHART_W = 420.0


def _ascii(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in s)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rgb(c: tuple[int, int, int]) -> str:
    return " ".join(f"{v / 255:.3f}" for v in c)


class _Doc:
    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = PAGE_H - MARGIN

    def _room(self, need: float) -> None:
        if self.y - need < MARGIN + 16:
            self.pages.append([])
            self.y = PAGE_H - MARGIN

    def line(self, text: str, bold: bool = False, size: float = FONT) -> None:
        lead = size * LEAD / FONT
        self._room(lead)
        self.y -= lead
        font = "F2" if bold else "F1"
        self.pages[-1].append(
            f"BT /{font} {size:.1f} Tf {MARGIN} {self.y:.2f} Td ({_esc(text)}) Tj ET"
        )

    def para(self, text: Any, bold: bool = False, indent: int = 0) -> None:
        prefix = " " * indent
        for ln in textwrap.wrap(
            _ascii(text), LINE_CHARS - indent, break_long_words=True, break_on_hyphens=False
        ) or [""]:
            self.line(prefix + ln, bold)

    def gap(self, h: float = LEAD / 2) -> None:
        self.y -= h

    def heading(self, text: str, level: int = 1) -> None:
        self.gap(LEAD)
        self._room(40)
        size = 15.0 if level == 0 else 12.0 if level == 1 else 10.0
        self.line(_ascii(text)[: int(LINE_CHARS * FONT / size)], True, size)
        self.gap(2)

    def bullets(self, items: list[str]) -> None:
        for t in items:
            lines = textwrap.wrap(
                _ascii(t), LINE_CHARS - 4, break_long_words=True, break_on_hyphens=False
            ) or [""]
            self.line("- " + lines[0])
            for ln in lines[1:]:
                self.line("  " + ln)

    def table(self, columns: list[str], rows: list[list[str]], note: str = "") -> None:
        cols = [_ascii(c) for c in columns]
        body = [[_ascii(c) for c in r] for r in rows]
        widths = [min(20, max([len(c)] + [len(r[i]) for r in body])) for i, c in enumerate(cols)]
        keep, used = 0, 0
        for w in widths:
            if used + w + (2 if keep else 0) > LINE_CHARS:
                break
            used += w + (2 if keep else 0)
            keep += 1
        keep = max(keep, 1)
        natural = max([len(cols[keep - 1])] + [len(r[keep - 1]) for r in body])
        widths[keep - 1] = min(natural, widths[keep - 1] + max(0, LINE_CHARS - used))

        def fmt(cells: list[str]) -> str:
            return "  ".join(
                c[:w].ljust(w) for c, w in zip(cells[:keep], widths[:keep], strict=False)
            ).rstrip()

        self._room(LEAD * min(len(body) + 1, 4))  # a header never sits alone at the page bottom
        self.line(fmt(cols), bold=True)
        for r in body:
            self.line(fmt(r))
        if keep < len(cols):
            self.line(f"({len(cols) - keep} more columns are in the CSV export)")
        if note:
            self.line(note)

    def chart(self, scene: Scene) -> None:
        f = CHART_W / scene.width
        h = scene.height * f
        self._room(h + 8)
        top = self.y
        ox = MARGIN
        ops: list[str] = ["q"]

        def X(x: float) -> float:
            return ox + x * f

        def Y(y: float) -> float:
            return top - y * f

        for it in scene.items:
            k = it[0]
            if k == "rect":
                _, x, y, w, hh, c = it
                ops.append(f"{_rgb(c)} rg {X(x):.2f} {Y(y + hh):.2f} {w * f:.2f} {hh * f:.2f} re f")
            elif k == "line":
                _, x1, y1, x2, y2, c, wd = it
                ops.append(
                    f"{_rgb(c)} RG {wd * f:.2f} w {X(x1):.2f} {Y(y1):.2f} m {X(x2):.2f} {Y(y2):.2f} l S"
                )
            elif k == "poly":
                _, pts, c, wd = it
                path = " ".join(
                    f"{X(x):.2f} {Y(y):.2f} {'m' if i == 0 else 'l'}"
                    for i, (x, y) in enumerate(pts)
                )
                ops.append(f"{_rgb(c)} RG {wd * f:.2f} w {path} S")
            elif k == "dot":
                _, cx, cy, r, c = it
                rr = r * f
                ops.append(
                    f"{_rgb(c)} rg {X(cx) - rr:.2f} {Y(cy) - rr:.2f} {2 * rr:.2f} {2 * rr:.2f} re f"
                )
            elif k == "text":
                _, x, y, s, size, anchor, c = it
                s = _ascii(s)
                fs = (14 if size == "t" else 12) * f
                w = len(s) * CHAR_W * fs
                tx = X(x) - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
                font = "F2" if size == "t" else "F1"
                ops.append(
                    f"BT {_rgb(c)} rg /{font} {fs:.2f} Tf {tx:.2f} {Y(y):.2f} Td ({_esc(s)}) Tj ET"
                )
        ops.append("Q")
        self.pages[-1].append("\n".join(ops))
        self.y = top - h - 6

    def build(self) -> bytes:
        total = len(self.pages)
        objs: list[bytes] = []
        kids = []
        for n, ops in enumerate(self.pages):
            footer = f"BT /F1 8 Tf {MARGIN} 28 Td (Page {n + 1} of {total}) Tj ET"
            stream = zlib.compress(("\n".join([*ops, footer])).encode("ascii"), 9)
            page_id, content_id = 5 + 2 * n, 6 + 2 * n
            kids.append(f"{page_id} 0 R")
            objs.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>".encode()
            )
            objs.append(
                f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode()
                + stream
                + b"\nendstream"
            )
        head = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {total} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>",
        ]
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for i, body in enumerate([*head, *objs], start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


def build_pdf(session: Session) -> bytes:
    from sie.workspace.view import model_view, sample_view

    ws = session.workspace
    requests = {
        s.analysis_id: s.request for s in session.steps if s.analysis_id and s.kind == "plan"
    }
    shown = [*ws.items, *session.items]
    good = [i for i in shown if i.exportable]
    bad = [i for i in shown if not i.exportable]
    d = _Doc()
    d.heading("Analysis report", 0)
    d.para(f"Source file {ws.source.name}, {ws.profile.rows} rows, {ws.profile.columns} columns.")
    d.para(f"SHA-256 {ws.source.sha256}")
    if ws.sample:
        d.heading("Sample")
        d.para(sample_view(ws.sample)["text"])
    if ws.cards:
        d.heading("Key figures")
        d.table(
            ["figure", "value", "detail"],
            [[c.label, c.value, c.detail] for c in ws.cards if c.verified],
        )
    if ws.findings:
        d.heading("Findings")
        d.bullets([f.text for f in ws.findings])
    if ws.cleaning_steps:
        check = ws.cleaning_verification
        status = check.status if check is not None else "not_run"
        d.heading("Data cleaning")
        d.para(
            "The loaded file was not changed. These steps were approved one by one and applied to a "
            f"copy; independent check: {status}. {ws.original_rows} rows before, {ws.profile.rows} after."
        )
        d.bullets([e["text"] for e in ws.cleaning_steps])
    d.heading("Analyses")
    for item in good:
        p = item.planned
        d.heading(p.title, 2)
        if requests.get(p.id):
            d.para(f"Request: {requests[p.id]}")
        d.para(
            f"Verified by an independent calculation ({item.verification.checks} checks). "
            f"Result hash {item.result.provenance.result_sha256}"
        )
        table = item.result.table
        scene = chart_scene(p.chart, table, p.x, p.y, p.title)
        if scene is not None:
            d.chart(scene)
        rows = [
            [display_number(v) for v in r] for r in table.head(TABLE_ROWS).itertuples(index=False)
        ]
        note = (
            f"(first {TABLE_ROWS} of {len(table)} rows; the CSV export has all of them)"
            if len(table) > TABLE_ROWS
            else ""
        )
        d.table([str(c) for c in table.columns], rows, note)
        for w in item.result.warnings:
            d.para(f"Warning: {w}")
    if bad:
        d.heading("Not included (failed verification or could not run)")
        d.bullets(
            [
                f"{i.planned.title}: "
                + (
                    i.error
                    or i.verification.reason
                    or "; ".join(i.verification.mismatches[:3])
                    or "not verified"
                )
                for i in bad
            ]
        )
    if session.models:
        d.heading("Models")
    for run in session.models:
        v = model_view(run.id, run.report, ws.source.sha256, None)
        d.heading(f"Model comparison {run.id}: {v['problem']}", 2)
        for t in v["why"]:
            d.para(t)
        if v["table"] is None:
            d.para(
                f"Not included: the scores did not pass the independent check ({v.get('reason', '')})."
            )
        else:
            d.table(v["table"]["columns"], v["table"]["rows"])
            ev = v["evaluation"]
            d.para(f"Evaluation: {ev['method']}. {ev['selection']}.")
            if v["importance"]:
                d.para("Columns that mattered most (score drop when shuffled):")
                d.bullets([f"{i['column']}: {i['drop']}" for i in v["importance"]])
            if v["clusters"]:
                for g in v["clusters"]["profiles"]:
                    means = ", ".join(f"{k} {m}" for k, m in g["means"].items())
                    if g.get("common"):
                        means += "; most common: " + ", ".join(
                            f"{k} {m}" for k, m in g["common"].items()
                        )
                    d.para(f"Group {g['group']} ({g['rows']} rows): {means}")
        if v["excluded"]:
            d.para("Columns left out:")
            d.bullets([f"{e['column']}: {e['reason']}" for e in v["excluded"]])
        for w in v["warnings"]:
            d.para(f"Warning: {w}")
        d.bullets(list(v["limitations"]))
        d.para(f"Report hash {v['report_sha256']}")
    if session.steps:
        d.heading("Request history")
        d.bullets([f"{s.request} -> {s.kind}: {s.message}" for s in session.steps])
    d.heading("Limitations")
    d.bullets(list(ws.limitations))
    return d.build()
