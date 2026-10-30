"""Track E: write result tables and the manifest into a directory. See design sections 9 and 11.

Safety is decided by the value's type, never by its text: numbers are written as numbers and are never
altered; text that a spreadsheet could read as a formula is neutralised (CSV: a leading ``'``; XLSX: an
explicit string cell with the quote-prefix style). Neutralising is presentation only and is outside every
hash. CSV files and ``manifest.json`` are byte-reproducible; the XLSX is logically reproducible only.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from sie.datasets.models import (
    SERIALISATION_VERSION,
    STANDARD_LIMITATIONS,
    CleanResult,
    Dataset,
    DatasetError,
    IntegrityError,
    Limits,
    OperationResult,
    ProfileResult,
)
from sie.datasets.provenance import canonical_json, format_float, sha256_bytes, tool_environment
from sie.datasets.store import sanitise_filename

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "sie.datasets.manifest"
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
XLSX_MAX_ROWS = 1_048_576  # including the header row
XLSX_MAX_COLUMNS = 16_384
EXACT_XLSX_INTEGER = 2**53  # larger integers cannot be stored exactly in a spreadsheet number
_FIXED_STAMP = "2000-01-01T00:00:00Z"
_FIXED_ZIP_DATE = (2000, 1, 1, 0, 0, 0)
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")
XLSX_LIMITATION = "The XLSX workbook is logically but not byte-for-byte reproducible; CSV files and manifest.json are."


def neutralise_text(value: str) -> tuple[str, bool]:
    """CSV text-cell protection: a leading ``=``, ``+``, ``-``, ``@``, tab or CR gets a ``'`` prefix.
    Returns (value, changed). Only for text values; numbers are never passed here."""
    if value and value[0] in FORMULA_STARTS:
        return "'" + value, True
    return value, False


# --- cell formatting -----------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """numpy and pandas scalars as plain Python; missing values as ``None``."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value
    if hasattr(value, "item") and type(value).__module__.split(".")[0] == "numpy":
        value = value.item()
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and value != value:
        return None
    return value


def _midnight_columns(frame: pd.DataFrame) -> set[str]:
    out: set[str] = set()
    for name in frame.columns:
        values = [_plain(v) for v in frame[name].tolist()]
        stamps = [v for v in values if isinstance(v, pd.Timestamp)]
        if stamps and all(
            s.hour == s.minute == s.second == s.microsecond == s.nanosecond == 0 for s in stamps
        ):
            out.add(str(name))
    return out


def _csv_cell(value: Any, date_only: bool) -> tuple[str, bool]:
    """One CSV cell and whether it was neutralised."""
    value = _plain(value)
    if value is None:
        return "", False
    if isinstance(value, bool):
        return ("true" if value else "false"), False
    if isinstance(value, int):
        return str(value), False
    if isinstance(value, float):
        text = format_float(value)
        return (text if text is not None else ""), False
    if isinstance(value, pd.Timestamp | datetime):
        return (value.date().isoformat() if date_only else value.isoformat()), False
    if isinstance(value, date):
        return value.isoformat(), False
    return neutralise_text(str(value))


