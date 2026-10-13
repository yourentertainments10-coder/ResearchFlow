"""Retention of fetched web content, and registering a fetched source.

Default is ``excerpt``: the ledger keeps the URL, status, headers, retrieval time, the SHA-256 and size of
what was fetched, and (as claims) the short excerpts that back a statement. The full page is not stored.
``full`` is allowed only for a host with a recorded policy (``data/evidence/source_policies.csv``): who
decided, which terms or licence, when. Even then it is kept for ``EVIDENCE_FULL_RETENTION_DAYS`` (default
90) and dropped by the owner-run ``sie evidence prune``. Nothing here makes a network request.

What an excerpt-only source gives up: its bytes are gone, so a claim can be checked against them only at
the moment of fetching; later the ledger can re-check just the stored excerpt's own hash
(docs/EVIDENCE_RETENTION.md).
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Connection, Engine, text

from sie.config import get_settings
from sie.evidence.errors import EvidenceError, PolicyRefused, UploadRejected
from sie.evidence.ingest import IngestResult
from sie.evidence.scope import ProjectScope, scoped_api
from sie.evidence.txn import advisory_lock, run_ingest_txn
from sie.pipeline.raw import store_raw

POLICY_COLUMNS = ("host", "terms_ref", "recorded_on", "allows_full", "licence", "notes")
MODES = ("full", "excerpt", "metadata")
WEB_SOURCE = "web"


@dataclass(frozen=True)
class SourcePolicy:
    host: str
    terms_ref: str
    recorded_on: date
    allows_full: bool
    licence: str
    notes: str


def load_policies(path: Path) -> dict[str, SourcePolicy]:
    """The recorded policies by host. A missing or empty file means no host may be kept in full."""
    if not path.exists():
        return {}
    out: dict[str, SourcePolicy] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != POLICY_COLUMNS:
            raise EvidenceError(f"{path.name} must have the columns {', '.join(POLICY_COLUMNS)}")
        for n, row in enumerate(reader, start=2):
            host = (row["host"] or "").strip().lower()
            allows = (row["allows_full"] or "").strip().lower()
            if not host or allows not in ("true", "false"):
                raise EvidenceError(
                    f"{path.name} line {n}: host and allows_full (true/false) needed"
                )
            terms = (row["terms_ref"] or "").strip()
            if allows == "true" and not terms:
                raise EvidenceError(
                    f"{path.name} line {n}: a policy that allows full needs terms_ref"
                )
            try:
                recorded = date.fromisoformat((row["recorded_on"] or "").strip())
            except ValueError as exc:
                raise EvidenceError(
                    f"{path.name} line {n}: recorded_on must be YYYY-MM-DD"
                ) from exc
            if host in out:
                raise EvidenceError(f"{path.name} line {n}: host {host} is listed twice")
            out[host] = SourcePolicy(
                host, terms, recorded, allows == "true", (row["licence"] or "").strip(),
                (row["notes"] or "").strip(),
            )  # fmt: skip
    return out


def policy_for(policies: dict[str, SourcePolicy], url: str) -> SourcePolicy | None:
    """The policy of the URL's host, or of a parent domain (``www.x.org`` matches ``x.org``)."""
    host = (urlsplit(url).hostname or "").lower()
    parts = host.split(".")
    for i in range(len(parts) - 1):  # never match a bare top-level domain
        candidate = ".".join(parts[i:])
        if candidate in policies:
            return policies[candidate]
    return None


def normalise_url(url: str) -> str:
    """http(s) only, no credentials, no fragment, lower-case scheme and host. The path and query are
    kept exactly (a query can be the content), including tracking parameters."""
    if not isinstance(url, str) or len(url) > 2000:
        raise EvidenceError("a source URL must be a string of at most 2000 characters")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise EvidenceError("a source URL must be http or https with a host")
    if parts.username or parts.password:
        raise EvidenceError("a source URL must not contain credentials")
    host = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


def web_url(project_id: int, requested_url: str) -> str:
    """The namespaced raw-document key of a full web copy: one document per project and URL."""
    return f"rf-project/{project_id}/web/{requested_url}"


