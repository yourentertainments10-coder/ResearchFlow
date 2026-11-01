"""A workspace session: the requests asked so far, what each became, and a replay that proves it.

The history is data (request text, outcome kind, the typed spec, the chart, the result hash). It holds no
timestamps, paths or random ids, so the same file, cleaning plan and requests always give the same history
and the same ``sha256()``. ``replay`` re-asks the recorded requests on a fresh workspace and reports any
difference in the source hash, a spec, a chart or a result hash instead of trusting the record.

Every number still comes from ``sie.datasets`` (``run_operation``) and is re-checked by ``verify_result``;
this module only sequences planner -> engine -> verifier -> chart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sie.datasets import CleanPlan, DatasetError, Limits, LoadOptions, run_operation
from sie.datasets.provenance import result_sha256
from sie.workspace.build import Workspace, build_workspace
from sie.workspace.charts import render_chart
from sie.workspace.models import AnalysisItem, PlannedAnalysis
from sie.workspace.planner import (
    ChartChange,
    NeedsClarification,
    Plan,
    Unsupported,
    plan_request,
    shown,
)
from sie.workspace.verify import verify_result

HISTORY_VERSION = 1


class ReplayMismatch(Exception):
    """A replay did not reproduce the recorded history; ``differences`` says exactly where."""

    def __init__(self, differences: list[str]) -> None:
        self.differences = differences
        super().__init__("; ".join(differences))


@dataclass(frozen=True)
class Step:
    index: int
    request: str
    kind: str  # plan | chart_change | clarification | unsupported
    message: str  # interpretation, question or reason
    spec: dict[str, Any] | None = None
    chart: str | None = None
    x: str | None = None
    y: str | None = None
    title: str | None = None
    result_sha256: str | None = None
    verification: str | None = None  # verified | mismatch | not_run ... (Verification.status)
    analysis_id: str | None = None
    notes: tuple[str, ...] = ()
    options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "request": self.request,
            "kind": self.kind,
            "message": self.message,
            "spec": self.spec,
            "chart": self.chart,
            "x": self.x,
            "y": self.y,
            "title": self.title,
            "result_sha256": self.result_sha256,
            "verification": self.verification,
            "analysis_id": self.analysis_id,
            "notes": list(self.notes),
            "options": list(self.options),
        }

    def replay_key(self) -> dict[str, Any]:
        """What a replay must reproduce exactly."""
        d = self.to_dict()
        return {k: d[k] for k in ("request", "kind", "spec", "chart", "x", "y", "result_sha256")}


@dataclass
class Session:
    workspace: Workspace
    steps: list[Step] = field(default_factory=list)
    items: list[AnalysisItem] = field(
        default_factory=list
    )  # analyses asked for, in order (r1, r2, ...)
    _current: tuple[Plan, AnalysisItem] | None = field(default=None, repr=False)

    def ask(self, text: object) -> Step:
        """One request. Always returns a recorded ``Step``; never raises for bad requests or data."""
        profile = self.workspace.profile
        current = self._current[0] if self._current else None
        outcome = plan_request(text, profile, current=current)
        request = shown(text, 300)
        if isinstance(outcome, Plan):
            step = self._run(request, outcome)
        elif isinstance(outcome, ChartChange):
            step = self._redraw(request, outcome)
        elif isinstance(outcome, NeedsClarification):
            step = self._record(request, "clarification", outcome.question, options=outcome.options)
        else:
            assert isinstance(outcome, Unsupported)
            step = self._record(request, "unsupported", outcome.reason)
        return step

    # -- internals ---------------------------------------------------------------------------------
    def _next_id(self) -> str:
        return f"r{len(self.items) + 1}"

    def _record(self, request: str, kind: str, message: str, **fields: Any) -> Step:
        step = Step(len(self.steps) + 1, request, kind, message, **fields)
        self.steps.append(step)
        return step

    def _run(self, request: str, plan: Plan) -> Step:
        ws = self.workspace
        try:
            result = run_operation(ws.dataset, plan.spec, cleaning=ws._cleaning)
        except DatasetError as exc:
            return self._record(
                request, "unsupported", f"The analysis could not run: {shown(exc, 200)}"
            )
        verification = verify_result(ws.dataset, plan.spec, result.table)
        aid = self._next_id()
        planned = PlannedAnalysis(aid, plan.title, plan.spec, plan.chart, plan.x, plan.y)
        svg = render_chart(plan.chart, result.table, plan.x, plan.y, plan.title)
        item = AnalysisItem(planned, result, svg=svg, verification=verification)
        self.items.append(item)
        self._current = (plan, item)
        return self._record(
            request, "plan", plan.interpretation, spec=plan.spec.model_dump(mode="json"),
            chart=plan.chart, x=plan.x, y=plan.y, title=plan.title,
            result_sha256=result.provenance.result_sha256, verification=verification.status,
            analysis_id=aid, notes=plan.notes,
        )  # fmt: skip

    def _redraw(self, request: str, change: ChartChange) -> Step:
        assert self._current is not None  # the planner returns ChartChange only with a current plan
        plan, previous = self._current
        redrawn = Plan(
            plan.spec, change.chart, plan.x, plan.y, plan.title, plan.interpretation, plan.notes
        )
        aid = self._next_id()
        planned = PlannedAnalysis(aid, plan.title, plan.spec, change.chart, plan.x, plan.y)
        assert previous.result is not None
        svg = render_chart(change.chart, previous.result.table, plan.x, plan.y, plan.title)
        # same table object, same verification: nothing is recomputed, so the hash cannot change
        item = AnalysisItem(planned, previous.result, svg=svg, verification=previous.verification)
        self.items.append(item)
        self._current = (redrawn, item)
        return self._record(
            request, "chart_change", change.interpretation, spec=plan.spec.model_dump(mode="json"),
            chart=change.chart, x=plan.x, y=plan.y, title=plan.title,
            result_sha256=previous.result.provenance.result_sha256,
            verification=previous.verification.status, analysis_id=aid,
        )  # fmt: skip

    # -- history -----------------------------------------------------------------------------------
    def history(self) -> dict[str, Any]:
        return {
            "version": HISTORY_VERSION,
            "source_sha256": self.workspace.source.sha256,
            "cleaning": self.workspace.cleaning,
            "steps": [s.to_dict() for s in self.steps],
        }

    def sha256(self) -> str:
        return result_sha256(self.history())


def start_session(
    data: bytes,
    name: str,
    *,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    plan: CleanPlan | None = None,
) -> Session:
    return Session(build_workspace(data, name, options=options, limits=limits, plan=plan))


def replay(
    data: bytes,
    name: str,
    history: dict[str, Any],
    *,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    plan: CleanPlan | None = None,
) -> Session:
    """Re-ask every recorded request on a fresh workspace built from the same bytes.

    Returns the new ``Session`` when source hash, cleaning record, specs, charts and result hashes all
    match; raises ``ReplayMismatch`` listing every difference otherwise (a changed file, another cleaning
    plan, or an engine change that moves a number)."""
    differences: list[str] = []
    if not isinstance(history, dict) or history.get("version") != HISTORY_VERSION:
        raise ReplayMismatch([f"history version is not {HISTORY_VERSION}"])
    session = start_session(data, name, options=options, limits=limits, plan=plan)
    if history.get("source_sha256") != session.workspace.source.sha256:
        differences.append("source file hash differs from the recorded one")
    if history.get("cleaning") != session.workspace.cleaning:
        differences.append("cleaning record differs from the recorded one")
    recorded = history.get("steps")
    if not isinstance(recorded, list):
        raise ReplayMismatch([*differences, "history has no step list"])
    for old in recorded:
        if not isinstance(old, dict) or not isinstance(old.get("request"), str):
            differences.append("a recorded step has no request text")
            continue
        new = session.ask(old["request"])
        for key, value in new.replay_key().items():
            if old.get(key) != value:
                differences.append(f"step {old.get('index')}: {key} differs from the recorded one")
    if differences:
        raise ReplayMismatch(differences)
    return session
