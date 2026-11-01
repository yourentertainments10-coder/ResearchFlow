"""Regression (found by the owner on the Titanic data): grouping by a whole-number column such as Pclass
made the independent check reject a correct result ("group (1,) does not exist"), and min/max over a
numeric column compared the values as text ("11" < "5"). Expected numbers are worked out by hand."""

from __future__ import annotations

import pandas as pd
import pytest

from sie.datasets import open_dataset, run_operation
from sie.datasets.models import GroupAggregate, Limits, LoadOptions, TopN
from sie.workspace import start_session
from sie.workspace.verify import verify_result

DATA = b"k,v,label\n1,5,a\n1,11,a\n2,100,b\n2,9,b\n,7,b\n"


def ds():
    return open_dataset(DATA, "ints.csv", LoadOptions(), Limits())


def table_of(session, request):
    step = session.ask(request)
    assert step.kind == "plan", step.message
    return session.items[-1], step


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("Sum v by k", {1: 16, 2: 109, "(null)": 7}),
        ("Average v by k", {1: 8.0, 2: 54.5, "(null)": 7.0}),
        ("Minimum v by k", {1: 5, 2: 9, "(null)": 7}),  # as text "100" < "9" would be wrong
        ("Maximum v by k", {1: 11, 2: 100, "(null)": 7}),  # as text "5" > "11" would be wrong
    ],
)
def test_integer_group_columns_verify_with_hand_computed_numbers(request_text, expected):
    s = start_session(DATA, "ints.csv")
    item, step = table_of(s, request_text)
    assert step.verification == "verified", item.verification.mismatches
    t = item.result.table
    assert dict(zip(t["k"], t[t.columns[-1]], strict=True)) == expected


def test_top_n_by_an_integer_column_verifies():
    s = start_session(DATA, "ints.csv")
    item, step = table_of(s, "Top 2 k by v")
    assert step.verification == "verified", item.verification.mismatches
    assert list(item.result.table["k"]) == [2, 1]  # sums 109 then 16


def test_the_check_still_catches_wrong_numbers_and_wrong_groups_for_integer_keys():
    data = ds()
    spec = GroupAggregate(by=["k"], metrics=["v:sum"])
    good = run_operation(data, spec).table
    assert verify_result(data, spec, good).status == "verified"
    tampered = good.copy()
    tampered.loc[tampered["k"] == 1, "v:sum"] = 17
    assert verify_result(data, spec, tampered).status == "unverified"
    renamed = good.copy()
    renamed["k"] = renamed["k"].astype(object)
    renamed.loc[renamed["k"] == 2, "k"] = 3
    assert verify_result(data, spec, renamed).status == "unverified"
    missing = good[good["k"] != 2]
    assert verify_result(data, spec, missing).status == "unverified"
    top = TopN(by="k", metric="v:sum", n=2)
    t = run_operation(data, top).table
    assert verify_result(data, top, t).status == "verified"
    bad = t.copy()
    bad.loc[0, "v:sum"] = 1
    assert verify_result(data, top, bad).status == "unverified"


def test_text_codes_with_leading_zeros_stay_text_and_still_verify():
    data = b"code,v\n007,1\n007,2\n010,4\n"
    s = start_session(data, "codes.csv")
    item, step = table_of(s, "Sum v by code")
    assert step.verification == "verified", item.verification.mismatches
    assert list(item.result.table["code"]) == [
        "010",
        "007",
    ]  # largest sum first (4 then 3); codes kept as text


def test_numbers_in_a_numeric_column_order_as_numbers_not_text():
    data = pd.DataFrame({"g": ["x", "x", "y"], "n": [11, 5, 100]}).to_csv(index=False).encode()
    s = start_session(data, "n.csv")
    item, step = table_of(s, "Minimum n by g")
    assert step.verification == "verified"
    assert dict(zip(item.result.table["g"], item.result.table["n:min"], strict=True)) == {
        "x": 5,
        "y": 100,
    }
