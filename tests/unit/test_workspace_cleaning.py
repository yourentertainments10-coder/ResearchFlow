"""Workspace cleaning: typed steps on a copy, every number hand-computed, nothing applied unasked,
independent check, session re-ask, undo, replay and report. See docs/WORKSPACE_CLEANING.md."""

from __future__ import annotations

import json

import pytest

from sie.datasets import Limits, LoadOptions, open_dataset, profile
from sie.datasets.models import OperationError
from sie.workspace import build as build_module
from sie.workspace import cleaning as cl
from sie.workspace import replay, start_session
from sie.workspace.build import build_workspace
from sie.workspace.clean_verify import verify_cleaning
from sie.workspace.models import Verification
from sie.workspace.report import build_report
from sie.workspace.view import session_view
from sie.workspace.writer import exportable_document

# age: 20 30 40 50 22 26 28 34 (two blanks): median (28+30)/2 = 29, mean 250/8 = 31.25
# fare: 10..18 and 300: Q1 12.25, Q3 16.75, fences 5.5 .. 23.5, so only 300 is an outlier
# city: X x5, Y x4, one blank -> mode X; cabin: empty everywhere
CSV = (
    b"name,age,fare,city,cabin\n"
    b"a,20,10,X,\nb,,11,Y,\nc,30,12,X,\nd,40,13,,\ne,,14,X,\n"
    b"f,50,15,Y,\ng,22,16,X,\nh,26,17,Y,\ni,28,18,X,\nj,34,300,Y,\n"
)
SRC = "t.csv"


def dataset(data: bytes = CSV):
    return open_dataset(data, SRC, LoadOptions(), Limits())


def column(ds, name):
    return ds.frame[name].tolist()


def test_fill_median_mean_mode_constant_use_the_stated_method():
    ds = dataset()
    out = cl.apply_steps(ds, [cl.FillMissing(column="age", method="median")])
    assert column(out.dataset, "age")[1] == "29" and column(out.dataset, "age")[4] == "29"
    assert out.entries[0]["cells_filled"] == 2 and out.entries[0]["filled_with"] == 29
    mean = cl.apply_steps(ds, [cl.FillMissing(column="age", method="mean")]).dataset
    assert column(mean, "age")[1] == "31.25"
    mode = cl.apply_steps(ds, [cl.FillMissing(column="city", method="mode")]).dataset
    assert column(mode, "city")[3] == "X"
    const = cl.apply_steps(ds, [cl.FillMissing(column="age", method="constant", value="0")]).dataset
    assert column(const, "age")[1] == "0"


def test_the_loaded_data_is_never_changed():
    ds = dataset()
    before = ds.frame.copy()
    cl.apply_steps(
        ds,
        [
            cl.FillMissing(column="age", method="median"),
            cl.DropColumn(column="cabin"),
            cl.RemoveOutliers(column="fare"),
        ],
    )
    assert ds.frame.equals(before) and ds.rows == 10 and "cabin" in ds.columns


def test_a_text_column_has_no_median_and_nothing_is_guessed():
    with pytest.raises(OperationError):
        cl.apply_steps(dataset(), [cl.FillMissing(column="city", method="median")])
    with pytest.raises(OperationError):
        cl.apply_steps(dataset(), [cl.FillMissing(column="age", method="constant")])
    with pytest.raises(OperationError):
        cl.apply_steps(dataset(), [cl.FillMissing(column="age", method="constant", value="abc")])
    with pytest.raises(OperationError):
        cl.apply_steps(dataset(), [cl.DropColumn(column="nope")])


def test_nothing_missing_means_nothing_filled():
    out = cl.apply_steps(dataset(), [cl.FillMissing(column="fare", method="median")])
    assert out.entries[0]["cells_filled"] == 0 and out.dataset.rows == 10


def test_drop_rows_and_columns():
    ds = dataset()
    rows = cl.apply_steps(ds, [cl.DropMissingRows(columns=["age"])]).dataset
    assert rows.rows == 8 and list(rows.frame.index)[:3] == [1, 3, 4]  # row ids survive
    col = cl.apply_steps(ds, [cl.DropColumn(column="cabin")]).dataset
    assert col.columns == ["name", "age", "fare", "city"]
    only = open_dataset(b"a\n1\n", SRC, LoadOptions(), Limits())
    with pytest.raises(OperationError):
        cl.apply_steps(only, [cl.DropColumn(column="a")])


def test_remove_duplicates_keeps_the_first():
    ds = dataset(CSV + b"a,20,10,X,\n")
    out = cl.apply_steps(ds, [cl.RemoveDuplicates()])
    assert ds.rows == 11 and out.dataset.rows == 10 and out.entries[0]["rows_removed"] == 1
    assert 11 not in list(out.dataset.frame.index)


