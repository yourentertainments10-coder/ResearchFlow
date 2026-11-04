"""The JSON-able state of a session for a page or an API client: display text only, no raw objects.

Unverified analyses carry their reason and no numbers, image or table, so nothing unchecked can be shown
as a result. Deterministic; no clock.
"""

from __future__ import annotations

from typing import Any

from sie.workspace.models import AnalysisItem
from sie.workspace.report import _cell, svg_data_uri
from sie.workspace.session import Session

VIEW_ROWS = 200  # table rows sent to the page; exports always carry every row


def _table(item: AnalysisItem) -> dict[str, Any]:
    frame = item.result.table
    rows = [[_cell(v)[0] for v in row] for row in frame.head(VIEW_ROWS).itertuples(index=False)]
    return {
        "columns": [str(c) for c in frame.columns],
        "rows": rows,
        "total_rows": len(frame),
        "truncated": len(frame) > VIEW_ROWS,
    }


def analysis_view(item: AnalysisItem, *, origin: str, request: str | None = None) -> dict[str, Any]:
    p = item.planned
    view: dict[str, Any] = {
        "id": p.id,
        "origin": origin,  # "starter" (chosen by fixed rules) or "asked" (from a request)
        "title": p.title,
        "chart": p.chart,
        "request": request,
        "spec": p.spec.model_dump(mode="json"),
        "exportable": item.exportable,
        "verification": {"status": item.verification.status, "reason": item.verification.reason},
        "error": item.error,
        "image": None,
        "table": None,
    }
    if item.exportable:
        view["table"] = _table(item)
        view["result_sha256"] = item.result.provenance.result_sha256
        view["warnings"] = [str(w) for w in item.result.warnings]
        if item.svg:
            view["image"] = svg_data_uri(item.svg)
    else:
        view["reason"] = (
            item.error
            or item.verification.reason
            or "; ".join(item.verification.mismatches[:3])
            or "not verified"
        )
    return view


def cleaning_view(session: Session) -> dict[str, Any]:
    ws = session.workspace
    check = ws.cleaning_verification
    return {
        "applied": [
            {"text": e["text"], "op": e["op"], "parameters": e["parameters"]}
            for e in ws.cleaning_steps
        ],
        "verification": check.to_dict() if check is not None else None,
        "rows_loaded": ws.original_rows,
        "rows_now": ws.profile.rows,
        "suggestions": [s.to_dict() for s in session.suggestions()],
        "can_undo": bool(session.cleaning),
        "note": "The loaded data is never changed. Cleaning is applied to a copy, one approved step "
        "at a time, and every step is listed here and in the report.",
    }


def session_view(session: Session) -> dict[str, Any]:
    ws = session.workspace
    asked: dict[str, str] = {}
    for s in session.steps:
        if s.analysis_id and s.kind in ("plan", "chart_change"):
            asked.setdefault(s.analysis_id, s.request)
    analyses = [analysis_view(i, origin="starter") for i in ws.items]
    analyses += [
        analysis_view(i, origin="asked", request=asked.get(i.planned.id)) for i in session.items
    ]
    return {
        "source": {
            "name": ws.source.name,
            "sha256": ws.source.sha256,
            "rows": ws.profile.rows,
            "columns": ws.profile.columns,
        },
        "columns": [{"name": c.name, "type": c.type} for c in ws.profile.column_profiles],
        "cards": [
            {"id": c.id, "label": c.label, "value": c.value, "detail": c.detail}
            for c in ws.cards
            if c.verified
        ],
        "findings": [{"severity": f.severity, "kind": f.kind, "text": f.text} for f in ws.findings],
        "analyses": analyses,
        "cleaning": cleaning_view(session),
        "steps": [s.to_dict() for s in session.steps],
        "limitations": ws.limitations,
        "session_sha256": session.sha256(),
    }
