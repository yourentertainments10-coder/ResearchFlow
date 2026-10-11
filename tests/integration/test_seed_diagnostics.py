"""`sie seed-reference` must not look hung: bounded waits, visible progress, read-only diagnostics.

Reproduced on the throwaway test database only (never production). See ADR-038.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from sie.cli import app
from sie.db import diagnostics as diag
from sie.reference import seed_reference

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def env(db_url, tmp_path, monkeypatch):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    for key, value in {
        "DATABASE_URL": db_url.render_as_string(hide_password=False),
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    return db_url


@pytest.fixture()
def holder(env):
    """A second session that holds a row lock on the competition and sits idle in its transaction."""
    first = create_engine(env)
    with first.begin() as c:
        seed_reference(c, ROOT / "data" / "reference")
        c.execute(text("DELETE FROM country_aliases"))  # so a later partial write would be visible
        c.execute(text("DELETE FROM countries WHERE code <> 'CHN'"))
    first.dispose()
    blocker = create_engine(env).connect()
    tx = blocker.begin()
    blocker.execute(text("UPDATE competitions SET name = name WHERE code = 'asiad-2026'"))
    yield blocker
    tx.rollback()
    blocker.close()


def count(url, table):
    eng = create_engine(url)
    with eng.connect() as c:
        return c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_a_lock_wait_is_bounded_and_names_the_blocker(env, holder):
    started = time.monotonic()
    res = CliRunner().invoke(
        app, ["seed-reference", "--lock-timeout", "1", "--heartbeat-seconds", "0"]
    )
    assert time.monotonic() - started < 15
    assert res.exit_code == 3, res.output
    err = res.stderr
    assert "waited more than 1s for a lock held by another session" in err
    assert "Nothing was written" in err
    assert "idle in transaction" in err and "UPDATE competitions" in err  # the blocker, named
    assert "postgresql" not in err and "://" not in err  # no connection string
    # the transaction rolled back: the partial state set up before the run is unchanged
    assert count(env, "countries") == 1 and count(env, "country_aliases") == 0


def test_the_same_run_succeeds_once_the_lock_is_released(env, holder):
    holder.rollback()
    res = CliRunner().invoke(app, ["seed-reference", "--lock-timeout", "5"])
    assert res.exit_code == 0, res.output
    assert count(env, "countries") > 1


def test_a_slow_statement_is_cancelled_by_the_statement_timeout(env):
    eng = create_engine(env)
    with eng.connect() as conn:
        diag.apply_timeouts(conn, lock_seconds=0, statement_seconds=1)
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "1s"
        with pytest.raises(DBAPIError) as err:
            conn.execute(text("SELECT pg_sleep(5)"))
        assert diag.sqlstate(err.value) == diag.QUERY_CANCELED
    with (
        eng.connect() as conn
    ):  # SET LOCAL: gone with the transaction, nothing leaks to the next one
        assert conn.execute(text("SHOW statement_timeout")).scalar_one() == "0"


def test_zero_means_no_limit(env):
    eng = create_engine(env)
    with eng.begin() as conn:
        diag.apply_timeouts(conn, lock_seconds=0, statement_seconds=0)
        assert conn.execute(text("SHOW lock_timeout")).scalar_one() == "0"


def test_the_heartbeat_names_the_statement_in_progress_without_values(env):
    eng = create_engine(env)
    lines: list[str] = []
    with diag.heartbeat(eng, lines.append, interval=0.2) as progress, eng.begin() as conn:
        conn.execute(text("SELECT pg_sleep(1), 'secret-value-123'"))
    assert progress.statements >= 1
    assert any("still running" in x and "pg_sleep" in x for x in lines), lines
    assert progress.current.startswith("SELECT pg_sleep")


def test_activity_is_read_only_and_reports_blocked_sessions(env, holder):
    done = threading.Event()

    def blocked():
        eng = create_engine(env)
        try:
            with eng.begin() as c:
                c.execute(text("SET LOCAL lock_timeout = '4s'"))
                c.execute(text("UPDATE competitions SET name = name"))
        except Exception:  # noqa: BLE001 - lock timeout is the expected end
            pass
        finally:
            done.set()

    threading.Thread(target=blocked, daemon=True).start()
    time.sleep(1.0)
    eng = create_engine(env)
    lines = diag.format_activity(diag.activity(eng))
    text_ = "\n".join(lines)
    assert "Lock/transactionid" in text_ and "blocked by pid" in text_
    with (  # the diagnostic session cannot write
        pytest.raises(DBAPIError, match="read-only"),
        eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c,
    ):
        c.execute(text("SET default_transaction_read_only = on"))
        c.execute(text("UPDATE competitions SET name = name"))
    done.wait(6)


def test_db_activity_command_prints_the_same(env, holder):
    res = CliRunner().invoke(app, ["db-activity"])
    assert res.exit_code == 0
    assert "idle in transaction" in res.output
