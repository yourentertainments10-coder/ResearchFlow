"""Markdown for a verified answer. Nothing unverified can be rendered, and the output is deterministic."""

from __future__ import annotations

from datetime import UTC

from sie.answer.models import Answer, Layer, Refusal
from sie.answer.numbers import PLACEHOLDER, format_figure
from sie.answer.verify import assert_verified

_HEADINGS = {
    Layer.VERIFIED_FACT: "Verified facts",
    Layer.DERIVED_ANALYSIS: "Derived analysis",
    Layer.INTERPRETATION: "Interpretation",
    Layer.EXTERNAL_EVIDENCE: "External evidence (cited, not verified)",
    Layer.LIMITATION: "Limitations",
}


def render_markdown(item: Answer | Refusal) -> str:
    assert_verified(item)
    if isinstance(item, Refusal):
        return f"**Cannot answer** ({item.code.value}): {item.reason}\n"
    figures = {f.id: f for f in item.figures}
    evidence = {e.id: e for e in item.evidence}
    numbering: dict[str, int] = {}
    lines: list[str] = []
    for layer in Layer:
        rows = [s for s in item.statements if s.layer is layer]
        if not rows:
            continue
        lines += [f"## {_HEADINGS[layer]}", ""]
        for s in rows:
            text = PLACEHOLDER.sub(lambda m: format_figure(figures[m.group(1)]), s.text)
            marks = ""
            for eid in s.evidence:
                marks += f" [{numbering.setdefault(eid, len(numbering) + 1)}]"
            lines.append(f"- {text}{marks}")
        lines.append("")
    if numbering:
        lines += ["## Sources", ""]
        for eid, n in sorted(numbering.items(), key=lambda kv: kv[1]):
            e = evidence[eid]
            when = e.retrieved_at.astimezone(UTC).strftime("%Y-%m-%d")
            lines.append(f"[{n}] {e.title}, {e.url} (retrieved {when}, source tier {e.tier})")
        lines.append("")
    return "\n".join(lines)
