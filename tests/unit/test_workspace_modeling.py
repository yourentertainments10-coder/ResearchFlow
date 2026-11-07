"""Workspace modeling: problem type, leakage guards, honest evaluation, independent score check, session
and view. Metric numbers are hand-computed. See docs/WORKSPACE_MODELING.md."""

from __future__ import annotations

import ast
import functools
import inspect
import json

import numpy as np
import pytest

pytest.importorskip("sklearn")

from sie.datasets import Limits, LoadOptions, open_dataset, profile  # noqa: E402
from sie.datasets.models import OperationError  # noqa: E402
from sie.workspace import model_verify as mv  # noqa: E402
from sie.workspace import modeling as md  # noqa: E402
from sie.workspace import replay, start_session  # noqa: E402
from sie.workspace.report import build_report  # noqa: E402
from sie.workspace.view import model_view, session_view  # noqa: E402


def csv(n: int = 160) -> bytes:
    rows = ["id,name,region,x,score,label,outcome,dup,label_flag,band,const,sparse"]
    for i in range(n):
        x = (i * 7) % 50
        label = "high" if x >= 25 else "low"
        if i % 10 == 0:  # a tenth of the rows break the rule, so no model is perfect
            label = "low" if label == "high" else "high"
        rows.append(
            ",".join(
                [
                    str(i + 1),
                    f"person{i}",
                    "NSEW"[i % 4],
                    str(x),
                    str(x * 2 + i % 5),
                    label,
                    "1" if label == "high" else "0",
                    label,  # a copy of the target
                    "y" if label == "high" else "n",  # name contains the target name
                    "H" if label == "high" else "L",  # a pure function of the target
                    "k",
                    "v" if i % 5 == 0 else "",  # 80 % empty
                ]
            )
        )
    return ("\n".join(rows) + "\n").encode()


def load(data: bytes):
    ds = open_dataset(data, "m.csv", LoadOptions(), Limits())
    return ds, profile(ds)


@functools.cache
def report(target: str | None, n: int = 160, seed: int = 0):
    ds, pr = load(csv(n))
    return md.run_model(ds, pr, md.ModelSpec(target, seed))


def by_name(rep, name):
    return next(m for m in rep.models if m["name"] == name)


# -- the metrics, by hand ----------------------------------------------------------------------------

PROBA = np.array([[0.8, 0.2], [0.4, 0.6], [0.3, 0.7], [0.1, 0.9]])
TRUTH = np.array(["a", "a", "b", "b"], dtype=object)


def test_classification_scores_match_the_hand_computed_ones_in_both_paths():
    # predictions a b b b: class a precision 1, recall 1/2, F1 2/3; class b precision 2/3, recall 1, F1 4/5
    want = {"accuracy": 0.75, "precision": 0.833333, "recall": 0.75, "f1": 0.733333, "roc_auc": 1.0}
    got = md.classification_scores(TRUTH, PROBA, ["a", "b"])
    again = mv.classification(TRUTH, PROBA, ["a", "b"])
    for key, value in want.items():
        assert got[key] == pytest.approx(value, abs=1e-6)
        assert again[key] == pytest.approx(value, abs=1e-6)


def test_regression_scores_and_silhouette_match_the_hand_computed_ones():
    y, pred = np.array([1.0, 2, 3, 4]), np.array([1.0, 2, 3, 6])
    for got in (md.regression_scores(y, pred), mv.regression(y, pred)):
        assert got["rmse"] == pytest.approx(1.0) and got["mae"] == pytest.approx(0.5)
        assert got["r2"] == pytest.approx(0.2)  # 1 - 4/5
    points = np.array([[0.0], [1.0], [10.0], [11.0]])
    # a=1, b=10.5 and 9.5 on each side: (9.5/10.5 + 8.5/9.5)/2
    assert mv.silhouette(points, np.array([0, 0, 1, 1])) == pytest.approx(0.8997494, abs=1e-6)
    assert mv.silhouette(points, np.array([0, 0, 0, 0])) is None


