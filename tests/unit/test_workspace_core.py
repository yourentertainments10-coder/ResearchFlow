"""Workspace core: starter plan, hand-computed numbers, independent verification, cards, findings,
charts and the written directory. Expected values are worked out by hand from SALES below, never taken
from the code under test."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pandas as pd
import pytest

from sie.datasets import DatasetError, open_dataset
from sie.workspace import build as build_module
from sie.workspace import build_workspace
from sie.workspace.charts import MAX_BARS, bar_chart, line_chart
from sie.workspace.figs import template_digits
from sie.workspace.models import Verification
from sie.workspace.verify import verify_result
from sie.workspace.writer import directory_name, verify_written, write_workspace

SALES = (
    b"region,product,sold_on,units,revenue\n"
    b"North,A,2025-01-05,2,20\n"
    b"North,B,2025-01-20,3,30\n"
    b"South,A,2025-02-03,5,50\n"
    b"South,A,2025-02-17,,\n"
    b"East,B,2025-04-01,1,10\n"
    b",B,2025-04-02,4,40\n"
    b"South,B,2025-04-10,6,60\n"
    b"North,A,2025-04-11,2,20\n"
)


def ws(data: bytes = SALES, name: str = "sales.csv"):
    return build_workspace(data, name)


def item(workspace, item_id: str):
    return next(i for i in workspace.items if i.planned.id == item_id)


def rows(workspace, item_id: str) -> list[list]:
    return [
        [None if (isinstance(v, float) and pd.isna(v)) else v for v in r]
        for r in item(workspace, item_id).result.table.values.tolist()
    ]


# --- plan ------------------------------------------------------------------------------------------


def test_the_starter_plan_follows_the_column_types():
    plan = [(i.planned.id, i.planned.spec.op, i.planned.chart) for i in ws().items]
    assert plan == [
        ("describe", "describe", "table"),
        ("a1", "value_counts", "bar"),
        ("a2", "value_counts", "bar"),
        ("a3", "top_n", "bar"),
        ("a4", "top_n", "bar"),
        ("a5", "time_trend", "line"),
        ("a6", "correlation", "table"),
    ]


def test_an_identifier_column_is_not_used_as_a_metric():
    data = b"id,kind,amount\n" + b"".join(
        f"{n},{'x' if n % 2 else 'y'},{n % 4}\n".encode() for n in range(1, 21)
    )
    used = {getattr(i.planned.spec, "metric", "") for i in ws(data, "ids.csv").items}
    assert "amount:sum" in used and not any(m.startswith("id:") for m in used)


def test_a_file_with_only_free_text_gets_only_the_column_summary():
    data = b"note\n" + b"".join(f"text number {n}\n".encode() for n in range(10))
    assert [i.planned.id for i in ws(data, "notes.csv").items] == ["describe"]


def test_a_header_only_file_has_no_analyses_and_says_so():
    result = ws(b"a,b\n", "empty.csv")
    assert result.items == []
    assert any("no data rows" in f.text for f in result.findings)


# --- numbers worked out by hand ------------------------------------------------------------------


def test_value_counts_include_the_null_group_and_shares():
    assert rows(ws(), "a1") == [
        ["North", 3, 3 / 8], ["South", 3, 3 / 8], ["East", 1, 1 / 8], ["(null)", 1, 1 / 8],
    ]  # fmt: skip


def test_top_n_sums_skip_nulls_and_keep_the_null_group():
    table = item(ws(), "a3").result.table
    assert table.columns.tolist() == ["rank", "region", "rows", "units:sum"]
    # South 5+6 (one empty cell skipped), North 2+3+2, empty region 4, East 1
    assert table.values.tolist() == [
        [1, "South", 3, 11], [2, "North", 3, 7], [3, "(null)", 1, 4], [4, "East", 1, 1],
    ]  # fmt: skip
    assert rows(ws(), "a4") == [[1, "B", 4, 14], [2, "A", 4, 9]]


def test_the_monthly_trend_lists_the_empty_month_as_a_gap():
    assert rows(ws(), "a5") == [
        ["2025-01", 2, 5], ["2025-02", 2, 5], ["2025-03", 0, None], ["2025-04", 4, 13],
    ]  # fmt: skip


def test_correlation_of_proportional_columns_is_one():
    [[a, b, r, n]] = rows(ws(), "a6")
    assert (a, b, n) == ("units", "revenue", 7) and abs(r - 1.0) < 1e-12


def test_cards_are_exact_figures():
    cards = {c.label: c for c in ws().cards}
    assert {k: v.value for k, v in cards.items()} == {
        "Rows": "8", "Columns": "5", "Missing cells": "3", "Missing share": "7.5%",
        "Duplicate rows": "0", "Mean of units": "3.29", "Minimum of units": "1", "Maximum of units": "6",
    }  # fmt: skip
    assert all(c.verified for c in cards.values())
    assert cards["Rows"].figure["source_kind"] == "user_file"
    assert cards["Mean of units"].figure["source_kind"] == "operation"


# --- determinism -----------------------------------------------------------------------------------


def test_the_same_file_gives_the_same_result_hash_and_charts():
    a, b = ws(), ws()
    assert a.sha256() == b.sha256()
    assert [i.svg for i in a.items] == [i.svg for i in b.items]


def test_one_changed_cell_changes_the_hash():
    assert (
        ws().sha256()
        != ws(SALES.replace(b"North,A,2025-01-05,2,20", b"North,A,2025-01-05,9,20")).sha256()
    )


# --- independent verification --------------------------------------------------------------------


def _tamper(workspace, item_id: str, row: int, column: str, value):
    ds = workspace.dataset
    result = item(workspace, item_id)
    table = result.result.table.copy()
    table.at[row, column] = value
    return verify_result(ds, result.planned.spec, table)


@pytest.mark.parametrize(
    ("item_id", "row", "column", "value"),
    [
        ("a1", 0, "count", 4),
        ("a3", 0, "units:sum", 12),
        ("a3", 1, "rank", 5),
        ("a5", 3, "units:sum", 14),
        ("a5", 2, "rows", 1),
        ("a6", 0, "coefficient", 0.5),
        ("a6", 0, "n_used", 8),
        ("describe", 3, "mean", 99.0),
    ],
)
def test_a_wrong_number_is_caught_by_the_independent_check(item_id, row, column, value):
    result = _tamper(ws(), item_id, row, column, value)
    assert result.status == "unverified" and result.mismatches


def test_a_missing_group_is_caught():
    workspace = ws()
    result = item(workspace, "a3")
    table = result.result.table.iloc[1:].reset_index(drop=True)
    assert verify_result(workspace.dataset, result.planned.spec, table).status == "unverified"


def test_every_planned_result_is_verified_with_real_checks():
    for i in ws().items:
        assert i.verification.status == "verified" and i.verification.checks > 0, i.planned.id


def test_a_request_the_checker_does_not_cover_is_unverified_not_passed():
    class Mystery:
        op = "mystery"

    workspace = ws()
    result = verify_result(workspace.dataset, Mystery(), pd.DataFrame({"x": [1]}))
    assert result.status == "unverified" and "no independent check" in result.reason


def test_a_crash_inside_the_checker_never_passes_a_result():
    workspace = ws()
    spec = item(workspace, "a1").planned.spec
    broken = pd.DataFrame({"unexpected": [1]})
    assert verify_result(workspace.dataset, spec, broken).status == "unverified"


def test_the_checker_does_not_use_the_calculation_module():
    source = Path(build_module.__file__).with_name("verify.py").read_text()
    assert "sie.datasets.calc" not in source and "run_operation" not in source


def test_null_markers_follow_the_dataset_options():
    from sie.datasets import LoadOptions

    data = b"g,v\na,1\na,N/A\nb,5\nb,5\nb,6\nc,2\nc,2\n"
    result = build_workspace(data, "na.csv", options=LoadOptions(na_values=("N/A",)))
    assert all(i.verification.status == "verified" for i in result.items)


# --- unverified results are never exported ---------------------------------------------------------


def test_an_unverified_analysis_is_not_written(tmp_path, monkeypatch):
    real = build_module.verify_result

    def fail_one(ds, spec, table):
        if getattr(spec, "op", "") == "time_trend":
            return Verification("unverified", 3, ["forced mismatch"])
        return real(ds, spec, table)

    monkeypatch.setattr(build_module, "verify_result", fail_one)
    workspace = ws()
    directory, _, skipped = write_workspace(workspace, tmp_path)
    names = {p.name for p in directory.iterdir()}
    assert "analysis_a5.csv" not in names and "analysis_a5.svg" not in names
    assert "analysis_a3.csv" in names
    assert [s["id"] for s in skipped] == ["a5"] and "forced mismatch" in skipped[0]["reason"]
    document = json.loads((directory / "workspace.json").read_text())
    trend = next(a for a in document["analyses"] if a["id"] == "a5")
    assert "result" not in trend and trend["exportable"] is False
    assert any("could not be verified" in f["text"] for f in document["findings"])
    assert not any("sum of units was" in f["text"] for f in document["findings"])
    manifest = verify_written(directory)
    assert manifest["not_exported"][0]["id"] == "a5"


def test_a_failed_profile_check_drops_the_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(
        build_module, "verify_profile_figures", lambda ds, f: Verification("unverified", 1, ["x"])
    )
    workspace = ws()
    document = json.loads((write_workspace(workspace, tmp_path)[0] / "workspace.json").read_text())
    labels = {c["label"] for c in document["cards"]}
    assert "Rows" not in labels and "Mean of units" in labels


# --- findings --------------------------------------------------------------------------------------


def test_finding_templates_contain_no_digits_of_their_own():
    findings = ws(SALES + SALES.split(b"\n")[1] + b"\n").findings
    assert findings
    for f in findings:
        assert template_digits(f.template) == [], f.template


def test_findings_report_quality_issues_with_exact_counts():
    texts = [f.text for f in ws(SALES + b"North,A,2025-01-05,2,20\n").findings]
    assert any(t.startswith("1 rows repeat an earlier row exactly") for t in texts)
    assert any("Column units has no value in 1 rows (11.1% of all rows)" in t for t in texts)


def test_a_correlation_finding_says_association_not_cause():
    text = next(f.text for f in ws().findings if "correlation" in f.text)
    assert "strong positive correlation of 1.00 over 7 rows" in text and "not cause" in text


def test_the_trend_finding_does_not_claim_a_cause():
    text = next(f.text for f in ws().findings if "was 5 in 2025-01" in f.text)
    assert text.endswith("This describes the values, not a cause.")


# --- charts and hostile text ------------------------------------------------------------------------


def test_chart_text_is_escaped():
    table = pd.DataFrame({"k": ["<script>alert(1)</script>", 'a"b&c'], "v": [3, 2]})
    svg = bar_chart(table, "k", "v", "t <b>")
    assert "<script>" not in svg and "&lt;script&gt;" in svg and "t &lt;b&gt;" in svg


def test_the_chart_for_no_values_says_so_instead_of_drawing_zero():
    svg = bar_chart(pd.DataFrame({"k": ["a"], "v": [None]}), "k", "v", "t")
    assert "no values to chart" in svg and '<rect class="bar"' not in svg


def test_a_missing_value_breaks_the_line():
    table = pd.DataFrame({"p": ["a", "b", "c", "d"], "v": [1, 2, None, 4]})
    svg = line_chart(table, "p", "v", "t")
    assert (
        svg.count("<polyline") == 1 and svg.count("<circle") == 3
    )  # a-b joined; d alone has a point


def test_the_bar_chart_limit_is_stated():
    table = pd.DataFrame({"k": [f"k{n}" for n in range(MAX_BARS + 5)], "v": range(MAX_BARS + 5)})
    assert f"first {MAX_BARS} of {MAX_BARS + 5} rows shown" in bar_chart(table, "k", "v", "t")


def test_hostile_category_names_are_neutralised_in_the_csv_export(tmp_path):
    data = (
        b"g,v\n" + b"".join(b"=1+1,5\n" for _ in range(4)) + b"".join(b"ok,3\n" for _ in range(4))
    )
    workspace = build_workspace(data, "h.csv")
    directory, _, skipped = write_workspace(workspace, tmp_path)
    assert not skipped
    text = (directory / "analysis_a1.csv").read_text()
    assert "'=1+1" in text and "\n=1+1" not in text
    assert "=1+1" in (directory / "analysis_a1.svg").read_text()  # in a text node, not a formula


# --- written directory -------------------------------------------------------------------------------


def test_writing_is_idempotent_and_verifiable(tmp_path):
    workspace = ws()
    first, present, _ = write_workspace(workspace, tmp_path)
    again, present_again, _ = write_workspace(ws(), tmp_path)
    assert (present, present_again) == (False, True) and first == again
    assert first.name == directory_name(workspace)
    verify_written(first)


def test_a_changed_file_in_the_directory_is_detected(tmp_path):
    directory, _, _ = write_workspace(ws(), tmp_path)
    target = directory / "analysis_a1.csv"
    target.chmod(0o644)
    target.write_text(target.read_text() + "x")
    with pytest.raises(ValueError):
        verify_written(directory)


def test_a_failure_while_writing_publishes_nothing(tmp_path, monkeypatch):
    import sie.workspace.writer as writer

    def boom(frame):
        raise RuntimeError("disk full")

    monkeypatch.setattr(writer, "table_csv_bytes", boom)
    with pytest.raises(RuntimeError):
        write_workspace(ws(), tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- no network, bad input --------------------------------------------------------------------------


def test_building_a_workspace_makes_no_network_connection(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    write_workspace(ws(), tmp_path)


def test_a_malformed_file_raises_a_dataset_error_and_returns_nothing():
    with pytest.raises(DatasetError):
        build_workspace(b"a,b\n1,2,3\n", "ragged.csv")
    with pytest.raises(DatasetError):
        build_workspace(b"a,b\n1,\x00\n", "nul.csv")


def test_the_loader_is_reused_not_reimplemented():
    assert open_dataset(SALES, "x.csv").rows == ws().dataset.rows
