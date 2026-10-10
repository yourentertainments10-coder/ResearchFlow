"""Controlled failure and recovery, end to end, in an isolated database.

Nothing here touches production: the database is the per-test copy from ``conftest.py``, the portal
is the in-memory fake, and the webhook is a local HTTP server (``webhook_hook.Hook``). Delivery is
therefore *real HTTP to a local receiver*, not delivery to a hosted chat service; that last hop needs
the owner's ``NOTIFY_WEBHOOK_URL`` and is covered by the manual drill in docs/DEPLOYMENT.md.

The story: healthy -> portal outage -> alert once -> still down (no repeat) -> channel down (retry) ->
channel back (delivered) -> interrupted run (stuck alert, does not block or hide anything) ->
recovery (everything resolved) -> the same outage again (alerts again) -> corrupt state (alerts again).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from portal_fake import UA, Portal, real_pages
from sie.cli import app
from sie.config import Settings
from sie.pipeline.scheduler import ScheduleStatus, SourceTask, close_stuck_runs, run_scheduled
from sie.reference import seed_reference
from sie.sources.bornan.fetch import PortalClient, fetch_capture
from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed
from webhook_hook import Hook

ROOT = Path(__file__).resolve().parents[2]
COMP = "asiad-2026"
REPEATED = f"{COMP}:{SOURCE}:repeated_failures"
STUCK = f"{COMP}:{SOURCE}:stuck_runs"


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
        http_user_agent=UA,
        portal_fetch_enabled=True,
        scheduled_sources=SOURCE,
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


@pytest.fixture()
def hook():
    h = Hook()
    yield h
    h.close()


@pytest.fixture()
def cli_env(settings, monkeypatch, hook):
    for key, value in {
        "DATABASE_URL": settings.database_url,
        "DATA_DIR": str(settings.data_dir),
        "COMPETITION_ID": COMP,
        "SCHEDULED_SOURCES": SOURCE,
        "NOTIFY_WEBHOOK_URL": hook.url,
    }.items():
        monkeypatch.setenv(key, value)


def refresh(engine, settings, pages) -> ScheduleStatus:
    def fetch():
        from sie.pipeline.runner import SourceInput

        content = fetch_capture(PortalClient(UA, get=Portal(pages), sleep=lambda s: None))
        return SourceInput(
            source=SOURCE,
            url="portal:AG2026/medals",
            content=content,
            content_type="application/json",
            extension="json",
            parse=lambda raw: capture_to_parsed(raw, COMP),
        )

    task = SourceTask(source=SOURCE, url="portal:AG2026/medals", fetch=fetch)
    return run_scheduled(engine, settings, task, sleep=lambda s: None).status


def down() -> dict:
    pages = real_pages()
    pages["ARC/medals/discipline"] = 503
    return pages


def health(*extra: str) -> tuple[int, dict]:
    res = CliRunner().invoke(app, ["health", "--channel", "webhook", *extra])
    return res.exit_code, json.loads(res.stdout)


def health_in_new_process(settings, hook) -> tuple[int, dict]:
    """The same command in a separate interpreter: what a later workflow run does."""
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "DATA_DIR": str(settings.data_dir),
        "COMPETITION_ID": COMP,
        "SCHEDULED_SOURCES": SOURCE,
        "NOTIFY_WEBHOOK_URL": hook.url,
    }
    done = subprocess.run(
        [sys.executable, "-c", "from sie.cli import app; app()", "health", "--channel", "webhook"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, json.loads(done.stdout)


def running(engine, started_at) -> int:
    with engine.begin() as c:
        cid = c.execute(text("SELECT id FROM competitions")).scalar_one()
        return c.execute(
            text(
                "INSERT INTO ingest_runs (competition_id, started_at, status, source) "
                "VALUES (:c, :t, 'running', :s) RETURNING id"
            ),
            {"c": cid, "t": started_at, "s": SOURCE},
        ).scalar_one()


def state(settings) -> dict:
    return json.loads((settings.data_dir / "alert_state.json").read_text())["alerts"]


def test_failure_alert_dedup_persistence_retry_stuck_recovery_and_relapse(
    seeded, settings, cli_env, hook
):
    # 1. healthy: no alert, nothing delivered
    assert refresh(seeded, settings, real_pages()) == ScheduleStatus.SUCCEEDED
    code, out = health()
    assert code == 0 and out["healthy"] and out["notification"]["sent"] == []
    assert hook.received == []

    # 2. portal outage: three recorded fetch failures, last good data kept, one alert delivered
    placings = _count(seeded, "SELECT count(*) FROM placings WHERE is_current")
    assert refresh(seeded, settings, down()) == ScheduleStatus.FAILED
    assert _count(seeded, "SELECT count(*) FROM placings WHERE is_current") == placings
    code, out = health()
    assert code == 1
    assert [a["key"] for a in out["alerts"]] == [REPEATED]
    assert out["notification"]["sent"] == [REPEATED] and out["notification"]["failed"] == {}
    assert len(hook.received) == 1
    body = hook.received[0][1]
    assert body["alerts"][0]["key"] == REPEATED and "failed 3 times" in body["text"]
    assert state(settings)[REPEATED]["active"] is True

    # 3. still down, checked from a new process: state persisted, so no second message
    assert refresh(seeded, settings, down()) == ScheduleStatus.FAILED
    code, out = health_in_new_process(settings, hook)
    assert code == 1 and out["notification"]["sent"] == []
    assert out["notification"]["suppressed"] == [REPEATED]
    assert len(hook.received) == 1

    # 4. an interrupted run appears while the channel is broken: the new alert is not lost
    stuck_id = running(seeded, datetime.now(UTC) - timedelta(hours=3))
    hook.status = 500
    code, out = health()
    assert code == 1 and STUCK in out["notification"]["failed"]
    assert state(settings)[STUCK]["last_notified_at"] is None  # not marked as delivered
    hook.status = 200
    code, out = health()  # channel back: retried, delivered exactly once
    assert out["notification"]["sent"] == [STUCK]
    assert out["notification"]["suppressed"] == [REPEATED]
    code, out = health()
    assert out["notification"]["sent"] == []

    # 5. the abandoned row neither blocks the next run nor hides anything: the next run (which holds
    #    the source lock) closes it itself, and the manual command then has nothing left to do
    assert refresh(seeded, settings, real_pages()) == ScheduleStatus.SUCCEEDED
    assert (
        _count(
            seeded, f"SELECT count(*) FROM ingest_runs WHERE id = {stuck_id} AND status = 'failed'"
        )
        == 1
    )
    res = CliRunner().invoke(app, ["recover-stuck", "--older-than-minutes", "60"])
    assert res.exit_code == 0 and "closed 0 stuck run(s)" in res.output

    # 6. recovery: healthy again, both alerts resolved in the state, no further message
    sent_before = len(hook.received)
    code, out = health()
    assert code == 0 and out["healthy"]
    assert sorted(out["notification"]["resolved"]) == sorted([REPEATED, STUCK])
    assert len(hook.received) == sent_before
    assert not any(r["active"] for r in state(settings).values())

    # 7. the same outage again is a new condition and alerts again
    assert refresh(seeded, settings, down()) == ScheduleStatus.FAILED
    code, out = health()
    assert code == 1 and out["notification"]["sent"] == [REPEATED]
    assert len(hook.received) == sent_before + 1

    # 8. lost or corrupt state means a repeated alert, never a missing one
    (settings.data_dir / "alert_state.json").write_text("{not json")
    code, out = health()
    assert out["notification"]["sent"] == [REPEATED]
    assert (settings.data_dir / "alert_state.json.corrupt").exists()
    assert len(hook.received) == sent_before + 2


def test_the_command_closes_an_interrupted_run_and_leaves_a_young_one_alone(seeded, settings):
    old = running(seeded, datetime.now(UTC) - timedelta(hours=2))
    young = running(seeded, datetime.now(UTC) - timedelta(minutes=5))
    closed = close_stuck_runs(
        seeded, settings, source=SOURCE, now=datetime.now(UTC), older_than=timedelta(hours=1)
    )
    assert closed == [old] and young not in closed  # a possibly live run is left alone
    assert (
        refresh(seeded, settings, real_pages()) == ScheduleStatus.SUCCEEDED
    )  # no lock left behind


def _count(engine, sql: str) -> int:
    with engine.connect() as c:
        return c.execute(text(sql)).scalar_one()
