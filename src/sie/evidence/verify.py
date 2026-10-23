"""Verifying that a claim's value is *located* in its stored source.

``source_located`` means exactly this and nothing more: the locator resolves, in the stored bytes
re-hashed against the ledger, to a place whose text equals the claimed value (and, for a numeric claim,
whose deterministic parse equals ``value_numeric``). It does not mean the value is correct, current or
trustworthy. It is written only by this module, as a ``system:`` actor, and a person's
``human_confirmed``, ``disputed`` or ``rejected`` is never overwritten by a re-check.

Cases that cannot be checked say why instead of guessing:

* XLSX: pending the shared datasets loader (formulas are never evaluated; a cached value would be
  reported as cached). No reader is duplicated here.
* excerpt-only, metadata-only or pruned sources: the bytes are gone, so only the stored excerpt's own
  hash can be re-checked, which is weaker and is recorded as such.
* PDF pages, character ranges and the rest: not verifiable in this phase.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from sie.evidence.claims import excerpt_sha256, get_claim, review_claim
from sie.evidence.errors import NotFound
from sie.evidence.locators import (
    NotLocated,
    TimeBudgetExceeded,
    locate_csv,
    locate_json,
    nfc,
    parse_number,
)
from sie.evidence.scope import ProjectScope, scoped_api
from sie.evidence.sources import read_version_bytes

VERIFIER_VERSION = "evidence-verify/1"
VERIFIER_ACTOR = "system:verifier"
DEFAULT_BUDGET_SECONDS = 60.0
NOT_A_TRUTH_CHECK = "this records where the value was found in the source; it does not establish that the value is true"
PENDING_XLSX = "xlsx_verification_pending_datasets_loader"
# A person's decision stands: a re-check may not replace these with a machine result.
HUMAN_OWNED = ("human_confirmed", "disputed", "rejected")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class VerificationResult:
    claim_id: int
    status: str  # the status this check concluded: source_located or unverified
    reason: str
    checks: tuple[Check, ...]
    written: bool  # False when the claim already had that outcome, or a person's decision stands


def _fail(reason: str, checks: list[Check]) -> tuple[str, str, list[Check]]:
    return "unverified", reason, checks


def _evaluate(conn: Connection, claim, source, deadline: float) -> tuple[str, str, list[Check]]:
    checks: list[Check] = []
    own = Check(
        "excerpt_hash",
        excerpt_sha256(claim.excerpt) == claim.excerpt_sha256,
        "the stored excerpt matches its recorded hash",
    )
    checks.append(own)
    if not own.passed:
        return _fail("the stored excerpt no longer matches its recorded hash", checks)

    if claim.locator_kind == "xlsx_cell":
        checks.append(Check("xlsx", False, PENDING_XLSX))
        return _fail(PENDING_XLSX, checks)
    if claim.locator_kind not in ("table_cell", "json_pointer"):
        reason = f"locator_kind_{claim.locator_kind}_not_verifiable_in_this_phase"
        checks.append(Check("locator_kind", False, reason))
        return _fail(reason, checks)
    if source.raw_version_id is None:
        reason = (
            f"bytes_not_retained: source is kept as {source.retention_mode}; only the excerpt's own "
            "hash could be re-checked"
        )
        checks.append(Check("bytes_retained", False, reason))
        return _fail(reason, checks)

    try:
        content = read_version_bytes(conn, source.raw_version_id, source.content_sha256)
    except Exception as exc:  # noqa: BLE001 - a corrupt or oversized blob is a failed check, with its reason
        checks.append(Check("stored_bytes", False, f"{type(exc).__name__}: {exc}"))
        return _fail(f"stored bytes unusable: {exc}", checks)
    checks.append(Check("stored_bytes", True, "re-hashed against the ledger"))

    try:
        if claim.locator_kind == "table_cell":
            located = locate_csv(content, claim.locator, deadline)
        else:
            located = locate_json(content, claim.locator, deadline)
    except TimeBudgetExceeded:
        checks.append(Check("location", False, "time_budget_exceeded"))
        return _fail("time_budget_exceeded", checks)
    except NotLocated as exc:
        checks.append(Check("location", False, f"{exc.code}: {exc}"))
        return _fail(f"not_located: {exc.code}", checks)
    checks.append(Check("location", True, f"the locator resolves to a {located.kind}"))

    same_text = nfc(claim.value_text) == located.text
    checks.append(
        Check(
            "value_text", same_text, "equals the text at the location" if same_text else "differs"
        )
    )
    same_excerpt = nfc(claim.excerpt) == located.text
    checks.append(
        Check(
            "excerpt",
            same_excerpt,
            "equals the text at the location" if same_excerpt else "the quoted excerpt differs",
        )
    )
    numeric_ok = True
    if claim.value_numeric is not None:
        fmt = claim.locator.get("number_format") if claim.locator_kind == "table_cell" else None
        parsed: Decimal | None = parse_number(located.text, fmt)
        numeric_ok = parsed is not None and parsed == claim.value_numeric
        detail = (
            "equals the deterministic parse of the located text"
            if numeric_ok
            else "the located text is not a number under the declared format, or differs"
        )
        checks.append(Check("value_numeric", numeric_ok, detail))
    if same_text and same_excerpt and numeric_ok:
        return "source_located", "value found at the stated location in the stored bytes", checks
    failed = next(c for c in checks if not c.passed)
    return _fail(f"mismatch: {failed.name}", checks)


@scoped_api
def verify_claim(
    conn: Connection,
    scope: ProjectScope,
    claim_id: int,
    *,
    now: datetime,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
) -> VerificationResult:
    """Check one claim of this project and append the outcome as a review (when it adds information)."""
    claim = get_claim(conn, scope, claim_id)
    source = conn.execute(
        text(
            """SELECT id, raw_version_id, content_sha256, retention_mode FROM evidence_sources
               WHERE project_id = :p AND id = :s"""
        ),
        {"p": scope.project_id, "s": claim.source_id},
    ).one_or_none()
    if source is None:  # cannot happen through the composite foreign key; fail closed anyway
        raise NotFound("no such source")
    status, reason, checks = _evaluate(conn, claim, source, time.monotonic() + budget_seconds)
    reason_text = f"{reason}. {NOT_A_TRUTH_CHECK}." if status == "source_located" else reason

    latest = conn.execute(
        text(
            """SELECT status, reason FROM claim_reviews
               WHERE project_id = :p AND claim_id = :c ORDER BY id DESC LIMIT 1"""
        ),
        {"p": scope.project_id, "c": claim_id},
    ).one_or_none()
    if claim.status in HUMAN_OWNED:
        return VerificationResult(claim_id, status, reason, tuple(checks), written=False)
    if latest is not None and (latest.status, latest.reason) == (status, reason_text):
        return VerificationResult(claim_id, status, reason, tuple(checks), written=False)
    review_claim(
        conn,
        scope,
        claim_id,
        status=status,
        reason=reason_text,
        actor=VERIFIER_ACTOR,
        now=now,
        verifier_version=VERIFIER_VERSION,
        checks=[c.as_json() for c in checks],
    )
    return VerificationResult(claim_id, status, reason, tuple(checks), written=True)
