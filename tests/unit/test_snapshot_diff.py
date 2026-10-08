"""Change detection between two snapshots (ANALYTICS_SPEC section 10), without a database."""

from __future__ import annotations

from sie.analytics.snapshots import diff_snapshots


def kinds(changes):
    return sorted(
        (c["change_type"], c["country_id"], c["sport_id"], c["gender"], c["detail"])
        for c in changes
    )


def test_nothing_changed_means_no_changes():
    rows = {(1, 1, "Men"): (1, 0, 0), (2, 1, "Men"): (0, 1, 0)}
    assert diff_snapshots(rows, dict(rows)) == []


def test_a_new_medal_is_gained_and_a_first_medal_in_a_sport_is_flagged():
    before = {(1, 1, "Men"): (1, 0, 0)}
    after = {(1, 1, "Men"): (2, 0, 0), (2, 1, "Men"): (0, 1, 0), (1, 2, "Women"): (0, 0, 1)}
    assert kinds(diff_snapshots(before, after)) == [
        ("medals_gained", 1, 1, "Men", "Gold +1"),
        ("medals_gained", 1, 2, "Women", "Bronze +1"),
        ("medals_gained", 2, 1, "Men", "Silver +1"),
        ("new_sport", 1, 2, None, "first medal in this sport"),
        ("new_sport", 2, 1, None, "first medal in this sport"),
    ]


def test_a_reallocation_is_a_loss_for_one_country_and_a_gain_for_another_with_rank_moves():
    before = {(1, 1, "Men"): (2, 0, 0), (2, 1, "Men"): (1, 0, 0)}
    after = {(1, 1, "Men"): (0, 0, 0), (2, 1, "Men"): (3, 0, 0)}
    found = kinds(diff_snapshots(before, after))
    assert ("medals_lost", 1, 1, "Men", "Gold -2") in found
    assert ("medals_gained", 2, 1, "Men", "Gold +2") in found
    assert ("rank_change", 1, None, None, "podium rank 1 -> 2") in found
    assert ("rank_change", 2, None, None, "podium rank 2 -> 1") in found


def test_one_cell_can_gain_one_medal_type_and_lose_another():
    found = kinds(diff_snapshots({(1, 1, "Men"): (1, 0, 0)}, {(1, 1, "Men"): (0, 1, 0)}))
    assert ("medals_gained", 1, 1, "Men", "Silver +1") in found
    assert ("medals_lost", 1, 1, "Men", "Gold -1") in found
