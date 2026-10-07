"""Source health and alerts on a real PostgreSQL. Read-only checks over runs created by the pipeline."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from sie.cli import app
from sie.config import Settings
from sie.pipeline.alerts import AlertKind
from sie.pipeline.health import check_health, discover_sources, source_health
from sie.pipeline.notify import RecordingNotifier, deliver
from sie.pipeline.runner import SourceInput, record_fetch_failure, run_ingest
from sie.pipeline.scheduler import close_stuck_runs
from sie.reference import seed_reference
from sie.sources.manual import parser as manual

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "tests/fixtures/manual"
MAX_AGE = timedelta(minutes=30)
COMP = "asiad-2026"


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="fs",
    )


@pytest.fixture()
def seeded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    return engine


def ingest(engine, settings, source="manual", name="golden.csv", url=None):
    return run_ingest(
        engine,
        SourceInput(
            source=source,
            url=url or f"file://{source}/{name}",
            content=(MANUAL / name).read_bytes(),
            content_type="text/csv",
            extension="csv",
            parse=lambda raw: manual.parse_manual_csv(raw, max_rows=1000, max_cell_chars=200),
        ),
        settings,
    )


def fail_fetch(engine, settings, source, times=1):
    for _ in range(times):
        record_fetch_failure(
            engine,
            settings,
            source=source,
            url=f"https://x.test/{source}",
            error="down",
            http_status=503,
        )


def report(engine, sources, now=None, **kw):
    with engine.connect() as c:
        return check_health(c, COMP, sources, now or datetime.now(UTC), max_age=MAX_AGE, **kw)


def kinds(rep, source=None):
    return [a.kind for a in rep.alerts if source in (None, a.source)]


# --- the four states -----------------------------------------------------------------------------------


def test_a_fresh_source_is_healthy_with_its_evidence_and_raises_no_alert(seeded, settings):
    ok = ingest(seeded, settings)
    rep = report(seeded, ["manual"])
    (h,) = rep.sources
    assert h.status == "fresh" and rep.healthy and rep.alerts == []
    assert (
        h.last_success.run_id == ok.run_id
        and h.last_failure is None
        and h.consecutive_failures == 0
    )
    assert h.freshness.fingerprint and h.freshness.artifacts[0].version_no == 1
    assert (settings.data_dir / "raw" / h.freshness.artifacts[0].path).exists()


def test_an_unchanged_rerun_is_still_healthy(seeded, settings):
    ingest(seeded, settings)
    ingest(seeded, settings)
    assert report(seeded, ["manual"]).healthy


def test_a_stale_source_raises_exactly_one_stale_alert(seeded, settings):
    ingest(seeded, settings)
    rep = report(seeded, ["manual"], now=datetime.now(UTC) + timedelta(hours=3))
    assert rep.sources[0].status == "stale" and kinds(rep) == [AlertKind.STALE]
    assert rep.alerts[0].evidence["max_age_seconds"] == 1800


def test_a_failing_source_alerts_only_once_failures_repeat(seeded, settings):
    ingest(seeded, settings)
    fail_fetch(seeded, settings, "manual", times=1)
    one = report(seeded, ["manual"])
    assert one.sources[0].status == "failing" and one.sources[0].consecutive_failures == 1
    assert one.alerts == []  # a single failure may be retried: no alert yet

    fail_fetch(seeded, settings, "manual", times=2)
    three = report(seeded, ["manual"])
    assert three.sources[0].status == "failing" and kinds(three) == [AlertKind.REPEATED_FAILURES]
    alert = three.alerts[0]
    assert alert.evidence["consecutive_failures"] == 3
    assert alert.evidence["last_failure_category"] == "fetch_failure"
    assert "HTTP 503" in alert.evidence["last_error"]
    assert three.sources[0].last_success.run_id < three.sources[0].last_failure.run_id


def test_a_source_that_never_succeeded_is_told_apart_from_stale_and_failing(seeded, settings):
    nothing = report(seeded, ["official"])  # configured but never ran
    assert nothing.sources[0].status == "never_succeeded" and kinds(nothing) == [
        AlertKind.NEVER_SUCCEEDED
    ]
    assert "no run has been recorded" in nothing.alerts[0].message
    assert nothing.sources[0].last_success is None and nothing.sources[0].last_failure is None

    fail_fetch(seeded, settings, "official", times=3)
    failed = report(seeded, ["official"])
    assert failed.sources[0].status == "never_succeeded"
    assert kinds(failed) == [AlertKind.NEVER_SUCCEEDED, AlertKind.REPEATED_FAILURES]
    assert "after 3 failed run(s)" in failed.alerts[0].message


def test_recovery_from_repeated_failures_clears_every_alert(seeded, settings):
    fail_fetch(seeded, settings, "manual", times=3)
    ingest(seeded, settings)  # the source works again... but had never succeeded before
    ingest(seeded, settings, name="golden_reallocated.csv", url="file://manual/golden.csv")
    fail_fetch(seeded, settings, "manual", times=3)
    assert kinds(report(seeded, ["manual"])) == [AlertKind.REPEATED_FAILURES]

    ingest(seeded, settings)
    healed = report(seeded, ["manual"])
    assert healed.sources[0].status == "fresh" and healed.sources[0].consecutive_failures == 0
    assert healed.alerts == [] and healed.healthy
    assert healed.sources[0].last_failure is not None  # history stays visible after recovery


# --- stuck runs ----------------------------------------------------------------------------------------


def _running(engine, source, started_at):
    with engine.begin() as c:
        cid = c.execute(text("SELECT id FROM competitions")).scalar_one()
        return c.execute(
            text(
                "INSERT INTO ingest_runs (competition_id, started_at, status, source) "
                "VALUES (:c, :t, 'running', :s) RETURNING id"
            ),
            {"c": cid, "t": started_at, "s": source},
        ).scalar_one()


def test_a_stuck_run_alerts_until_it_is_closed_and_a_young_run_does_not(seeded, settings):
    ingest(seeded, settings)
    now = datetime.now(UTC)
    stuck = _running(seeded, "manual", now - timedelta(hours=3))
    _running(seeded, "manual", now - timedelta(minutes=5))  # might be alive
    rep = report(seeded, ["manual"], now=now)
    assert kinds(rep) == [AlertKind.STUCK_RUNS] and rep.alerts[0].evidence["run_ids"] == [stuck]

    close_stuck_runs(seeded, settings, source="manual", now=now, older_than=timedelta(hours=1))
    after = report(seeded, ["manual"], now=now)
    assert AlertKind.STUCK_RUNS not in kinds(after)
    assert after.sources[0].consecutive_failures == 1  # the abandoned run counts as one failure
    assert (
        after.sources[0].last_failure.failure_category is None
    )  # and carries no invented category


def test_a_stuck_run_of_another_source_is_not_attributed_to_this_one(seeded, settings):
    ingest(seeded, settings)
    _running(seeded, "official", datetime.now(UTC) - timedelta(hours=3))
    assert report(seeded, ["manual"]).healthy
    assert kinds(report(seeded, ["official"])) == [AlertKind.NEVER_SUCCEEDED, AlertKind.STUCK_RUNS]


# --- several sources, determinism, delivery -----------------------------------------------------------


def test_sources_are_evaluated_independently(seeded, settings):
    ingest(seeded, settings, source="manual")  # healthy
    fail_fetch(seeded, settings, "official", times=3)  # never succeeded, repeatedly failing
    rep = report(seeded, ["manual", "official", "federation"])  # federation never ran
    status = {h.source: h.status for h in rep.sources}
    assert status == {
        "federation": "never_succeeded",
        "manual": "fresh",
        "official": "never_succeeded",
    }
    assert kinds(rep, "manual") == []
    assert kinds(rep, "official") == [AlertKind.NEVER_SUCCEEDED, AlertKind.REPEATED_FAILURES]
    assert kinds(rep, "federation") == [AlertKind.NEVER_SUCCEEDED]
    assert len({a.key for a in rep.alerts}) == len(rep.alerts) == 3


def test_the_report_is_deterministic_and_independent_of_source_order(seeded, settings):
    ingest(seeded, settings)
    fail_fetch(seeded, settings, "official", times=3)
    now = datetime.now(UTC)
    a = report(seeded, ["manual", "official"], now=now).to_dict()
    b = report(seeded, ["official", "manual", "manual"], now=now).to_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [s["source"] for s in a["sources"]] == ["manual", "official"]


def test_alerts_flow_through_the_notification_abstraction_and_a_healthy_source_sends_nothing(
    seeded, settings
):
    ingest(seeded, settings, source="manual")
    fail_fetch(seeded, settings, "official", times=3)
    notifier = RecordingNotifier()
    healthy = report(seeded, ["manual"])
    assert deliver(notifier, healthy.alerts).ok and notifier.sent == []  # no false alert
    broken = report(seeded, ["manual", "official"])
    result = deliver(notifier, broken.alerts)
    assert result.ok and [a.source for a in notifier.sent] == ["official", "official"]


def test_discovery_finds_sources_that_have_run(seeded, settings):
    with seeded.connect() as c:
        assert discover_sources(c, COMP) == []
    ingest(seeded, settings, source="manual")
    fail_fetch(seeded, settings, "official")
    with seeded.connect() as c:
        assert discover_sources(c, COMP) == ["manual", "official"]
        assert (
            source_health(c, COMP, "manual", datetime.now(UTC), max_age=MAX_AGE).status == "fresh"
        )


# --- the command ---------------------------------------------------------------------------------------


@pytest.fixture()
def cli_env(settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("COMPETITION_ID", COMP)
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("SCHEDULED_SOURCES", "official")


def test_the_health_command_exits_zero_when_healthy_and_one_with_alerts(
    seeded, settings, cli_env, monkeypatch
):
    runner = CliRunner()
    monkeypatch.setenv("SCHEDULED_SOURCES", "")
    ingest(seeded, settings)
    ok = runner.invoke(app, ["health"])
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["healthy"] is True

    monkeypatch.setenv("SCHEDULED_SOURCES", "official")  # configured but never ran
    bad = runner.invoke(app, ["health"])
    data = json.loads(bad.stdout)
    assert bad.exit_code == 1 and data["healthy"] is False
    assert [a["kind"] for a in data["alerts"]] == ["never_succeeded"]
    assert [s["source"] for s in data["sources"]] == ["manual", "official"]


def test_a_first_parse_failure_alerts_immediately_and_clears_when_fixed(seeded, settings):
    def explode(_raw):
        raise ValueError("layout changed")

    run_ingest(
        seeded,
        SourceInput(
            source="manual", url="file://x.csv", content=b"a,b\n", content_type="text/csv",
            extension="csv", parse=explode,
        ),
        settings,
    )  # fmt: skip
    rep = report(seeded, ["manual"])
    assert kinds(rep) == [
        AlertKind.NEVER_SUCCEEDED,
        AlertKind.NEEDS_ATTENTION,
    ]  # one failure, no repeat yet
    attention = rep.alerts[1]
    assert attention.evidence["failure_category"] == "parse_failure"
    assert "layout changed" in attention.evidence["last_error"]

    ingest(seeded, settings)
    assert report(seeded, ["manual"]).healthy


def test_the_health_command_delivers_a_new_alert_once_and_stays_quiet_until_it_changes(
    seeded, settings, cli_env
):
    from webhook_hook import Hook

    hook = Hook()
    try:
        env = {"NOTIFY_WEBHOOK_URL": hook.url}
        runner = CliRunner()
        first = runner.invoke(app, ["health", "--channel", "webhook"], env=env)
        second = runner.invoke(app, ["health", "--channel", "webhook"], env=env)
        d1, d2 = json.loads(first.stdout), json.loads(second.stdout)
        assert first.exit_code == second.exit_code == 1  # the condition still holds
        assert d1["notification"]["sent"] == ["asiad-2026:official:never_succeeded"]
        assert (
            d2["notification"]["sent"] == []
            and d2["notification"]["suppressed"] == d1["notification"]["sent"]
        )
        assert len(hook.received) == 1  # the channel heard it once

        ingest(seeded, settings, source="official")  # the source finally works
        third = runner.invoke(app, ["health", "--channel", "webhook"], env=env)
        assert third.exit_code == 0 and json.loads(third.stdout)["notification"]["resolved"] == [
            "asiad-2026:official:never_succeeded"
        ]
        assert len(hook.received) == 1
    finally:
        hook.close()


def test_the_health_command_rejects_a_missing_or_insecure_webhook_and_unknown_channels(
    seeded, settings, cli_env
):
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["health", "--channel", "webhook"], env={"NOTIFY_WEBHOOK_URL": ""}
        ).exit_code
        == 2
    )
    assert runner.invoke(
        app, ["health", "--channel", "webhook"], env={"NOTIFY_WEBHOOK_URL": "http://example.org/x"}
    ).exit_code == 2  # fmt: skip
    assert runner.invoke(app, ["health", "--channel", "pigeon"]).exit_code == 2


def test_the_none_channel_keeps_no_state(seeded, settings, cli_env):
    CliRunner().invoke(app, ["health", "--channel", "none"])
    assert not (settings.data_dir / "alert_state.json").exists()
