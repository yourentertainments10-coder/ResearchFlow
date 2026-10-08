"""ANALYTICS_SPEC formulas on a frame small enough to check by hand."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from sie.analytics import metrics
from sie.analytics.insights import build_insights
from sie.analytics.invariants import InvariantError, assert_invariants, check_invariants

COLUMNS = [
    "country_code",
    "country_name",
    "sport",
    "discipline_name",
    "event_id",
    "gender",
    "medal",
]
ROWS = [
    ("IND", "India", "Archery", "Archery", 1, "Men", "Gold"),
    ("KOR", "Korea", "Archery", "Archery", 1, "Men", "Silver"),
    ("CHN", "China", "Archery", "Archery", 1, "Men", "Bronze"),
    ("IND", "India", "Archery", "Archery", 2, "Women", "Gold"),
    ("IND", "India", "Archery", "Archery", 2, "Women", "Silver"),
    ("CHN", "China", "Boxing", "Boxing", 3, "Men", "Gold"),
    ("KOR", "Korea", "Boxing", "Boxing", 3, "Men", "Bronze"),
]
DF = pd.DataFrame(ROWS, columns=COLUMNS)


def row(frame: pd.DataFrame, **match):
    mask = pd.Series(True, index=frame.index)
    for k, v in match.items():
        mask &= frame[k] == v
    assert mask.sum() == 1, match
    return frame[mask].iloc[0]


def test_country_summary_counts_points_and_ranks():
    t = metrics.country_summary(DF).set_index("country_code")
    assert t.loc["IND", ["Gold", "Silver", "Bronze", "Total", "Points"]].tolist() == [2, 1, 0, 3, 8]
    assert t.loc["CHN", "Points"] == 4 and t.loc["KOR", "Points"] == 3
    assert t["podium_rank"].to_dict() == {"IND": 1, "CHN": 2, "KOR": 3}
    assert t["total_rank"].to_dict() == {"IND": 1, "CHN": 2, "KOR": 2}  # equal totals share a rank
    assert t.loc["IND", "gold_ratio"] == pytest.approx(2 / 3)


def test_shares_and_market_share():
    t = metrics.country_sport(DF)
    ind = row(t, country_code="IND", sport="Archery")
    assert ind["share_of_country"] == 1.0
    assert ind["market_share"] == pytest.approx(3 / 5)
    assert ind["gold_market_share"] == 1.0  # both archery golds
    chn_boxing = row(t, country_code="CHN", sport="Boxing")
    assert chn_boxing["gold_market_share"] == 1.0 and chn_boxing["market_share"] == 0.5


def test_gold_market_share_is_null_where_nobody_won_gold():
    only_silver = DF[DF["medal"] == "Silver"]
    t = metrics.country_sport(only_silver)
    assert t["gold_market_share"].isna().all()  # division by zero is null, never zero


def test_concentration_hhi_effective_number_and_labels():
    t = metrics.concentration(DF).set_index("country_code")
    assert t.loc["IND", "hhi"] == 1.0 and t.loc["IND", "effective_units"] == 1.0
    assert t.loc["IND", "label"] == "concentrated"
    assert t.loc["CHN", "hhi"] == 0.5 and t.loc["CHN", "effective_units"] == 2.0
    assert t.loc["CHN", "top1_share"] == 0.5 and t.loc["CHN", "top3_share"] == 1.0
    assert t.loc["CHN", "entropy"] == pytest.approx(math.log(2))
    assert t.loc["CHN", "sports_with_gold"] == 1 and t.loc["KOR", "sports_with_gold"] == 0
    assert bool(t.loc["CHN", "small_sample"]) and not bool(t.loc["IND", "small_sample"])


@pytest.mark.parametrize(
    ("hhi", "label"),
    [(0.26, "concentrated"), (0.25, "moderate"), (0.10, "moderate"), (0.09, "diversified")],
)
def test_hhi_label_thresholds(hhi, label):
    assert metrics.hhi_label(hhi) == label


def test_rca_matches_the_formula_and_hides_tiny_cells():
    t = metrics.rca(DF)
    ind = row(t, country_code="IND", sport="Archery")
    assert ind["rca"] == pytest.approx((3 / 3) / (5 / 7))  # 1.4
    assert math.isnan(row(t, country_code="CHN", sport="Boxing")["rca"])  # one medal: hidden
    assert bool(row(t, country_code="CHN", sport="Boxing")["small_sample"])


def test_conversion_counts_medals_per_completed_event_and_sweeps():
    t = metrics.conversion(DF)
    ind = row(t, country_code="IND", sport="Archery")
    assert ind["completed_events"] == 2 and ind["event_conversion"] == pytest.approx(1.5)
    assert ind["gold_conversion"] == pytest.approx(1.0) and ind["podium_sweeps"] == 1
    assert row(t, country_code="KOR", sport="Archery")["podium_sweeps"] == 0


def test_completion_flags_partial_units_and_feeds_conversion():
    events = pd.DataFrame(
        {
            "sport": ["Archery", "Archery", "Archery", "Boxing"],
            "discipline": ["Archery", "Archery", "Archery", "Boxing"],
            "status": ["completed", "completed", "scheduled", "completed"],
        }
    )
    done = metrics.unit_completion(events, "sport").set_index("sport")
    assert done.loc["Archery", "events_total"] == 3 and done.loc["Archery", "events_completed"] == 2
    assert bool(done.loc["Archery", "partial"]) and not bool(done.loc["Boxing", "partial"])
    t = metrics.conversion(DF, "sport", metrics.unit_completion(events, "sport"))
    assert row(t, country_code="IND", sport="Archery")["completed_events"] == 2
    assert bool(row(t, country_code="IND", sport="Archery")["partial"])


def test_tiers_use_pareto_cut_offs_and_list_sports_with_no_medals():
    t = metrics.tiers(DF)
    chn = t[t["country_code"] == "CHN"].set_index("sport")
    assert chn.loc["Boxing", "tier"] == "Core"  # 3 of 4 points
    assert chn.loc["Archery", "tier"] == "Secondary"  # points before it: 75 percent
    ind = t[t["country_code"] == "IND"].set_index("sport")
    assert ind.loc["Boxing", "tier"] == "No medals" and ind.loc["Boxing", "unit_events"] == 1
    assert bool(ind.loc["Archery", "gold_heavy"])  # 2 golds of 3, at least 3 medals
    assert not bool(ind.loc["Archery", "specialised"])  # RCA 1.4 is under 1.5


def test_gender_summary_keeps_categories_apart_and_hides_small_gaps():
    t = metrics.gender_summary(DF).set_index("country_code")
    assert t.loc["IND", "Women_medals"] == 2 and t.loc["IND", "Men_medals"] == 1
    assert t.loc["IND", "gender_gap"] == pytest.approx(5 / 8)
    assert math.isnan(t.loc["CHN", "gender_gap"])  # fewer than 3 men's and women's medals together
    assert t.loc["IND", "women_share"] == pytest.approx(2 / 3)
    assert t.loc["IND", "Mixed_medals"] == 0 and t.loc["IND", "Open_medals"] == 0


def test_open_is_a_category_of_its_own():
    df = pd.concat(
        [
            DF,
            pd.DataFrame(
                [("IND", "India", "Esports", "Esports", 9, "Open", "Gold")], columns=COLUMNS
            ),
        ]
    )
    t = metrics.gender_summary(df).set_index("country_code")
    assert t.loc["IND", "Open_medals"] == 1 and t.loc["IND", "Mixed_medals"] == 0
    assert t.loc["IND", "Women_medals"] == 2


def test_women_only_version_is_the_same_function_on_a_subset():
    women = metrics.country_summary(DF[DF["gender"] == "Women"])
    assert women["country_code"].tolist() == ["IND"] and int(women["Total"].iloc[0]) == 2


def test_sport_summary_has_hhi_leader_and_gender_split():
    t = metrics.sport_summary(DF).set_index("sport")
    assert t.loc["Archery", "Total"] == 5 and t.loc["Archery", "countries"] == 3
    assert t.loc["Archery", "leader"] == "IND" and t.loc[
        "Archery", "leader_share"
    ] == pytest.approx(0.6)
    assert t.loc["Archery", "hhi"] == pytest.approx(0.6**2 + 0.2**2 + 0.2**2)
    assert t.loc["Archery", "Women_medals"] == 2 and t.loc["Boxing", "women_share"] == 0


def all_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "country_summary": metrics.country_summary(df),
        "country_sport": metrics.country_sport(df),
        "country_sport_gender": metrics.country_sport_gender(df),
        "sport_summary": metrics.sport_summary(df),
        "gender_summary": metrics.gender_summary(df),
        "concentration": metrics.concentration(df),
    }


def test_invariants_hold_on_the_sample():
    assert check_invariants(DF, **all_tables(DF)) == []


def test_invariants_catch_a_broken_table():
    tables = all_tables(DF)
    tables["country_sport"] = tables["country_sport"].iloc[:-1]
    with pytest.raises(InvariantError, match="country_sport"):
        assert_invariants(DF, **tables)


def test_insights_are_templates_over_the_tables_and_skip_small_samples():
    tables = all_tables(DF)
    out = build_insights(
        country_sport=tables["country_sport"],
        concentration=tables["concentration"],
        rca=metrics.rca(DF),
        gender_summary=tables["gender_summary"],
        women_country_sport=metrics.country_sport(DF[DF["gender"] == "Women"]),
    )
    assert out["country_code"].unique().tolist() == ["IND"]  # CHN and KOR have 2 medals each
    kinds = dict(zip(out["kind"], out["text"], strict=True))
    assert kinds["dependence"] == (
        "India: 100% of medals come from Archery; effective number of sports is 1.0 (concentrated)."
    )
    assert kinds["women"] == "India's women contribute 67% of medals, led by Archery."
    assert "specialisation" not in kinds  # RCA 1.4 is under 1.5
