"""Upload ingestion: store a user's file in the raw store, once, per project, safely under concurrency.

One call is one transaction at READ COMMITTED (docs/EVIDENCE_LEDGER_DESIGN.md section 5):

1. lock the project row ``FOR SHARE`` (a purge takes ``FOR UPDATE`` and so waits for us, and we for it);
2. take a transaction-scoped advisory lock on the document key;
3. store the bytes with ``pipeline.raw.store_raw`` (``db`` backend only, so no file can be orphaned);
4. write the ledger rows with ``ON CONFLICT DO NOTHING``;
5. commit. Everything or nothing.

An upload's document URL contains its SHA-256, so a document never gets a second version and the
``max()+1`` numbering race cannot happen here. Collisions that still occur (unique violation, deadlock,
serialization failure) retry the whole transaction a few times, then raise ``IngestBusy``.

Logs carry only the project id and a 12-character hash prefix, never file names or content.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from sie.config import get_settings
from sie.evidence.errors import IngestBusy, ProjectClosed, UploadRejected
from sie.evidence.scope import ProjectScope, scoped_api
from sie.pipeline.raw import store_raw

log = logging.getLogger(__name__)

UPLOAD_SOURCE = "upload"
UPLOAD_TIER = 5  # user-provided (docs/PRODUCT_VISION.md section 6)
RETRYABLE_SQLSTATES = {
    "23505",
    "40001",
    "40P01",
}  # unique violation, serialization failure, deadlock
MAX_ATTEMPTS = 3
_NO_FILES = Path("/nonexistent/uploads-never-use-the-filesystem")  # the db backend never reads it
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitise_filename(name: str) -> str:
    """Last path component, NFC, only letters digits ``._-``, no leading dots, at most 100 characters,
    ``upload`` if nothing is left. The same rules the datasets design specifies (section 10), so a name
    can never act as a path. It is display metadata only: bytes are never stored under it."""
    last = unicodedata.normalize("NFC", name).replace("\\", "/").split("/")[-1]
    cleaned = _SAFE_CHARS.sub("_", last).lstrip(".")[:100]
    return cleaned or "upload"


def upload_url(project_id: int, sha256: str) -> str:
    """The namespaced raw-document key: the same bytes in two projects are two documents."""
    return f"rf-project/{project_id}/upload/{sha256}"


@dataclass(frozen=True)
class IngestResult:
    source_id: int
    document_id: int
    raw_version_id: int
    sha256: str
    outcome: str  # new_version (first time for this project) or unchanged
    created_source: bool


def _advisory_key(url: str) -> str:
    return f"rf:raw:{UPLOAD_SOURCE}|{url}"


def _one_attempt(
    conn: Connection,
    scope: ProjectScope,
    content: bytes,
    sha: str,
    safe_name: str,
    claimed_media_type: str | None,
    title: str,
    added_by: str | None,
    now: datetime,
    hook: Callable[[str], None] | None,
) -> IngestResult:
    isolation = conn.execute(text("SHOW transaction_isolation")).scalar_one()
    if isolation != "read committed":  # the serialisation argument below only holds at this level
        raise IngestBusy(f"ingestion needs READ COMMITTED, the connection is {isolation}")
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text("SET LOCAL statement_timeout = '60s'"))

    row = conn.execute(
        text("SELECT archived_at, purged_at FROM projects WHERE id = :p FOR SHARE"),
        {"p": scope.project_id},
    ).one_or_none()
    if row is None or row.purged_at is not None or row.archived_at is not None:
        raise ProjectClosed("project is archived or purged and takes no new evidence")

    url = upload_url(scope.project_id, sha)
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": _advisory_key(url)}
    )
    if hook:
        hook("locked")

    raw = store_raw(
        conn,
        source=UPLOAD_SOURCE,
        url=url,
        content=content,
        content_type=(claimed_media_type or "application/octet-stream")[:100],
        extension="bin",
        now=now,
        run_id=None,  # ingest_runs needs a competition; an upload has none, and says so honestly
        backend="db",
        raw_dir=_NO_FILES,
    )
    if hook:
        hook("stored")

    conn.execute(
        text(
            """INSERT INTO project_documents (project_id, raw_document_id, role, original_filename,
                                              claimed_media_type, added_at, added_by)
               VALUES (:p, :d, 'upload', :f, :m, :t, :by)
               ON CONFLICT (project_id, raw_document_id) DO NOTHING"""
        ),
        {
            "p": scope.project_id,
            "d": raw.document_id,
            "f": safe_name,
            "m": claimed_media_type[:100] if claimed_media_type else None,
            "t": now,
            "by": added_by,
        },
    )
    inserted = conn.execute(
        text(
            """INSERT INTO evidence_sources (project_id, kind, retention_mode, raw_version_id,
                                             content_sha256, size_bytes, title, tier, retrieved_at,
                                             created_at)
               VALUES (:p, 'upload', 'full', :rv, :h, :n, :title, :tier, :t, :t)
               ON CONFLICT (project_id, raw_version_id) WHERE raw_version_id IS NOT NULL DO NOTHING
               RETURNING id"""
        ),
        {
            "p": scope.project_id,
            "rv": raw.version_id,
            "h": sha,
            "n": len(content),
            "title": title,
            "tier": UPLOAD_TIER,
            "t": now,
        },
    ).scalar_one_or_none()
    source_id = inserted
    if source_id is None:  # a concurrent or earlier ingest of the same bytes won; read its row
        source_id = conn.execute(
            text("SELECT id FROM evidence_sources WHERE project_id = :p AND raw_version_id = :rv"),
            {"p": scope.project_id, "rv": raw.version_id},
        ).scalar_one()
    return IngestResult(
        source_id=source_id,
        document_id=raw.document_id,
        raw_version_id=raw.version_id,
        sha256=sha,
        outcome=raw.outcome,
        created_source=inserted is not None,
    )


@scoped_api
def ingest_upload(
    engine: Engine,
    scope: ProjectScope,
    content: bytes,
    filename: str,
    *,
    now: datetime,
    claimed_media_type: str | None = None,
    added_by: str | None = None,
    max_bytes: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    hook: Callable[[str], None] | None = None,
) -> IngestResult:
    """Store ``content`` as an upload of the project and register it as an evidence source.

    Idempotent: the same bytes into the same project return the same source. The same bytes into a
    different project are a different, independent source (no cross-project sharing or deduplication).
    ``hook`` is a test seam called at named points inside the transaction.
    """
    if not isinstance(content, bytes | bytearray | memoryview):
        raise UploadRejected("not_bytes", "an upload must be bytes")
    content = bytes(content)
    limit = max_bytes if max_bytes is not None else get_settings().evidence_max_upload_bytes
    if not content:
        raise UploadRejected("empty", "an empty file is not evidence")
    if len(content) > limit:
        raise UploadRejected("too_large", f"file is {len(content)} bytes; the limit is {limit}")

    sha = hashlib.sha256(content).hexdigest()  # outside the transaction: nothing to hold open
    safe_name = sanitise_filename(filename)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with (
                engine.connect().execution_options(isolation_level="READ COMMITTED") as conn,
                conn.begin(),
            ):
                result = _one_attempt(
                    conn, scope, content, sha, safe_name, claimed_media_type, safe_name,
                    added_by, now, hook,
                )  # fmt: skip
            log.info(
                "upload ingested",
                extra={
                    "project_id": scope.project_id,
                    "sha256_prefix": sha[:12],
                    "outcome": result.outcome,
                    "attempt": attempt,
                },
            )
            return result
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            if state not in RETRYABLE_SQLSTATES:
                raise
            if attempt == MAX_ATTEMPTS:
                raise IngestBusy(
                    f"upload kept colliding with concurrent writers ({state}); nothing was written"
                ) from exc
            log.warning(
                "upload retry",
                extra={"project_id": scope.project_id, "sqlstate": state, "attempt": attempt},
            )
            sleep(random.uniform(0.05, 0.2) * attempt)
    raise AssertionError("unreachable")  # pragma: no cover
