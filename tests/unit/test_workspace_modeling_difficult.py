"""Difficult data in modeling: a test on the latest rows (time split), many-valued categories, free
text, and unequal classes. See docs/WORKSPACE_LARGE_DATA.md."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from sie.datasets import Limits, LoadOptions, open_dataset, profile  # noqa: E402
from sie.datasets.models import OperationError  # noqa: E402
from sie.workspace import modeling as md  # noqa: E402
from sie.workspace import start_session  # noqa: E402


def load(data: bytes):
    ds = open_dataset(data, "d.csv", LoadOptions(), Limits())
    return ds, profile(ds)


def dated(n: int = 200, *, shuffle: bool = False, gaps: bool = False) -> bytes:
    """A history: sales rise over time and depend on x. One row per day."""
    rows = []
    for i in range(n):
        day = pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)
        x = (i * 7) % 11
        rows.append(
            (
                day.strftime("%Y-%m-%d") if not (gaps and i % 25 == 3) else "",
                x,
                100 + i + 3 * x + i % 4,
            )
        )
    if shuffle:
        order = np.random.RandomState(5).permutation(len(rows))
        rows = [rows[i] for i in order]
    lines = ["day,x,sales"] + [f"{d},{x},{s}" for d, x, s in rows]
    return ("\n".join(lines) + "\n").encode()


# -- time split --------------------------------------------------------------------------------------


def test_the_latest_rows_are_the_test_rows_and_the_date_is_only_used_for_order():
    ds, pr = load(dated(200))
    rep, _ = md.run_model(ds, pr, md.ModelSpec("sales", time_column="day"))
    ev = rep.evaluation
    assert ev["method"] == "time_holdout" and ev["train_rows"] == 150 and ev["test_rows"] == 50
    assert ev["train_until"] == "2024-05-29T00:00:00"  # day 150 of 200
    assert ev["test_from"] == "2024-05-30T00:00:00"
    assert [f["column"] for f in rep.features] == ["x"]
    day = next(e for e in rep.excluded if e["column"] == "day")
    assert "time order" in day["reason"]
    assert rep.verification.status == "verified"
    assert rep.spec.to_dict()["time_column"] == "day"


def test_the_order_of_the_rows_in_the_file_does_not_change_the_result():
    a = md.run_model(*load(dated(200)), md.ModelSpec("sales", time_column="day"))[0]
    b = md.run_model(*load(dated(200, shuffle=True)), md.ModelSpec("sales", time_column="day"))[0]
    assert a.sha256() == b.sha256()


def test_rows_without_a_date_are_dropped_and_counted():
    ds, pr = load(dated(200, gaps=True))
    rep, _ = md.run_model(ds, pr, md.ModelSpec("sales", time_column="day"))
    assert rep.rows_dropped["time_missing"] == 8
    assert rep.rows_used == 192


def test_time_split_refusals_say_why():
    ds, pr = load(dated(60))
    with pytest.raises(OperationError, match="at least 100 rows"):
        md.run_model(ds, pr, md.ModelSpec("sales", time_column="day"))
    ds, pr = load(dated(200))
    with pytest.raises(OperationError, match="not a date column"):
        md.run_model(ds, pr, md.ModelSpec("sales", time_column="x"))
    with pytest.raises(OperationError, match="not a date column"):
        md.run_model(ds, pr, md.ModelSpec("sales", time_column="nope"))


def test_a_classification_over_time_works_even_when_an_early_block_has_one_class():
    lines = ["day,x,flag"]
    for i in range(240):
        day = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        flag = "no" if i < 70 else ("yes" if (i % 3) else "no")  # the first blocks are all "no"
        lines.append(f"{day},{i % 9},{flag}")
    ds, pr = load(("\n".join(lines) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("flag", time_column="day"))
    assert rep.problem == "classification" and rep.evaluation["method"] == "time_holdout"
    assert rep.verification.status == "verified"


def test_requests_over_time_in_words():
    cols = ["day", "created", "x", "sales"]
    one = ["day"]
    want = md.ModelSpec("sales", time_column="day")
    assert md.parse_model_request("Predict sales over time", cols, one) == want
    assert (
        md.parse_model_request("predict sales over time using Day", cols, ["day", "created"])
        == want
    )
    assert md.parse_model_request("Predict sales", cols, one) == md.ModelSpec("sales")
    assert "no date column" in md.parse_model_request("Predict sales over time", cols, [])
    assert "more than one date column" in md.parse_model_request(
        "Predict sales over time", cols, ["day", "created"]
    )
    assert "not a date column" in md.parse_model_request(
        "Predict sales over time using x", cols, one
    )
    assert "no column named" in md.parse_model_request("Predict profit over time", cols, one)


def test_the_session_and_the_server_words_run_the_same_request():
    s = start_session(dated(200), "d.csv")
    step = s.ask("Predict sales over time")
    assert step.kind == "model" and step.verification == "verified"
    assert s.ask("Predict sales over time using day").kind == "already_shown"
    assert s.models[0].report.spec.time_column == "day"


# -- many-valued categories and free text ------------------------------------------------------------


def many(n: int = 400, kinds: int = 40) -> bytes:
    rng = np.random.RandomState(1)
    lines = ["shop,code,note,x,y"]
    for i in range(n):
        shop = f"s{i % kinds:02d}"
        x = int(rng.randint(0, 10))
        y = (i % kinds) * 3 + x + int(rng.randint(0, 3))
        note = f"customer {i} asked about delivery of order number {i * 7} on a weekday"
        lines.append(f"{shop},{'ab'[i % 2]}{i % 70:02d},{note},{x},{y}")
    return ("\n".join(lines) + "\n").encode()


def test_a_many_valued_category_is_kept_with_the_rare_ones_grouped_and_it_says_so():
    ds, pr = load(many())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("y"))
    assert "shop" in [f["column"] for f in rep.features]
    assert any("shop has 40 different values" in w and "'other'" in w for w in rep.warnings)
    assert rep.verification.status == "verified"
    forest = next(m for m in rep.models if m["name"] == "Random forest")
    assert forest["metrics"]["rmse"] < rep.models[0]["metrics"]["rmse"]  # it carries real signal


def test_the_encoder_never_makes_more_than_the_cap_of_columns_per_category_column():
    ds, pr = load(many())
    frame = pd.DataFrame({"shop": [f"s{i % 40:02d}" for i in range(400)]})
    pipe = md._pipeline("passthrough", [], ["shop"]).fit(frame)
    width = pipe.transform(frame).shape[1]
    assert width == md.MAX_CATEGORIES


def test_categories_seen_only_after_the_split_fall_into_the_other_group_without_error():
    train = pd.DataFrame({"c": [f"a{i % 25}" for i in range(250)]})
    pipe = md._pipeline("passthrough", [], ["c"]).fit(train)
    out = pipe.transform(pd.DataFrame({"c": ["never_seen", "a1"]}))
    assert out.shape == (2, md.MAX_CATEGORIES)
    assert out.sum(axis=1).tolist() == [1.0, 1.0] or out.sum(axis=1).tolist() == [0.0, 1.0]


def test_free_text_is_left_out_with_the_reason_but_short_codes_stay():
    ds, pr = load(many())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("y"))
    note = next(e for e in rep.excluded if e["column"] == "note")
    assert "free text" in note["reason"] or "identifier" in note["reason"]
    assert "code" in [f["column"] for f in rep.features]  # 70 short codes, not sentences


def test_free_text_that_repeats_a_little_is_free_text_not_an_identifier():
    lines = ["note,x,y"]
    for i in range(300):
        lines.append(
            f'"great product but it arrived late and the box was damaged {i % 60}",{i % 7},{i % 5}'
        )
    ds, pr = load(("\n".join(lines) + "\n").encode())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("y"))
    assert "free text" in next(e for e in rep.excluded if e["column"] == "note")["reason"]


# -- unequal classes ---------------------------------------------------------------------------------


def skewed(n: int = 600, share: float = 0.05) -> bytes:
    rng = np.random.RandomState(3)
    lines = ["x,z,rare"]
    for _ in range(n):
        positive = rng.rand() < share
        x = rng.normal(1.0 if positive else 0.0, 1.0)
        lines.append(f"{x:.4f},{rng.normal():.4f},{'yes' if positive else 'no'}")
    return ("\n".join(lines) + "\n").encode()


def test_unequal_classes_get_balanced_weights_and_say_so():
    ds, pr = load(skewed())
    rep, _ = md.run_model(ds, pr, md.ModelSpec("rare"))
    assert "balanced" in rep.evaluation["class_weights"]
    assert any("very unequal" in w for w in rep.warnings)
    assert rep.verification.status == "verified"


def test_equal_classes_get_no_weights():
    ds, pr = load(skewed(share=0.5))
    rep, _ = md.run_model(ds, pr, md.ModelSpec("rare"))
    assert "class_weights" not in rep.evaluation


def test_weights_are_set_on_the_models_that_accept_them_only_when_asked():
    weighted = dict(md._estimators("classification", 0, True))
    plain = dict(md._estimators("classification", 0, False))
    for name in ("Logistic regression", "Decision tree", "Random forest"):
        assert weighted[name].class_weight == "balanced" and plain[name].class_weight is None
    assert not hasattr(weighted["Gradient boosting"], "class_weight")


def test_the_rare_class_is_found_more_often_with_weights():
    ds, pr = load(skewed(1500, 0.05))
    rep, _ = md.run_model(ds, pr, md.ModelSpec("rare"))
    X = pd.DataFrame({"x": ds.frame["x"].astype(float), "z": ds.frame["z"].astype(float)})
    y = ds.frame["rare"].to_numpy(dtype=object)
    from sklearn.base import clone

    def recall(balanced: bool) -> float:
        est = dict(md._estimators("classification", 0, balanced))["Logistic regression"]
        pipe = clone(md._pipeline(est, ["x", "z"], []))
        cut = 1100
        pipe.fit(X.iloc[:cut], y[:cut])
        pred = pipe.predict(X.iloc[cut:])
        truth = y[cut:] == "yes"
        return float(((pred == "yes") & truth).sum() / max(1, truth.sum()))

    assert recall(True) > recall(False)
    assert rep.models[0]["name"].startswith("Baseline")


# -- the notes ---------------------------------------------------------------------------------------


def texts(data: bytes) -> list[str]:
    s = start_session(data, "d.csv")
    return [f.text for f in s.workspace.findings]


def test_difficulty_notes_for_small_wide_many_valued_and_dated_files():
    small = texts(b"a,b\n1,2\n3,4\n5,6\n")
    assert any(t.startswith("Only 3 rows") for t in small)
    wide = texts(b"a,b,c,d,e\n1,2,3,4,5\n6,7,8,9,1\n")
    assert any("5 columns but only 2 rows" in t for t in wide)
    big = texts(many())
    assert any("Column shop has 40 different values" in t for t in big)
    assert any("holds dates" in t for t in texts(dated(200)))
    assert not any("holds dates" in t for t in texts(dated(60)))  # too few rows to model over time
