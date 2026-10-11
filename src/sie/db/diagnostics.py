"""Bounded waits and visible progress for commands that talk to a remote PostgreSQL (Neon).

Two things made ``sie seed-reference`` look hung against Neon (reproduced on a throwaway database,
see docs/DECISIONS.md ADR-038):

* it sends 361 statements one after another, so the time is ``361 x network round trip`` with no
  output meanwhile (about 40 s at 100 ms, 150 s at 400 ms);
* an upsert waits without limit if another transaction holds a lock on the same row.

This module does not change what any command writes. It puts a limit on waiting for a lock and on one
statement (``SET LOCAL``: it lasts for one transaction, so it is safe behind a transaction pooler),
prints a heartbeat naming the statement in progress, and, read-only, says which session blocks which.
Nothing here prints a connection string or a parameter value.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.exc import DBAPIError

LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout expired
QUERY_CANCELED = "57014"  # statement_timeout expired (or a user cancel)


def apply_timeouts(conn: Connection, lock_seconds: float, statement_seconds: float) -> None:
    """Limit this transaction's waiting. 0 means no limit. ``SET LOCAL`` ends with the transaction."""
    for name, seconds in (("lock_timeout", lock_seconds), ("statement_timeout", statement_seconds)):
        conn.execute(
            text(f"SELECT set_config('{name}', :v, true)"), {"v": f"{int(seconds * 1000)}ms"}
        )


def statement_head(statement: str, width: int = 90) -> str:
    """First meaningful words of a statement: enough to name it, never any parameter value."""
    return " ".join(statement.split())[:width]


@dataclass
class Progress:
    started: float = field(default_factory=time.monotonic)
    statements: int = 0
    current: str = ""
    current_since: float = field(default_factory=time.monotonic)

    def line(self) -> str:
        now = time.monotonic()
        return (
            f"still running after {now - self.started:.0f}s: {self.statements} statements done; "
            f"waiting {now - self.current_since:.0f}s on: {self.current or '(connecting)'}"
        )


@contextmanager
def heartbeat(
    engine: Engine, emit: Callable[[str], None], interval: float = 10.0
) -> Iterator[Progress]:
    """Call ``emit`` with a progress line every ``interval`` seconds while the block runs."""
    progress = Progress()

    def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        progress.current = statement_head(statement)
        progress.current_since = time.monotonic()

    def after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        progress.statements += 1

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            emit(progress.line())

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield progress
    finally:
        stop.set()
        thread.join(timeout=1)
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


def sqlstate(exc: BaseException) -> str | None:
    orig = exc.orig if isinstance(exc, DBAPIError) else exc
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


_ACTIVITY_SQL = """
SELECT a.pid, a.state, a.wait_event_type, a.wait_event,
       round(extract(epoch FROM now() - coalesce(a.xact_start, a.query_start)))::int AS seconds,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 100) AS query,
       pg_blocking_pids(a.pid) AS blocked_by
FROM pg_stat_activity a
WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
  AND (a.state <> 'idle' OR a.xact_start IS NOT NULL)
ORDER BY seconds DESC NULLS LAST
"""


def activity(engine: Engine) -> list[dict[str, Any]]:
    """Read-only list of open or running sessions and which sessions block them.

    Uses its own short connection with a 5 s statement limit; changes nothing. Session queries are
    cut to 100 characters and run through ``left``; bind values do not appear in pg_stat_activity.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET statement_timeout = '5s'"))
        conn.execute(text("SET default_transaction_read_only = on"))
        rows = [dict(r._mapping) for r in conn.execute(text(_ACTIVITY_SQL))]
    return rows


def format_activity(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["no other open or running sessions on this database"]
    lines = []
    for r in rows:
        blockers = f", blocked by pid {r['blocked_by']}" if r["blocked_by"] else ""
        wait = f"{r['wait_event_type']}/{r['wait_event']}" if r["wait_event"] else "no wait"
        lines.append(
            f"pid {r['pid']} {r['state']} for {r['seconds']}s ({wait}{blockers}): {r['query']}"
        )
    return lines
