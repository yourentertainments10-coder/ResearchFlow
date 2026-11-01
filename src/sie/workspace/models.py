"""Plain data shared by the workspace modules. No I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChartKind = Literal["bar", "line", "table"]
VerifyStatus = Literal["verified", "unverified", "not_run"]


@dataclass(frozen=True)
class PlannedAnalysis:
    """One starter analysis: an ``OperationSpec`` (data, never code) plus how to show its table."""

    id: str
    title: str
    spec: Any  # a sie.datasets operation spec model
    chart: ChartKind
    x: str | None = None  # result column used for labels / the x axis
    y: str | None = None  # result column used for values


@dataclass
class Verification:
    """Outcome of the independent recomputation. ``unverified`` blocks export of that item."""

    status: VerifyStatus
    checks: int = 0
    mismatches: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": self.checks,
            "mismatches": self.mismatches,
            "reason": self.reason,
        }


@dataclass
class AnalysisItem:
    planned: PlannedAnalysis
    result: Any | None  # sie.datasets OperationResult, None when the operation failed
    error: str = ""
    svg: str | None = None
    verification: Verification = field(default_factory=lambda: Verification("not_run"))

    @property
    def exportable(self) -> bool:
        return self.result is not None and self.verification.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": self.planned.id,
            "title": self.planned.title,
            "chart": self.planned.chart,
            "x": self.planned.x,
            "y": self.planned.y,
            "spec": self.planned.spec.model_dump(mode="json"),
            "verification": self.verification.to_dict(),
            "exportable": self.exportable,
            "error": self.error,
        }
        if self.result is not None:
            item["result"] = self.result.to_dict()
        return item


@dataclass(frozen=True)
class Card:
    id: str
    label: str
    value: str  # display text produced by sie.answer.format_figure
    detail: str
    source: str  # "profile" or an analysis id
    figure: dict[str, Any]
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "value": self.value,
            "detail": self.detail,
            "source": self.source,
            "figure": self.figure,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class Finding:
    """A sentence whose numbers are all figures. ``template`` holds ``{F:id}`` for figures and
    ``{V:key}`` for data values (category names, periods); it contains no digits of its own."""

    id: str
    kind: Literal["quality", "result"]
    severity: Literal["info", "warning"]
    template: str
    text: str
    source: str
    figures: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "template": self.template,
            "text": self.text,
            "source": self.source,
            "figures": self.figures,
            "values": self.values,
        }