def test_text_to_number_converts_or_refuses_without_changing_anything():
    ds = dataset(b'amt\n1,200\n350\n"2,000.5"\n'.replace(b"1,200", b'"1,200"'))
    out = cl.apply_steps(ds, [cl.TextToNumber(column="amt", thousands=",")])
    assert column(out.dataset, "amt") == [1200, 350, 2000.5]
    assert out.entries[0]["cells_converted"] == 3
    with pytest.raises(OperationError):
        cl.apply_steps(dataset(), [cl.TextToNumber(column="city")])


def test_outliers_are_flagged_without_changing_data_and_removed_only_when_asked():
    ds = dataset()
    flag = cl.apply_steps(ds, [cl.FlagOutliers(column="fare")])
    e = flag.entries[0]
    assert (e["outliers"], e["low"], e["high"], e["row_ids"]) == (1, 5.5, 23.5, [10])
    assert flag.dataset.rows == 10 and flag.dataset.frame.equals(ds.frame)
    gone = cl.apply_steps(ds, [cl.RemoveOutliers(column="fare")])
    assert gone.dataset.rows == 9 and 10 not in list(gone.dataset.frame.index)
    few = open_dataset(b"v\n1\n2\n900\n", SRC, LoadOptions(), Limits())
    assert cl.apply_steps(few, [cl.FlagOutliers(column="v")]).entries[0]["outliers"] == 0


def test_sentences_carry_the_numbers_and_the_value_used():
    ds = dataset()
    out = cl.apply_steps(ds, [cl.FillMissing(column="age", method="median")])
    text = cl.describe(out.entries[0], ds.source.sha256)
    assert text == "2 missing age values were filled with the median (29)."
    drop = cl.apply_steps(ds, [cl.DropColumn(column="cabin")])
    assert cl.describe(drop.entries[0], ds.source.sha256) == (
        "Column cabin was removed (10 of 10 values were empty)."
    )


def test_suggestions_come_from_the_profile_and_change_nothing():
    ds = dataset(CSV + b"a,20,10,X,\n")
    before = ds.frame.copy()
    found = {s.id: s for s in cl.suggest(ds, profile(ds), ds.source.sha256)}
    assert ds.frame.equals(before)
    assert set(found) == {
        "remove_duplicates", "drop_column:cabin", "fill_missing:age", "fill_missing:city",
        "flag_outliers:fare",
    }  # fmt: skip
    assert found["drop_column:cabin"].reason.startswith("cabin is 100% empty")
    assert found["fill_missing:age"].step == cl.FillMissing(column="age", method="median")
    assert found["fill_missing:city"].step == cl.FillMissing(column="city", method="mode")
    assert "1 rows repeat" in found["remove_duplicates"].reason


def test_a_mostly_empty_numeric_column_is_suggested_for_removal_not_filling():
    rows = b"id,x\n" + b"".join(f"{i},{'' if i < 8 else i}\n".encode() for i in range(10))
    ds = dataset(rows)
    ids = {s.id for s in cl.suggest(ds, profile(ds), ds.source.sha256)}
    assert "drop_column:x" in ids and "fill_missing:x" not in ids


def test_identifier_like_text_is_not_suggested_for_numbers():
    ds = dataset(b"zip\n02134\n10001\n")
    assert not [
        s for s in cl.suggest(ds, profile(ds), ds.source.sha256) if "text_to_number" in s.id
    ]


def test_independent_check_agrees_and_catches_a_wrong_cell():
    ds = dataset()
    steps = [
        cl.FillMissing(column="age", method="median"),
        cl.DropColumn(column="cabin"),
        cl.RemoveOutliers(column="fare"),
    ]
    out = cl.apply_steps(ds, steps)
    assert verify_cleaning(ds, steps, out.dataset, out.entries).status == "verified"
    out.dataset.frame.loc[2, "age"] = "30000"
    bad = verify_cleaning(ds, steps, out.dataset, out.entries)
    assert bad.status == "unverified" and "age" in bad.reason


def test_a_failed_cleaning_check_blocks_export(monkeypatch):
    monkeypatch.setattr(
        build_module,
        "verify_cleaning",
        lambda *a, **k: Verification("unverified", 1, ["x"], "forced"),
    )
    ws = build_workspace(CSV, SRC, steps=[cl.DropColumn(column="cabin")])
    assert ws.items and not any(i.exportable for i in ws.items)
    doc = exportable_document(ws)
    assert all("result" not in a for a in doc["analyses"])


def test_a_session_rejects_a_step_whose_check_fails(monkeypatch):
    s = start_session(CSV, SRC)
    monkeypatch.setattr(
        build_module,
        "verify_cleaning",
        lambda *a, **k: Verification("unverified", 1, ["x"], "forced"),
    )
    step = s.clean({"op": "drop_column", "column": "cabin"})
    assert step.kind == "clean_rejected" and "independent check" in step.message
    assert s.cleaning == [] and "cabin" in [c["name"] for c in session_view(s)["columns"]]


