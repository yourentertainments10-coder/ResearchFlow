"""Answer contract (ADR-041): layered statements, a number lock, deterministic rendering.

Phase 1 of the general research engine. Library only: no command, no database, no network.
"""

from sie.answer.adapters import (
    FigureError,
    figure_from_cell,
    figure_from_value,
    operation_provenance,
    to_decimal,
)
from sie.answer.models import (
    Answer,
    EvidenceRef,
    Figure,
    Gap,
    GapKind,
    Layer,
    Refusal,
    RefusalCode,
    SourceKind,
    Statement,
    answer_sha256,
    canonical_json,
)
from sie.answer.numbers import format_figure
from sie.answer.render import render_markdown
from sie.answer.verify import (
    AnswerRejected,
    VerificationReport,
    Violation,
    assert_verified,
    verify,
)

__all__ = [
    "Answer",
    "AnswerRejected",
    "EvidenceRef",
    "Figure",
    "FigureError",
    "Gap",
    "GapKind",
    "Layer",
    "Refusal",
    "RefusalCode",
    "SourceKind",
    "Statement",
    "VerificationReport",
    "Violation",
    "answer_sha256",
    "assert_verified",
    "canonical_json",
    "figure_from_cell",
    "figure_from_value",
    "format_figure",
    "operation_provenance",
    "render_markdown",
    "to_decimal",
    "verify",
]
