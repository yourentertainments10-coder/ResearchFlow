"""Raw store: keep what was fetched exactly as received, versioned by content hash.

Rules (docs/DATA_PIPELINE.md section 4): the same URL with the same hash is ``unchanged``; a new hash
is a ``new_version``; a hash equal to an older version is ``reverted`` (no new row or bytes, the
document's latest pointer moves back). Bytes are write-once. Every call is logged in ``raw_fetches``.

Two backends hold the bytes: ``db`` (gzip in ``raw_blobs``, transactional, for hosted runs) and
``fs`` (a file under the raw directory). ``raw_versions.storage_backend`` records which one a version
used, so a history that mixes both still reads back.
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import Connection, text

Outcome = Literal["new_version", "unchanged", "reverted"]
BACKENDS = ("db", "fs")


class RawStoreError(RuntimeError):
    """Raw bytes cannot be stored or read back intact."""


@dataclass(frozen=True)
class RawVersion:
    document_id: int
    version_id: int
    version_no: int
    sha256: str
    outcome: Outcome


def storage_key(source: str, sha256: str, first_fetched: datetime, extension: str) -> str:
    """``<source>/<YYYY-MM-DD>/<HHMMSS>_<sha256-first-12>.<ext>`` (docs/DATA_PIPELINE.md section 4)."""
    return (
        f"{source}/{first_fetched:%Y-%m-%d}/{first_fetched:%H%M%S}_{sha256[:12]}"
        f".{extension.lstrip('.')}"
    )


def _write_once(root: Path, key: str, content: bytes) -> None:
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:  # exclusive: never overwrite an existing object
            handle.write(content)
    except FileExistsError:
        if target.read_bytes() != content:
            raise RawStoreError(f"{key} already exists with different bytes") from None


def store_raw(
    conn: Connection,
    *,
    source: str,
    url: str,
    content: bytes,
    content_type: str,
    extension: str,
    now: datetime,
    run_id: int | None,
    backend: str,
    raw_dir: Path,
) -> RawVersion:
    if backend not in BACKENDS:
        raise RawStoreError(f"unknown raw storage backend {backend!r}; use one of {BACKENDS}")
    sha = hashlib.sha256(content).hexdigest()
    doc = conn.execute(
        text(
            """INSERT INTO raw_documents (source, url, first_seen_at) VALUES (:s, :u, :n)
               ON CONFLICT (source, url) DO UPDATE SET source = EXCLUDED.source
               RETURNING id, latest_version_id"""
        ),
        {"s": source, "u": url, "n": now},
    ).one()
    existing = conn.execute(
        text("SELECT id, version_no FROM raw_versions WHERE document_id = :d AND sha256 = :h"),
        {"d": doc.id, "h": sha},
    ).one_or_none()

    if existing is not None:
        outcome: Outcome = "unchanged" if existing.id == doc.latest_version_id else "reverted"
        if outcome == "reverted":
            conn.execute(
                text("UPDATE raw_documents SET latest_version_id = :v WHERE id = :d"),
                {"v": existing.id, "d": doc.id},
            )
        version_id, version_no = existing.id, existing.version_no
    else:
        outcome = "new_version"
        version_no = conn.execute(
            text(
                "SELECT coalesce(max(version_no), 0) + 1 FROM raw_versions WHERE document_id = :d"
            ),
            {"d": doc.id},
        ).scalar_one()
        path: str | None = None
        if backend == "fs":
            path = storage_key(source, sha, now, extension)
            _write_once(raw_dir, path, content)  # before the row: a row never points at nothing
        version_id = conn.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, storage_backend,
                                             size_bytes, content_type, first_fetched_at)
                   VALUES (:d, :no, :h, :p, :b, :sz, :ct, :n) RETURNING id"""
            ),
            {
                "d": doc.id, "no": version_no, "h": sha, "p": path, "b": backend,
                "sz": len(content), "ct": content_type, "n": now,
            },
        ).scalar_one()  # fmt: skip
        if backend == "db":
            conn.execute(
                text("INSERT INTO raw_blobs (version_id, content) VALUES (:v, :c)"),
                {"v": version_id, "c": gzip.compress(content, mtime=0)},
            )
        conn.execute(
            text("UPDATE raw_documents SET latest_version_id = :v WHERE id = :d"),
            {"v": version_id, "d": doc.id},
        )

    conn.execute(
        text(
            """INSERT INTO raw_fetches (document_id, version_id, run_id, fetched_at, outcome)
               VALUES (:d, :v, :r, :n, :o)"""
        ),
        {"d": doc.id, "v": version_id, "r": run_id, "n": now, "o": outcome},
    )
    return RawVersion(doc.id, version_id, version_no, sha, outcome)


def read_raw(conn: Connection, version_id: int, raw_dir: Path) -> bytes:
    """The bytes of a stored version, checked against the recorded hash."""
    row = conn.execute(
        text("SELECT sha256, path, storage_backend FROM raw_versions WHERE id = :v"),
        {"v": version_id},
    ).one_or_none()
    if row is None:
        raise RawStoreError(f"no raw version {version_id}")
    if row.storage_backend == "db":
        blob = conn.execute(
            text("SELECT content FROM raw_blobs WHERE version_id = :v"), {"v": version_id}
        ).scalar_one_or_none()
        if blob is None:
            raise RawStoreError(f"raw version {version_id} has no stored bytes")
        content = gzip.decompress(bytes(blob))
    elif row.storage_backend == "fs":
        content = (raw_dir / row.path).read_bytes()
    else:
        raise RawStoreError(f"backend {row.storage_backend!r} cannot be read here")
    if hashlib.sha256(content).hexdigest() != row.sha256:
        raise RawStoreError(f"raw version {version_id} no longer matches its recorded hash")
    return content
