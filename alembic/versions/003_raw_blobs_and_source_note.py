"""raw_blobs, raw_versions.storage_backend, placings.source_note, ingest_runs.source

Revision ID: 003
Revises: 002
Create Date: 2026-10-04

The upgrade DDL lives in src/sie/db/sql/003_raw_blobs_and_source_note.sql and is quoted in
docs/DATABASE.md. Never edit this migration after it has been applied anywhere: add 004 instead.
"""

from importlib.resources import files

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (files("sie.db") / "sql" / "003_raw_blobs_and_source_note.sql").read_text(
        encoding="utf-8"
    )
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE ingest_runs DROP COLUMN source")
    op.execute("ALTER TABLE placings DROP COLUMN source_note")
    op.execute("DROP TABLE raw_blobs")
    op.execute("ALTER TABLE raw_versions DROP CONSTRAINT ck_raw_versions_location")
    op.execute("UPDATE raw_versions SET path = 'unknown' WHERE path IS NULL")
    op.execute("ALTER TABLE raw_versions ALTER COLUMN path SET NOT NULL")
    op.execute("ALTER TABLE raw_versions DROP COLUMN storage_backend")
