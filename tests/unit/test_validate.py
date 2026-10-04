"""Validation rules from docs/DATA_PIPELINE.md section 6 and docs/DOMAIN_MODEL.md (test cases 3, 4, 23, 24)."""

from __future__ import annotations

from datetime import date

import pytest
from pipeline_fixtures import (
    BOXING,
    BOXING_D,
    CHN,
    COMPETITION,
    IND,
    JPN,
    KOR,
    reference,
    row,
)

from sie.pipeline.models import Reason
from sie.pipeline.validate import is_complete, validate


def run(*rows):
    return validate(rows, reference(), COMPETITION)


def slots(outcome):
    return [(p.row.row_number, p.row.medal, p.slot) for p in outcome.placed]


def reasons(outcome):
    return {r.row_number: r.reason for r in outcome.rejections}


def test_a_normal_podium_gets_slot_one_for_each_medal():
    out = run(row(2, "Gold", IND), row(3, "Silver", KOR), row(4, "Bronze", CHN))
    assert slots(out) == [(2, "Gold", 1), (3, "Silver", 1), (4, "Bronze", 1)]
    assert not out.rejections and not out.warnings


def test_a_sweep_is_two_placings_for_one_country_in_an_individual_event():
    out = run(row(2, "Gold", KOR), row(3, "Silver", KOR), row(4, "Bronze", JPN))
    assert len(out.placed) == 3  # two country medals for KOR (docs/DOMAIN_MODEL.md case 24)


def test_exact_duplicate_rows_are_skipped_and_counted():
    out = run(row(2, "Gold", IND, entrant="A"), row(3, "Gold", IND, entrant="a"))
    assert len(out.placed) == 1
    assert out.duplicates_skipped == 1
    assert not out.rejections


def test_second_gold_without_a_tie_flag_is_rejected():
    out = run(row(2, "Gold", IND), row(3, "Gold", KOR))
    assert slots(out) == [(2, "Gold", 1)]
    assert reasons(out) == {3: Reason.MEDAL_COUNT_EXCEEDED}


def test_a_marked_tie_stores_two_placings_with_the_flag(test_case=23):
    out = run(
        row(2, "Gold", IND, is_tie=True), row(3, "Gold", KOR, is_tie=True), row(4, "Bronze", CHN)
    )
    assert slots(out) == [(2, "Gold", 1), (3, "Gold", 2), (4, "Bronze", 1)]
    assert all(p.row.is_tie for p in out.placed if p.row.medal == "Gold")


def test_a_tie_flag_on_only_one_of_the_two_rows_is_not_enough():
    out = run(row(2, "Silver", IND, is_tie=True), row(3, "Silver", KOR))
    assert reasons(out) == {3: Reason.MEDAL_COUNT_EXCEEDED}


def test_double_bronze_is_allowed_only_in_sports_flagged_for_it():
    boxing = {"sport": BOXING, "discipline": BOXING_D, "event": "Men's 57kg"}
    out = run(row(2, "Bronze", IND, **boxing), row(3, "Bronze", KOR, **boxing))
    assert slots(out) == [(2, "Bronze", 1), (3, "Bronze", 2)]
    assert not any(p.row.is_tie for p in out.placed)  # two bronzes are not a tie

    out = run(row(2, "Bronze", IND), row(3, "Bronze", KOR))  # archery: not flagged
    assert reasons(out) == {3: Reason.MEDAL_COUNT_EXCEEDED}


def test_a_third_placing_of_one_medal_is_rejected_even_in_a_double_bronze_sport():
    boxing = {"sport": BOXING, "discipline": BOXING_D, "event": "Men's 57kg"}
    out = run(*(row(n, "Bronze", c, **boxing) for n, c in ((2, IND), (3, KOR), (4, CHN))))
    assert reasons(out) == {4: Reason.MEDAL_COUNT_EXCEEDED}


def test_explicit_slots_are_kept_and_a_repeated_slot_is_a_conflict():
    out = run(
        row(2, "Bronze", IND, slot=2, sport=BOXING, discipline=BOXING_D),
        row(3, "Bronze", KOR, slot=2, sport=BOXING, discipline=BOXING_D),
    )
    assert slots(out) == [(2, "Bronze", 2)]
    assert reasons(out) == {3: Reason.SLOT_CONFLICT}


def test_team_event_allows_a_country_only_once_but_pairs_and_individuals_may_repeat():
    team = {"event": "Compound Team", "participation": "Team"}
    out = run(
        row(2, "Gold", IND, **team), row(3, "Silver", IND, **team), row(4, "Bronze", CHN, **team)
    )
    assert slots(out) == [(2, "Gold", 1), (4, "Bronze", 1)]
    assert reasons(out) == {3: Reason.DUPLICATE_COUNTRY_IN_TEAM_EVENT}

    pair = {"event": "Doubles", "participation": "Pair"}
    out = run(row(2, "Gold", CHN, **pair), row(3, "Silver", CHN, **pair))
    assert len(out.placed) == 2  # an all-Chinese final is legitimate


def test_rows_of_one_event_must_agree_on_participation():
    out = run(
        row(2, "Gold", IND, participation="Team"), row(3, "Silver", KOR, participation="Individual")
    )
    assert reasons(out) == {3: Reason.EVENT_PARTICIPATION_CONFLICT}


def test_events_are_validated_separately():
    out = run(row(2, "Gold", IND, event="E1"), row(3, "Gold", KOR, event="E2"))
    assert len(out.placed) == 2 and not out.rejections  # one gold in each, not two in one


def test_dates_outside_the_competition_warn_but_still_load():
    out = run(row(2, "Gold", IND, event_date=date(2026, 8, 1)))
    assert len(out.placed) == 1
    assert "outside the competition dates" in out.warnings[0]
    assert not run(row(2, "Gold", IND, event_date=date(2026, 9, 25))).warnings


def test_several_dates_for_one_event_warn():
    out = run(
        row(2, "Gold", IND, event_date=date(2026, 9, 25)),
        row(3, "Silver", KOR, event_date=date(2026, 9, 26)),
    )
    assert any("several dates" in w for w in out.warnings)


@pytest.mark.parametrize(
    ("gold", "silver", "bronze", "complete"),
    [
        (1, 1, 1, True),
        (1, 1, 2, True),  # double bronze
        (1, 1, 0, False),
        (1, 0, 0, False),
        (0, 0, 0, False),
        (2, 0, 1, True),  # tie for gold: silver is omitted
        (2, 0, 0, False),
        (1, 2, 0, True),  # tie for silver: bronze is omitted
    ],
)
def test_completeness_is_tie_aware(gold, silver, bronze, complete):
    assert is_complete(gold, silver, bronze) is complete


def test_the_output_keeps_file_order():
    out = run(row(5, "Bronze", CHN), row(2, "Gold", IND), row(3, "Silver", KOR))
    assert [p.row.row_number for p in out.placed] == [2, 3, 5]