def _csv_bytes(frame: pd.DataFrame, *, with_index: bool) -> tuple[bytes, int]:
    """UTF-8 (no BOM), ``\\n`` line ends, fixed formats. Returns the bytes and the neutralised count."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    neutralised = 0
    header: list[str] = []
    for name in ([str(frame.index.name or "row_id")] if with_index else []) + [
        str(c) for c in frame.columns
    ]:
        text, changed = neutralise_text(name)
        header.append(text)
        neutralised += changed
    writer.writerow(header)
    midnight = _midnight_columns(frame)
    names = [str(c) for c in frame.columns]
    columns = [frame[c].tolist() for c in frame.columns]
    ids = frame.index.tolist() if with_index else []
    for position in range(len(frame)):
        row: list[str] = [str(int(ids[position]))] if with_index else []
        for name, values in zip(names, columns, strict=True):
            text, changed = _csv_cell(values[position], name in midnight)
            row.append(text)
            neutralised += changed
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8"), neutralised


def table_csv_bytes(frame: pd.DataFrame) -> tuple[bytes, int]:
    """Public CSV form of a result table (no row index): bytes and the neutralised-cell count."""
    return _csv_bytes(frame, with_index=False)


# --- XLSX ----------------------------------------------------------------------------------------


def _sheet_name(raw: str, used: set[str]) -> str:
    base = _SHEET_BAD.sub("_", raw).strip("'") or "sheet"
    base = base[:31]
    name, k = base, 2
    while name.lower() in used:
        suffix = f"_{k}"
        name = base[: 31 - len(suffix)] + suffix
        k += 1
    used.add(name.lower())
    return name


def _xlsx_cell(ws: Any, value: Any, date_only: bool, counts: dict[str, int]) -> Any:
    from openpyxl.cell import WriteOnlyCell

    value = _plain(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > EXACT_XLSX_INTEGER:
            counts["large_integers_as_text"] += 1
            return str(value)
        return value
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    if isinstance(value, pd.Timestamp | datetime):
        if getattr(value, "tzinfo", None) is not None:
            counts["timezone_values_as_text"] += 1
            return value.isoformat()
        native = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
        return native.date() if date_only else native
    if isinstance(value, date):
        return value
    text = str(value)
    cleaned = _ILLEGAL_XML.sub("�", text)
    if cleaned != text:
        counts["illegal_characters_replaced"] += 1
        text = cleaned
    if text and text[0] in FORMULA_STARTS:
        cell = WriteOnlyCell(ws, value=text)
        cell.data_type = "s"  # an explicit string, never a formula
        cell.quotePrefix = True
        counts["neutralised"] += 1
        return cell
    return text


def _normalise_xlsx(data: bytes) -> bytes:
    """Fixed member order, fixed ZIP dates, fixed creator and timestamps. A convenience, not a promise."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            payload = source.read(name)
            if name == "docProps/core.xml":
                text = payload.decode("utf-8")
                text = re.sub(
                    r"(<dcterms:(?:created|modified)[^>]*>)[^<]*", rf"\g<1>{_FIXED_STAMP}", text
                )
                text = re.sub(r"(<dc:creator[^>]*>)[^<]*", r"\g<1>sie.datasets", text)
                payload = text.encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, payload)
    return out.getvalue()


def _xlsx_bytes(sheets: list[tuple[str, pd.DataFrame, bool]]) -> tuple[bytes, dict[str, Any]]:
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    counts = {
        "neutralised": 0,
        "large_integers_as_text": 0,
        "timezone_values_as_text": 0,
        "illegal_characters_replaced": 0,
    }
    skipped: list[dict[str, Any]] = []
    used: set[str] = set()
    for raw_name, frame, with_index in sheets:
        if (
            len(frame) + 1 > XLSX_MAX_ROWS
            or len(frame.columns) + int(with_index) > XLSX_MAX_COLUMNS
        ):
            skipped.append(
                {"sheet": raw_name, "rows": len(frame), "reason": "exceeds the XLSX sheet size"}
            )
            continue
        ws = book.create_sheet(_sheet_name(raw_name, used))
        header = ([str(frame.index.name or "row_id")] if with_index else []) + [
            str(c) for c in frame.columns
        ]
        ws.append([_xlsx_cell(ws, h, False, counts) for h in header])
        midnight = _midnight_columns(frame)
        names = [str(c) for c in frame.columns]
        columns = [frame[c].tolist() for c in frame.columns]
        ids = frame.index.tolist() if with_index else []
        for position in range(len(frame)):
            row = [int(ids[position])] if with_index else []
            row.extend(
                _xlsx_cell(ws, values[position], name in midnight, counts)
                for name, values in zip(names, columns, strict=True)
            )
            ws.append(row)
    if not book.sheetnames:  # a workbook needs at least one sheet
        book.create_sheet("empty")
    buffer = io.BytesIO()
    book.save(buffer)
    return _normalise_xlsx(buffer.getvalue()), {**counts, "sheets_skipped": skipped}


