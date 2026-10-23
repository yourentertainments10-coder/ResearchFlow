"""Reading sources back, always through a project scope.

An id that belongs to another project raises the same ``NotFound`` as an id that does not exist, so the
answer never reveals that someone else holds it. Callers pass evidence-source ids only: there is no
function that accepts a raw document id, a raw version id or a bare hash.
"""

from __future__ import annotations

import gzip
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, text

from sie.config import get_settings
from sie.evidence.errors import BytesNotRetained, EvidenceError, NotFound
from sie.evidence.scope import ProjectScope, scoped_api
from sie.pipeline.raw import RawStoreError


@dataclass(frozen=True)
class SourceInfo:
    id: int
    kind: str
    retention_mode: str
    title: str
    content_sha256: str
    size_bytes: int
    tier: int
    requested_url: str | None
    final_url: str | None
    http_status: int | None
    content_type: str | None
    terms_ref: str | None
    retrieved_at: datetime
    retain_until: datetime | None
    pruned_at: datetime | None
    original_filename: str | None


_COLUMNS = """s.id, s.kind, s.retention_mode, s.title, s.content_sha256, s.size_bytes, s.tier,
              s.requested_url, s.final_url, s.http_status, s.content_type, s.terms_ref, s.retrieved_at,
              s.retain_until, s.pruned_at,
              (SELECT d.original_filename FROM project_documents d
                 JOIN raw_versions v ON v.document_id = d.raw_document_id
                WHERE d.project_id = s.project_id AND v.id = s.raw_version_id) AS original_filename"""


@scoped_api
def list_sources(conn: Connection, scope: ProjectScope) -> list[SourceInfo]:
    rows = conn.execute(
        text(f"SELECT {_COLUMNS} FROM evidence_sources s WHERE s.project_id = :p ORDER BY s.id"),
        {"p": scope.project_id},
    ).all()
    return [SourceInfo(*tuple(r)) for r in rows]


@scoped_api
def get_source(conn: Connection, scope: ProjectScope, source_id: int) -> SourceInfo:
    row = conn.execute(
        text(f"SELECT {_COLUMNS} FROM evidence_sources s WHERE s.project_id = :p AND s.id = :s"),
        {"p": scope.project_id, "s": source_id},
    ).one_or_none()
    if row is None:
        raise NotFound("no such source")
    return SourceInfo(*tuple(row))


@scoped_api
def read_bytes(conn: Connection, scope: ProjectScope, source_id: int) -> bytes:
    """The stored bytes of a source, re-hashed against the ledger's hash and size-capped.

    Only ``full`` sources have bytes. The decompressed size is bounded before it is held in memory.
    """
    row = conn.execute(
        text(
            """SELECT s.raw_version_id, s.content_sha256, s.retention_mode
               FROM evidence_sources s WHERE s.project_id = :p AND s.id = :s"""
        ),
        {"p": scope.project_id, "s": source_id},
    ).one_or_none()
    if row is None:
        raise NotFound("no such source")
    if row.raw_version_id is None:
        raise BytesNotRetained(
            f"this source is kept as {row.retention_mode}; its bytes are not stored"
        )
    return read_version_bytes(conn, row.raw_version_id, row.content_sha256)


def read_version_bytes(conn: Connection, raw_version_id: int, expected_sha256: str) -> bytes:
    """Internal: decompress a stored blob with a hard ceiling and check its hash.

    Not scoped on purpose and not exported: callers must have resolved the version through a scoped
    source row first.
    """
    ceiling = get_settings().evidence_max_decompressed_bytes
    blob = conn.execute(
        text("SELECT content FROM raw_blobs WHERE version_id = :v"), {"v": raw_version_id}
    ).scalar_one_or_none()
    if blob is None:
        raise RawStoreError(f"raw version {raw_version_id} has no stored bytes")
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(bytes(blob))) as stream:
        while chunk := stream.read(1024 * 1024):
            out += chunk
            if len(out) > ceiling:
                raise EvidenceError(f"stored bytes exceed the {ceiling} byte decompression ceiling")
    if hashlib.sha256(out).hexdigest() != expected_sha256:
        raise RawStoreError("stored bytes no longer match the recorded hash")
    return bytes(out)
