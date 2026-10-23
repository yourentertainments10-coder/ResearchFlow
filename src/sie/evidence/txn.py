"""The shared write transaction of the evidence ledger.

Every ingestion path (uploads, registered web sources) runs through :func:`run_ingest_txn`:

* one transaction at READ COMMITTED (the serialisation argument needs it, so it is asserted);
* ``SET LOCAL`` lock and statement timeouts, so a stuck writer fails instead of queuing forever;
* the project row locked ``FOR SHARE``, so a purge (which takes ``FOR UPDATE``) and an ingest cannot
  interleave: one waits for the other, and an ingest that waited sees the archive or purge and refuses;
* bounded retry of the whole transaction on unique violation, serialization failure and deadlock.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from sie.evidence.errors import IngestBusy, ProjectClosed

log = logging.getLogger(__name__)
T = TypeVar("T")

RETRYABLE_SQLSTATES = {
    "23505",
    "40001",
    "40P01",
}  # unique violation, serialization failure, deadlock
MAX_ATTEMPTS = 3


def preamble(conn: Connection, project_id: int) -> None:
    """The checks and locks every write transaction starts with."""
    isolation = conn.execute(text("SHOW transaction_isolation")).scalar_one()
    if isolation != "read committed":  # the serialisation argument only holds at this level
        raise IngestBusy(f"ingestion needs READ COMMITTED, the connection is {isolation}")
    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    conn.execute(text("SET LOCAL statement_timeout = '60s'"))
    row = conn.execute(
        text("SELECT archived_at, purged_at FROM projects WHERE id = :p FOR SHARE"),
        {"p": project_id},
    ).one_or_none()
    if row is None or row.purged_at is not None or row.archived_at is not None:
        raise ProjectClosed("project is archived or purged and takes no new evidence")


def advisory_lock(conn: Connection, key: str) -> None:
    """A transaction-scoped lock on a document key. A hash collision only adds waiting."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def run_ingest_txn(
    engine: Engine,
    project_id: int,
    work: Callable[[Connection], T],
    *,
    op: str,
    log_extra: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with (
                engine.connect().execution_options(isolation_level="READ COMMITTED") as conn,
                conn.begin(),
            ):
                preamble(conn, project_id)
                result = work(conn)
            log.info(op, extra={"project_id": project_id, "attempt": attempt, **log_extra})
            return result
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None)
            if state not in RETRYABLE_SQLSTATES:
                raise
            if attempt == MAX_ATTEMPTS:
                raise IngestBusy(
                    f"{op} kept colliding with concurrent writers ({state}); nothing was written"
                ) from exc
            log.warning(
                f"{op} retry",
                extra={"project_id": project_id, "sqlstate": state, "attempt": attempt},
            )
            sleep(random.uniform(0.05, 0.2) * attempt)
    raise AssertionError("unreachable")  # pragma: no cover
