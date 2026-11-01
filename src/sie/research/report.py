"""Analysis, evidence-linked answer, coverage and chart over collected tables. Deterministic, no model.

Numbers in the answer come only from ``sie.datasets`` operation results, enter the text through ``{F:id}``
placeholders, and pass the Phase 1 number lock before anything is rendered. Digits that belong to source
names (a title such as "Lok Sabha 2019") are declared literals: they are labels from the source, not
results. Coverage, conflicts, flags, stale cache use and provider failures become disclosed gaps, so an
answer cannot present partial or disputed data as complete.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from sie.answer import (
    Answer,
    EvidenceRef,
    Figure,
    Gap,
    GapKind,
    Layer,
    SourceKind,
    Statement,
    canonical_json,
    figure_from_value,
    operation_provenance,
    render_markdown,
)
from sie.answer.numbers import PLACEHOLDER, numeric_tokens
from sie.datasets import CleanPlan, DatasetError, OperationResult, clean, run_operation
from sie.evidence.errors import EvidenceError
from sie.research.collect import CollectedTable, coverage

MAX_ROWS_SHOWN = 10
MAX_BARS = 15


@dataclass(frozen=True)
class AnalysisItem:
    name: str
    result: OperationResult
    label_column: str
    value_column: str


ANALYSIS_ERRORS = (DatasetError,)  # what an invalid request or unusable table raises


@dataclass(frozen=True)
class Report:
    answer: Answer
    markdown: str
    svg: str | None
    coverage: list[dict]

    def answer_json(self) -> str:
        """The verified answer in its canonical JSON form (sorted keys, stable bytes)."""
        return canonical_json(self.answer)


def analyze(table: CollectedTable, spec: dict, plan: CleanPlan | None = None) -> OperationResult:
    """Run one deterministic operation, cleaning first only when a plan is given."""
    if plan is None:
        return run_operation(table.dataset, spec)
    cleaned = clean(table.dataset, plan)
    return run_operation(cleaned.dataset, spec, cleaning=cleaned)


def default_columns(table: pd.DataFrame) -> tuple[str, str]:
    """(label column, value column) for a result table: the first column that is not a rank, and the last
    column, which is the final metric in every operation's output. Callers can override both."""
    cols = [str(c) for c in table.columns]
    if len(cols) < 2:
        raise EvidenceError("the result has no value column to report")
    label = next((c for c in cols if c != "rank"), cols[0])
    return label, cols[-1]


def plan_from_dict(data: dict) -> CleanPlan:
    """A cleaning plan from validated JSON (never code)."""
    try:
        return CleanPlan.model_validate(data)
    except ValueError as exc:
        raise EvidenceError(f"invalid cleaning plan: {exc}") from exc


def _text(text: str) -> tuple[str, tuple[str, ...]]:
    """Source-derived text with its digits declared: they are names from the source, not results."""
    found = tuple(dict.fromkeys(numeric_tokens(PLACEHOLDER.sub("", text))))
    return text, found


def _decimals(value) -> int:
    from sie.answer import to_decimal

    exp = to_decimal(value).normalize().as_tuple().exponent
    return min(9, max(0, -exp)) if isinstance(exp, int) else 0


