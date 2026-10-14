"""The dataset commands: exit codes, output location and idempotence. The commands hold no logic of
their own (see test_datasets_run.py); these tests check the adapter."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from sie.datasets_cli import register

app = typer.Typer()
register(app)
runner = CliRunner()

SALES = b"region,amount\nnorth,10\nsouth,20\nnorth,5\n"
EU = b'region,amount\nnorth,"1.234,5"\nsouth,"10,5"\n'


def invoke(tmp_path: Path, *args: str):
    return runner.invoke(app, list(args), env={"DATA_DIR": str(tmp_path / "data")})


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_operations_lists_the_six_operations_as_json(tmp_path: Path):
    result = invoke(tmp_path, "operations")
    assert result.exit_code == 0
    assert sorted(json.loads(result.output)) == [
        "correlation", "describe", "group_aggregate", "time_trend", "top_n", "value_counts",
    ]  # fmt: skip


def test_profile_writes_only_under_out_and_the_uploads_directory(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    before = tree(tmp_path)
    result = invoke(tmp_path, "profile", str(source), "--out", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    directory = Path(summary["directory"])
    assert directory.parent == tmp_path / "out" and (directory / "manifest.json").is_file()
    assert Path(summary["raw_copy"]).read_bytes() == SALES
    created = tree(tmp_path) - before
    assert all(p.startswith(("out", "data/uploads")) for p in created)
    assert source.read_bytes() == SALES  # the original is never changed


def test_the_same_command_twice_is_idempotent(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    args = ("profile", str(source), "--out", str(tmp_path / "out"))
    first = json.loads(invoke(tmp_path, *args).output)
    second = json.loads(invoke(tmp_path, *args).output)
    assert first["already_present"] is False and second["already_present"] is True
    assert first["directory"] == second["directory"]


def test_calc_group_aggregate_with_cleaning(tmp_path: Path):
    source = write(tmp_path, "eu.csv", EU)
    result = invoke(
        tmp_path, "calc", str(source), "--out", str(tmp_path / "out"), "--parse-number", "amount:,:.",
        "--op", "group_aggregate", "--by", "region", "--metrics", "amount:sum",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    directory = Path(json.loads(result.output)["directory"])
    assert (directory / "result_01_group_aggregate.csv").read_text().split("\n")[:3] == [
        "region,rows,amount:sum",
        "north,1,1234.5",
        "south,1,10.5",
    ]


def test_calc_spec_runs_json_operations(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    spec = write(
        tmp_path, "spec.json", b'[{"op": "describe"}, {"op": "value_counts", "column": "region"}]'
    )
    result = invoke(
        tmp_path, "calc-spec", str(source), "--spec", str(spec), "--out", str(tmp_path / "out")
    )
    assert result.exit_code == 0, result.output
    names = {p.name for p in Path(json.loads(result.output)["directory"]).iterdir()}
    assert {"result_01_describe.csv", "result_02_value_counts.csv"} <= names


def test_clean_needs_a_cleaning_option_and_reports_rejects(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    out = str(tmp_path / "out")
    assert invoke(tmp_path, "clean", str(source), "--out", out).exit_code == 2
    result = invoke(tmp_path, "clean", str(source), "--out", out, "--trim", "--drop-duplicates")
    assert result.exit_code == 0, result.output


def test_invalid_requests_exit_2_and_publish_nothing(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    out = tmp_path / "out"
    cases = [
        ("calc", str(source), "--out", str(out), "--op", "exec"),
        ("calc", str(source), "--out", str(out), "--op", "describe", "--by", "region"),
        (
            "calc",
            str(source),
            "--out",
            str(out),
            "--op",
            "group_aggregate",
            "--by",
            "nope",
            "--metrics",
            "amount:sum",
        ),
        (
            "calc",
            str(source),
            "--out",
            str(out),
            "--op",
            "group_aggregate",
            "--by",
            "region",
            "--metrics",
            "region:sum",
        ),
        ("profile", str(source), "--delimiter", ";;", "--out", str(out)),
        ("clean", str(source), "--out", str(out), "--parse-number", "amount"),
    ]
    for args in cases:
        result = invoke(tmp_path, *args)
        assert result.exit_code == 2, (args, result.output)
    assert not out.exists() or list(out.iterdir()) == []


def test_bad_spec_files_exit_2(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    for content in (b"not json", b'{"op": "exec"}', b"[]", b"\xff\xfe"):
        spec = write(tmp_path, "spec.json", content)
        result = invoke(
            tmp_path, "calc-spec", str(source), "--spec", str(spec), "--out", str(tmp_path / "out")
        )
        assert result.exit_code == 2, content


def test_unusable_files_exit_1(tmp_path: Path):
    out = str(tmp_path / "out")
    ragged = write(tmp_path, "ragged.csv", b"a,b\n1,2\n3\n")
    assert invoke(tmp_path, "profile", str(ragged), "--out", out).exit_code == 1
    assert invoke(tmp_path, "profile", str(tmp_path / "missing.csv"), "--out", out).exit_code == 1
    assert (
        invoke(tmp_path, "profile", str(write(tmp_path, "x.txt", b"a")), "--out", out).exit_code
        == 1
    )
    real = write(tmp_path, "real.csv", SALES)
    link = tmp_path / "link.csv"
    link.symlink_to(real)
    refused = invoke(tmp_path, "profile", str(link), "--out", out)
    assert refused.exit_code == 1 and "symbolic link" in refused.output


def test_too_many_rejects_exit_1_and_publish_nothing(tmp_path: Path):
    source = write(tmp_path, "sales.csv", SALES)
    out = tmp_path / "out"
    result = invoke(
        tmp_path, "clean", str(source), "--out", str(out), "--parse-number", "region:.:"
    )
    assert result.exit_code == 1
    assert not out.exists() or list(out.iterdir()) == []


def test_ragged_reject_option_keeps_the_good_rows(tmp_path: Path):
    source = write(tmp_path, "ragged.csv", b"a,b\n1,2\n3\n4,5\n")
    result = invoke(
        tmp_path, "profile", str(source), "--ragged", "reject", "--out", str(tmp_path / "out")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["completeness"]["rows_rejected_on_load"] == 1


def test_no_examples_keeps_values_out_of_the_profile(tmp_path: Path):
    source = write(tmp_path, "secret.csv", b"name\nvery-private-value\n")
    result = invoke(
        tmp_path, "profile", str(source), "--no-examples", "--out", str(tmp_path / "out")
    )
    directory = Path(json.loads(result.output)["directory"])
    assert "very-private-value" not in (directory / "profile.json").read_text()
    assert "very-private-value" not in (directory / "profile_columns.csv").read_text()
    assert "very-private-value" not in result.output