@scoped_api
def register_fetched_source(
    engine: Engine,
    scope: ProjectScope,
    *,
    content: bytes,
    requested_url: str,
    title: str,
    tier: int,
    now: datetime,
    final_url: str | None = None,
    http_status: int | None = None,
    content_type: str | None = None,
    retention_mode: str = "excerpt",
    policies: dict[str, SourcePolicy] | None = None,
    retention_days: int | None = None,
    added_by: str | None = None,
    max_bytes: int | None = None,
    sleep=None,
) -> IngestResult:
    """Register bytes that were already fetched (by Phase 4 code, or by hand) as a web source.

    ``retention_mode='full'`` is refused unless the host has a recorded policy that allows it.
    ``excerpt`` and ``metadata`` store no bytes at all.
    """
    settings = get_settings()
    if retention_mode not in MODES:
        raise EvidenceError(f"retention_mode must be one of {MODES}")
    if not isinstance(content, bytes | bytearray | memoryview) or not bytes(content):
        raise UploadRejected("empty", "fetched content must be non-empty bytes")
    content = bytes(content)
    limit = max_bytes if max_bytes is not None else settings.evidence_max_upload_bytes
    if len(content) > limit:
        raise UploadRejected("too_large", f"content is {len(content)} bytes; the limit is {limit}")
    if not 1 <= tier <= 4:
        raise EvidenceError("a fetched web source has tier 1 to 4 (5 is for user-provided files)")
    if not title.strip():
        raise EvidenceError("a source needs a title")
    requested = normalise_url(requested_url)
    final = normalise_url(final_url) if final_url else None
    sha = hashlib.sha256(content).hexdigest()

    terms_ref: str | None = None
    retain_until: datetime | None = None
    if retention_mode == "full":
        table = policies if policies is not None else load_policies(settings.evidence_policy_file)
        policy = policy_for(table, final or requested) or policy_for(table, requested)
        if policy is None or not policy.allows_full:
            raise PolicyRefused(
                "keeping this page in full needs a recorded policy for its host "
                "(data/evidence/source_policies.csv); it can be kept as excerpts"
            )
        days = (
            retention_days if retention_days is not None else settings.evidence_full_retention_days
        )
        if days < 1:
            raise EvidenceError("retention_days must be at least 1")
        terms_ref = f"{policy.host}: {policy.terms_ref} (recorded {policy.recorded_on.isoformat()})"
        retain_until = now + timedelta(days=days)

    common = {
        "p": scope.project_id, "h": sha, "n": len(content), "title": title.strip()[:300],
        "req": requested, "fin": final, "st": http_status, "ct": content_type[:200] if content_type else None,
        "tier": tier, "t": now,
    }  # fmt: skip

    def work(conn: Connection) -> IngestResult:
        if retention_mode == "full":
            url = web_url(scope.project_id, requested)
            advisory_lock(conn, f"rf:raw:{WEB_SOURCE}|{url}")
            raw = store_raw(
                conn, source=WEB_SOURCE, url=url, content=content,
                content_type=(content_type or "application/octet-stream")[:200], extension="bin",
                now=now, run_id=None, backend="db", raw_dir=Path("/nonexistent/web-never-use-fs"),
            )  # fmt: skip
            conn.execute(
                text(
                    """INSERT INTO project_documents (project_id, raw_document_id, role, added_at, added_by)
                       VALUES (:p, :d, 'fetched', :t, :by)
                       ON CONFLICT (project_id, raw_document_id) DO NOTHING"""
                ),
                {"p": scope.project_id, "d": raw.document_id, "t": now, "by": added_by},
            )
            inserted = conn.execute(
                text(
                    """INSERT INTO evidence_sources (project_id, kind, retention_mode, raw_version_id,
                           content_sha256, size_bytes, title, requested_url, final_url, http_status,
                           content_type, tier, terms_ref, retrieved_at, retain_until, created_at)
                       VALUES (:p, 'web', 'full', :rv, :h, :n, :title, :req, :fin, :st, :ct, :tier,
                               :terms, :t, :until, :t)
                       ON CONFLICT (project_id, raw_version_id) WHERE raw_version_id IS NOT NULL
                       DO NOTHING RETURNING id"""
                ),
                {**common, "rv": raw.version_id, "terms": terms_ref, "until": retain_until},
            ).scalar_one_or_none()
            source_id = (
                inserted
                or conn.execute(
                    text(
                        "SELECT id FROM evidence_sources WHERE project_id = :p AND raw_version_id = :rv"
                    ),
                    {"p": scope.project_id, "rv": raw.version_id},
                ).scalar_one()
            )
            return IngestResult(
                source_id, raw.document_id, raw.version_id, sha, raw.outcome, inserted is not None
            )
        advisory_lock(conn, f"rf:ledger:{WEB_SOURCE}|{scope.project_id}|{requested}|{sha}")
        inserted = conn.execute(
            text(
                """INSERT INTO evidence_sources (project_id, kind, retention_mode, content_sha256,
                       size_bytes, title, requested_url, final_url, http_status, content_type, tier,
                       retrieved_at, created_at)
                   VALUES (:p, 'web', :mode, :h, :n, :title, :req, :fin, :st, :ct, :tier, :t, :t)
                   ON CONFLICT (project_id, requested_url, content_sha256) WHERE raw_version_id IS NULL
                   DO NOTHING RETURNING id"""
            ),
            {**common, "mode": retention_mode},
        ).scalar_one_or_none()
        source_id = (
            inserted
            or conn.execute(
                text(
                    """SELECT id FROM evidence_sources WHERE project_id = :p AND requested_url = :req
                   AND content_sha256 = :h AND raw_version_id IS NULL"""
                ),
                {"p": scope.project_id, "req": requested, "h": sha},
            ).scalar_one()
        )
        return IngestResult(
            source_id,
            None,
            None,
            sha,
            "new_version" if inserted else "unchanged",
            inserted is not None,
        )

    kwargs = {} if sleep is None else {"sleep": sleep}
    return run_ingest_txn(
        engine,
        scope.project_id,
        work,
        op="web source registered",
        log_extra={"sha256_prefix": sha[:12], "mode": retention_mode},
        **kwargs,
    )
