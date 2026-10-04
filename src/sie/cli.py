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


@app.command("load-capture")
def load_capture(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Load a portal capture (JSON from the browser snippet) into the database. Safe to rerun."""
    from sie.load import LoadError, load_placings, parse_capture

    engine = make_engine(get_settings())
    placings, raw = parse_capture(path)
    try:
        with engine.begin() as conn:
            result = load_placings(conn, placings, raw, path.name)
    except LoadError as exc:
        typer.echo(f"load error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"events: {result.events}  placings: {result.placings}  raw unchanged: {result.raw_unchanged}"
    )


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
