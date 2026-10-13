"""Errors of the evidence ledger. Specific, so callers and tests can tell the cases apart."""

from __future__ import annotations


class EvidenceError(RuntimeError):
    """Base class for every error this package raises on purpose."""


class NotFound(EvidenceError):
    """The thing does not exist *for this project*.

    A row that exists in another project raises exactly this too, with the same text, so a caller
    cannot learn whether an id belongs to someone else (docs/EVIDENCE_LEDGER_DESIGN.md section 8).
    """


class ProjectClosed(EvidenceError):
    """The project is archived or purged and takes no new evidence."""


class UploadRejected(EvidenceError):
    """The upload breaks a rule. ``code`` is machine readable: empty, too_large, not_bytes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IngestBusy(EvidenceError):
    """The write kept colliding with a concurrent writer and was given up after bounded retries."""


class BytesNotRetained(EvidenceError):
    """The source's bytes are not stored (excerpt-only, metadata-only or pruned)."""


class PolicyRefused(EvidenceError):
    """A retention request breaks the recorded-policy rules (docs/EVIDENCE_RETENTION.md)."""
