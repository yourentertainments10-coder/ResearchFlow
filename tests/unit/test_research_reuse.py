"""Saved data is reused only when it can answer this question and operation."""

from __future__ import annotations

from datetime import timedelta

from research_fakes import NOW
from sie.research.reuse import assess, required_columns

META = {
    "key": "k1", "state": "active", "columns": ["party", "seats", "votes"],
    "page_url": "https://data.example.gov.uk/election/results.csv", "table_caption": "csv",
    "retrieved_at": NOW.isoformat(), "extra": {"title": "General election results", "flags": []},
}  # fmt: skip
Q = "how many seats did each party win in the general election"


def test_required_columns_read_every_operation_shape():
    assert required_columns({"op": "top_n", "by": "party", "metric": "seats:sum"}) == {
        "party",
        "seats",
    }
    assert required_columns({"op": "group_aggregate", "by": ["a", "b"], "metrics": ["c:mean"]}) == {
        "a",
        "b",
        "c",
    }
    assert required_columns({"op": "time_trend", "date_column": "d", "metric": "rows"}) == {"d"}
    assert required_columns({"op": "describe"}) == set()


def test_fresh_covering_data_with_the_columns_is_sufficient():
    d = assess(
        META, Q, {"op": "top_n", "by": "party", "metric": "seats:sum"}, now=NOW, max_age_days=30
    )
    assert d and d.sufficient and not d.stale


def test_a_missing_column_makes_it_insufficient_with_the_reason():
    d = assess(
        META, Q, {"op": "top_n", "by": "party", "metric": "turnout:mean"}, now=NOW, max_age_days=30
    )
    assert d and not d.sufficient and "turnout" in d.reasons[0]


def test_old_data_is_stale_not_sufficient():
    d = assess(META, Q, {"op": "describe"}, now=NOW + timedelta(days=45), max_age_days=30)
    assert d and d.stale and not d.sufficient and "45 days" in d.reasons[0]


def test_an_unrelated_or_expired_dataset_is_not_considered():
    assert assess(META, "rainfall in kerala", {"op": "describe"}, now=NOW, max_age_days=30) is None
    assert (
        assess({**META, "state": "expired"}, Q, {"op": "describe"}, now=NOW, max_age_days=30)
        is None
    )


def test_disclosed_limitations_travel_with_a_sufficient_dataset():
    m = {**META, "extra": {"title": "General election results", "flags": ["rejected_rows:2"]}}
    d = assess(m, Q, {"op": "describe"}, now=NOW, max_age_days=30)
    assert d.sufficient and "rejected_rows:2" in d.reasons[-1]
