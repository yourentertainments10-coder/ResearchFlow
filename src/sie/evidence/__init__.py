"""Evidence ledger (ADR-042): project-scoped sources, claims and their verification status.

Nothing here decides what is true. ``source_located`` means a verifier found the claimed value at the
stated place in the stored bytes; ``human_confirmed`` means a named person confirmed the claim matches
its source. Neither is factual verification (docs/EVIDENCE_LEDGER_DESIGN.md section 6).
"""

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

__all__ = [
    "SCOPED_API",
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
