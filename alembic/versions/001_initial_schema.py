"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-10-02

The DDL lives in src/sie/db/sql/001_initial_schema.sql and is quoted in docs/DATABASE.md.
Never edit this migration after it has been applied anywhere: add 002 instead.
"""

from importlib.resources import files

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

# Reverse creation order (views and dependents first).
_DROP_ORDER = [
    "VIEW v_medal_facts",
    "TABLE snapshot_changes",
    "TABLE snapshot_rows",
    "TABLE analytics_snapshots",
    "TABLE reconciliation_results",
    "TABLE quarantine",
    "TABLE source_conflicts",
    "TABLE source_observations",
    "TABLE placing_history",
    "TABLE placings",
    "TABLE raw_fetches",
    "TABLE raw_documents CASCADE",  # circular FK with raw_versions
    "TABLE raw_versions",
    "TABLE ingest_runs",
    "TABLE entrants",
    "TABLE events",
    "TABLE sport_aliases",
    "TABLE disciplines",
    "TABLE sports",
    "TABLE country_aliases",
    "TABLE countries",
    "TABLE competitions",
]


def upgrade() -> None:
    sql = (files("sie.db") / "sql" / "001_initial_schema.sql").read_text(encoding="utf-8")
    # exec_driver_sql bypasses SQLAlchemy bind-parameter parsing of ':' in the script.
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    for target in _DROP_ORDER:
        op.execute(f"DROP {target}")
