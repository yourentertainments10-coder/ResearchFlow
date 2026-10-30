"""``sie workspace run FILE --out DIR``: a thin adapter over ``sie.workspace``.

Exit codes: 0 every analysis verified and exported; 1 the file or data is unusable; 2 invalid usage;
3 finished, but at least one analysis failed its independent check (or could not run) and was not exported.
Files are written only under ``--out`` and ``DATA_DIR/uploads``. Nothing is sent over a network.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from sie.datasets import DatasetError, Limits
from sie.datasets.run import exit_code_for
from sie.datasets.store import read_regular_file, store_raw
from sie.datasets_cli import _fail, _load_options
from sie.workspace import build_workspace
from sie.workspace.writer import verify_written, write_workspace

workspace_app = typer.Typer(help="Visual analysis workspace (CSV first).", no_args_is_help=True)


@workspace_app.command("run")
def run_cmd(
    file: Path = typer.Argument(..., help="CSV file to analyse"),
    out: Path = typer.Option(..., "--out", help="Directory for the results"),
    delimiter: str = typer.Option(",", help="Field delimiter"),
    encoding: str = typer.Option("utf-8", help="Text encoding"),
    na_values: str | None = typer.Option(None, help="Extra null markers, comma separated"),
    ragged: str = typer.Option("error", help="error or reject"),
    rename_duplicates: bool = typer.Option(False, help="Rename duplicate headers"),
) -> None:
    from sie.config import get_settings

    options = _load_options(None, delimiter, encoding, na_values, ragged, rename_duplicates)
    limits = Limits()
    try:
        data = read_regular_file(file, limits.max_bytes)
        workspace = build_workspace(data, file.name, options=options, limits=limits)
        store_raw(data, file.name, get_settings().data_dir / "uploads")
        directory, present, skipped = write_workspace(workspace, out)
        verify_written(directory)
    except DatasetError as exc:
        raise _fail(str(exc), exit_code_for(exc)) from exc
    summary = {
        "directory": str(directory),
        "already_present": present,
        "input_sha256": workspace.source.sha256,
        "workspace_sha256": workspace.sha256(),
        "analyses": len(workspace.items),
        "exported": len(workspace.items) - len(skipped),
        "not_exported": skipped,
        "cards": [
            {"label": c.label, "value": c.value, "verified": c.verified} for c in workspace.cards
        ],
        "findings": [f.text for f in workspace.findings],
    }
    typer.echo(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    if skipped:
        raise typer.Exit(code=3)


def register(app: typer.Typer) -> None:
    app.add_typer(workspace_app, name="workspace")
