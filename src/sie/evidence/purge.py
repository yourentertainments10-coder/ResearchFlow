"""Owner-only deletion: ``purge_project`` (a whole project) and ``prune_expired`` (aged-out full copies).

Both are dry runs unless ``execute=True`` and the confirmation token is given. Both refuse to run as the
pipeline or reader role.

What a purge does and does NOT do is stated in the report and in the tombstone, in plain words:

* it deletes the project's rows and raw bytes from the live database, in one transaction, after waiting
  for any ingest that holds the project row;
* it does NOT erase backups: encrypted dumps keep those bytes until they expire (14 days by default);
* it does NOT know the database provider's point-in-time history window unless the owner has checked it
  and set ``DB_HISTORY_WINDOW_HOURS``; until then that is reported as NOT VERIFIED;
* deleted rows are removed from disk when the database vacuums, not instantly;
* restoring a backup taken before the purge brings the data back, so the purge must be repeated after
  any restore.

The word "erased" is never used for the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, text

from sie.config import Settings, get_settings
from sie.evidence.dataset_store import count_project_datasets, delete_project_datasets
from sie.evidence.errors import NotFound, PurgeRefused
from sie.evidence.owner import require_owner
from sie.evidence.txn import advisory_lock

PRUNE_CONFIRMATION = "prune"
# Tables a purge deletes from itself. A foreign key from anywhere else into the project's raw rows means
# something else depends on them, and the purge refuses instead of breaking it.
_OURS = {
    "raw_blobs",
    "raw_fetches",
    "project_documents",
    "evidence_sources",
    "raw_versions",
    "raw_documents",
}


@dataclass
class PurgeReport:
    project: str
    project_id: int
    counts: dict[str, int]
    executed: bool
    lines: list[str] = field(default_factory=list)
    tombstone_id: int | None = None


def backup_and_history_lines(settings: Settings, now: datetime) -> list[str]:
    days = settings.backup_retention_days
    until = (now + timedelta(days=days)).date().isoformat()
    window = settings.db_history_window_hours
    history = (
        f"the database provider's history window was reported by the owner as {window} hours"
        if window is not None
        else "the database provider's point-in-time history window is NOT VERIFIED "
        "(check the provider console; set DB_HISTORY_WINDOW_HOURS once known)"
    )
    return [
        f"NOT erased from backups: encrypted backups keep these bytes for up to {days} days (until about {until}).",
        f"NOT erased from provider history: {history}.",
        "Deleted rows leave the disk when the database vacuums, which runs on its own schedule.",
        "Restoring a backup taken before this purge brings the data back: repeat the purge after any restore "
        "and keep a record of this purge outside the database (docs/DEPLOYMENT.md).",
    ]


def _project_row(conn: Connection, slug: str, lock: bool):
    query = "SELECT id, slug, purged_at FROM projects WHERE slug = :s"
    row = conn.execute(text(query + (" FOR UPDATE" if lock else "")), {"s": slug}).one_or_none()
    if row is None:
        raise NotFound("no such project")
    if row.purged_at is not None:
        raise PurgeRefused("this project was already purged")
    return row


def _raw_ids(conn: Connection, project_id: int) -> tuple[list[int], list[int]]:
    docs = [
        r[0]
        for r in conn.execute(
            text("SELECT raw_document_id FROM project_documents WHERE project_id = :p ORDER BY 1"),
            {"p": project_id},
        )
    ]
    versions = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT id FROM raw_versions WHERE document_id = ANY(CAST(:d AS bigint[])) ORDER BY 1"
            ),
            {"d": docs},
        )
    ]
    return docs, versions


def _check_exclusive(
    conn: Connection, project_id: int, docs: list[int], versions: list[int]
) -> None:
    """Refuse unless every raw row to delete belongs to this project alone and nothing else uses it."""
    prefix = f"rf-project/{project_id}/"
    foreign = conn.execute(
        text(
            "SELECT count(*) FROM raw_documents WHERE id = ANY(CAST(:d AS bigint[])) AND url NOT LIKE :pre"
        ),
        {"d": docs, "pre": prefix + "%"},
    ).scalar_one()
    if foreign:
        raise PurgeRefused(
            "a document of this project is not namespaced to it; refusing to delete it"
        )
    shared = conn.execute(
        text(
            """SELECT count(*) FROM project_documents
               WHERE raw_document_id = ANY(CAST(:d AS bigint[])) AND project_id <> :p"""
        ),
        {"d": docs, "p": project_id},
    ).scalar_one()
    if shared:
        raise PurgeRefused("another project references one of these documents; refusing")
    quote = conn.dialect.identifier_preparer.quote
    refs = conn.execute(
        text(
            """SELECT c.conrelid::regclass::text AS tbl, a.attname AS col, c.confrelid::regclass::text AS target
               FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
               WHERE c.contype = 'f' AND c.confrelid IN ('raw_versions'::regclass, 'raw_documents'::regclass)"""
        )
    ).all()
    for ref in refs:
        if ref.tbl in _OURS:
            continue
        ids = versions if ref.target == "raw_versions" else docs
        used = conn.execute(
            text(
                f"SELECT count(*) FROM {quote(ref.tbl)} WHERE {quote(ref.col)} = ANY(CAST(:ids AS bigint[]))"
            ),
            {"ids": ids},
        ).scalar_one()
        if used:
            raise PurgeRefused(
                f"{ref.tbl}.{ref.col} still references this project's raw data; refusing"
            )


def _counts(
    conn: Connection, project_id: int, docs: list[int], versions: list[int]
) -> dict[str, int]:
    def n(sql: str, **params) -> int:
        return conn.execute(text(sql), params).scalar_one()

    return {
        "claim_conflicts": n(
            "SELECT count(*) FROM claim_conflicts WHERE project_id = :p", p=project_id
        ),
        "claim_reviews": n(
            "SELECT count(*) FROM claim_reviews WHERE project_id = :p", p=project_id
        ),
        "claims": n("SELECT count(*) FROM claims WHERE project_id = :p", p=project_id),
        "evidence_sources": n(
            "SELECT count(*) FROM evidence_sources WHERE project_id = :p", p=project_id
        ),
        "project_documents": n(
            "SELECT count(*) FROM project_documents WHERE project_id = :p", p=project_id
        ),
        "raw_documents": len(docs),
        "raw_versions": len(versions),
        "raw_blobs": n(
            "SELECT count(*) FROM raw_blobs WHERE version_id = ANY(CAST(:v AS bigint[]))",
            v=versions,
        ),
        "raw_fetches": n(
            "SELECT count(*) FROM raw_fetches WHERE document_id = ANY(CAST(:d AS bigint[]))", d=docs
        ),
        "raw_bytes_stored": n(
            """SELECT coalesce(sum(size_bytes), 0)::bigint FROM raw_versions
               WHERE id = ANY(CAST(:v AS bigint[]))""",
            v=versions,
        ),
    }


def purge_project(
    engine: Engine,
    slug: str,
    *,
    now: datetime,
    execute: bool = False,
    confirm: str | None = None,
    purged_by: str = "",
    settings: Settings | None = None,
) -> PurgeReport:
    """Dry run by default. ``execute=True`` also needs ``confirm`` to repeat the slug and a named person."""
    settings = settings or get_settings()
    if execute:
        if confirm != slug:
            raise PurgeRefused("to purge, repeat the project slug as the confirmation")
        if not purged_by.strip() or purged_by.strip().startswith("system:"):
            raise PurgeRefused("a purge needs the name of the person who ordered it")
    with engine.connect().execution_options(isolation_level="READ COMMITTED") as conn, conn.begin():
        require_owner(conn)
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        conn.execute(text("SET LOCAL statement_timeout = '300s'"))
        row = _project_row(conn, slug, lock=execute)  # FOR UPDATE waits for in-flight ingests
        docs, versions = _raw_ids(conn, row.id)
        _check_exclusive(conn, row.id, docs, versions)
        counts = _counts(conn, row.id, docs, versions)
        lines = [
            f"{'Deleted from' if execute else 'Would delete from'} the live database: "
            + ", ".join(f"{v} {k}" for k, v in counts.items() if k != "raw_bytes_stored" and v)
            + f" ({counts['raw_bytes_stored']} bytes of stored files)."
        ]
        stored = count_project_datasets(settings.evidence_dataset_dir, row.id)
        lines.append(
            f"{'Deleted' if execute else 'Would delete'} {stored} stored collected dataset(s) from "
            f"{settings.evidence_dataset_dir} (files on this machine; copies in backups are not touched)."
        )
        lines += backup_and_history_lines(settings, now)
        if not execute:  # nothing was written: the transaction ends without changes
            return PurgeReport(
                slug, row.id, counts, False, lines + ["Dry run: nothing was changed."]
            )

        pid = row.id
        for table in (
            "claim_conflicts",
            "claim_reviews",
            "claims",
            "evidence_sources",
            "project_documents",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid}
            )  # fixed names
        conn.execute(
            text("DELETE FROM raw_blobs WHERE version_id = ANY(CAST(:v AS bigint[]))"),
            {"v": versions},
        )
        conn.execute(
            text("DELETE FROM raw_fetches WHERE document_id = ANY(CAST(:d AS bigint[]))"),
            {"d": docs},
        )
        conn.execute(  # the circular pointer is deferred, but clearing it first keeps the order obvious
            text(
                "UPDATE raw_documents SET latest_version_id = NULL WHERE id = ANY(CAST(:d AS bigint[]))"
            ),
            {"d": docs},
        )
        conn.execute(
            text("DELETE FROM raw_versions WHERE id = ANY(CAST(:v AS bigint[]))"), {"v": versions}
        )
        conn.execute(
            text("DELETE FROM raw_documents WHERE id = ANY(CAST(:d AS bigint[]))"), {"d": docs}
        )
        note = " ".join(lines[1:])
        tombstone = conn.execute(
            text(
                """INSERT INTO purge_tombstones (project_id, purged_at, purged_by, row_counts,
                       backup_retention_days, db_history_window_hours, db_history_verified, note)
                   VALUES (:p, :t, :by, CAST(:rc AS jsonb), :bd, :hw, :hv, :note) RETURNING id"""
            ),
            {
                "p": pid, "t": now, "by": purged_by.strip(), "rc": json.dumps(counts),
                "bd": settings.backup_retention_days, "hw": settings.db_history_window_hours,
                "hv": settings.db_history_window_hours is not None, "note": note,
            },
        ).scalar_one()  # fmt: skip
        conn.execute(
            text(
                """UPDATE projects SET slug = :s, name = '[purged]', owner_ref = NULL,
                       archived_at = coalesce(archived_at, :t), purged_at = :t WHERE id = :p"""
            ),
            {"s": f"purged-{pid}", "t": now, "p": pid},
        )
    delete_project_datasets(
        settings.evidence_dataset_dir, pid
    )  # after the commit: files follow the rows
    return PurgeReport(slug, pid, counts, True, lines, tombstone)


# ---- prune ------------------------------------------------------------------------------------------


@dataclass
class PruneReport:
    executed: bool
    candidates: list[dict] = field(default_factory=list)
    pruned: int = 0
    blocked: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def prune_expired(
    engine: Engine,
    *,
    now: datetime,
    execute: bool = False,
    confirm: str | None = None,
    settings: Settings | None = None,
) -> PruneReport:
    """Drop the stored bytes of full web copies whose retention has ended. Owner only; dry run by default.

    The evidence-source row stays as metadata (hash, size, URL, time) so claims keep their source; only
    the bytes go. A source that would collide with an existing excerpt-only row for the same content is
    reported as blocked and left alone.
    """
    settings = settings or get_settings()
    if execute and confirm != PRUNE_CONFIRMATION:
        raise PurgeRefused(f"to prune, pass the confirmation {PRUNE_CONFIRMATION!r}")
    with engine.connect() as conn, conn.begin():
        require_owner(conn)
        rows = conn.execute(
            text(
                """SELECT s.id, s.project_id, s.raw_version_id, s.requested_url, s.retain_until
                   FROM evidence_sources s
                   WHERE s.retain_until IS NOT NULL AND s.retain_until < :now AND s.pruned_at IS NULL
                     AND s.raw_version_id IS NOT NULL ORDER BY s.id"""
            ),
            {"now": now},
        ).all()
    report = PruneReport(
        execute,
        [
            {"source_id": r.id, "project_id": r.project_id, "url": r.requested_url,
             "retain_until": r.retain_until.isoformat()}
            for r in rows
        ],
    )  # fmt: skip
    verb = "Pruned" if execute else "Would prune"
    if not execute:
        report.lines = [
            f"{verb} {len(rows)} expired full web copies. Dry run: nothing was changed."
        ]
        return report
    for r in rows:
        with (
            engine.connect().execution_options(isolation_level="READ COMMITTED") as conn,
            conn.begin(),
        ):
            require_owner(conn)
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            project = conn.execute(
                text("SELECT purged_at FROM projects WHERE id = :p FOR SHARE"), {"p": r.project_id}
            ).one_or_none()
            if project is None or project.purged_at is not None:
                continue
            advisory_lock(conn, f"rf:raw:web|rf-project/{r.project_id}/web/{r.requested_url}")
            clash = conn.execute(
                text(
                    """SELECT 1 FROM evidence_sources e JOIN evidence_sources f
                         ON f.project_id = e.project_id AND f.requested_url = e.requested_url
                        AND f.content_sha256 = e.content_sha256 AND f.raw_version_id IS NULL
                       WHERE e.project_id = :p AND e.id = :s"""
                ),
                {"p": r.project_id, "s": r.id},
            ).first()
            if clash:
                report.blocked.append(
                    f"source {r.id}: an excerpt-only source with the same content already exists"
                )
                continue
            conn.execute(
                text("DELETE FROM raw_blobs WHERE version_id = :v"), {"v": r.raw_version_id}
            )
            conn.execute(
                text(
                    """UPDATE evidence_sources SET retention_mode = 'metadata', raw_version_id = NULL,
                           pruned_at = :t WHERE project_id = :p AND id = :s"""
                ),
                {"t": now, "p": r.project_id, "s": r.id},
            )
            report.pruned += 1
    report.lines = [
        f"{verb} {report.pruned} expired full web copies; {len(report.blocked)} blocked.",
        *backup_and_history_lines(settings, now)[:2],
    ]
    return report
