"""Command line interface: ``sie <command>``."""

from __future__ import annotations

from pathlib import Path

import typer

from sie import __version__
from sie.config import get_settings
from sie.db.roles import PIPELINE, READER, apply_roles
from sie.db.session import make_engine
from sie.logging_setup import setup_logging
from sie.reference import ReferenceDataError, seed_reference

app = typer.Typer(help="Sports Intelligence Engine", no_args_is_help=True)


@app.callback()
def _main() -> None:
    setup_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations (alembic upgrade)."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, revision)


@app.command("seed-reference")
def seed_reference_cmd(
    reference_dir: Path | None = typer.Option(None, help="Defaults to DATA_DIR/reference"),
) -> None:
    """Load countries, sports, aliases and the competition from data/reference/*.csv."""
    settings = get_settings()
    directory = reference_dir or settings.reference_dir
    engine = make_engine(settings)
    try:
        with engine.begin() as conn:  # one transaction: all of it or none of it
            result = seed_reference(conn, directory)
    except ReferenceDataError as exc:
        typer.echo(f"reference data error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for name, count in result.__dict__.items():
        typer.echo(f"{name:>16}: {count}")


def _ingest(src, settings) -> None:
    from sie.pipeline.load import LoadError
    from sie.pipeline.runner import run_ingest

    try:
        result = run_ingest(make_engine(settings), src, settings)
    except LoadError as exc:
        typer.echo(f"load error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"run {result.run_id} {result.status}: raw {result.raw_outcome}, events {result.events}, "
        f"placings {result.placings}, quarantined {result.rows_quarantined}, "
        f"duplicates skipped {result.duplicates_skipped}"
    )
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if result.status != "success":
        typer.echo(f"failed: {result.error}", err=True)
        raise typer.Exit(code=1)


def _capture_input(path: Path, settings):
    from sie.pipeline.runner import SourceInput
    from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed

    competition = settings.competition_id
    return SourceInput(
        source=SOURCE,
        url=f"capture:{path.name}",
        content=path.read_bytes(),
        content_type="application/json",
        extension="json",
        parse=lambda raw: capture_to_parsed(raw, competition),
    )


def _csv_input(path: Path, settings):
    from sie.pipeline.runner import SourceInput
    from sie.sources.manual.parser import SOURCE, parse_manual_csv

    return SourceInput(
        source=SOURCE,
        url=f"file://{path.name}",
        content=path.read_bytes(),
        content_type="text/csv",
        extension="csv",
        parse=lambda raw: parse_manual_csv(
            raw,
            max_rows=settings.manual_csv_max_rows,
            max_cell_chars=settings.manual_csv_max_cell_chars,
        ),
    )


@app.command("load-capture")
def load_capture(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Load a portal capture (JSON from the browser snippet) into the database. Safe to rerun."""
    settings = get_settings()
    _ingest(_capture_input(path, settings), settings)


@app.command("import-csv")
def import_csv(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Import a manual results CSV (docs/DATA_PIPELINE.md section 9). Safe to rerun."""
    settings = get_settings()
    _ingest(_csv_input(path, settings), settings)


@app.command("scheduled-run")
def scheduled_run(
    kind: str = typer.Argument(
        ..., help="'capture' (portal capture JSON) or 'csv' (manual results CSV)."
    ),
    path: Path = typer.Argument(..., help="The file to ingest. Read on every attempt."),
) -> None:
    """Ingest a file the way the scheduler does: one run per source at a time, retries per the contract.

    Exit 0 when the run succeeded or was skipped because another run of the same source is active;
    exit 1 when it failed; exit 2 for a usage error.
    """
    from sie.pipeline.load import LoadError
    from sie.pipeline.scheduler import FetchError, ScheduleStatus, SourceTask, run_scheduled

    settings = get_settings()
    builders = {"capture": _capture_input, "csv": _csv_input}
    if kind not in builders:
        typer.echo(f"unknown kind {kind!r}; use one of {sorted(builders)}", err=True)
        raise typer.Exit(code=2)

    def fetch():
        try:
            return builders[kind](path, settings)
        except OSError as exc:
            raise FetchError(f"cannot read {path}: {exc}") from exc

    source = "manual" if kind == "csv" else "official"
    task = SourceTask(source=source, url=f"{kind}:{path.name}", fetch=fetch)
    try:
        result = run_scheduled(make_engine(settings), settings, task)
    except LoadError as exc:
        typer.echo(f"load error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    final = result.final
    typer.echo(
        f"{result.source}: {result.status}, fetch attempts {result.fetch_attempts}, "
        f"load retries {result.load_retries}, stuck runs closed {result.stuck_closed}"
        + (f", run {final.run_id}" if final else "")
    )
    if result.status == ScheduleStatus.FAILED:
        typer.echo(f"failed: {final.error if final else 'no run'}", err=True)
        raise typer.Exit(code=1)


@app.command("recover-stuck")
def recover_stuck(
    older_than_minutes: int = typer.Option(60, help="Only runs started longer ago than this."),
    source: str | None = typer.Option(None, help="Limit to one source."),
) -> None:
    """Close runs left 'running' by a process that died. Does not touch data or raw evidence."""
    from datetime import UTC, datetime, timedelta

    from sie.pipeline.scheduler import close_stuck_runs

    settings = get_settings()
    closed = close_stuck_runs(
        make_engine(settings),
        settings,
        source=source,
        now=datetime.now(UTC),
        older_than=timedelta(minutes=older_than_minutes),
    )
    typer.echo(f"closed {len(closed)} stuck run(s): {closed}")


MIN_PASSWORD_LENGTH = 16


@app.command("db-roles")
def db_roles() -> None:
    """Create or refresh the least-privilege roles (run as the schema owner, after migrating).

    Reads SIE_READER_PASSWORD and SIE_PIPELINE_PASSWORD from the environment. Use the reader role in
    the dashboard and the pipeline role in scheduled runs; keep DATABASE_URL (owner) for migrations only.
    """
    settings = get_settings()
    passwords = {
        READER: settings.sie_reader_password,
        PIPELINE: settings.sie_pipeline_password,
    }
    for role, password in passwords.items():
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            typer.echo(
                f"set {role.upper()}_PASSWORD to at least {MIN_PASSWORD_LENGTH} characters",
                err=True,
            )
            raise typer.Exit(code=2)
    engine = make_engine(settings)
    with engine.begin() as conn:
        apply_roles(
            conn,
            reader_password=settings.sie_reader_password or "",
            pipeline_password=settings.sie_pipeline_password or "",
        )
    typer.echo(f"roles ready: {READER} (reporting views only), {PIPELINE} (no DELETE)")


@app.command("run-summary")
def run_summary(run_id: int = typer.Argument(..., help="An ingest_runs id.")) -> None:
    """Print the structured summary of one ingestion run as JSON."""
    import json

    from sie.pipeline.observe import load_run_summary

    with make_engine(get_settings()).connect() as conn:
        try:
            summary = load_run_summary(conn, run_id)
        except LookupError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(summary.to_dict(), indent=2))


@app.command("source-status")
def source_status(
    source: str = typer.Argument(..., help="Source name, for example 'official'."),
) -> None:
    """Print a source's freshness as JSON. Exits 1 unless the source is fresh (usable by a scheduler)."""
    import json
    from datetime import UTC, datetime, timedelta

    from sie.pipeline.freshness import Freshness, source_freshness

    settings = get_settings()
    max_age = timedelta(minutes=settings.freshness_threshold_minutes)
    with make_engine(settings).connect() as conn:
        result = source_freshness(conn, settings.competition_id, source, datetime.now(UTC), max_age)
    typer.echo(json.dumps(result.to_dict(), indent=2))
    if result.status != Freshness.FRESH:
        raise typer.Exit(code=1)


@app.command("health")
def health(
    source: list[str] = typer.Option(
        None,
        "--source",
        help="Source to check; repeat for several. Default: configured and seen sources.",
    ),
    stuck_after_minutes: int = typer.Option(
        60, help="A run still 'running' after this long is stuck."
    ),
    channel: str = typer.Option(
        "log",
        help="Where new alerts go: 'log', 'webhook' (NOTIFY_WEBHOOK_URL) or 'none' (no state kept).",
    ),
) -> None:
    """Check every source's health and print JSON with the alerts. Exit 1 if any alert is raised.

    The JSON always lists every alert that holds. The channel only hears about *new* ones: what was
    already delivered is remembered (docs/DATA_PIPELINE.md section 12), and a failed delivery is
    retried on the next check.

    Why not `source-status`: that command reports one source's freshness. This one covers several
    sources at once, adds failure counts and stuck runs, and produces the alert list.
    """
    import json
    from datetime import UTC, datetime, timedelta

    from sie.pipeline.alert_state import FileAlertState, notify_changes
    from sie.pipeline.health import check_health, discover_sources
    from sie.pipeline.notify import LogNotifier, WebhookNotifier

    settings = get_settings()
    if channel not in ("log", "webhook", "none"):
        typer.echo("channel must be log, webhook or none", err=True)
        raise typer.Exit(code=2)
    notifier = None
    if channel == "webhook":
        if not settings.notify_webhook_url:
            typer.echo("NOTIFY_WEBHOOK_URL is not set", err=True)
            raise typer.Exit(code=2)
        try:
            notifier = WebhookNotifier(settings.notify_webhook_url)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    elif channel == "log":
        notifier = LogNotifier()

    now = datetime.now(UTC)
    with make_engine(settings).connect() as conn:
        sources = (
            list(source)
            if source
            else sorted(
                set(settings.scheduled_source_list)
                | set(discover_sources(conn, settings.competition_id))
            )
        )
        report = check_health(
            conn,
            settings.competition_id,
            sources,
            now,
            max_age=timedelta(minutes=settings.freshness_threshold_minutes),
            stuck_after=timedelta(minutes=stuck_after_minutes),
        )
    data = report.to_dict()
    if notifier is not None:
        renotify = timedelta(minutes=settings.alert_renotify_minutes) or None
        outcome = notify_changes(
            FileAlertState(settings.alert_state_file), notifier, report.alerts, now, renotify
        )
        data["notification"] = outcome.to_dict()
    typer.echo(json.dumps(data, indent=2, sort_keys=True))
    if report.alerts:
        raise typer.Exit(code=1)


@app.command("backup")
def backup_cmd(
    out_dir: Path = typer.Option(
        Path("data/backups"), help="Where dumps and manifests are written."
    ),
    keep: int = typer.Option(14, help="How many dumps to keep (older ones are deleted)."),
    verify: bool = typer.Option(
        True, help="Restore the new dump into a scratch database and check it."
    ),
) -> None:
    """Dump the database (pg_dump, custom format) with a manifest, then prove it restores. Exit 1 if not."""
    import json
    from datetime import UTC, datetime

    from sie.ops.backup import BackupError, backup, restore_test

    settings = get_settings()
    now = datetime.now(UTC)
    try:
        result = backup(
            settings.database_url,
            out_dir,
            settings.competition_id,
            now,
            keep=keep,
            bin_dir=settings.pg_bin_dir,
        )
        out = {
            "dump": str(result.dump),
            "manifest": result.data,
            "pruned": [str(p) for p in result.pruned],
        }
        if verify:
            report = restore_test(result.dump, settings.database_url, bin_dir=settings.pg_bin_dir)
            out["restore_test"] = report.to_dict()
    except BackupError as exc:
        typer.echo(f"backup failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(out, indent=2, sort_keys=True))
    if verify and not report.ok:
        raise typer.Exit(code=1)


@app.command("restore-test")
def restore_test_cmd(dump: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Restore a dump into a scratch database, compare it with its manifest, drop the scratch. Exit 1 on any failed check."""
    import json

    from sie.ops.backup import BackupError, restore_test

    settings = get_settings()
    try:
        report = restore_test(dump, settings.database_url, bin_dir=settings.pg_bin_dir)
    except BackupError as exc:
        typer.echo(f"restore test failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("publish")
def publish_cmd(
    out_dir: Path = typer.Option(
        Path("data/publish"), "--out", help="Directory for the export bundle."
    ),
) -> None:
    """Write the export bundle (medals.csv, events.csv, manifest.json) from the reporting views."""
    import json
    from datetime import UTC, datetime

    from sie.ops.publish import PublishError
    from sie.ops.publish import publish as _publish

    settings = get_settings()
    try:
        result = _publish(
            make_engine(settings), settings.competition_id, out_dir, datetime.now(UTC)
        )
    except PublishError as exc:
        typer.echo(f"publish failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result.manifest, indent=2, sort_keys=True))