def svg_bar_chart(title: str, labels: Sequence[str], values: Sequence[float]) -> str:
    """A plain horizontal bar chart as SVG. Text is escaped; the same input always gives the same bytes."""
    n = min(len(values), MAX_BARS)
    if n == 0:
        raise EvidenceError("nothing to chart")
    top = max(abs(v) for v in values[:n]) or 1.0
    row, left, width = 24, 220, 640
    height = 50 + row * n
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<text x="8" y="20" font-family="sans-serif" font-size="14" font-weight="bold">'
        f"{html.escape(title)}</text>",
    ]
    for i in range(n):
        y = 36 + row * i
        bar = max(1.0, abs(values[i]) / top * (width - left - 90))
        parts.append(
            f'<text x="{left - 6}" y="{y + 14}" text-anchor="end" font-family="sans-serif" '
            f'font-size="12">{html.escape(str(labels[i])[:34])}</text>'
            f'<rect x="{left}" y="{y}" width="{bar:.1f}" height="16" fill="#2b6cb0"/>'
            f'<text x="{left + bar + 4:.1f}" y="{y + 13}" font-family="sans-serif" font-size="12">'
            f"{html.escape(format(values[i], '.15g'))}</text>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _md_table(frame: pd.DataFrame, limit: int = 20) -> list[str]:
    cols = [str(c) for c in frame.columns]
    out = ["| " + " | ".join(["row_id", *cols]) + " |", "|" + "---|" * (len(cols) + 1)]
    for idx, row in frame.head(limit).iterrows():
        cells = [str(idx), *("" if pd.isna(v) else str(v).replace("|", "/") for v in row)]
        out.append("| " + " | ".join(cells) + " |")
    if len(frame) > limit:
        out.append(f"\n({len(frame) - limit} more rows not shown; the full result is reproducible)")
    return out


def build_report(
    question: str,
    tables: Sequence[CollectedTable],
    analyses: Sequence[AnalysisItem],
    *,
    now: datetime,
    tiers: dict[str, int] | None = None,
    conflicts: pd.DataFrame | None = None,
    provider_notes: Sequence[str] = (),
    stale_urls: Sequence[str] = (),
    stale_datasets: Sequence[str] = (),
    not_saved: Sequence[str] = (),
    agreement_note: str = "",
    lead_limitations: dict[str, Sequence[str]] | None = None,
) -> Report:
    if not tables or not analyses:
        raise EvidenceError("a report needs at least one table and one analysis")
    tiers = tiers or {}
    evidence: list[EvidenceRef] = []
    gaps: list[Gap] = []
    statements: list[Statement] = []
    figures: list[Figure] = []
    cov = [coverage(t) for t in tables]

    seen: dict[str, str] = {}
    for t in tables:
        if t.url in seen:
            continue
        eid = f"src{len(seen) + 1}"
        seen[t.url] = eid
        evidence.append(
            EvidenceRef(
                id=eid, url=t.url, title=f"Data table {t.label} from {t.url}"[:300],
                retrieved_at=datetime.fromisoformat(t.retrieved_at), tier=tiers.get(t.url, 4),
            )
        )  # fmt: skip
        lic = (
            f"licence as reported by {t.catalogue or 'the source'}: {t.licence}"
            if t.licence
            else "no licence stated by the catalogue"
        )
        text, lits = _text(f"Data was taken from {t.url} (table {t.label}); {lic}.")
        statements.append(
            Statement(
                layer=Layer.EXTERNAL_EVIDENCE, text=text, evidence=(eid,), declared_literals=lits
            )
        )

    first = analyses[0]
    res = first.result
    ref, prov = operation_provenance(res)
    table = res.table
    shown = table.head(MAX_ROWS_SHOWN)
    labels, values, no_value = [], [], []
    for idx, row in shown.iterrows():
        value = row[first.value_column]
        label = str(row[first.label_column])
        if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
            no_value.append(label)  # reported as a gap below; never replaced by zero or an estimate
            continue
        i = len(figures) + 1
        fig = figure_from_value(
            f"f{i}", value, label=f"{first.value_column} for {label}"[:200],
            source_kind=SourceKind.OPERATION, source_ref=ref, decimals=_decimals(value),
            provenance={**prov, "result_row": str(idx)},
        )  # fmt: skip
        figures.append(fig)
        text, lits = _text(f"{label}: {{F:f{i}}}")
        statements.append(
            Statement(layer=Layer.DERIVED_ANALYSIS, text=text, declared_literals=lits)
        )
        labels.append(label)
        values.append(float(value))

    def gap(kind: GapKind, message: str) -> None:
        gid = f"g{len(gaps) + 1}"
        gaps.append(Gap(id=gid, kind=kind, message=message[:300]))
        text, lits = _text(message[:300])
        statements.append(
            Statement(layer=Layer.LIMITATION, text=text, gaps=(gid,), declared_literals=lits)
        )

    if no_value:
        gap(
            GapKind.MISSING_DATA,
            "No result value (every input was missing) for: " + ", ".join(no_value),
        )
    if not figures:
        raise EvidenceError("the analysis has no numeric result to report")
    for c in cov:
        if c["missing_values_by_column"]:
            gap(GapKind.MISSING_DATA, f"Missing values in {c['table']} of {c['url']}: "
                + ", ".join(f"{k} ({v} rows)" for k, v in c["missing_values_by_column"].items()))  # fmt: skip
        if c["rejected_rows"]:
            gap(GapKind.EXCLUDED_ROWS, f"{c['rejected_rows']} rows of {c['table']} at {c['url']} were rejected by the loader and are not in any result.")  # fmt: skip
        if c["flags"]:
            gap(GapKind.PARTIAL_DATA, f"Parsing flags for {c['table']} at {c['url']}: " + ", ".join(c["flags"]))  # fmt: skip
    for w in res.warnings:
        gap(GapKind.PARTIAL_DATA, f"Operation warning: {w}")
    unlicensed: set[str] = set()
    for t in tables:
        if not t.licence and t.url not in unlicensed:
            gap(GapKind.UNVERIFIED, f"No licence or terms were stated for {t.url}; check them before reusing the data.")  # fmt: skip
            unlicensed.add(t.url)  # one disclosure per source
    if conflicts is not None and len(conflicts):
        gap(GapKind.CONFLICT, f"{len(conflicts)} values conflict between sources; none was chosen over another (see the conflicts table).")  # fmt: skip
        if agreement_note:
            text, lits = _text(f"{agreement_note}.")
            statements.append(Statement(layer=Layer.LIMITATION, text=text, declared_literals=lits))
    elif conflicts is not None:
        text, lits = _text(f"{agreement_note}: no conflicting values were found.")
        statements.append(Statement(layer=Layer.LIMITATION, text=text, declared_literals=lits))
    elif agreement_note:
        gap(GapKind.UNVERIFIED, f"Agreement between sources was not checked: {agreement_note}.")
    for t_url, notes in (lead_limitations or {}).items():
        if t_url not in seen:
            continue
        for note in notes:  # one gap per note, so a long address never truncates a limitation away
            gap(GapKind.UNVERIFIED, f"{t_url} was chosen from a search lead and not verified against the question: {note}.")  # fmt: skip
    scope_text, lits = _text(
        "Whether the collected table covers everything the question asks about (for example every "
        f"constituency or every year) was not checked; it has {sum(c['rows'] for c in cov)} rows in total."
    )
    statements.append(Statement(layer=Layer.LIMITATION, text=scope_text, declared_literals=lits))
    for item in stale_datasets:
        gap(
            GapKind.STALE,
            f"Saved data from {item} is older than the freshness limit and no newer data could be collected.",
        )
    for item in not_saved:
        text, lits = _text(f"Not saved for reuse, a follow-up will fetch it again: {item}.")
        statements.append(Statement(layer=Layer.LIMITATION, text=text, declared_literals=lits))
    for url in stale_urls:
        gap(
            GapKind.STALE,
            f"{url} was served from an older cached copy because a live fetch failed.",
        )
    for note in provider_notes:
        gap(GapKind.MISSING_DATA, f"A free source was unavailable: {note}")
    text, lits = _text(
        "Data come from free public sources as published; they were not independently verified, and "
        "coverage of free sources can be incomplete."
    )
    statements.append(Statement(layer=Layer.LIMITATION, text=text, declared_literals=lits))

    answer = Answer(
        question=question[:2000], figures=tuple(figures), evidence=tuple(evidence),
        gaps=tuple(gaps), statements=tuple(statements),
    )  # fmt: skip
    body = render_markdown(answer)

    lines = [f"# {question}", "", body, "## Result tables", ""]
    for a in analyses:
        lines += [f"### {a.name}", *_md_table(a.result.table), ""]
        for w in a.result.limitations:
            lines.append(f"- {w}")
        lines.append("")
    lic_by_url = {t.url: t.licence for t in tables}
    lines += ["## Sources and coverage", ""]
    for c in cov:
        lines.append(
            f"- {c['url']} ({c['table']}): licence {lic_by_url.get(c['url']) or 'not stated'}, retrieved {c['retrieved_at']}, sha256 {c['raw_sha256']}, "
            f"{c['rows']} rows, {c['rejected_rows']} rejected, flags: {', '.join(c['flags']) or 'none'}"
        )
    if conflicts is not None and len(conflicts):
        lines += ["", "## Conflicts (none resolved)", "", *_md_table(conflicts)]
    lines += ["", f"Generated {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}.", ""]
    svg = svg_bar_chart(first.name, labels, values) if values else None
    return Report(answer, "\n".join(lines), svg, cov)
