"""CSV-first analysis workspace core (docs/CSV_JOURNEY_DESIGN.md, slice 1).

A library: no printing, no ``sys.exit``, no network, no server. It reuses ``sie.datasets`` for every
calculation and ``sie.answer`` for exact figure display, and re-checks every number it shows with a
separate plain-pandas path (``verify``). Nothing here sends data anywhere.
"""

from sie.workspace.build import Workspace, build_workspace
from sie.workspace.planner import (
    ChartChange,
    NeedsClarification,
    Plan,
    Unsupported,
    plan_request,
)
from sie.workspace.session import ReplayMismatch, Session, Step, replay, start_session

__all__ = [
    "ChartChange",
    "NeedsClarification",
    "Plan",
    "ReplayMismatch",
    "Session",
    "Step",
    "Unsupported",
    "Workspace",
    "build_workspace",
    "plan_request",
    "replay",
    "start_session",
]
