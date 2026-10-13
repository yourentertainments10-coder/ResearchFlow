"""Evidence ledger (ADR-042): project-scoped sources, claims and their verification status.

Nothing here decides what is true. ``source_located`` means a verifier found the claimed value at the
stated place in the stored bytes; ``human_confirmed`` means a named person confirmed the claim matches
its source. Neither is factual verification (docs/EVIDENCE_LEDGER_DESIGN.md section 6).
"""

from sie.evidence.adapters import (
    ClaimFigure,
    ClaimNotUsable,
    assert_single_project,
    evidence_ref_from_source,
    figure_from_claim,
)
from sie.evidence.claims import (
    STATUSES,
    ClaimInfo,
    ClaimInput,
    ClaimResult,
    add_claim,
    claim_hash,
    get_claim,
    list_claims,
    review_claim,
)
from sie.evidence.conflicts import (
    ConflictInfo,
    detect_conflicts,
    list_conflicts,
    resolve_conflict,
)
from sie.evidence.errors import (
    BytesNotRetained,
    EvidenceError,
    IngestBusy,
    NotFound,
    PolicyRefused,
    ProjectClosed,
    UploadRejected,
)
from sie.evidence.ingest import IngestResult, ingest_upload, sanitise_filename, upload_url
from sie.evidence.scope import SCOPED_API, ProjectScope, create_project, open_scope
from sie.evidence.sources import SourceInfo, get_source, list_sources, read_bytes
from sie.evidence.verify import VERIFIER_VERSION, VerificationResult, verify_claim

__all__ = [
    "ClaimFigure",
    "ClaimNotUsable",
    "ConflictInfo",
    "assert_single_project",
    "detect_conflicts",
    "evidence_ref_from_source",
    "figure_from_claim",
    "list_conflicts",
    "resolve_conflict",
    "SCOPED_API",
    "STATUSES",
    "VERIFIER_VERSION",
    "ClaimInfo",
    "ClaimInput",
    "ClaimResult",
    "VerificationResult",
    "add_claim",
    "claim_hash",
    "get_claim",
    "list_claims",
    "review_claim",
    "verify_claim",
    "BytesNotRetained",
    "EvidenceError",
    "IngestBusy",
    "IngestResult",
    "NotFound",
    "PolicyRefused",
    "ProjectClosed",
    "ProjectScope",
    "SourceInfo",
    "UploadRejected",
    "create_project",
    "get_source",
    "ingest_upload",
    "list_sources",
    "open_scope",
    "read_bytes",
    "sanitise_filename",
    "upload_url",
]
