"""Claims: immutable extracted statements of a source, each pointing at a place in it.

A claim is never edited. Its status is the latest row in ``claim_reviews`` (append-only), and re-adding
the same claim is a no-op because ``claim_hash`` is unique per project and source.

Two statuses matter and neither means the claim is true:

* ``source_located``  a verifier found the claimed value at the stated place in the stored bytes;
* ``human_confirmed`` a named person confirmed the claim matches its source.

Whether the source itself is right is outside this ledger (docs/EVIDENCE_LEDGER_DESIGN.md section 6).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from sie.evidence.errors import EvidenceError, NotFound
from sie.evidence.locators import nfc, validate_locator
from sie.evidence.scope import ProjectScope, scoped_api

STATUSES = ("unverified", "source_located", "human_confirmed", "disputed", "rejected")
SYSTEM_PREFIX = "system:"
EXTRACTION_METHODS = ("manual", "rule", "model")
XLSX_LIMITATION = (
    "xlsx_cached_value_not_evaluated"  # formulas are never evaluated (datasets design)
)


@dataclass(frozen=True)
class ClaimInput:
    subject: str
    metric: str
    value_text: str
    excerpt: str
    locator_kind: str
    locator: dict[str, Any]
    extraction_method: str
    extractor_version: str
    period: str | None = None
    unit: str | None = None
    value_numeric: Decimal | None = None
    value_source: str = "literal"
    limitations: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ClaimResult:
    claim_id: int
    created: bool
    claim_hash: str


@dataclass(frozen=True)
class ClaimInfo:
    id: int
    source_id: int
    subject: str
    metric: str
    period: str | None
    unit: str | None
    value_text: str
    value_numeric: Decimal | None
    value_source: str
    locator_kind: str
    locator: dict[str, Any]
    excerpt: str
    excerpt_sha256: str
    extraction_method: str
    extractor_version: str
    claim_hash: str
    limitations: list[str]
    status: str  # the latest review, or 'unverified'


def _canonical_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def claim_hash(claim: ClaimInput) -> str:
    """SHA-256 of the canonical claim content. The same claim always hashes the same."""
    body = {
        "subject": nfc(claim.subject),
        "metric": nfc(claim.metric),
        "period": nfc(claim.period) if claim.period else None,
        "unit": nfc(claim.unit) if claim.unit else None,
        "value_text": nfc(claim.value_text),
        "value_numeric": _canonical_decimal(claim.value_numeric),
        "value_source": claim.value_source,
        "locator_kind": claim.locator_kind,
        "locator": claim.locator,
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def excerpt_sha256(excerpt: str) -> str:
    return hashlib.sha256(nfc(excerpt).encode("utf-8")).hexdigest()


def _validate(claim: ClaimInput) -> None:
    for name in ("subject", "metric"):
        value = getattr(claim, name)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 300:
            raise EvidenceError(f"{name} must be 1 to 300 characters")
    if not isinstance(claim.value_text, str) or not 1 <= len(claim.value_text) <= 1000:
        raise EvidenceError("value_text must be 1 to 1000 characters")
    if not isinstance(claim.excerpt, str) or not 1 <= len(claim.excerpt) <= 500:
        raise EvidenceError("excerpt must be 1 to 500 characters (a quote, never a whole page)")
    if claim.extraction_method not in EXTRACTION_METHODS:
        raise EvidenceError(f"extraction_method must be one of {EXTRACTION_METHODS}")
    if not claim.extractor_version:
        raise EvidenceError("extractor_version is required")
    if claim.value_source not in ("literal", "cached_formula_value", "excerpt"):
        raise EvidenceError("unknown value_source")
    if claim.value_numeric is not None and (
        not isinstance(claim.value_numeric, Decimal) or not claim.value_numeric.is_finite()
    ):
        raise EvidenceError("value_numeric must be a finite Decimal (floats are refused)")
    validate_locator(claim.locator_kind, claim.locator)
    if claim.value_source == "cached_formula_value" and claim.locator_kind != "xlsx_cell":
        raise EvidenceError("cached_formula_value only applies to an xlsx_cell locator")


def _source_row(conn: Connection, scope: ProjectScope, source_id: int):
    row = conn.execute(
        text("SELECT id FROM evidence_sources WHERE project_id = :p AND id = :s"),
        {"p": scope.project_id, "s": source_id},
    ).one_or_none()
    if row is None:
        raise NotFound("no such source")
    return row


@scoped_api
def add_claim(
    conn: Connection,
    scope: ProjectScope,
    source_id: int,
    claim: ClaimInput,
    *,
    now: datetime,
    created_by: str | None = None,
) -> ClaimResult:
    """Record a claim of a source of this project. Re-adding the identical claim returns the same id."""
    _validate(claim)
    _source_row(conn, scope, source_id)
    limitations = list(claim.limitations)
    if claim.locator_kind == "xlsx_cell" and XLSX_LIMITATION not in limitations:
        limitations.append(XLSX_LIMITATION)
    digest = claim_hash(claim)
    inserted = conn.execute(
        text(
            """INSERT INTO claims (project_id, source_id, subject, metric, period, unit, value_text,
                                   value_numeric, value_source, locator_kind, locator, excerpt,
                                   excerpt_sha256, extraction_method, extractor_version, claim_hash,
                                   limitations, created_at, created_by)
               VALUES (:p, :s, :subject, :metric, :period, :unit, :vt, :vn, :vs, :lk,
                       CAST(:loc AS jsonb), :ex, :exh, :em, :ev, :ch, :lim, :t, :by)
               ON CONFLICT (project_id, source_id, claim_hash) DO NOTHING
               RETURNING id"""
        ),
        {
            "p": scope.project_id,
            "s": source_id,
            "subject": nfc(claim.subject.strip()),
            "metric": nfc(claim.metric.strip()),
            "period": claim.period,
            "unit": claim.unit,
            "vt": nfc(claim.value_text),
            "vn": claim.value_numeric,
            "vs": claim.value_source,
            "lk": claim.locator_kind,
            "loc": json.dumps(claim.locator, sort_keys=True),
            "ex": nfc(claim.excerpt),
            "exh": excerpt_sha256(claim.excerpt),
            "em": claim.extraction_method,
            "ev": claim.extractor_version,
            "ch": digest,
            "lim": limitations,
            "t": now,
            "by": created_by,
        },
    ).scalar_one_or_none()
    if inserted is not None:
        return ClaimResult(inserted, True, digest)
    existing = conn.execute(
        text("SELECT id FROM claims WHERE project_id = :p AND source_id = :s AND claim_hash = :h"),
        {"p": scope.project_id, "s": source_id, "h": digest},
    ).scalar_one()
    return ClaimResult(existing, False, digest)


_CLAIM_COLUMNS = """c.id, c.source_id, c.subject, c.metric, c.period, c.unit, c.value_text,
                    c.value_numeric, c.value_source, c.locator_kind, c.locator, c.excerpt,
                    c.excerpt_sha256, c.extraction_method, c.extractor_version, c.claim_hash,
                    c.limitations, v.status"""


@scoped_api
def get_claim(conn: Connection, scope: ProjectScope, claim_id: int) -> ClaimInfo:
    row = conn.execute(
        text(
            f"""SELECT {_CLAIM_COLUMNS} FROM claims c
                JOIN evidence_claims_current v ON v.claim_id = c.id AND v.project_id = c.project_id
                WHERE c.project_id = :p AND c.id = :c"""
        ),
        {"p": scope.project_id, "c": claim_id},
    ).one_or_none()
    if row is None:
        raise NotFound("no such claim")
    return ClaimInfo(*tuple(row))


@scoped_api
def list_claims(
    conn: Connection, scope: ProjectScope, source_id: int | None = None
) -> list[ClaimInfo]:
    if source_id is None:
        rows = conn.execute(
            text(
                f"""SELECT {_CLAIM_COLUMNS} FROM claims c
                    JOIN evidence_claims_current v ON v.claim_id = c.id AND v.project_id = c.project_id
                    WHERE c.project_id = :p ORDER BY c.id"""
            ),
            {"p": scope.project_id},
        ).all()
    else:
        _source_row(conn, scope, source_id)
        rows = conn.execute(
            text(
                f"""SELECT {_CLAIM_COLUMNS} FROM claims c
                    JOIN evidence_claims_current v ON v.claim_id = c.id AND v.project_id = c.project_id
                    WHERE c.project_id = :p AND c.source_id = :s ORDER BY c.id"""
            ),
            {"p": scope.project_id, "s": source_id},
        ).all()
    return [ClaimInfo(*tuple(r)) for r in rows]


@scoped_api
def review_claim(
    conn: Connection,
    scope: ProjectScope,
    claim_id: int,
    *,
    status: str,
    reason: str,
    actor: str,
    now: datetime,
    verifier_version: str | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> int:
    """Append a review. ``source_located`` is for verifiers (``system:`` actors), ``human_confirmed``
    for named people; the database enforces the same split. Returns the review id."""
    if status not in STATUSES:
        raise EvidenceError(f"status must be one of {STATUSES}")
    if not reason.strip():
        raise EvidenceError("a review needs a reason")
    if not actor.strip():
        raise EvidenceError("a review needs an actor")
    is_system = actor.startswith(SYSTEM_PREFIX)
    if status == "source_located" and not is_system:
        raise EvidenceError("source_located is set by a verifier, not by a person")
    if status == "human_confirmed" and is_system:
        raise EvidenceError("human_confirmed needs a named person, not a system actor")
    found = conn.execute(
        text("SELECT id FROM claims WHERE project_id = :p AND id = :c FOR SHARE"),
        {"p": scope.project_id, "c": claim_id},
    ).one_or_none()
    if found is None:
        raise NotFound("no such claim")
    return conn.execute(
        text(
            """INSERT INTO claim_reviews (project_id, claim_id, status, reason, actor, verifier_version,
                                          checks, at)
               VALUES (:p, :c, :st, :r, :a, :v, CAST(:chk AS jsonb), :t) RETURNING id"""
        ),
        {
            "p": scope.project_id,
            "c": claim_id,
            "st": status,
            "r": reason.strip(),
            "a": actor.strip(),
            "v": verifier_version,
            "chk": None if checks is None else json.dumps(checks),
            "t": now,
        },
    ).scalar_one()
