"""Least-privilege database roles (docs/DATABASE.md section 13).

* ``sie_reader``   reads only the ``reporting`` views. The dashboard and any future API use it.
* ``sie_pipeline`` reads and writes the core tables but can never DELETE, and cannot UPDATE the
  append-only evidence tables. The scheduled pipeline uses it.

Both roles are LOGIN roles created by the schema owner (the role in DATABASE_URL, used for migrations).
``apply_roles`` is idempotent: run it after every migration that adds tables, because privileges on
tables created later are not retroactive for the owner's other sessions (default privileges cover
tables the owner creates from now on).
"""

from __future__ import annotations

from psycopg import sql
from sqlalchemy import Connection

READER = "sie_reader"
PIPELINE = "sie_pipeline"

# Rows here are evidence or history: the pipeline may add rows but never change them.
APPEND_ONLY = (
    "placing_history",
    "raw_versions",
    "raw_blobs",
    "raw_fetches",
    "source_observations",
    "analytics_snapshots",
    "snapshot_rows",
    "snapshot_changes",
)
# Bookkeeping that only the owner touches.
OWNER_ONLY = ("alembic_version",)


def _ensure_role(cur, name: str, password: str) -> None:
    cur.execute(
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {lit}) "
            "THEN CREATE ROLE {ident} LOGIN; END IF; END $$"
        ).format(lit=sql.Literal(name), ident=sql.Identifier(name))
    )
    cur.execute(
        sql.SQL("ALTER ROLE {ident} WITH LOGIN PASSWORD {pw}").format(
            ident=sql.Identifier(name), pw=sql.Literal(password)
        )
    )


def apply_roles(conn: Connection, *, reader_password: str, pipeline_password: str) -> None:
    """Create or refresh both roles and their grants inside the caller's transaction."""
    if not reader_password or not pipeline_password:
        raise ValueError("both role passwords are required")
    # psycopg's own composition quotes the password literal correctly (including % and ').
    raw = conn.connection.driver_connection
    with raw.cursor() as cur:
        _ensure_role(cur, READER, reader_password)
        _ensure_role(cur, PIPELINE, pipeline_password)

        # Reader: reporting views only.
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sie_reader")
        cur.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sie_reader")
        cur.execute("REVOKE CREATE ON SCHEMA public FROM sie_reader, sie_pipeline")
        cur.execute("GRANT USAGE ON SCHEMA reporting TO sie_reader, sie_pipeline")
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO sie_reader, sie_pipeline")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA reporting "
            "GRANT SELECT ON TABLES TO sie_reader, sie_pipeline"
        )

        # Pipeline: read, insert, update. No DELETE anywhere.
        cur.execute("GRANT USAGE ON SCHEMA public TO sie_pipeline")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO sie_pipeline")
        cur.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sie_pipeline")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE ON TABLES TO sie_pipeline"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO sie_pipeline"
        )
        for table in APPEND_ONLY:
            cur.execute(
                sql.SQL("REVOKE UPDATE ON {} FROM sie_pipeline").format(sql.Identifier(table))
            )
        for table in OWNER_ONLY:
            cur.execute(sql.SQL("REVOKE ALL ON {} FROM sie_pipeline").format(sql.Identifier(table)))
