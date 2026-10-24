"""The orchestration and the command-line shorthand: one request, atomic, idempotent, auditable."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sie.datasets import (
    IntegrityError,
    LimitExceeded,
    Limits,
    LoadOptions,
    OperationError,
    ParseError,
    RejectedTooMany,
)
from sie.datasets.models import DatasetError
from sie.datasets.provenance import sha256_bytes
from sie.datasets.run import exit_code_for, parameters_hash, run_request
from sie.datasets.shorthand import (
    build_plan,
    build_spec,
    parse_date_spec,
    parse_number_spec,
    parse_spec_document,
    split_list,
)

STAMP = "2026-10-14T00:00:00Z"
CSV = b'region,amount,day\nnorth,"1.234,5",03/04/2026\nsouth,"10,5",04/04/2026\nnorth,"10,5",04/04/2026\n'
SPEC = build_spec("group_aggregate", {"by": "region", "metrics": "amount:sum"})


def run(tmp_path: Path, data: bytes = CSV, name: str = "s.csv", **kwargs):
    return run_request(
        data,
        name,
        uploads_root=tmp_path / "uploads",
        out_root=tmp_path / "out",
        generated_at=STAMP,
        **kwargs,
    )


def clean_plan():
    return build_plan(
        parse_number=["amount:,:."], parse_date=["day:%d/%m/%Y"], drop_duplicates=True
    )


# --- shorthand -----------------------------------------------------------------------------------


def test_split_list():
    assert split_list(None) is None and split_list("a, b") == ["a", "b"]
    for bad in ("", "a,,b", " "):
        with pytest.raises(OperationError):
            split_list(bad)


def test_number_and_date_specs():
    fmt = parse_number_spec("amount:,:.")
    assert (fmt.column, fmt.decimal, fmt.thousands) == ("amount", ",", ".")
    assert (
        parse_number_spec("a:b:.:'").thousands == "'"
        and parse_number_spec("a:b:.:'").column == "a:b"
    )
    assert parse_number_spec("x:.:").thousands == ""
    for bad in ("amount", "amount:,", ":,:.", "x:,:,", "x:;:."):
        with pytest.raises(OperationError):
            parse_number_spec(bad)
    date = parse_date_spec("day:%H:%M")
    assert (date.column, date.format) == ("day", "%H:%M")
    for bad in ("day", ":%Y", "day:"):
        with pytest.raises(OperationError):
            parse_date_spec(bad)


def test_build_plan():
    assert build_plan() is None
    plan = build_plan(
        trim=True,
        drop_empty_columns=["x"],
        drop_duplicates=True,
        subset=["a"],
        max_reject_share=0.5,
    )
    assert plan.trim == "all" and plan.drop_empty_columns == ["x"]
    assert plan.drop_duplicates.subset == ["a"] and plan.max_reject_share == 0.5
    assert build_plan(trim_columns=["a"]).trim == ["a"]
    assert build_plan(drop_duplicates=True).drop_duplicates.subset is None
    for bad in (
        {"subset": ["a"]},
        {"trim": True, "trim_columns": ["a"]},
        {"trim": True, "max_reject_share": 2},
    ):
        with pytest.raises(OperationError):
            build_plan(**bad)


def test_build_spec_accepts_only_the_arguments_an_operation_takes():
    spec = build_spec("top_n", {"by": "region", "metric": "amount:sum", "n": 3, "order": None})
    assert (spec.by, spec.n, spec.order) == ("region", 3, "desc")
    assert build_spec("correlation", {"columns": "a,b", "method": "spearman"}).columns == ["a", "b"]
    with pytest.raises(OperationError, match="does not take"):
        build_spec("describe", {"by": "region"})
    with pytest.raises(OperationError, match="exactly one"):
        build_spec("top_n", {"by": "a,b", "metric": "x:sum"})
    with pytest.raises(OperationError, match="unknown operation"):
        build_spec("exec", {})
    with pytest.raises(OperationError):
        build_spec("group_aggregate", {"by": "r"})  # metrics missing


def test_spec_documents():
    one = '{"op": "describe"}'
    many = '[{"op": "describe"}, {"op": "value_counts", "column": "a"}]'
    wrapped = '{"operations": [{"op": "describe"}]}'
    assert [s.op for s in parse_spec_document(one)] == ["describe"]
    assert [s.op for s in parse_spec_document(many)] == ["describe", "value_counts"]
    assert len(parse_spec_document(wrapped)) == 1
    for bad in ("not json", "[]", "5", "[1]", '[{"op": "nope"}]'):
        with pytest.raises(OperationError):
            parse_spec_document(bad)


def test_exit_codes():
    assert exit_code_for(OperationError("x")) == 2
    for error in (
        ParseError("x"),
        LimitExceeded("l", "x"),
        RejectedTooMany("x"),
        IntegrityError("x"),
        DatasetError("x"),
    ):
        assert exit_code_for(error) == 1


# --- run_request ---------------------------------------------------------------------------------


def test_a_full_run_publishes_one_verified_directory_and_keeps_the_raw_file(tmp_path: Path):
    outcome = run(tmp_path, plan=clean_plan(), specs=[SPEC])
    assert not outcome.already_present
    assert outcome.input_sha256 == sha256_bytes(CSV)
    assert outcome.directory.parent == tmp_path / "out"
    assert outcome.directory.name.startswith(sha256_bytes(CSV)[:12] + "-")
    assert outcome.raw_copy.read_bytes() == CSV and outcome.raw_copy.parent.name == sha256_bytes(
        CSV
    )
    names = sorted(p.name for p in outcome.directory.iterdir())
    assert names == [
        "cleaned.csv", "duplicates.csv", "manifest.json", "profile.json", "profile_columns.csv",
        "rejects.csv", "result_01_group_aggregate.csv", "results.xlsx",
    ]  # fmt: skip
    # north: 1.234,5 + 10,5 = 1245 (formatted with 15 significant digits); south: 10,5
    result = (outcome.directory / "result_01_group_aggregate.csv").read_text().split("\n")
    assert result[:3] == ["region,rows,amount:sum", "north,2,1245", "south,1,10.5"]
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".tmp-")]
    summary = outcome.summary()
    assert summary["already_present"] is False and "manifest.json" not in summary["files"]
    json.dumps(summary)  # serialisable


def test_the_same_request_is_idempotent_and_does_not_change_anything(tmp_path: Path):
    first = run(tmp_path, plan=clean_plan(), specs=[SPEC])
    before = {p.name: p.read_bytes() for p in first.directory.iterdir()}
    again = run(
        tmp_path,
        plan=clean_plan(),
        specs=[SPEC],
    )
    assert again.already_present and again.directory == first.directory
    assert {p.name: p.read_bytes() for p in again.directory.iterdir()} == before
    assert (
        len(list((tmp_path / "out").iterdir())) == 1
        and len(list((tmp_path / "uploads").iterdir())) == 1
    )


def test_different_parameters_get_a_different_directory_for_the_same_input(tmp_path: Path):
    a = run(tmp_path, plan=clean_plan(), specs=[SPEC])
    b = run(tmp_path, plan=clean_plan(), specs=[SPEC, build_spec("describe", {})])
    c = run(tmp_path, plan=clean_plan(), specs=[SPEC], examples=False)
    assert len({a.directory.name, b.directory.name, c.directory.name}) == 3
    assert a.directory.name.split("-")[0] == b.directory.name.split("-")[0]


def test_parameters_hash_is_stable_and_sensitive():
    base = (LoadOptions(), Limits(), None, [])
    same = parameters_hash(*base, with_profile=True, examples=True)
    assert same == parameters_hash(*base, with_profile=True, examples=True)
    assert same != parameters_hash(
        LoadOptions(delimiter=";"), Limits(), None, [], with_profile=True, examples=True
    )
    assert same != parameters_hash(
        LoadOptions(), Limits(max_rows=5), None, [], with_profile=True, examples=True
    )
    assert same != parameters_hash(*base, with_profile=False, examples=True)


def test_profile_only_run_has_no_cleaning_files(tmp_path: Path):
    outcome = run(tmp_path)
    assert sorted(p.name for p in outcome.directory.iterdir()) == [
        "manifest.json", "profile.json", "profile_columns.csv", "results.xlsx",
    ]  # fmt: skip


def test_a_bad_file_stores_nothing_and_publishes_nothing(tmp_path: Path):
    with pytest.raises(ParseError):
        run(tmp_path, b"a,b\n1,2\n3\n")
    assert not (tmp_path / "uploads").exists() or list((tmp_path / "uploads").iterdir()) == []
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_too_many_rejects_publish_nothing_but_the_raw_copy_is_kept(tmp_path: Path):
    plan = build_plan(parse_number=["region:.:"])  # every region is text: all rows rejected
    with pytest.raises(RejectedTooMany):
        run(tmp_path, plan=plan)
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []
    assert (
        len(list((tmp_path / "uploads").iterdir())) == 1
    )  # the original is preserved for the user


def test_an_invalid_operation_publishes_nothing(tmp_path: Path):
    with pytest.raises(OperationError):
        run(
            tmp_path, specs=[build_spec("group_aggregate", {"by": "nope", "metrics": "amount:sum"})]
        )
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []


def test_a_tampered_published_directory_is_an_error_not_silently_reused(tmp_path: Path):
    outcome = run(tmp_path, plan=clean_plan(), specs=[SPEC])
    (outcome.directory / "result_01_group_aggregate.csv").write_text("tampered")
    with pytest.raises(IntegrityError):
        run(tmp_path, plan=clean_plan(), specs=[SPEC])


def test_a_tampered_raw_copy_is_an_error(tmp_path: Path):
    outcome = run(tmp_path)
    os.chmod(outcome.raw_copy.parent, 0o755)
    os.chmod(outcome.raw_copy, 0o644)
    outcome.raw_copy.write_bytes(b"changed")
    with pytest.raises(IntegrityError):
        run(tmp_path)


def test_stale_temp_directories_are_removed_on_the_next_run(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    stale = out / ".tmp-crashed"
    stale.mkdir()
    old = 1_000_000_000
    os.utime(stale, (old, old))
    run(tmp_path)
    assert not stale.exists()


def test_the_published_manifest_ties_numbers_back_to_the_raw_file(tmp_path: Path):
    outcome = run(tmp_path, plan=clean_plan(), specs=[SPEC])
    manifest = outcome.manifest
    assert manifest["input"]["sha256"] == sha256_bytes(CSV)
    assert Path(outcome.raw_copy).read_bytes() == CSV
    assert manifest["operations"][0]["provenance"]["input_sha256"] == sha256_bytes(CSV)
    assert manifest["cleaning"]["rows_in"] == 3 and manifest["generated_at"] == STAMP
