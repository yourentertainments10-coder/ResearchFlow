"""The number lock and the layer rules (docs/ANSWER_CONTRACT_DESIGN.md section 4).

``verify`` returns every violation it finds; ``assert_verified`` raises. An answer that does not pass is
never rendered. The lock covers numbers only: it does not check names or that a sentence is true.
"""

from __future__ import annotations

from dataclasses import dataclass

from sie.answer.models import Answer, Layer, Refusal, SourceKind, Statement
from sie.answer.numbers import PLACEHOLDER, numeric_tokens


class AnswerRejected(ValueError):
    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.code}: {v.detail}" for v in violations))


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    statement: int | None = None  # index into Answer.statements


@dataclass(frozen=True)
class VerificationReport:
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> set[str]:
        return {v.code for v in self.violations}


# Which figure sources each layer may cite.
_ALLOWED_SOURCES: dict[Layer, set[SourceKind]] = {
    Layer.VERIFIED_FACT: {SourceKind.DATABASE},
    Layer.DERIVED_ANALYSIS: {SourceKind.OPERATION, SourceKind.USER_FILE},
    Layer.INTERPRETATION: {SourceKind.DATABASE, SourceKind.OPERATION, SourceKind.USER_FILE},
    Layer.EXTERNAL_EVIDENCE: {SourceKind.EVIDENCE},
    Layer.LIMITATION: {
        SourceKind.DATABASE,
        SourceKind.OPERATION,
        SourceKind.USER_FILE,
        SourceKind.EVIDENCE,
    },
}


def _scan_text(text: str, declared: tuple[str, ...], where: int | None, out: list[Violation]):
    """Number lock for one piece of text. Returns the figure ids its placeholders name."""
    ids = PLACEHOLDER.findall(text)
    rest = PLACEHOLDER.sub("", text)
    if "{" in rest or "}" in rest:
        out.append(Violation("bad_placeholder", "a brace that is not a {F:id} placeholder", where))
    allowed = {d.casefold() for d in declared}
    tokens = numeric_tokens(rest)
    seen = {t.casefold() for t in tokens}
    for tok in tokens:
        if tok.casefold() not in allowed:
            out.append(Violation("literal_number", f"number {tok!r} is not from a figure", where))
    for d in declared:
        if d.casefold() not in seen:
            out.append(
                Violation(
                    "declared_literal_absent", f"{d!r} is declared but not in the text", where
                )
            )
    return ids


def _check_statement(a: Answer, i: int, s: Statement, out: list[Violation]) -> set[str]:
    figures = {f.id: f for f in a.figures}
    evidence = {e.id for e in a.evidence}
    gaps = {g.id for g in a.gaps}
    ids = _scan_text(s.text, s.declared_literals, i, out)
    used = set(ids)
    for fid in ids:
        fig = figures.get(fid)
        if fig is None:
            out.append(Violation("unknown_figure", f"no figure {fid!r}", i))
        elif fig.source_kind not in _ALLOWED_SOURCES[s.layer]:
            out.append(
                Violation(
                    "layer_source_mismatch",
                    f"{s.layer.value} may not cite {fig.source_kind.value} figure {fid!r}",
                    i,
                )
            )
    for eid in s.evidence:
        if eid not in evidence:
            out.append(Violation("unknown_evidence", f"no evidence {eid!r}", i))
    for gid in s.gaps:
        if gid not in gaps:
            out.append(Violation("unknown_gap", f"no gap {gid!r}", i))
    if s.layer in (Layer.VERIFIED_FACT, Layer.DERIVED_ANALYSIS, Layer.INTERPRETATION) and not ids:
        out.append(Violation("no_basis", f"{s.layer.value} must cite at least one figure", i))
    if s.layer is Layer.EXTERNAL_EVIDENCE and not s.evidence:
        out.append(
            Violation("no_basis", "external_evidence must cite at least one evidence item", i)
        )
    if s.layer is not Layer.EXTERNAL_EVIDENCE and s.evidence:
        out.append(
            Violation("evidence_outside_layer", "only external_evidence may cite evidence", i)
        )
    return used


def verify(item: Answer | Refusal) -> VerificationReport:
    out: list[Violation] = []
    if isinstance(item, Refusal):
        _scan_text(item.reason, item.declared_literals, None, out)
        return VerificationReport(tuple(out))
    a = item
    for kind, ids in (
        ("figure", [f.id for f in a.figures]),
        ("evidence", [e.id for e in a.evidence]),
        ("gap", [g.id for g in a.gaps]),
    ):
        dup = {i for i in ids if ids.count(i) > 1}
        for d in sorted(dup):
            out.append(Violation("duplicate_id", f"{kind} id {d!r} is used twice"))
    evidence_ids = {e.id for e in a.evidence}
    for f in a.figures:
        if f.source_kind is SourceKind.EVIDENCE and f.source_ref not in evidence_ids:
            out.append(
                Violation("figure_evidence_missing", f"figure {f.id!r} cites no known evidence")
            )
    used: set[str] = set()
    disclosed: set[str] = set()
    for i, s in enumerate(a.statements):
        used |= _check_statement(a, i, s, out)
        disclosed |= set(s.gaps) if s.layer is Layer.LIMITATION else set()
    for f in a.figures:
        if f.id not in used:
            out.append(Violation("unused_figure", f"figure {f.id!r} is never shown"))
    for g in a.gaps:
        if g.id not in disclosed:
            out.append(
                Violation("gap_not_disclosed", f"gap {g.id!r} ({g.kind.value}) has no limitation")
            )
    if not any(s.layer is Layer.LIMITATION for s in a.statements):
        out.append(Violation("no_limitations", "an answer must state at least one limitation"))
    return VerificationReport(tuple(out))


def assert_verified(item: Answer | Refusal) -> None:
    report = verify(item)
    if not report.ok:
        raise AnswerRejected(list(report.violations))
