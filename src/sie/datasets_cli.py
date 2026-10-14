"""``sie profile | clean | calc | calc-spec | operations``: a thin adapter over ``sie.datasets``.

All behaviour lives in the library (``sie.datasets.run``, ``sie.datasets.shorthand``); this module only
declares options, reads the one input file, calls the library and maps errors to exit codes:
0 success, 1 the file or data is unusable (parse failure, limits, rejects over the threshold,
integrity), 2 an invalid request or usage. Files are written only under ``--out`` and
``DATA_DIR/uploads``. Cell values are never written to logs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from sie.datasets import DatasetError, Limits, LoadOptions, list_operations
from sie.datasets.run import exit_code_for, run_request
from sie.datasets.shorthand import (
    OPERATION_ARGUMENTS,
    build_plan,
    build_spec,
    parse_spec_document,
    split_list,
)
from sie.datasets.store import read_regular_file

MAX_SPEC_BYTES = 1_000_000


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _data_dir() -> Path:
    from sie.config import get_settings

    return get_settings().data_dir


def _load_options(
    sheet: str | None,
    delimiter: str,
    encoding: str,
    na_values: str | None,
    ragged: str,
    rename_duplicates: bool,
) -> LoadOptions:
    try:
        return LoadOptions(
            sheet=sheet,
            delimiter="\t" if delimiter in ("\\t", "tab") else delimiter,
            encoding=encoding,
            na_values=tuple(split_list(na_values) or ()),
            ragged=ragged,  # type: ignore[arg-type]
            rename_duplicates=rename_duplicates,
        )
    except ValueError as exc:
        raise _fail(f"invalid option: {exc}", 2) from exc


def _execute(
    file: Path,
    options: LoadOptions,
    out: Path,
    *,
    plan: Any = None,
    specs: list[Any] | None = None,
    examples: bool = True,
) -> None:
    limits = Limits()
    try:
        data = read_regular_file(file, limits.max_bytes)
        outcome = run_request(
            data,
            file.name,
            uploads_root=_data_dir() / "uploads",
            out_root=out,
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            options=options,
            limits=limits,
            plan=plan,
            specs=specs,
            examples=examples,
        )
    except DatasetError as exc:
        raise _fail(str(exc), exit_code_for(exc)) from exc
    typer.echo(json.dumps(outcome.summary(), indent=2, sort_keys=True))


def _plan(
    trim: bool,
    trim_columns: str | None,
    drop_empty_columns: str | None,
    parse_number: list[str] | None,
    parse_date: list[str] | None,
    drop_duplicates: bool,
    subset: str | None,
    max_reject_share: float,
) -> Any:
    try:
        return build_plan(
            trim=trim,
            trim_columns=split_list(trim_columns),
            drop_empty_columns=split_list(drop_empty_columns),
            parse_number=parse_number,
            parse_date=parse_date,
            drop_duplicates=drop_duplicates,
            subset=split_list(subset),
            max_reject_share=max_reject_share,
        )
    except DatasetError as exc:
        raise _fail(str(exc), 2) from exc


def profile_cmd(
    file: Path = typer.Argument(..., help="CSV, XLSX or JSON file."),
    sheet: str | None = typer.Option(None, "--sheet", help="Excel sheet (default: first visible)."),
    delimiter: str = typer.Option(",", "--delimiter", help="CSV delimiter; never guessed."),
    encoding: str = typer.Option("utf-8", "--encoding", help="CSV encoding if not UTF-8."),
    na_values: str | None = typer.Option(None, "--na-values", help="Extra null markers, A,B."),
    ragged: str = typer.Option("error", "--ragged", help="error or reject."),
    rename_duplicates: bool = typer.Option(False, "--rename-duplicates"),
    no_examples: bool = typer.Option(False, "--no-examples", help="Omit example values."),
    out: Path | None = typer.Option(None, "--out", help="Default: DATA_DIR/analysis."),
) -> None:
    """Profile a table: types, nulls, hints. Writes a verified result directory."""
    options = _load_options(sheet, delimiter, encoding, na_values, ragged, rename_duplicates)
    _execute(file, options, out or _data_dir() / "analysis", examples=not no_examples)


def clean_cmd(
    file: Path = typer.Argument(..., help="CSV, XLSX or JSON file."),
    out: Path = typer.Option(..., "--out", help="Output directory."),
    sheet: str | None = typer.Option(None, "--sheet"),
    delimiter: str = typer.Option(",", "--delimiter"),
    encoding: str = typer.Option("utf-8", "--encoding"),
    na_values: str | None = typer.Option(None, "--na-values"),
    ragged: str = typer.Option("error", "--ragged"),
    rename_duplicates: bool = typer.Option(False, "--rename-duplicates"),
    no_examples: bool = typer.Option(False, "--no-examples"),
    trim: bool = typer.Option(False, "--trim", help="Trim whitespace in every text column."),
    trim_columns: str | None = typer.Option(None, "--trim-columns", help="Trim only these, A,B."),
    drop_empty_columns: str | None = typer.Option(None, "--drop-empty-columns", help="A,B."),
    parse_number: list[str] | None = typer.Option(
        None, "--parse-number", help="COL:DECIMAL:THOUSANDS, repeatable, e.g. amount:,:."
    ),
    parse_date: list[str] | None = typer.Option(
        None, "--parse-date", help="COL:FORMAT (strptime), repeatable."
    ),
    drop_duplicates: bool = typer.Option(False, "--drop-duplicates"),
    subset: str | None = typer.Option(None, "--subset", help="Duplicate key columns, A,B."),
    max_reject_share: float = typer.Option(0.10, "--max-reject-share"),
) -> None:
    """Apply an explicit, logged cleaning plan. Rejected rows are listed, never dropped silently."""
    options = _load_options(sheet, delimiter, encoding, na_values, ragged, rename_duplicates)
    plan = _plan(
        trim, trim_columns, drop_empty_columns, parse_number, parse_date, drop_duplicates, subset,
        max_reject_share,
    )  # fmt: skip
    if plan is None:
        raise _fail("clean needs at least one cleaning option (see --help)", 2)
    _execute(file, options, out, plan=plan, examples=not no_examples)


def calc_cmd(
    file: Path = typer.Argument(..., help="CSV, XLSX or JSON file."),
    out: Path = typer.Option(..., "--out", help="Output directory."),
    op: str = typer.Option(..., "--op", help=f"One of {sorted(OPERATION_ARGUMENTS)}."),
    sheet: str | None = typer.Option(None, "--sheet"),
    delimiter: str = typer.Option(",", "--delimiter"),
    encoding: str = typer.Option("utf-8", "--encoding"),
    na_values: str | None = typer.Option(None, "--na-values"),
    ragged: str = typer.Option("error", "--ragged"),
    rename_duplicates: bool = typer.Option(False, "--rename-duplicates"),
    no_examples: bool = typer.Option(False, "--no-examples"),
    trim: bool = typer.Option(False, "--trim"),
    trim_columns: str | None = typer.Option(None, "--trim-columns"),
    drop_empty_columns: str | None = typer.Option(None, "--drop-empty-columns"),
    parse_number: list[str] | None = typer.Option(None, "--parse-number"),
    parse_date: list[str] | None = typer.Option(None, "--parse-date"),
    drop_duplicates: bool = typer.Option(False, "--drop-duplicates"),
    subset: str | None = typer.Option(None, "--subset"),
    max_reject_share: float = typer.Option(0.10, "--max-reject-share"),
    columns: str | None = typer.Option(None, "--columns", help="describe, correlation: A,B."),
    by: str | None = typer.Option(None, "--by", help="group_aggregate: A,B; top_n: one column."),
    metrics: str | None = typer.Option(
        None, "--metrics", help="group_aggregate: col:sum,col:mean."
    ),
    metric: str | None = typer.Option(None, "--metric", help="top_n, time_trend: col:function."),
    sort: str | None = typer.Option(None, "--sort", help="group_aggregate: a metric to sort by."),
    n: int | None = typer.Option(None, "--n", help="top_n: how many."),
    order: str | None = typer.Option(None, "--order", help="top_n: desc or asc."),
    date_column: str | None = typer.Option(None, "--date-column", help="time_trend."),
    freq: str | None = typer.Option(None, "--freq", help="time_trend: day, month, quarter, year."),
    start: str | None = typer.Option(None, "--start", help="time_trend: YYYY-MM-DD."),
    end: str | None = typer.Option(None, "--end", help="time_trend: YYYY-MM-DD."),
    method: str | None = typer.Option(None, "--method", help="correlation: pearson or spearman."),
    column: str | None = typer.Option(None, "--column", help="value_counts."),
    top: int | None = typer.Option(None, "--top", help="value_counts: how many values."),
    normalize: bool | None = typer.Option(None, "--normalize/--no-normalize"),
) -> None:
    """Run one operation (describe, group_aggregate, top_n, time_trend, correlation, value_counts)."""
    options = _load_options(sheet, delimiter, encoding, na_values, ragged, rename_duplicates)
    plan = _plan(
        trim, trim_columns, drop_empty_columns, parse_number, parse_date, drop_duplicates, subset,
        max_reject_share,
    )  # fmt: skip
    arguments = {
        "columns": columns, "by": by, "metrics": metrics, "metric": metric, "sort": sort, "n": n,
        "order": order, "date_column": date_column, "freq": freq, "start": start, "end": end,
        "method": method, "column": column, "top": top, "normalize": normalize,
    }  # fmt: skip
    try:
        spec = build_spec(op, arguments)
    except DatasetError as exc:
        raise _fail(str(exc), 2) from exc
    _execute(file, options, out, plan=plan, specs=[spec], examples=not no_examples)


def calc_spec_cmd(
    file: Path = typer.Argument(..., help="CSV, XLSX or JSON file."),
    spec: Path = typer.Option(..., "--spec", help="JSON file with one or more operations."),
    out: Path = typer.Option(..., "--out", help="Output directory."),
    sheet: str | None = typer.Option(None, "--sheet"),
    delimiter: str = typer.Option(",", "--delimiter"),
    encoding: str = typer.Option("utf-8", "--encoding"),
    na_values: str | None = typer.Option(None, "--na-values"),
    ragged: str = typer.Option("error", "--ragged"),
    rename_duplicates: bool = typer.Option(False, "--rename-duplicates"),
    no_examples: bool = typer.Option(False, "--no-examples"),
    trim: bool = typer.Option(False, "--trim"),
    trim_columns: str | None = typer.Option(None, "--trim-columns"),
    drop_empty_columns: str | None = typer.Option(None, "--drop-empty-columns"),
    parse_number: list[str] | None = typer.Option(None, "--parse-number"),
    parse_date: list[str] | None = typer.Option(None, "--parse-date"),
    drop_duplicates: bool = typer.Option(False, "--drop-duplicates"),
    subset: str | None = typer.Option(None, "--subset"),
    max_reject_share: float = typer.Option(0.10, "--max-reject-share"),
) -> None:
    """Run operations given as JSON specs (what a planner would emit); specs are data, never code."""
    options = _load_options(sheet, delimiter, encoding, na_values, ragged, rename_duplicates)
    plan = _plan(
        trim, trim_columns, drop_empty_columns, parse_number, parse_date, drop_duplicates, subset,
        max_reject_share,
    )  # fmt: skip
    try:
        text = read_regular_file(spec, MAX_SPEC_BYTES).decode("utf-8")
        specs = parse_spec_document(text)
    except UnicodeDecodeError as exc:
        raise _fail("the spec file is not UTF-8 text", 2) from exc
    except DatasetError as exc:
        raise _fail(str(exc), 2) from exc
    _execute(file, options, out, plan=plan, specs=specs, examples=not no_examples)


def operations_cmd() -> None:
    """List the operations and their parameter schemas as JSON."""
    typer.echo(json.dumps(list_operations(), indent=2, sort_keys=True))


def register(app: typer.Typer) -> None:
    """Add the five commands to a Typer app (``sie.cli`` calls this; tests use a bare app)."""
    app.command("profile")(profile_cmd)
    app.command("clean")(clean_cmd)
    app.command("calc")(calc_cmd)
    app.command("calc-spec")(calc_spec_cmd)
    app.command("operations")(operations_cmd)