def test_session_cleaning_reasks_cards_on_the_cleaned_data_and_never_reuses_ids():
    s = start_session(CSV, SRC)
    s.ask("Average age by city")
    assert s.items[0].planned.id == "r1"
    step = s.clean({"op": "fill_missing", "column": "age", "method": "mean"})
    assert step.kind == "clean" and step.message == (
        "2 missing age values were filled with the mean (31.25)."
    )
    assert [i.planned.id for i in s.items] == ["r2"]  # asked again on the cleaned data, new id
    assert [(x.kind, x.auto) for x in s.steps] == [
        ("plan", False),
        ("clean", False),
        ("plan", True),
    ]
    assert all(i.exportable for i in s.items)
    means = dict(
        zip(s.items[0].result.table["city"], s.items[0].result.table["age:mean"], strict=True)
    )
    # X: 20, 30, 31.25(filled), 22, 28 -> 131.25/5 = 26.25 ; Y: 31.25, 50, 26, 34 -> 141.25/4 = 35.3125
    assert means["X"] == 26.25 and means["Y"] == 35.3125
    view = session_view(s)
    assert view["cleaning"]["applied"][0]["text"] == step.message
    assert view["cleaning"]["rows_loaded"] == 10 and view["cleaning"]["can_undo"]


def test_a_rejected_step_changes_nothing_and_is_recorded():
    s = start_session(CSV, SRC)
    s.ask("Sum fare by city")
    before = s.sha256()
    for bad in (
        {"op": "fill_missing", "column": "city", "method": "median"},
        {"op": "drop_column", "column": "nope"},
        {"op": "delete_everything"},
        "text",
        None,
    ):
        assert s.clean(bad).kind == "clean_rejected"
    assert s.cleaning == [] and len(s.items) == 1
    assert s.sha256() != before  # the attempts are in the history
    assert [x.kind for x in s.steps].count("clean_rejected") == 5


def test_undo_restores_the_loaded_numbers():
    s = start_session(CSV, SRC)
    s.ask("Sum fare by city")
    first = s.items[0].result.provenance.result_sha256
    s.clean({"op": "remove_outliers", "column": "fare"})
    assert s.items[0].result.provenance.result_sha256 != first
    undone = s.undo_clean()
    assert undone.kind == "clean_undo" and s.cleaning == []
    assert s.items[0].result.provenance.result_sha256 == first
    assert s.undo_clean().kind == "clean_rejected"


def test_replay_reproduces_cleaning_and_detects_a_changed_log():
    s = start_session(CSV, SRC)
    s.ask("Sum fare by city")
    s.clean({"op": "fill_missing", "column": "age", "method": "median"})
    s.clean({"op": "drop_column", "column": "nope"})
    s.ask("Average age by city")
    s.undo_clean()
    s.undo_clean()
    history = json.loads(json.dumps(s.history()))
    assert replay(CSV, SRC, history).sha256() == s.sha256()
    history["steps"][1]["cleaning"]["entry"]["filled_with"] = 30
    with pytest.raises(Exception) as err:
        replay(CSV, SRC, history)
    assert "cleaning differs" in str(err.value)


def test_report_and_workspace_json_list_every_cleaning_step():
    s = start_session(CSV, SRC)
    s.clean({"op": "fill_missing", "column": "age", "method": "median"})
    s.clean({"op": "flag_outliers", "column": "fare"})
    html = build_report(s)
    assert "<h2>Data cleaning</h2>" in html
    assert "2 missing age values were filled with the median (29)." in html
    assert "1 fare values lie outside 5.50 to 23.50" in html
    assert "10 rows before, 10 after" in html
    doc = exportable_document(s.workspace)
    assert [e["op"] for e in doc["cleaning_steps"]] == ["fill_missing", "flag_outliers"]
    assert doc["cleaning_steps_verification"]["status"] == "verified"


def test_without_cleaning_the_workspace_document_is_unchanged():
    ws = build_workspace(CSV, SRC)
    assert "cleaning_steps" not in ws.to_dict()
    assert "Data cleaning" not in build_report(start_session(CSV, SRC))


def test_a_step_from_outside_is_validated():
    for bad in (
        {"op": "fill_missing", "column": "a", "method": "median", "extra": 1},
        {"op": "fill_missing", "column": "", "method": "median"},
        {"op": "flag_outliers", "column": "a", "k": 0},
        {"op": "drop_missing_rows", "columns": []},
    ):
        with pytest.raises(OperationError):
            cl.parse_step(bad)
    assert cl.parse_step({"op": "drop_column", "column": "a"}) == cl.DropColumn(column="a")
