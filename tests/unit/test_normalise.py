"""Normalisation: names resolve through reference data, nothing is auto-created (test cases 1, 2, 6)."""

from __future__ import annotations

from datetime import date

import pytest
from pipeline_fixtures import (
    AQUATICS,
    ARCHERY,
    ARCHERY_D,
    COMPETITION,
    IND,
    SWIMMING_D,
    parsed,
    reference,
)

from sie.pipeline.models import NormalisedResult, Reason, Rejection
from sie.pipeline.normalise import normalise


def run(**fields: str) -> NormalisedResult | Rejection:
    return normalise(parsed(**fields), reference(), COMPETITION)


def rejected(**fields: str) -> Rejection:
    result = run(**fields)
    assert isinstance(result, Rejection), result
    return result


def accepted(**fields: str) -> NormalisedResult:
    result = run(**fields)
    assert isinstance(result, NormalisedResult), result
    return result


@pytest.mark.parametrize("country", ["IND", "India", "Republic of India", " india ", "India 🇮🇳"])
def test_every_spelling_of_india_resolves_to_one_country(country):
    assert accepted(country=country).country_id == IND


def test_unknown_country_is_rejected_not_created():
    r = rejected(country="Atlantis")
    assert r.reason is Reason.UNKNOWN_COUNTRY
    assert r.row_number == 2
    assert r.row.country == "Atlantis"  # the original row is kept for the quarantine payload


@pytest.mark.parametrize("country", ["IND;KOR", "IND|KOR"])
def test_a_cell_naming_two_countries_is_a_multi_country_credit(country):
    assert rejected(country=country).reason is Reason.MULTI_COUNTRY_ENTRANT


@pytest.mark.parametrize("medal", ["Gold", "gold", " GOLD "])
def test_medal_spellings(medal):
    assert accepted(medal=medal).medal == "Gold"


@pytest.mark.parametrize("medal", ["Platinum", "1st", "G"])
def test_invalid_medal(medal):
    assert rejected(medal=medal).reason is Reason.INVALID_MEDAL


@pytest.mark.parametrize("field", ["competition", "sport", "event", "medal", "country"])
def test_each_required_field_is_required(field):
    r = rejected(**{field: ""})
    assert r.reason is Reason.MISSING_FIELD
    assert field in r.detail


def test_row_for_another_competition_is_a_mismatch_unless_it_is_unknown():
    assert rejected(competition="other-games").reason is Reason.UNKNOWN_COMPETITION
    assert accepted(competition="ASIAD 2026").competition_id == COMPETITION.id


# --- sport and discipline ---------------------------------------------------------------------------


def test_sport_alias_and_same_named_discipline():
    r = accepted(sport="Track and Field", event="Men's 100m")
    assert (r.sport_id, r.discipline_id) == (13, 130)


def test_discipline_column_resolves_inside_a_multi_discipline_sport():
    r = accepted(sport="Aquatics", discipline="Swimming")
    assert (r.sport_id, r.discipline_id) == (AQUATICS, SWIMMING_D)


def test_a_discipline_name_in_the_sport_column_is_enough():
    assert accepted(sport="Swimming").discipline_id == SWIMMING_D


def test_sport_with_several_disciplines_needs_one_named():
    r = rejected(sport="Aquatics")
    assert r.reason is Reason.UNKNOWN_DISCIPLINE
    assert "several disciplines" in r.detail


def test_discipline_of_another_sport_is_a_mismatch():
    assert (
        rejected(sport="Archery", discipline="Swimming").reason is Reason.SPORT_DISCIPLINE_MISMATCH
    )


def test_unknown_sport_and_unknown_discipline():
    assert rejected(sport="Underwater Chess").reason is Reason.UNKNOWN_SPORT
    assert rejected(sport="Aquatics", discipline="Curling").reason is Reason.UNKNOWN_DISCIPLINE


def test_a_sport_with_one_discipline_needs_no_discipline_column():
    assert accepted(sport="Archery").discipline_id == ARCHERY_D
    assert accepted(sport="Archery").sport_id == ARCHERY


# --- gender -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "event", "expected"),
    [
        ("Men", "Whatever", "Men"),
        ("Male", "Whatever", "Men"),
        ("W", "Whatever", "Women"),
        ("", "Recurve Men's Individual", "Men"),
        ("", "Women's 100m", "Women"),
        ("", "Mixed Doubles", "Mixed"),
        ("", "Men's Open", "Men"),  # a men's event, not an open one
        ("", "Skeet Open", "Open"),
        ("", "Men's 4 x 100m Relay", "Men"),  # the single letter x is never read as Mixed
    ],
)
def test_gender_comes_from_the_column_then_the_event_name(column, event, expected):
    assert accepted(gender=column, event=event).gender == expected


@pytest.mark.parametrize(
    ("column", "event"),
    [("Robots", "E"), ("", "100m"), ("", "Men and Women combined"), ("", "4 x 100m Relay")],
)
def test_ambiguous_or_missing_gender_is_rejected_not_guessed(column, event):
    assert rejected(gender=column, event=event).reason is Reason.UNKNOWN_GENDER


# --- participation ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "entrant", "column", "expected"),
    [
        ("Compound Women's Team", "", "", "Team"),
        ("Men's 4 x 100m Relay", "", "", "Team"),
        ("Mixed Doubles", "", "", "Pair"),
        ("Recurve Men's Individual", "KIM Jongho", "", "Individual"),
        ("Men's Kabaddi", "India", "", "Team"),  # the entrant is the country itself
        ("Men's Kabaddi", "Pro Kabaddi XI", "Team", "Team"),  # explicit column wins
        ("Compound Women's Team", "", "individual", "Individual"),
    ],
)
def test_participation_rules(event, entrant, column, expected):
    assert accepted(event=event, entrant=entrant, participation=column).participation == expected


def test_invalid_participation_is_rejected():
    assert rejected(participation="Squad").reason is Reason.INVALID_PARTICIPATION


# --- slot, tie flag, date ----------------------------------------------------------------------------


def test_slot_tie_and_date_are_converted():
    r = accepted(slot="2", is_tie="Yes", date="2026-09-30", entrant="  KIM   Jongho ")
    assert (r.slot, r.is_tie, r.event_date, r.entrant) == (2, True, date(2026, 9, 30), "KIM Jongho")
    assert accepted().slot is None and accepted().is_tie is False and accepted().event_date is None


@pytest.mark.parametrize("slot", ["0", "-1", "1.5", "two", "²"])
def test_invalid_slot(slot):
    assert rejected(slot=slot).reason is Reason.INVALID_SLOT


def test_invalid_flag_and_date():
    assert rejected(is_tie="maybe").reason is Reason.INVALID_FLAG
    assert rejected(date="30/09/2026").reason is Reason.INVALID_DATE
    assert rejected(date="2026-13-40").reason is Reason.INVALID_DATE


def test_event_name_is_cleaned_and_the_raw_text_is_kept():
    r = accepted(event="  Recurve   Men's  Individual ")
    assert r.event_name == "Recurve Men's Individual"
    assert r.event_name_raw == "  Recurve   Men's  Individual "