# --- manifest ------------------------------------------------------------------------------------


def _profile_summary(profile: ProfileResult) -> dict[str, Any]:
    return {
        "rows": profile.rows,
        "columns": profile.columns,
        "duplicate_rows": profile.duplicate_rows,
        "empty_columns": profile.empty_columns,
        "constant_columns": profile.constant_columns,
        "column_types": {c.name: c.type for c in profile.column_profiles},
        "nulls": {c.name: c.nulls for c in profile.column_profiles},
    }


def build_manifest(
    ds: Dataset,
    limits: Limits,
    profile: ProfileResult | None,
    cleaning: CleanResult | None,
    results: list[OperationResult],
    *,
    generated_at: str,
    outputs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The manifest dict (design section 11). Pure; ``generated_at`` is supplied by the caller."""
    outputs = outputs or {}
    notes = [dict(n) for n in ds.notes]
    neutralised = {
        name: info["neutralised_cells"]
        for name, info in sorted(outputs.items())
        if info.get("neutralised_cells")
    }
    completeness: dict[str, Any] = {
        "rows_loaded": ds.rows,
        "rows_rejected_on_load": 0 if ds.rejects is None else len(ds.rejects),
        "loader_notes": notes,
        "nulls": None if profile is None else {c.name: c.nulls for c in profile.column_profiles},
        "duplicate_rows": None if profile is None else profile.duplicate_rows,
        "formula_without_cached_value": sum(
            n["count"] for n in notes if n["code"] == "formula_without_cached_value"
        ),
        "neutralised_cells": neutralised,
        "neutralised_cells_total": sum(neutralised.values()),
    }
    if cleaning is not None:
        completeness.update(
            rows_cleaned=cleaning.dataset.rows,
            rows_rejected_by_cleaning=len(cleaning.rejects),
            rows_duplicate_removed=len(cleaning.duplicates),
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "schema_version": 1,
        "serialisation_version": SERIALISATION_VERSION,
        "input": {
            **ds.source.model_dump(mode="json"),
            "sanitised_name": sanitise_filename(ds.source.name),
        },
        "limits": limits.to_dict(),
        "options": ds.options.model_dump(mode="json"),
        "profile": None if profile is None else _profile_summary(profile),
        "cleaning": None if cleaning is None else cleaning.to_dict(),
        "operations": [
            {"provenance": r.provenance.to_dict(), "warnings": list(r.warnings), "extra": r.extra}
            for r in results
        ],
        "result_hashes": [
            {"operation": r.provenance.operation, "result_sha256": r.provenance.result_sha256}
            for r in results
        ],
        "completeness": completeness,
        "outputs": {name: dict(info) for name, info in sorted(outputs.items())},
        "limitations": [*STANDARD_LIMITATIONS, XLSX_LIMITATION],
        "tool_environment": tool_environment(),  # outside every hash
        "generated_at": generated_at,  # outside every hash
    }


# --- writing and verifying -----------------------------------------------------------------------


def _write_new(directory: Path, name: str, data: bytes) -> dict[str, Any]:
    target = directory / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {"sha256": sha256_bytes(data), "size_bytes": len(data)}


def _profile_table(profile: ProfileResult) -> pd.DataFrame:
    return pd.DataFrame(
        [
            [
                c.name,
                c.type,
                c.non_null,
                c.nulls,
                c.null_share,
                c.distinct,
                c.min,
                c.max,
                "; ".join(c.hints),
            ]  # fmt: skip
            for c in profile.column_profiles
        ],
        columns=[
            "column",
            "type",
            "non_null",
            "nulls",
            "null_share",
            "distinct",
            "min",
            "max",
            "hints",
        ],
        dtype=object,
    )


def write_outputs(
    directory: Path,
    ds: Dataset,
    limits: Limits,
    profile: ProfileResult | None,
    cleaning: CleanResult | None,
    results: list[OperationResult],
    *,
    generated_at: str,
) -> dict[str, dict[str, Any]]:
    """Write CSV tables, one XLSX workbook and ``manifest.json`` into the existing empty ``directory``.
    Returns {file name: {"sha256": ..., "size_bytes": ...}}. ``directory`` is the caller's temp
    directory; publishing atomically is ``store.publish``."""
    if not directory.is_dir() or any(directory.iterdir()):
        raise DatasetError("the output directory must exist and be empty")
    outputs: dict[str, dict[str, Any]] = {}
    sheets: list[tuple[str, pd.DataFrame, bool]] = []

    def add_csv(name: str, frame: pd.DataFrame, *, with_index: bool = False) -> None:
        data, neutralised = _csv_bytes(frame, with_index=with_index)
        info = _write_new(directory, name, data)
        info.update(rows=len(frame), neutralised_cells=neutralised)
        outputs[name] = info

    if profile is not None:
        info = _write_new(
            directory, "profile.json", canonical_json(profile.to_dict()).encode("utf-8")
        )
        outputs["profile.json"] = info
        table = _profile_table(profile)
        add_csv("profile_columns.csv", table)
        sheets.append(("profile", table, False))
    if cleaning is not None:
        add_csv("cleaned.csv", cleaning.dataset.frame, with_index=True)
        add_csv("rejects.csv", cleaning.rejects)
        add_csv("duplicates.csv", cleaning.duplicates)
        sheets += [
            ("cleaned", cleaning.dataset.frame, True),
            ("rejects", cleaning.rejects, False),
            ("duplicates", cleaning.duplicates, False),
        ]
    elif ds.rejects is not None and len(ds.rejects):
        add_csv("load_rejects.csv", ds.rejects)
        sheets.append(("load_rejects", ds.rejects, False))
    for number, result in enumerate(results, start=1):
        label = f"result_{number:02d}_{result.provenance.operation}"
        add_csv(f"{label}.csv", result.table)
        sheets.append((label, result.table, False))
    workbook, xlsx_counts = _xlsx_bytes(sheets)
    info = _write_new(directory, "results.xlsx", workbook)
    info.update(
        neutralised_cells=xlsx_counts["neutralised"],
        large_integers_as_text=xlsx_counts["large_integers_as_text"],
        timezone_values_as_text=xlsx_counts["timezone_values_as_text"],
        illegal_characters_replaced=xlsx_counts["illegal_characters_replaced"],
        sheets_skipped=xlsx_counts["sheets_skipped"],
    )
    outputs["results.xlsx"] = info
    manifest = build_manifest(
        ds, limits, profile, cleaning, results, generated_at=generated_at, outputs=outputs
    )
    outputs[MANIFEST_NAME] = _write_new(
        directory, MANIFEST_NAME, (canonical_json(manifest) + "\n").encode("utf-8")
    )
    verify_directory(directory)
    return outputs


def verify_directory(directory: Path) -> dict[str, Any]:
    """Check every file listed in ``manifest.json`` for size and hash, and that nothing else is there.
    Returns the manifest. Raises ``IntegrityError`` on any difference."""
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise IntegrityError(f"{directory} has no {MANIFEST_NAME}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        listed = manifest["outputs"]
    except (ValueError, KeyError) as exc:
        raise IntegrityError(f"{manifest_path} is not a valid manifest") from exc
    present = {p.name for p in directory.iterdir()}
    expected = set(listed) | {MANIFEST_NAME}
    if present != expected:
        raise IntegrityError(
            f"files differ from the manifest: unexpected {sorted(present - expected)}, "
            f"missing {sorted(expected - present)}"
        )
    for name, info in listed.items():
        data = (directory / name).read_bytes()
        if len(data) != info["size_bytes"] or sha256_bytes(data) != info["sha256"]:
            raise IntegrityError(f"{name} does not match the manifest")
    return manifest
