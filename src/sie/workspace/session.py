"""A workspace session: the requests asked so far, what each became, and a replay that proves it.

The history is data (request text, outcome kind, the typed spec, the chart, the result hash). It holds no
timestamps, paths or random ids, so the same file, cleaning plan and requests always give the same history
and the same ``sha256()``. ``replay`` re-asks the recorded requests on a fresh workspace and reports any
difference in the source hash, a spec, a chart or a result hash instead of trusting the record.

Every number still comes from ``sie.datasets`` (``run_operation``) and is re-checked by ``verify_result``;
this module only sequences planner -> engine -> verifier -> chart.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sie.datasets import CleanPlan, DatasetError, Limits, LoadOptions, run_operation
from sie.datasets.provenance import result_sha256
from sie.workspace.build import Workspace, build_workspace
from sie.workspace.charts import render_chart
from sie.workspace.cleaning import (
    CleaningStep,
    Suggestion,
    parse_step,
    step_title,
    suggest,
)
from sie.workspace.models import AnalysisItem, PlannedAnalysis, Verification
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
UNDO_REQUEST = "Undo the last cleaning step"


def _plain(entry: dict[str, Any]) -> dict[str, Any]:
    """A cleaning log entry as plain JSON values (it already is; this copies it)."""
    import json

    return json.loads(json.dumps(entry, sort_keys=True))


class ReplayMismatch(Exception):
    """A replay did not reproduce the recorded history; ``differences`` says exactly where."""

    def __init__(self, differences: list[str]) -> None:
        self.differences = differences
        super().__init__("; ".join(differences))


@dataclass(frozen=True)
class Step:
    index: int
    request: str
    # plan | chart_change | already_shown | clarification | unsupported | clean | clean_undo | clean_rejected
    kind: str
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
    cleaning: dict[str, Any] | None = None  # for clean / clean_undo / clean_rejected steps
    auto: bool = False  # asked again by the session after the data changed (not typed by a person)

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
            "cleaning": self.cleaning,
            "auto": self.auto,
        }

    def replay_key(self) -> dict[str, Any]:
        """What a replay must reproduce exactly."""
        d = self.to_dict()
        keys = ("request", "kind", "spec", "chart", "x", "y", "result_sha256", "cleaning", "auto")
        return {k: d[k] for k in keys}


@dataclass
class Session:
    workspace: Workspace
    steps: list[Step] = field(default_factory=list)
    items: list[AnalysisItem] = field(
        default_factory=list
    )  # analyses asked for, in order (r1, r2, ...)
    _current: tuple[Plan, AnalysisItem] | None = field(default=None, repr=False)
    cleaning: list[CleaningStep] = field(default_factory=list)  # approved steps, in order
    _rebuild: Callable[[Sequence[CleaningStep]], Workspace] | None = field(default=None, repr=False)
    _counter: int = field(default=0, repr=False)  # analysis ids never repeat, even after cleaning
    _live: list[str] = field(
        default_factory=list, repr=False
    )  # requests whose cards are on the page
    _auto: bool = field(default=False, repr=False)

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
        self._counter += 1
        return f"r{self._counter}"

    def _record(self, request: str, kind: str, message: str, **fields: Any) -> Step:
        step = Step(len(self.steps) + 1, request, kind, message, auto=self._auto, **fields)
        self.steps.append(step)
        return step

    def _existing(self, spec: Any, chart: str) -> AnalysisItem | None:
        """An analysis already on the page with the same typed spec and chart, else None."""
        wanted = spec.model_dump(mode="json")
        for item in [*self.workspace.items, *self.items]:
            if item.planned.chart == chart and item.planned.spec.model_dump(mode="json") == wanted:
                return item
        return None

    def _already_shown(self, request: str, item: AnalysisItem) -> Step:
        result = item.result.provenance.result_sha256 if item.result is not None else None
        return self._record(
            request, "already_shown",
            f"This analysis is already shown ({item.planned.title}). Nothing was added or recomputed.",
            spec=item.planned.spec.model_dump(mode="json"), chart=item.planned.chart,
            x=item.planned.x, y=item.planned.y, title=item.planned.title,
            result_sha256=result, verification=item.verification.status,
            analysis_id=item.planned.id,
        )  # fmt: skip

    def _run(self, request: str, plan: Plan) -> Step:
        ws = self.workspace
        shown_already = self._existing(plan.spec, plan.chart)
        if shown_already is not None:
            return self._already_shown(request, shown_already)
        try:
            result = run_operation(ws.dataset, plan.spec, cleaning=ws._cleaning)
        except DatasetError as exc:
            return self._record(
                request, "unsupported", f"The analysis could not run: {shown(exc, 200)}"
            )
        verification = verify_result(ws.dataset, plan.spec, result.table)
        if ws.cleaning_verification is not None and ws.cleaning_verification.status != "verified":
            verification = Verification(
                "unverified", reason="the data cleaning failed its independent check"
            )
        aid = self._next_id()
        planned = PlannedAnalysis(aid, plan.title, plan.spec, plan.chart, plan.x, plan.y)
        svg = render_chart(plan.chart, result.table, plan.x, plan.y, plan.title)
        item = AnalysisItem(planned, result, svg=svg, verification=verification)
        self.items.append(item)
        self._live.append(request)
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
        shown_already = self._existing(plan.spec, change.chart)
        if shown_already is not None:
            return self._already_shown(request, shown_already)
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
        self._live.append(request)
        self._current = (redrawn, item)
        return self._record(
            request, "chart_change", change.interpretation, spec=plan.spec.model_dump(mode="json"),
            chart=change.chart, x=plan.x, y=plan.y, title=plan.title,
            result_sha256=previous.result.provenance.result_sha256,
            verification=previous.verification.status, analysis_id=aid,
        )  # fmt: skip

    # -- cleaning ----------------------------------------------------------------------------------
    def suggestions(self) -> list[Suggestion]:
        """Cleaning worth considering, from the profile. Nothing is applied until ``clean`` is called."""
        ws = self.workspace
        return suggest(ws.dataset, ws.profile, ws.source.sha256)

    def _swap(self, workspace: Workspace, steps: list[CleaningStep]) -> list[str]:
        """Use the rebuilt workspace; returns the requests whose cards must be asked again."""
        again = list(self._live)
        self.workspace, self.cleaning = workspace, steps
        self.items, self._current, self._live = [], None, []
        return again

    def _reask(self, requests: list[str]) -> None:
        self._auto = True
        try:
            for text in requests:
                self.ask(text)
        finally:
            self._auto = False

    def clean(self, step: object) -> Step:
        """Apply one approved cleaning step to a copy of the data. Everything asked so far is asked again
        on the cleaned data, so no card shows numbers from before the change. A step that cannot run, or
        whose independent check fails, changes nothing and is recorded as ``clean_rejected``."""
        try:
            typed = parse_step(step)
        except DatasetError as exc:
            return self._record(
                "Clean: invalid step", "clean_rejected", f"Not applied: {shown(exc, 300)}"
            )
        request = f"Clean: {step_title(typed)}"
        dumped = typed.model_dump(mode="json")
        if self._rebuild is None:
            return self._record(request, "clean_rejected", "Cleaning is not available here.")
        try:
            rebuilt = self._rebuild([*self.cleaning, typed])
        except DatasetError as exc:
            return self._record(
                request,
                "clean_rejected",
                f"Not applied: {shown(exc, 300)}",
                cleaning={"step": dumped},
            )
        check = rebuilt.cleaning_verification
        if check is not None and check.status != "verified":
            return self._record(
                request, "clean_rejected",
                f"Not applied: the independent check of this cleaning failed ({shown(check.reason, 200)}).",
                cleaning={"step": dumped},
            )  # fmt: skip
        entry = rebuilt.cleaning_steps[-1]
        again = self._swap(rebuilt, [*self.cleaning, typed])
        step_record = self._record(
            request, "clean", entry["text"], cleaning={"step": dumped, "entry": _plain(entry)}
        )
        self._reask(again)
        return step_record

    def undo_clean(self) -> Step:
        """Remove the last approved cleaning step (the data is rebuilt from the loaded file)."""
        request = UNDO_REQUEST
        if not self.cleaning or self._rebuild is None:
            return self._record(request, "clean_rejected", "There is no cleaning step to undo.")
        undone = self.cleaning[-1]
        remaining = self.cleaning[:-1]
        rebuilt = self._rebuild(remaining)
        again = self._swap(rebuilt, list(remaining))
        step_record = self._record(
            request, "clean_undo", f"Undid: {step_title(undone)}.",
            cleaning={"undone": undone.model_dump(mode="json")},
        )  # fmt: skip
        self._reask(again)
        return step_record

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
    def rebuild(steps: Sequence[CleaningStep]) -> Workspace:
        return build_workspace(data, name, options=options, limits=limits, plan=plan, steps=steps)

    return Session(rebuild([]), _rebuild=rebuild)


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
        if old.get("auto"):
            continue  # asked again by the session itself; reproduced by the step that caused it
        kind = old.get("kind")
        if kind in ("clean", "clean_rejected") and isinstance(old.get("cleaning"), dict):
            try:
                session.clean(old["cleaning"].get("step"))
            except DatasetError:
                differences.append(
                    f"step {old.get('index')}: the recorded cleaning step is invalid"
                )
        elif kind == "clean_undo" or old["request"] == UNDO_REQUEST:
            session.undo_clean()
        elif kind == "clean_rejected":
            session.clean(None)
        else:
            session.ask(old["request"])
    for i, old in enumerate(recorded):
        if not isinstance(old, dict) or not isinstance(old.get("request"), str):
            continue
        if i >= len(session.steps):
            differences.append(f"step {old.get('index')}: missing in the replay")
            continue
        for key, value in session.steps[i].replay_key().items():
            if old.get(key) != value:
                differences.append(f"step {old.get('index')}: {key} differs from the recorded one")
    if len(session.steps) != len(recorded):
        differences.append("the replay has a different number of steps than the history")
    if differences:
        raise ReplayMismatch(differences)
    return session
