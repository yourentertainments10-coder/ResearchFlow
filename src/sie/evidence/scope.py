"""Project scope: the one door every evidence function goes through.

A ``ProjectScope`` can only be made by :func:`open_scope`, which checks that the project exists and is
not purged. Every public evidence function takes one and filters by its ``project_id``; the database
adds composite foreign keys as a second layer. Authentication will later plug in right here, so that
"who may open this project" is decided in one place (docs/EVIDENCE_LEDGER_DESIGN.md section 8).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from sie.evidence.errors import EvidenceError, NotFound, ProjectClosed

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TOKEN = object()  # only open_scope holds it, so a scope cannot be forged by accident


@dataclass(frozen=True)
class ProjectScope:
    project_id: int
    slug: str
    _issued_by: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued_by is not _TOKEN:
            raise EvidenceError("build a ProjectScope with open_scope(), not directly")


# Every public function that takes a scope is registered here, so a test can insist that each one has a
# cross-project case (tests/integration/test_evidence_isolation.py). A new function cannot skip it.
SCOPED_API: dict[str, Callable] = {}


def scoped_api(fn):
    """Register a public scoped function. Use as a decorator on every such function."""
    key = f"{fn.__module__}.{fn.__name__}"
    if key in SCOPED_API:
        raise EvidenceError(f"{key} is registered twice")
    SCOPED_API[key] = fn
    return fn


def create_project(conn: Connection, *, slug: str, name: str, now: datetime) -> int:
    """Create a project and return its id. The slug is lower-case letters, digits and hyphens."""
    if not SLUG_RE.match(slug):
        raise EvidenceError("project slug must match [a-z0-9][a-z0-9-]{0,62}")
    if not name.strip():
        raise EvidenceError("project name is required")
    try:
        with conn.begin_nested():
            return conn.execute(
                text(
                    "INSERT INTO projects (slug, name, created_at) VALUES (:s, :n, :t) RETURNING id"
                ),
                {"s": slug, "n": name.strip(), "t": now},
            ).scalar_one()
    except IntegrityError:
        raise EvidenceError(f"project {slug!r} already exists") from None


def open_scope(conn: Connection, project: str | int, *, for_write: bool = False) -> ProjectScope:
    """The scope for a project given by slug or id.

    Purged projects are never opened. Archived ones can be read but not written to.
    """
    if isinstance(project, int):
        query = "SELECT id, slug, archived_at, purged_at FROM projects WHERE id = :p"
    else:
        query = "SELECT id, slug, archived_at, purged_at FROM projects WHERE slug = :p"
    row = conn.execute(text(query), {"p": project}).one_or_none()
    if row is None:
        raise NotFound("no such project")
    if row.purged_at is not None:
        raise ProjectClosed("project was purged")
    if for_write and row.archived_at is not None:
        raise ProjectClosed("project is archived and takes no new evidence")
    return ProjectScope(row.id, row.slug, _TOKEN)
