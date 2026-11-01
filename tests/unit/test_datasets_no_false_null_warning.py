"""group_aggregate and top_n must not warn "no non-null values" when every group has values."""

from __future__ import annotations

import pandas as pd

from sie.datasets import from_frame, run_operation


def ds(values):
    return from_frame(pd.DataFrame({"party": ["BJP", "BJP", "INC"], "seats": values}), "t")


def test_no_warning_when_every_group_has_values():
    for spec in (
        {
            "op": "group_aggregate",
            "by": ["party"],
            "metrics": ["seats:sum", "seats:mean", "seats:min"],
        },
        {"op": "top_n", "by": "party", "metric": "seats:sum", "n": 5},
    ):
        result = run_operation(ds([10, 20, 5]), spec)
        assert [w for w in result.warnings if "no non-null values" in w] == []


def test_a_group_that_really_has_no_values_still_warns():
    result = run_operation(
        ds([10, 20, None]), {"op": "group_aggregate", "by": ["party"], "metrics": ["seats:sum"]}
    )
    assert any("no non-null values" in w for w in result.warnings)
