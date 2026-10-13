"""Read-only integrity check of one project's evidence (``sie evidence check``).

It re-reads every stored file against its recorded SHA-256 and every claim's excerpt against its recorded
hash, and checks that the project's raw documents are namespaced to it. It reports; it never repairs.
Excerpt-only sources have no bytes to re-read, which is counted, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from sie.evidence.claims import excerpt_sha256
from sie.evidence.scope import ProjectScope, scoped_api
from sie.evidence.sources import read_version_bytes


@dataclass
class IntegrityReport:
    sources: int = 0
    files_checked: int = 0
    bytes_not_retained: int = 0
    claims: int = 0
    claims_ok: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@scoped_api
def check_project(conn: Connection, scope: ProjectScope) -> IntegrityReport:
    report = IntegrityReport()
    prefix = f"rf-project/{scope.project_id}/"
    for s in conn.execute(
        text(
            """SELECT id, raw_version_id, content_sha256, retention_mode FROM evidence_sources
               WHERE project_id = :p ORDER BY id"""
        ),
        {"p": scope.project_id},
    ):
        report.sources += 1
        if s.raw_version_id is None:
            report.bytes_not_retained += 1
            continue
        try:
            read_version_bytes(conn, s.raw_version_id, s.content_sha256)
            report.files_checked += 1
        except Exception as exc:  # noqa: BLE001 - every failure is a finding, with its reason
            report.problems.append(f"source {s.id}: {type(exc).__name__}: {exc}")
    for c in conn.execute(
        text("SELECT id, excerpt, excerpt_sha256 FROM claims WHERE project_id = :p ORDER BY id"),
        {"p": scope.project_id},
    ):
        report.claims += 1
        if excerpt_sha256(c.excerpt) == c.excerpt_sha256:
            report.claims_ok += 1
        else:
            report.problems.append(f"claim {c.id}: the stored excerpt no longer matches its hash")
    stray = conn.execute(
        text(
            """SELECT count(*) FROM project_documents d JOIN raw_documents r ON r.id = d.raw_document_id
               WHERE d.project_id = :p AND r.url NOT LIKE :pre"""
        ),
        {"p": scope.project_id, "pre": prefix + "%"},
    ).scalar_one()
    if stray:
        report.problems.append(f"{stray} raw documents of this project are not namespaced to it")
    return report
