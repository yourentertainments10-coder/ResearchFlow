"""competition scope, daily snapshot uniqueness, reporting schema

Revision ID: 002
Revises: 001
Create Date: 2026-10-02

The upgrade DDL lives in src/sie/db/sql/002_competition_scope_and_reporting.sql and is quoted in
docs/DATABASE.md. Never edit this migration after it has been applied anywhere: add 003 instead.
"""

from importlib.resources import files

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

# View definition exactly as migration 001 created it (no competition_id column).
_V_MEDAL_FACTS_001 = """
CREATE VIEW v_medal_facts AS
SELECT p.id AS placing_id, p.event_id, p.medal, p.slot, p.is_tie,
       c.code AS country_code, c.name AS country,
       s.name AS sport, d.name AS discipline,
       e.gender, e.participation, e.name AS event, e.event_date, e.is_disputed,
       en.name AS entrant
FROM placings p
JOIN events e      ON e.id = p.event_id
JOIN disciplines d ON d.id = e.discipline_id
JOIN sports s      ON s.id = d.sport_id
JOIN countries c   ON c.id = p.country_id
LEFT JOIN entrants en ON en.id = p.entrant_id
WHERE p.is_current
"""


def upgrade() -> None:
    sql = (files("sie.db") / "sql" / "002_competition_scope_and_reporting.sql").read_text(
        encoding="utf-8"
    )
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    op.execute("DROP SCHEMA reporting CASCADE")  # the reporting views depend on v_medal_facts
    op.execute("DROP VIEW v_medal_facts")
    op.execute(_V_MEDAL_FACTS_001)
    op.execute("DROP INDEX ux_snapshots_daily")
    op.execute("DROP INDEX ix_snapshots_comp_time")
    op.execute("DROP INDEX ix_snapshots_comp_fingerprint")
    op.execute("CREATE INDEX ix_snapshots_fingerprint ON analytics_snapshots (data_fingerprint)")
    op.execute("ALTER TABLE analytics_snapshots DROP COLUMN local_date, DROP COLUMN competition_id")
    op.execute("ALTER TABLE ingest_runs DROP COLUMN competition_id")
