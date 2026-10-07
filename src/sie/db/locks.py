"""PostgreSQL advisory locks that stop two runs of the same source from overlapping.

The lock is *session level* and lives on one dedicated connection that the caller holds for the whole
run. Three properties make it safe:

* ``pg_try_advisory_lock`` never waits, so a second run is told "busy" immediately and can skip;
* it is released when the *session* ends, so a crashed process cannot leave a lock behind (and if an
  explicit unlock ever fails, the physical connection is discarded rather than returned to the pool);
* the key is derived from (competition, source), so independent sources never block each other.

The connection runs in AUTOCOMMIT so it is never "idle in transaction" while holding the lock.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text

LOCK_NAMESPACE = (
    0x53494521  # "SIE!": the first of the two int4 keys, so we never collide with others
)


def lock_key(competition: str, source: str) -> tuple[int, int]:
    """Two signed 32-bit integers, stable across processes and Python versions."""
    second = zlib.crc32(f"{competition}\x00{source}".encode())
    return _signed(LOCK_NAMESPACE), _signed(second)


def _signed(value: int) -> int:
    return value - (1 << 32) if value >= (1 << 31) else value


@contextmanager
def source_lock(engine: Engine, competition: str, source: str) -> Iterator[bool]:
    """Yield True if this process now holds the lock for the source, False if another run has it."""
    key = lock_key(competition, source)
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": key[0], "b": key[1]}
            ).scalar_one()
        )
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": key[0], "b": key[1]})
        except Exception:
            # A pooled connection handed back still holds a session lock, so the physical connection
            # must be discarded; ending the session is what drops the lock.
            conn.invalidate()
            raise
        finally:
            conn.close()
