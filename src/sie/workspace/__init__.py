"""CSV-first analysis workspace core (docs/CSV_JOURNEY_DESIGN.md, slice 1).

A library: no printing, no ``sys.exit``, no network, no server. It reuses ``sie.datasets`` for every
calculation and ``sie.answer`` for exact figure display, and re-checks every number it shows with a
separate plain-pandas path (``verify``). Nothing here sends data anywhere.
"""

from sie.workspace.build import Workspace, build_workspace

__all__ = ["Workspace", "build_workspace"]
