from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from sie.reference import ReferenceDataError, load_gender_aliases, normalise_key, seed_reference

REAL_REFERENCE = Path(__file__).resolve().parents[2] / "data" / "reference"


def _counts(conn):
    return {
        t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
        for t in (
            "competitions",
            "countries",
            "country_aliases",
            "sports",
            "disciplines",
            "sport_aliases",
        )
    }


def test_seed_loads_the_shipped_reference_files(engine):
    with engine.begin() as c:
        result = seed_reference(c, REAL_REFERENCE)
        counts = _counts(c)
    assert counts["countries"] == result.countries == 45
    assert counts["competitions"] == 1
    assert counts["sports"] == result.sports
    assert counts["country_aliases"] == result.country_aliases


def test_seed_is_idempotent(engine):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        first = _counts(c)
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        second = _counts(c)
    assert first == second


@pytest.mark.parametrize("alias", ["IND", "India", "Republic of India", "india 🇮🇳", "  INDIA "])
def test_every_spelling_of_india_resolves_to_one_country(engine, alias):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        code = c.execute(
            text(
                """SELECT c.code FROM country_aliases a JOIN countries c ON c.id = a.country_id
                   WHERE a.alias_norm = :k"""
            ),
            {"k": normalise_key(alias)},
        ).scalar_one()
    assert code == "IND"


@pytest.mark.parametrize(
    ("alias", "code"),
    [("Korea Republic", "KOR"), ("Hong Kong, China", "HKG"), ("Hong Kong", "HKG"),
     ("Timor-Leste", "TLS"), ("Timor Leste", "TLS"), ("Taiwan", "TPE"), ("Chinese Taipei", "TPE")],
)  # fmt: skip
def test_other_known_spellings(engine, alias, code):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        got = c.execute(
            text(
                """SELECT c.code FROM country_aliases a JOIN countries c ON c.id = a.country_id
                   WHERE a.alias_norm = :k"""
            ),
            {"k": normalise_key(alias)},
        ).scalar_one()
    assert got == code


def test_track_and_field_resolves_to_athletics_discipline(engine):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        row = c.execute(
            text(
                """SELECT s.name AS sport, d.name AS disc FROM sport_aliases a
                   JOIN sports s ON s.id = a.sport_id JOIN disciplines d ON d.id = a.discipline_id
                   WHERE a.alias_norm = :k"""
            ),
            {"k": normalise_key("Track and Field")},
        ).one()
    assert (row.sport, row.disc) == ("Athletics", "Athletics")


def test_swimming_resolves_inside_aquatics(engine):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        row = c.execute(
            text(
                """SELECT s.name AS sport, d.name AS disc FROM sport_aliases a
                   JOIN sports s ON s.id = a.sport_id JOIN disciplines d ON d.id = a.discipline_id
                   WHERE a.alias_norm = 'swimming'"""
            )
        ).one()
    assert (row.sport, row.disc) == ("Aquatics", "Swimming")


def test_boxing_is_flagged_double_bronze(engine):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        assert (
            c.execute(text("SELECT double_bronze FROM sports WHERE name = 'Boxing'")).scalar_one()
            is True
        )
        assert (
            c.execute(text("SELECT double_bronze FROM sports WHERE name = 'Archery'")).scalar_one()
            is False
        )


def test_official_event_total_is_left_unset(engine):
    with engine.begin() as c:
        seed_reference(c, REAL_REFERENCE)
        assert c.execute(text("SELECT official_event_total FROM competitions")).scalar_one() is None


# --- bad reference files fail loudly and change nothing -----------------------------------------


@pytest.fixture()
def ref_copy(tmp_path):
    dest = tmp_path / "reference"
    shutil.copytree(REAL_REFERENCE, dest)
    return dest


def test_alias_pointing_at_unknown_country_is_an_error(engine, ref_copy):
    with open(ref_copy / "country_aliases.csv", "a", encoding="utf-8") as fh:
        fh.write("Atlantis,ATL\n")
    with pytest.raises(ReferenceDataError, match="unknown country"), engine.begin() as c:
        seed_reference(c, ref_copy)


def test_one_alias_for_two_countries_is_an_error(engine, ref_copy):
    with open(ref_copy / "country_aliases.csv", "a", encoding="utf-8") as fh:
        fh.write("Korea,PRK\n")  # 'Korea' already means KOR
    with pytest.raises(ReferenceDataError, match="maps to both"), engine.begin() as c:
        seed_reference(c, ref_copy)


def test_discipline_of_unknown_sport_is_an_error(engine, ref_copy):
    with open(ref_copy / "disciplines.csv", "a", encoding="utf-8") as fh:
        fh.write("Quidditch,Seeker\n")
    with pytest.raises(ReferenceDataError, match="unknown sport"), engine.begin() as c:
        seed_reference(c, ref_copy)


def test_missing_column_is_an_error(engine, ref_copy):
    (ref_copy / "sports.csv").write_text("title\nArchery\n", encoding="utf-8")
    with pytest.raises(ReferenceDataError, match="missing columns"), engine.begin() as c:
        seed_reference(c, ref_copy)


def test_failed_seed_writes_nothing(engine, ref_copy):
    with open(ref_copy / "country_aliases.csv", "a", encoding="utf-8") as fh:
        fh.write("Atlantis,ATL\n")
    with pytest.raises(ReferenceDataError), engine.begin() as c:
        seed_reference(c, ref_copy)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM countries")).scalar_one() == 0


# --- gender aliases (used from Phase 2) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "gender"),
    [("Men's", "Men"), ("MALE", "Men"), ("m", "Men"), ("Women", "Women"), ("female", "Women"),
     ("Women’s", "Women"), ("Mixed", "Mixed"), ("Open", "Open")],
)  # fmt: skip
def test_gender_aliases(raw, gender):
    assert load_gender_aliases(REAL_REFERENCE)[normalise_key(raw)] == gender


def test_unknown_gender_is_not_guessed():
    assert normalise_key("Robots") not in load_gender_aliases(REAL_REFERENCE)