def test_auc_with_ties_and_three_classes():
    assert mv._auc(np.array([True, False, True, False]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    three = np.array(["a", "b", "c", "a", "b", "c"], dtype=object)
    proba = np.array([[0.7, 0.2, 0.1], [0.2, 0.6, 0.2], [0.1, 0.2, 0.7]] * 2)
    got = md.classification_scores(three, proba, ["a", "b", "c"])
    again = mv.classification(three, proba, ["a", "b", "c"])
    assert got["accuracy"] == again["accuracy"] == 1.0
    assert got["roc_auc"] == pytest.approx(1.0) and again["roc_auc"] == pytest.approx(1.0)


# -- the problem and the columns ---------------------------------------------------------------------


def test_a_text_target_is_classification_with_the_baseline_first():
    rep, _ = report("label")
    assert rep.problem == "classification" and rep.verification.status == "verified"
    assert [m["name"] for m in rep.models][0] == "Baseline (most frequent class)"
    assert rep.best not in ("Baseline (most frequent class)",)
    best, base = by_name(rep, rep.best)["metrics"], rep.models[0]["metrics"]
    assert best["f1"] > base["f1"] + 0.2 and best["accuracy"] >= 0.85
    assert base["roc_auc"] == 0.5


def test_a_numeric_target_with_few_whole_values_is_classes_and_a_measure_is_regression():
    assert report("outcome")[0].problem == "classification"
    rep, _ = report("score")
    assert rep.problem == "regression" and rep.verification.status == "verified"
    assert rep.models[0]["name"] == "Baseline (mean)"
    linear = by_name(rep, "Linear regression")["metrics"]
    assert (
        linear["r2"] > 0.5 and linear["rmse"] < by_name(rep, "Baseline (mean)")["metrics"]["rmse"]
    )


def test_leaky_and_unusable_columns_are_left_out_with_reasons():
    rep, _ = report("label")
    why = {e["column"]: e["reason"] for e in rep.excluded}
    assert "identifier" in why["name"] and "identifier" in why["id"]
    assert "copy of the target" in why["dup"]
    assert "name contains the target name" in why["label_flag"]
    assert "single target value" in why["band"]
    assert "every filled value is the same" in why["const"]
    assert "80% of its values are missing" in why["sparse"]
    used = {f["column"] for f in rep.features}
    assert used == {"region", "x", "score", "outcome"} or used == {"region", "x", "score"}
    assert not used & set(why)


def test_the_numeric_twin_of_a_class_target_is_also_caught_for_regression():
    rows = ["a,b,y"] + [f"{i},{i % 7},{i * 3}" for i in range(60)]
    ds, pr = load(("\n".join(rows) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("y"))
    assert [e["column"] for e in rep.excluded] == ["a"]
    assert "almost exactly" in rep.excluded[0]["reason"]


def test_refusals_say_why():
    ds, pr = load(csv(160))
    for spec, text in (
        (md.ModelSpec("nope"), "There is no column"),
        (md.ModelSpec("const"), "only one value"),
        (md.ModelSpec("name"), "different values"),
    ):
        with pytest.raises(OperationError, match=text):
            md.run_model(ds, pr, spec)
    tiny, tiny_pr = load(csv(15))
    with pytest.raises(OperationError, match="at least 20"):
        md.run_model(tiny, tiny_pr, md.ModelSpec("label"))
    only, only_pr = load(b"id,y\n" + b"".join(f"{i},{i % 2}\n".encode() for i in range(40)))
    with pytest.raises(OperationError, match="No column is usable"):
        md.run_model(only, only_pr, md.ModelSpec("y"))


# -- evaluation --------------------------------------------------------------------------------------


def test_100_rows_or_more_use_a_holdout_and_fewer_use_cross_validation():
    big = report("label")[0].evaluation
    assert (big["method"], big["train_rows"], big["test_rows"]) == ("holdout", 120, 40)
    small, _ = report("label", n=60)
    assert small.evaluation["method"] == "cross_validation" and small.evaluation["folds"] == 5
    assert any("Only 60 rows" in w for w in small.warnings)
    assert small.verification.status == "verified"


def test_filling_and_scaling_are_learned_inside_the_pipeline_on_training_rows_only():
    pipe = md._pipeline(object(), ["x"], ["region"])
    assert [n for n, _ in pipe.steps] == ["prep", "model"]
    prep = {name: part for name, part, _ in pipe.named_steps["prep"].transformers}
    assert [n for n, _ in prep["num"].steps] == ["fill", "scale"]
    assert [n for n, _ in prep["cat"].steps] == ["fill", "hot"]


def test_missing_values_in_the_features_do_not_stop_the_run():
    lines = csv(160).decode().splitlines()
    cells = [ln.split(",") for ln in lines]
    for i in range(1, len(cells), 9):
        cells[i][3] = ""  # x blank on some rows
        cells[i][2] = ""  # region blank too
    ds, pr = load(("\n".join(",".join(c) for c in cells) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("label"))
    assert rep.verification.status == "verified" and rep.best


def test_rows_without_a_target_are_dropped_and_counted():
    cells = [ln.split(",") for ln in csv(160).decode().splitlines()]
    for i in range(1, 11):
        cells[i][5] = ""
    ds, pr = load(("\n".join(",".join(c) for c in cells) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("label"))
    assert rep.rows_dropped == {"target_missing": 10} and rep.rows_used == 150


def test_a_large_file_is_sampled_with_a_fixed_seed_and_says_so(monkeypatch):
    monkeypatch.setattr(md, "MAX_ROWS", 120)
    ds, pr = load(csv(160))
    a, _ = md.run_model(ds, pr, md.ModelSpec("label"))
    b, _ = md.run_model(ds, pr, md.ModelSpec("label"))
    assert a.rows_used == 120 and a.rows_dropped == {"sampled_out": 40}
    assert any("fixed-seed sample" in w for w in a.warnings) and a.sha256() == b.sha256()


def test_very_unequal_classes_are_flagged():
    rows = ["x,y"] + [f"{i},{'rare' if i < 5 else 'common'}" for i in range(100)]
    ds, pr = load(("\n".join(rows) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("y"))
    assert any("very unequal" in w for w in rep.warnings)


def test_same_seed_same_report_and_hash_and_the_library_is_outside_the_hash():
    ds, pr = load(csv(160))
    a, _ = md.run_model(ds, pr, md.ModelSpec("label", 7))
    b, _ = md.run_model(ds, pr, md.ModelSpec("label", 7))
    assert a.to_dict() == b.to_dict() and a.sha256() == b.sha256()
    assert "library" not in a.to_dict() and a.library["scikit-learn"]
    other, _ = md.run_model(ds, pr, md.ModelSpec("label", 8))
    assert other.sha256() != a.sha256()  # the seed is part of what was done


def test_importance_names_the_column_that_carries_the_signal():
    rep, _ = report("label")
    assert rep.importance and rep.importance[0]["column"] in ("x", "score")
    assert rep.importance[0]["drop"] > 0
    assert "held-out" in rep.evaluation["importance_note"]


# -- the independent check ---------------------------------------------------------------------------


def test_a_wrong_reported_score_makes_the_report_unverified():
    rep, evidence = report("label")
    assert mv.verify_report(rep, evidence).status == "verified"
    tampered = json.loads(json.dumps(rep.to_dict()))
    assert tampered["verification"]["status"] == "verified"
    entry = by_name(rep, "Logistic regression")
    original = entry["metrics"]["f1"]
    entry["metrics"]["f1"] = original + 0.01
    try:
        bad = mv.verify_report(rep, evidence)
    finally:
        entry["metrics"]["f1"] = original
    assert bad.status == "unverified" and "f1" in bad.reason and "Logistic regression" in bad.reason


# -- groups ------------------------------------------------------------------------------------------


def test_two_far_apart_blobs_are_found_as_two_groups():
    rows = (
        ["a,b"]
        + [f"{i % 5},{i % 3}" for i in range(30)]
        + [f"{100 + i % 5},{100 + i % 3}" for i in range(30)]
    )
    ds, pr = load(("\n".join(rows) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec(None))
    assert (
        rep.problem == "clustering"
        and rep.best == "2 groups"
        and rep.verification.status == "verified"
    )
    assert sorted(rep.clusters["profiles"][i]["rows"] for i in range(2)) == [30, 30]
    assert next(m for m in rep.models if m["k"] == 2)["silhouette"] > 0.9
    assert not any("weak" in w for w in rep.warnings)


def test_clustering_needs_a_numeric_column():
    ds, pr = load(b"c\n" + b"".join(f"{'ab'[i % 2]}\n".encode() for i in range(40)))
    with pytest.raises(OperationError):
        md.run_model(ds, pr, md.ModelSpec(None))


# -- requests in words -------------------------------------------------------------------------------


def test_requests_in_words():
    cols = ["Survived", "Age", "age_group"]
    assert md.parse_model_request("Predict survived", cols) == md.ModelSpec("Survived")
    assert md.parse_model_request("predict the 'Age'", cols) == md.ModelSpec("Age")
    assert md.parse_model_request("Find groups", cols) == md.ModelSpec(None)
    assert md.parse_model_request("cluster the rows", cols) == md.ModelSpec(None)
    assert "no column named 'fare'" in md.parse_model_request("Predict fare", cols)
    assert md.parse_model_request("Sum age by region", cols) is None
    assert md.parse_model_request("Predict", cols) is None


# -- session, view, report ---------------------------------------------------------------------------


def session():
    return start_session(csv(160), "m.csv")


def test_the_session_records_a_model_step_and_does_not_refit_the_same_request():
    s = session()
    step = s.ask("Predict label")
    assert step.kind == "model" and step.verification == "verified"
    assert step.message.startswith("Predicting label: ") and "baseline scored" in step.message
    assert s.models[0].id == "m1" and step.model["id"] == "m1"
    again = s.ask("predict LABEL")
    assert again.kind == "already_shown" and again.model["id"] == "m1" and len(s.models) == 1
    assert s.ask("Predict nothing").kind == "unsupported"
    assert s.ask("Predict const").kind == "model_rejected"


def test_models_are_refitted_on_the_cleaned_data_and_replay_reproduces_everything():
    s = session()
    s.ask("Predict label")
    s.clean({"op": "drop_column", "column": "region"})
    assert [r.id for r in s.models] == ["m2"]  # asked again on the cleaned data
    assert "region" not in [f["column"] for f in s.models[0].report.features]
    assert [(x.kind, x.auto) for x in s.steps][-1] == ("model", True)
    s.ask("Find groups")
    history = json.loads(json.dumps(s.history()))
    assert replay(csv(160), "m.csv", history).sha256() == s.sha256()
    history["steps"][0]["model"]["report_sha256"] = "0" * 64
    with pytest.raises(Exception) as err:
        replay(csv(160), "m.csv", history)
    assert "model differs" in str(err.value)


def test_the_view_shows_scores_as_exact_figures_and_hides_unverified_ones():
    s = session()
    s.ask("Predict score")
    view = session_view(s)["models"][0]
    assert view["problem"] == "regression" and view["table"]["columns"] == [
        "Model",
        "RMSE",
        "MAE",
        "R squared",
    ]
    assert all(isinstance(c, str) for row in view["table"]["rows"] for c in row)
    assert any(row[0].endswith("(best)") for row in view["table"]["rows"])
    run = s.models[0]
    run.report.verification = type(run.report.verification)("unverified", 1, ["x"], "forced")
    hidden = model_view(run.id, run.report, s.workspace.source.sha256, None)
    assert hidden["table"] is None and hidden["importance"] == [] and hidden["reason"] == "forced"
    assert "<h2>Models</h2>" in build_report(
        s
    ) and "did not pass the independent check" in build_report(s)


def test_the_report_lists_models_exclusions_and_limits():
    s = session()
    s.ask("Predict label")
    html = build_report(s)
    assert "Model comparison m1: classification" in html
    assert "Baseline (most frequent class)" in html and "Columns left out" in html
    assert "does not show that one thing causes another" in html


def test_without_models_the_report_has_no_models_section():
    assert "<h2>Models</h2>" not in build_report(session())


def test_modeling_modules_import_sklearn_lazily_and_nothing_risky():
    for module in (md, mv):
        tree = ast.parse(inspect.getsource(module))
        top = {
            a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names
        } | {
            n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module
        }
        assert "sklearn" not in top
        everything = {
            a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
            for a in n.names
        } | {
            n.module.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module
        }
        assert not everything & {
            "os",
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "httpx",
            "pickle",
        }
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert not called & {"eval", "exec", "compile", "__import__", "open"}
