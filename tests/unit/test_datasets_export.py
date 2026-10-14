"""Track E: outputs, spreadsheet safety by value type, manifest, verification."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from sie.datasets import (
    CleanPlan,
    IntegrityError,
    Limits,
    LoadOptions,
    clean,
    from_frame,
    open_dataset,
    profile,
    run_operation,
)
from sie.datasets.export import (
    build_manifest,
    neutralise_text,
    verify_directory,
    write_outputs,
)
from sie.datasets.models import DatasetError
from sie.datasets.provenance import sha256_bytes
from sie.datasets.store import publish, run_name

STAMP = "2026-10-14T00:00:00Z"
SALES = "region,amount\nnorth,10\nsouth,20\nnorth,5\n"


def make_dir(tmp_path: Path, name: str = "out") -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def write(tmp_path, ds, *, with_profile=True, with_clean=False, ops=(), name="out", stamp=STAMP):
    prof = profile(ds) if with_profile else None
    cleaned = clean(ds, CleanPlan(drop_duplicates={})) if with_clean else None
    target = cleaned.dataset if cleaned else ds
    results = [run_operation(target, spec, cleaning=cleaned) for spec in ops]
    directory = make_dir(tmp_path, name)
    outputs = write_outputs(directory, ds, Limits(), prof, cleaned, results, generated_at=stamp)
    return directory, outputs


def csv_text(directory: Path, name: str) -> str:
    return (directory / name).read_bytes().decode("utf-8")


# --- neutralise_text -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx", "-250", "+91 98765 43210"]
)
def test_formula_like_text_is_prefixed(raw):
    assert neutralise_text(raw) == ("'" + raw, True)


@pytest.mark.parametrize("raw", ["", "abc", "a=b", " =x", "1-2", "'=x"])
def test_ordinary_text_is_unchanged(raw):
    assert neutralise_text(raw) == (raw, False)


# --- CSV: numbers are never altered, text is neutralised --------------------------------------------


def test_numbers_stay_numbers_and_text_is_neutralised_value_by_value(tmp_path: Path):
    frame = pd.DataFrame(
        {
            "mixed": [-250, "-250", "=1+1", -12.5, "plain"],
            "=header": ["+cmd", "@SUM(A1)", "-2+3", "ok", "line1\nline2"],
        },
        dtype=object,
    )
    ds = from_frame(frame, "t")
    directory, outputs = write(tmp_path, ds, with_profile=False, with_clean=True)
    lines = csv_text(directory, "cleaned.csv")
    header, *rows = list(csv_rows(lines))
    assert header == ["row_id", "mixed", "'=header"]
    assert [r[1] for r in rows] == ["-250", "'-250", "'=1+1", "-12.5", "plain"]
    assert [r[2] for r in rows] == ["'+cmd", "'@SUM(A1)", "'-2+3", "ok", "line1\nline2"]
    # header + 4 text cells in mixed/header columns: '-250 '=1+1 and 3 formula-like in the second
    assert outputs["cleaned.csv"]["neutralised_cells"] == 1 + 2 + 3


def csv_rows(text: str):
    import csv

    return csv.reader(io.StringIO(text, newline=""))


def test_csv_encoding_line_endings_and_decimal_format(tmp_path: Path):
    ds = from_frame(
        pd.DataFrame({"x": [1.5, 3.0, None, 0.1 + 0.2], "t": ["é", "a,b", '"q"', None]}), "t"
    )
    directory, _ = write(tmp_path, ds, with_profile=False, with_clean=True)
    raw = (directory / "cleaned.csv").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert raw.decode("utf-8").split("\n")[:5] == [
        "row_id,x,t",
        "1,1.5,é",
        '2,3,"a,b"',
        '3,,"""q"""',
        "4,0.3,",
    ]


def test_dates_are_written_as_iso_and_date_only_when_every_value_is_midnight(tmp_path: Path):
    raw = open_dataset(
        b"d,t\n2026-01-02,2026-01-02T10:30:00\n2026-02-03,2026-02-03T00:00:00\n", "t.csv"
    )
    typed = from_frame(
        pd.DataFrame(
            {
                "d": pd.to_datetime(["2026-01-02", "2026-02-03"]),
                "t": pd.to_datetime(["2026-01-02T10:30:00", "2026-02-03T00:00:00"]),
            }
        ),
        "t",
    )
    directory, _ = write(tmp_path, typed, with_profile=False, with_clean=True)
    assert csv_text(directory, "cleaned.csv").split("\n")[1:3] == [
        "1,2026-01-02,2026-01-02T10:30:00",
        "2,2026-02-03,2026-02-03T00:00:00",
    ]
    assert raw.rows == 2


def test_csv_files_are_byte_identical_across_runs(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "sales.csv")
    ops = [
        {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum"]},
        {"op": "describe"},
    ]
    a, _ = write(tmp_path, ds, with_clean=True, ops=ops, name="a")
    b, _ = write(tmp_path, ds, with_clean=True, ops=ops, name="b", stamp="2030-01-01T00:00:00Z")
    for name in sorted(p.name for p in a.iterdir() if p.suffix == ".csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    assert (a / "profile.json").read_bytes() == (b / "profile.json").read_bytes()
    ma, mb = (
        json.loads((a / "manifest.json").read_text()),
        json.loads((b / "manifest.json").read_text()),
    )
    for key in ("generated_at", "tool_environment"):
        ma.pop(key), mb.pop(key)
    assert ma == mb  # identical apart from the two fields kept outside every hash


# --- XLSX ----------------------------------------------------------------------------------------


def workbook(directory: Path):
    return load_workbook(directory / "results.xlsx")


def test_xlsx_numbers_are_numeric_cells_and_text_is_a_quoted_string_never_a_formula(tmp_path: Path):
    frame = pd.DataFrame(
        {"m": [-250, -12.5, "=1+1", "+cmd", "@SUM(A1)", "-2+3", "-250"]}, dtype=object
    )
    directory, outputs = write(
        tmp_path, from_frame(frame, "t"), with_profile=False, with_clean=True
    )
    sheet = workbook(directory)["cleaned"]
    cells = [sheet.cell(row=r, column=2) for r in range(2, 9)]
    assert (cells[0].value, cells[0].data_type) == (-250, "n")
    assert (cells[1].value, cells[1].data_type) == (-12.5, "n")
    for cell, text in zip(cells[2:], ["=1+1", "+cmd", "@SUM(A1)", "-2+3", "-250"], strict=True):
        assert cell.value == text and cell.data_type == "s" and cell.quotePrefix is True
    assert outputs["results.xlsx"]["neutralised_cells"] == 5
    # a numeric column still sorts and sums as numbers after a round trip
    nums = sorted(c.value for c in cells[:2])
    assert nums == [-250, -12.5]


def test_xlsx_header_cell_beginning_with_equals_is_neutralised(tmp_path: Path):
    ds = from_frame(pd.DataFrame({"=evil": [1]}), "t")
    directory, _ = write(tmp_path, ds, with_profile=False, with_clean=True)
    header = workbook(directory)["cleaned"]["B1"]
    assert header.data_type == "s" and header.quotePrefix is True and header.value == "=evil"


def test_our_own_loader_sees_no_formula_cells_in_the_workbook(tmp_path: Path):
    frame = pd.DataFrame({"m": ["=1+1", "-2+3", "ok"]})
    directory, _ = write(tmp_path, from_frame(frame, "t"), with_profile=False, with_clean=True)
    loaded = open_dataset(
        (directory / "results.xlsx").read_bytes(), "r.xlsx", LoadOptions(sheet="cleaned")
    )
    assert [n for n in loaded.notes if n["code"] == "formula_cells"][0]["count"] == 0
    assert loaded.frame["m"].tolist() == ["=1+1", "-2+3", "ok"]


def test_operation_results_round_trip_through_xlsx(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "sales.csv")
    spec = {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum", "amount:mean"]}
    directory, _ = write(tmp_path, ds, ops=[spec])
    loaded = open_dataset(
        (directory / "results.xlsx").read_bytes(),
        "r.xlsx",
        LoadOptions(sheet="result_01_group_aggregate"),
    )
    expected = run_operation(ds, spec).table
    assert loaded.columns == list(expected.columns)
    for got, want in zip(loaded.frame.values.tolist(), expected.values.tolist(), strict=True):
        assert got == want


def test_xlsx_edge_values_are_kept_exact_or_explained(tmp_path: Path):
    frame = pd.DataFrame(
        {
            "big": [2**60, 5],
            "tz": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]),
            "t": ["a\x01b", "ok"],
        },
        dtype=object,
    )
    frame["tz"] = pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"])
    directory, outputs = write(
        tmp_path, from_frame(frame, "t"), with_profile=False, with_clean=True
    )
    info = outputs["results.xlsx"]
    assert info["large_integers_as_text"] == 1 and info["illegal_characters_replaced"] == 1
    assert info["timezone_values_as_text"] == 2
    sheet = workbook(directory)["cleaned"]
    assert sheet["B2"].value == str(2**60) and sheet["B3"].value == 5
    assert sheet["D2"].value == "a�b"


def test_sheet_names_are_valid_and_unique(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "sales.csv")
    directory, _ = write(
        tmp_path, ds, with_clean=True, ops=[{"op": "describe"}, {"op": "describe"}]
    )
    names = workbook(directory).sheetnames
    assert names == [
        "profile",
        "cleaned",
        "rejects",
        "duplicates",
        "result_01_describe",
        "result_02_describe",
    ]
    assert all(len(n) <= 31 for n in names) and len({n.lower() for n in names}) == len(names)


def test_workbook_metadata_is_fixed(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "sales.csv")
    a, _ = write(tmp_path, ds, name="a")
    b, _ = write(tmp_path, ds, name="b")
    props = workbook(a).properties
    assert (
        props.creator == "sie.datasets"
        and props.created.year == 2000
        and props.modified.year == 2000
    )
    assert (a / "results.xlsx").read_bytes() == (
        b / "results.xlsx"
    ).read_bytes()  # a convenience, not a promise


# --- manifest ------------------------------------------------------------------------------------


def test_manifest_contents(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "../weird name.csv", retrieved_at="2026-10-01T00:00:00Z")
    spec = {"op": "group_aggregate", "by": ["region"], "metrics": ["amount:sum"]}
    directory, outputs = write(tmp_path, ds, with_clean=True, ops=[spec])
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["schema"] == "sie.datasets.manifest" and manifest["serialisation_version"] == 1
    assert manifest["input"]["sha256"] == ds.source.sha256 == sha256_bytes(SALES.encode())
    assert (
        manifest["input"]["name"] == "../weird name.csv"
        and manifest["input"]["sanitised_name"] == "weird_name.csv"
    )
    assert manifest["limits"]["max_rows"] == 1_000_000
    assert manifest["options"]["delimiter"] == ","
    assert manifest["profile"]["rows"] == 3 and manifest["profile"]["column_types"] == {
        "region": "text",
        "amount": "integer",
    }
    assert manifest["cleaning"]["rows_in"] == 3
    op = manifest["operations"][0]
    assert op["provenance"]["operation"] == "group_aggregate" and op["provenance"]["rows_used"] == 3
    assert manifest["result_hashes"] == [
        {"operation": "group_aggregate", "result_sha256": op["provenance"]["result_sha256"]}
    ]
    assert (
        manifest["completeness"]["rows_loaded"] == 3
        and manifest["completeness"]["rows_rejected_by_cleaning"] == 0
    )
    assert manifest["generated_at"] == STAMP and "pandas" in manifest["tool_environment"]
    assert any("Correlation" in text for text in manifest["limitations"])
    assert set(manifest["outputs"]) == set(outputs) - {"manifest.json"}
    for name, info in manifest["outputs"].items():
        assert (
            info["sha256"]
            == outputs[name]["sha256"]
            == sha256_bytes((directory / name).read_bytes())
        )


def test_build_manifest_is_pure_and_reports_neutralised_cells_per_file():
    ds = open_dataset(SALES.encode(), "s.csv")
    outputs = {
        "a.csv": {"sha256": "x", "size_bytes": 1, "neutralised_cells": 2},
        "b.csv": {"sha256": "y", "size_bytes": 1, "neutralised_cells": 0},
    }
    one = build_manifest(ds, Limits(), None, None, [], generated_at=STAMP, outputs=outputs)
    two = build_manifest(ds, Limits(), None, None, [], generated_at=STAMP, outputs=outputs)
    assert one == two
    assert (
        one["completeness"]["neutralised_cells"] == {"a.csv": 2}
        and one["completeness"]["neutralised_cells_total"] == 2
    )
    assert one["profile"] is None and one["cleaning"] is None


def test_loader_notes_and_completeness_reach_the_manifest(tmp_path: Path):
    ds = open_dataset(
        b"a,b\n1,2\n3\n",
        "t.csv",
        options=LoadOptions(ragged="reject"),
    )
    directory, _ = write(tmp_path, ds)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["completeness"]["rows_rejected_on_load"] == 1
    assert (directory / "load_rejects.csv").is_file()
    assert any(
        n["code"] == "ragged_rows_rejected" for n in manifest["completeness"]["loader_notes"]
    )


# --- write and verify ------------------------------------------------------------------------------


def test_write_outputs_needs_an_empty_existing_directory(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "s.csv")
    with pytest.raises(DatasetError):
        write_outputs(tmp_path / "missing", ds, Limits(), None, None, [], generated_at=STAMP)
    full = make_dir(tmp_path)
    (full / "x").write_text("x")
    with pytest.raises(DatasetError):
        write_outputs(full, ds, Limits(), None, None, [], generated_at=STAMP)


def test_verify_directory_detects_changes_extras_and_missing_files(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "s.csv")
    directory, _ = write(tmp_path, ds, ops=[{"op": "describe"}])
    assert verify_directory(directory)["schema"] == "sie.datasets.manifest"
    (directory / "result_01_describe.csv").write_text("tampered")
    with pytest.raises(IntegrityError, match="does not match"):
        verify_directory(directory)
    other, _ = write(tmp_path, ds, name="o2")
    (other / "extra.txt").write_text("x")
    with pytest.raises(IntegrityError, match="unexpected"):
        verify_directory(other)
    third, _ = write(tmp_path, ds, name="o3")
    (third / "results.xlsx").unlink()
    with pytest.raises(IntegrityError, match="missing"):
        verify_directory(third)
    with pytest.raises(IntegrityError, match="no manifest"):
        verify_directory(make_dir(tmp_path, "empty"))


def test_publish_with_write_outputs_is_atomic_and_idempotent(tmp_path: Path):
    ds = open_dataset(SALES.encode(), "s.csv")
    results = [run_operation(ds, {"op": "describe"})]
    name = run_name(ds.source.sha256, sha256_bytes(b"params"))

    def writer(temp: Path) -> None:
        write_outputs(temp, ds, Limits(), None, None, results, generated_at=STAMP)

    final, present = publish(tmp_path / "out", name, writer)
    assert not present and verify_directory(final)
    again, present = publish(tmp_path / "out", name, writer)
    assert present and again == final
    assert [p.name for p in (tmp_path / "out").iterdir()] == [name]

    def failing(temp: Path) -> None:
        write_outputs(temp, ds, Limits(), None, None, results, generated_at=STAMP)
        raise RuntimeError("late failure")

    with pytest.raises(RuntimeError):
        publish(tmp_path / "out2", name, failing)
    assert list((tmp_path / "out2").iterdir()) == []
