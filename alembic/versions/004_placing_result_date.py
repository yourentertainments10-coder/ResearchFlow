"""placings.result_date, placings.source_country

Revision ID: 004
Revises: 003
Create Date: 2026-10-06

The upgrade DDL lives in src/sie/db/sql/004_placing_result_date.sql and is quoted in
docs/DATABASE.md. Never edit this migration after it has been applied anywhere: add 005 instead.
"""

from importlib.resources import files

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (files("sie.db") / "sql" / "004_placing_result_date.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    # A view cannot lose a column through CREATE OR REPLACE: drop and recreate the old shape.
    op.execute("DROP VIEW reporting.medal_facts")
    op.execute("DROP VIEW v_medal_facts")
    op.execute(
        """CREATE VIEW v_medal_facts AS
           SELECT p.id AS placing_id, p.event_id, p.medal, p.slot, p.is_tie,
                  c.code AS country_code, c.name AS country,
                  s.name AS sport, d.name AS discipline,
                  e.gender, e.participation, e.name AS event, e.event_date, e.is_disputed,
                  en.name AS entrant, e.competition_id
           FROM placings p
           JOIN events e      ON e.id = p.event_id
           JOIN disciplines d ON d.id = e.discipline_id
           JOIN sports s      ON s.id = d.sport_id
           JOIN countries c   ON c.id = p.country_id
           LEFT JOIN entrants en ON en.id = p.entrant_id
           WHERE p.is_current"""
    )
    op.execute(
        """CREATE VIEW reporting.medal_facts AS
           SELECT competition_id, event_id, placing_id, medal, slot, is_tie, country_code, country,
                  sport, discipline, gender, participation, event, event_date, is_disputed, entrant
           FROM public.v_medal_facts"""
    )
    op.execute("ALTER TABLE placings DROP COLUMN source_country")
    op.execute("ALTER TABLE placings DROP COLUMN result_date")
