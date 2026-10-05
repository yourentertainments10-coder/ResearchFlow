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


@app.command("load-capture")
def load_capture(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Load a portal capture (JSON from the browser snippet) into the database. Safe to rerun."""
    from sie.pipeline.runner import SourceInput
    from sie.sources.bornan.to_parsed import SOURCE, capture_to_parsed

    settings = get_settings()
    competition = settings.competition_id
    src = SourceInput(
        source=SOURCE,
        url=f"capture:{path.name}",
        content=path.read_bytes(),
        content_type="application/json",
        extension="json",
        parse=lambda raw: capture_to_parsed(raw, competition),
    )
    _ingest(src, settings)


@app.command("import-csv")
def import_csv(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Import a manual results CSV (docs/DATA_PIPELINE.md section 9). Safe to rerun."""
    from sie.pipeline.runner import SourceInput
    from sie.sources.manual.parser import SOURCE, parse_manual_csv

    settings = get_settings()
    src = SourceInput(
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
    _ingest(src, settings)


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
