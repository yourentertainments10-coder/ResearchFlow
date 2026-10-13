"""Owner-only guard for destructive maintenance (purge, prune)."""

from __future__ import annotations

from sqlalchemy import Connection, text

from sie.evidence.errors import OwnerOnly

# The tables a destructive command writes to. The pipeline role has no DELETE on any of them
# (db/roles.py), so holding DELETE on all of them identifies the schema owner by privilege, not by name.
_NEEDS_DELETE = ("claims", "evidence_sources", "raw_blobs", "raw_documents", "raw_versions")


def require_owner(conn: Connection) -> None:
    for table in _NEEDS_DELETE:
        allowed = conn.execute(
            text("SELECT has_table_privilege(current_user, :t, 'DELETE')"), {"t": table}
        ).scalar_one()
        if not allowed:
            raise OwnerOnly(
                "this command deletes data and must run with the schema owner's DATABASE_URL, "
                "not as the pipeline or reader role"
            )
