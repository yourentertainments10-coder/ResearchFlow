"""``build_tables`` and the reconciliation helpers, on frames (no database)."""

from __future__ import annotations

import pandas as pd

from sie.analytics.analyze import (
    QUARANTINE_COLUMNS,
    SPEC_TABLES,
    build_tables,
    not_run_reconciliation,
    reconcile_with_official,
    reconciliation_status,
)

COLUMNS = [
    "country_code",
    "country_name",
    "sport",
    "discipline_name",
    "event_id",
    "gender",
    "medal",
]
DF = pd.DataFrame(
    [
        ("IND", "India", "Archery", "Archery", 1, "Men", "Gold"),
        ("KOR", "Korea", "Archery", "Archery", 1, "Men", "Silver"),
        ("CHN", "China", "Archery", "Archery", 1, "Men", "Bronze"),
        ("IND", "India", "Archery", "Archery", 2, "Women", "Gold"),
        ("IND", "India", "Archery", "Archery", 2, "Women", "Silver"),
        ("CHN", "China", "Boxing", "Boxing", 3, "Men", "Gold"),
        ("KOR", "Korea", "Boxing", "Boxing", 3, "Men", "Bronze"),
    ],
    columns=COLUMNS,
)
EVENTS = pd.DataFrame(
    {
        "sport": ["Archery", "Archery", "Boxing"],
        "discipline": ["Archery", "Archery", "Boxing"],
        "status": ["completed"] * 3,
    }
)


def official(counts: dict[str, tuple[dict, dict, dict]]) -> list[dict]:
    return [
        {"Org": code, "Count": {"ME_GOLD": g, "ME_SILVER": s, "ME_BRONZE": b}}
        for code, (g, s, b) in counts.items()
    ]


MATCHING = official(
    {
        "IND": ({"M": 1, "W": 1}, {"W": 1}, {}),
        "KOR": ({}, {"M": 1}, {"M": 1}),
        "CHN": ({"M": 1}, {}, {"M": 1}),
    }
)


def test_every_spec_table_is_built_in_order():
    tables = build_tables(
        DF,
        EVENTS,
        reconciliation=not_run_reconciliation(),
        quarantine=pd.DataFrame(columns=QUARANTINE_COLUMNS),
        changes=pd.DataFrame(),
        trajectory=pd.DataFrame(),
    )
    assert list(tables)[: len(SPEC_TABLES)] == list(SPEC_TABLES)
    assert (
        len(tables["country_summary"]) == 3 and int(tables["country_summary"]["Total"].sum()) == 7
    )
    assert tables["reconciliation"]["status"].tolist() == ["not_run"]


def test_reconciliation_against_the_official_table_matches_and_names_a_mismatch():
    ok = reconcile_with_official(DF, MATCHING)
    assert reconciliation_status(ok) == "reconciled" and ok["matches"].all()

    wrong = [dict(r) for r in MATCHING]
    wrong[0] = {**wrong[0], "Count": {**wrong[0]["Count"], "ME_GOLD": {"M": 2, "W": 1}}}
    bad = reconcile_with_official(DF, wrong)
    assert reconciliation_status(bad) == "mismatch"
    assert bad.loc[bad["status"] == "mismatch", "country_code"].tolist() == ["IND"]


def test_a_country_missing_from_the_official_table_is_a_mismatch_not_a_gap_filled_with_zero():
    short = [r for r in MATCHING if r["Org"] != "KOR"]
    t = reconcile_with_official(DF, short).set_index("country_code")
    assert t.loc["KOR", "status"] == "mismatch" and pd.isna(t.loc["KOR", "gold_official"])


def test_an_unrun_reconciliation_is_reported_as_not_run_and_never_as_a_pass():
    assert reconciliation_status(not_run_reconciliation()) == "not_run"
