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


def test_saved_data_that_names_another_year_or_country_is_not_reused():
    q = "UK general election 2024 constituency results"
    old = {**META, "page_url": "https://x.example.gov.uk/ge-2019-constituency-results.csv",
           "extra": {"title": "UK general election 2019 constituency results", "flags": []}}  # fmt: skip
    d = assess(old, q, {"op": "describe"}, now=NOW, max_age_days=30)
    assert d and not d.sufficient and "year mismatch" in d.reasons[0]
    us = {**old, "page_url": "https://x.example.org/us-ge-2024-constituency-results.csv",
          "extra": {"title": "US general election 2024 constituency results", "flags": []}}  # fmt: skip
    d = assess(us, q, {"op": "describe"}, now=NOW, max_age_days=30)
    assert d and not d.sufficient and "geography mismatch" in d.reasons[0]


def test_saved_data_that_does_not_state_place_or_year_is_reused_with_that_disclosed():
    q = "UK general election 2024 constituency results"
    m = {**META, "columns": ["constituency", "party", "votes"],
         "page_url": "https://x.example.org/election-results.csv",
         "extra": {"title": "csv", "flags": []}}  # fmt: skip
    d = assess(m, q, {"op": "describe"}, now=NOW, max_age_days=30)
    assert d and d.sufficient
    assert any("geography not stated" in r for r in d.reasons)
    assert any("year not stated" in r for r in d.reasons)
