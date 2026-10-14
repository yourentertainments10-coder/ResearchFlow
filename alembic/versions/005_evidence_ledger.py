"""evidence ledger: projects, project_documents, evidence_sources, claims, claim_reviews, claim_conflicts

Revision ID: 005
Revises: 004
Create Date: 2026-10-12

Additive only (ADR-042). The upgrade DDL lives in src/sie/db/sql/005_evidence_ledger.sql and is quoted in
docs/DATABASE.md. Never edit this migration after it has been applied anywhere: add 006 instead.

The downgrade drops the new tables. That destroys any evidence stored in them, so on a database that
holds real evidence the safe way back is a restore from a verified backup, not a downgrade
(docs/EVIDENCE_LEDGER_DESIGN.md section 10). It does not touch raw_documents, raw_versions or raw_blobs
rows that uploads created: those are ordinary raw rows and stay.
"""

from importlib.resources import files

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (files("sie.db") / "sql" / "005_evidence_ledger.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP VIEW evidence_claims_current")
    for table in (
        "claim_conflicts",
        "claim_reviews",
        "claims",
        "evidence_sources",
        "project_documents",
        "purge_tombstones",
        "projects",
    ):
        op.execute(f"DROP TABLE {table}")
