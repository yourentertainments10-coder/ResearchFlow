"""From the ledger to the Phase 1 answer contract (``sie.answer``).

The number lock proves that every number in an answer came from a declared ``Figure``. It does not prove
the figure is true. This module decides what may become a figure from a claim and what must travel with it:

* only ``source_located`` or ``human_confirmed`` claims with a numeric value, never unverified, disputed,
  rejected, or the losing side of a resolved conflict;
* a claim of a web source becomes an ``evidence`` figure (layer ``external_evidence`` only, so it can
  never be shown as a ``verified_fact``); a claim of an upload becomes a ``user_file`` figure;
* an open conflict, or a cached spreadsheet value, comes with a ``Gap`` the answer must disclose in a
  ``limitation`` statement that lists it, which the contract's verifier then enforces;
* provenance carries the project, claim, hashes and status, and states that the value is "as stated in
  the source", not independently checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

from sie.answer.models import Answer, EvidenceRef, Figure, Gap, GapKind, SourceKind
from sie.evidence.claims import get_claim
from sie.evidence.errors import EvidenceError
from sie.evidence.scope import ProjectScope, scoped_api
from sie.evidence.sources import get_source

USABLE_STATUSES = ("source_located", "human_confirmed")
AS_STATED = "as stated in the source; not independently verified"


class ClaimNotUsable(EvidenceError):
    """The claim may not become a figure. ``code`` says why (never silently dropped or fudged)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ClaimFigure:
    figure: Figure
    evidence: EvidenceRef | None  # web sources only; an upload has no URL to cite
    gaps: tuple[Gap, ...]  # must be disclosed by a statement that cites the figure
    limitations: tuple[str, ...]  # plain text for a limitation statement


@scoped_api
def evidence_ref_from_source(
    conn: Connection, scope: ProjectScope, source_id: int, *, quote: str = ""
) -> EvidenceRef:
    """The citable reference of a web source. An upload has no URL, so it is cited as a user file."""
    source = get_source(conn, scope, source_id)
    if source.kind != "web":
        raise EvidenceError(
            "an upload is cited through a user_file figure, not as external evidence"
        )
    url = source.final_url or source.requested_url
    return EvidenceRef(
        id=f"ev_{source.id}",
        url=url or "",
        title=source.title,
        retrieved_at=source.retrieved_at,
        tier=source.tier,
        quote=quote[:300],
    )


@scoped_api
def figure_from_claim(
    conn: Connection,
    scope: ProjectScope,
    claim_id: int,
    *,
    figure_id: str,
    label: str,
    decimals: int = 0,
    unit: str = "",
) -> ClaimFigure:
    claim = get_claim(conn, scope, claim_id)
    if claim.value_numeric is None:
        raise ClaimNotUsable("no_numeric_value", "this claim has no numeric value")
    if claim.status not in USABLE_STATUSES:
        raise ClaimNotUsable(
            claim.status if claim.status in ("disputed", "rejected") else "not_confirmed",
            f"a claim that is {claim.status} cannot become a figure",
        )
    source = get_source(conn, scope, claim.source_id)

    conflicts = conn.execute(
        text(
            """SELECT id, claim_a_id, claim_b_id, status FROM claim_conflicts
               WHERE project_id = :p AND (claim_a_id = :c OR claim_b_id = :c)
               ORDER BY id"""
        ),
        {"p": scope.project_id, "c": claim_id},
    ).all()
    gaps: list[Gap] = []
    for cf in conflicts:
        if cf.status == "open":
            other = cf.claim_b_id if cf.claim_a_id == claim_id else cf.claim_a_id
            gaps.append(
                Gap(
                    id=f"gap_conflict_{cf.id}",
                    kind=GapKind.CONFLICT,
                    message=f"claim {claim_id} disagrees with claim {other} of this project (open conflict)",
                )
            )
        elif (cf.status == "resolved_a" and cf.claim_b_id == claim_id) or (
            cf.status == "resolved_b" and cf.claim_a_id == claim_id
        ):
            raise ClaimNotUsable(
                "lost_conflict", "a person resolved a conflict in favour of another claim"
            )

    limitations = [AS_STATED]
    if claim.value_source == "cached_formula_value":
        gaps.append(
            Gap(
                id=f"gap_cached_{claim_id}",
                kind=GapKind.UNVERIFIED,
                message="the value is a formula result cached in the file; formulas were not evaluated",
            )
        )
    if source.retention_mode != "full":
        limitations.append(
            "the source's bytes are not stored, so the claim cannot be re-checked later"
        )

    evidence = None
    if source.kind == "web":
        kind = SourceKind.EVIDENCE
        evidence = evidence_ref_from_source(conn, scope, source.id, quote=claim.excerpt)
    else:
        kind = SourceKind.USER_FILE
    figure = Figure(
        id=figure_id,
        value=claim.value_numeric,
        decimals=decimals,
        unit=unit,
        label=label,
        source_kind=kind,
        # The answer contract requires an evidence figure to name its EvidenceRef; the claim itself is
        # identified in the provenance below. A user_file figure names the claim directly.
        source_ref=evidence.id if evidence is not None else f"claim:{claim.id}",
        provenance={
            "project_id": str(scope.project_id),
            "claim_id": str(claim.id),
            "claim_hash": claim.claim_hash,
            "source_id": str(source.id),
            "source_sha256": source.content_sha256,
            "excerpt_sha256": claim.excerpt_sha256,
            "locator_kind": claim.locator_kind,
            "status": claim.status,
            "attestation": AS_STATED,
        },
    )
    return ClaimFigure(figure, evidence, tuple(gaps), tuple(limitations))


def assert_single_project(answer: Answer) -> int | None:
    """Every evidence figure of an answer must come from one project. Returns that project's id.

    An answer assembled from two projects would mix one project's private evidence into another's
    output, so it is refused. Figures without a ``project_id`` (database or operation figures) are
    not evidence and are ignored.
    """
    projects = {f.provenance["project_id"] for f in answer.figures if "project_id" in f.provenance}
    if len(projects) > 1:
        raise EvidenceError("an answer may not mix evidence from different projects")
    return int(next(iter(projects))) if projects else None
