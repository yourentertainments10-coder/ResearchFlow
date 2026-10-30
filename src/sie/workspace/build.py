"""Build a workspace from the bytes of one file: load, profile, plan, compute, verify, draw.

No clock, no environment, no network, no files. The result is deterministic: the same bytes and options
give the same ``to_dict()`` and the same ``sha256()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sie.datasets import (
    CleanPlan,
    DatasetError,
    Limits,
    LoadOptions,
    clean,
    open_dataset,
    profile,
    run_operation,
)
from sie.datasets.models import STANDARD_LIMITATIONS, Dataset, ProfileResult, SourceRef
from sie.datasets.provenance import result_sha256
from sie.workspace.autoplan import plan_analyses
from sie.workspace.cards import build_cards, profile_figures
from sie.workspace.charts import render_chart
from sie.workspace.findings import build_findings
from sie.workspace.models import AnalysisItem, Card, Finding, Verification
from sie.workspace.verify import verify_profile_figures, verify_result

WORKSPACE_LIMITATIONS = (
    *STANDARD_LIMITATIONS,
    "Starter analyses are chosen by fixed rules from column types, not by understanding the data's meaning.",
    "A number is shown as verified only when a separate pandas calculation agrees; unverified results are not exported.",
    "Charts are deterministic SVG; PNG and PDF export are not available yet.",
)


@dataclass
class Workspace:
    dataset: Dataset
    source: SourceRef
    profile: ProfileResult
    profile_verification: Verification
    items: list[AnalysisItem]
    cards: list[Card]
    findings: list[Finding]
    cleaning: dict[str, Any] | None = None
    limitations: list[str] = field(default_factory=lambda: list(WORKSPACE_LIMITATIONS))

    @property
    def unverified(self) -> list[AnalysisItem]:
        return [i for i in self.items if not i.exportable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.model_dump(mode="json"),
            "profile": self.profile.to_dict(),
            "profile_verification": self.profile_verification.to_dict(),
            "cleaning": self.cleaning,
            "cards": [c.to_dict() for c in self.cards],
            "findings": [f.to_dict() for f in self.findings],
            "analyses": [i.to_dict() for i in self.items],
            "limitations": self.limitations,
        }

    def sha256(self) -> str:
        return result_sha256(self.to_dict())


def build_workspace(
    data: bytes,
    name: str,
    *,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    plan: CleanPlan | None = None,
    examples: bool = True,
) -> Workspace:
    """Raises ``DatasetError`` subclasses for an unusable file (nothing partial is returned)."""
    ds = open_dataset(data, name, options or LoadOptions(), limits or Limits())
    cleaning = clean(ds, plan) if plan is not None else None
    target = cleaning.dataset if cleaning is not None else ds
    prof = profile(target, examples=examples)
    items: list[AnalysisItem] = []
    for planned in plan_analyses(prof):
        try:
            result = run_operation(target, planned.spec, cleaning=cleaning)
        except DatasetError as exc:
            items.append(AnalysisItem(planned, None, error=str(exc)))
            continue
        verification = verify_result(target, planned.spec, result.table)
        svg = render_chart(planned.chart, result.table, planned.x, planned.y, planned.title)
        items.append(AnalysisItem(planned, result, svg=svg, verification=verification))
    profile_check = verify_profile_figures(target, profile_figures(prof))
    describe = next((i for i in items if i.planned.id == "describe"), None)
    cards = build_cards(
        prof, ds.source.sha256, describe, profile_verified=profile_check.status == "verified"
    )
    findings = build_findings(prof, ds.source.sha256, items)
    return Workspace(
        dataset=target,
        source=ds.source,
        profile=prof,
        profile_verification=profile_check,
        items=items,
        cards=cards,
        findings=findings,
        cleaning=cleaning.to_dict() if cleaning is not None else None,
    )
