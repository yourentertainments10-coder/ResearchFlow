"""Database rules from docs/DOMAIN_MODEL.md and docs/DATABASE.md, tested on real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from sie.db.placings import apply_placing

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)


@pytest.fixture()
def world(engine):
    """Minimal reference world: 3 countries, one event, one raw version."""
    with engine.begin() as c:
        comp = c.execute(
            text(
                "INSERT INTO competitions (code, name, timezone) VALUES ('t','T','Asia/Tokyo') RETURNING id"
            )
        ).scalar_one()
        country = {
            code: c.execute(
                text("INSERT INTO countries (code, name) VALUES (:c, :c) RETURNING id"), {"c": code}
            ).scalar_one()
            for code in ("IND", "KOR", "CHN")
        }
        sport = c.execute(
            text("INSERT INTO sports (name) VALUES ('Archery') RETURNING id")
        ).scalar_one()
        disc = c.execute(
            text("INSERT INTO disciplines (sport_id, name) VALUES (:s, 'Archery') RETURNING id"),
            {"s": sport},
        ).scalar_one()
        event = c.execute(
            text(
                """INSERT INTO events (competition_id, discipline_id, name, name_raw, gender,
                                       participation, status)
                   VALUES (:c, :d, 'Compound Team', 'Compound Team', 'Women', 'Team', 'completed')
                   RETURNING id"""
            ),
            {"c": comp, "d": disc},
        ).scalar_one()
        doc = c.execute(
            text(
                "INSERT INTO raw_documents (source, url, first_seen_at) VALUES ('official','u',:t) RETURNING id"
            ),
            {"t": T0},
        ).scalar_one()
        rv = c.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                   VALUES (:d, 1, 'aa', 'p', :t) RETURNING id"""
            ),
            {"d": doc, "t": T0},
        ).scalar_one()
    return SimpleNamespace(comp=comp, country=country, event=event, rv=rv, doc=doc, disc=disc)


def _apply(conn, w, medal, slot, code, now=T0, entrant=None, **kw):
    return apply_placing(
        conn, event_id=w.event, medal=medal, slot=slot, country_id=w.country[code],
        entrant_id=entrant, raw_version_id=w.rv, source="official", now=now, **kw,
    )  # fmt: skip


def _count(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


# --- placings: creation, uniqueness, double bronze ----------------------------------------------


def test_second_current_placing_in_same_slot_is_rejected(engine, world):
    with engine.begin() as c:
        _apply(c, world, "Gold", 1, "IND")
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO placings (event_id, medal, slot, country_id, valid_from, raw_version_id, source)
                   VALUES (:e, 'Gold', 1, :c, :t, :rv, 'x')"""
            ),
            {"e": world.event, "c": world.country["KOR"], "t": T0, "rv": world.rv},
        )


def test_double_bronze_uses_two_slots(engine, world):
    with engine.begin() as c:
        assert _apply(c, world, "Bronze", 1, "CHN") == "created"
        assert _apply(c, world, "Bronze", 2, "KOR") == "created"
        assert _count(c, "SELECT count(*) FROM v_medal_facts WHERE medal = 'Bronze'") == 2


# --- reallocation (docs/TESTING.md case 5) ------------------------------------------------------


def test_reallocation_keeps_the_audit_trail_without_breaking_uniqueness(engine, world):
    later = T0 + timedelta(days=3)
    with engine.begin() as c:
        assert _apply(c, world, "Gold", 1, "IND") == "created"
    with engine.begin() as c:
        assert _apply(c, world, "Gold", 1, "KOR", now=later, reason="doping DQ") == "reallocated"

    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, country_id, is_current, valid_to, supersedes_id FROM placings ORDER BY id"
            )
        ).all()
        assert len(rows) == 2
        old, new = rows
        assert (old.is_current, old.country_id) == (False, world.country["IND"])
        assert old.valid_to == later
        assert (new.is_current, new.country_id) == (True, world.country["KOR"])
        assert new.valid_to is None and new.supersedes_id == old.id

        history = c.execute(
            text("SELECT change_type, reason FROM placing_history ORDER BY id")
        ).all()
        assert [h.change_type for h in history] == ["created", "reallocated"]
        assert history[1].reason == "doping DQ"

        # Only the current version counts anywhere in analytics.
        facts = c.execute(text("SELECT country_code FROM v_medal_facts WHERE medal = 'Gold'")).all()
        assert [f.country_code for f in facts] == ["KOR"]


def test_replaying_the_same_input_changes_nothing(engine, world):
    with engine.begin() as c:
        _apply(c, world, "Gold", 1, "IND")
    with engine.begin() as c:
        assert _apply(c, world, "Gold", 1, "IND", now=T0 + timedelta(hours=1)) == "unchanged"
        assert _count(c, "SELECT count(*) FROM placings") == 1
        assert _count(c, "SELECT count(*) FROM placing_history") == 1


def test_a_failed_transaction_leaves_no_half_reallocation(engine, world):
    with engine.begin() as c:
        _apply(c, world, "Gold", 1, "IND")
    with pytest.raises(RuntimeError), engine.begin() as c:
        _apply(c, world, "Gold", 1, "KOR", now=T0 + timedelta(days=1))
        raise RuntimeError("crash after the reallocation, before commit")
    with engine.connect() as c:
        assert _count(c, "SELECT count(*) FROM placings") == 1
        assert _count(c, "SELECT country_id FROM placings WHERE is_current") == world.country["IND"]
        assert _count(c, "SELECT count(*) FROM placing_history") == 1


def test_same_country_with_a_different_entrant_is_a_correction(engine, world):
    with engine.begin() as c:
        e1 = c.execute(
            text(
                """INSERT INTO entrants (competition_id, country_id, kind, name)
                   VALUES (:comp, :c, 'Team', 'India A') RETURNING id"""
            ),
            {"comp": world.comp, "c": world.country["IND"]},
        ).scalar_one()
        e2 = c.execute(
            text(
                """INSERT INTO entrants (competition_id, country_id, kind, name)
                   VALUES (:comp, :c, 'Team', 'India B') RETURNING id"""
            ),
            {"comp": world.comp, "c": world.country["IND"]},
        ).scalar_one()
        _apply(c, world, "Gold", 1, "IND", entrant=e1)
        assert (
            _apply(c, world, "Gold", 1, "IND", entrant=e2, now=T0 + timedelta(hours=2))
            == "corrected"
        )
        assert _count(c, "SELECT count(*) FROM placings WHERE is_current") == 1


# --- domain invariants --------------------------------------------------------------------------


def test_entrant_country_must_match_placing_country(engine, world):
    """Invariant I1, enforced by the composite foreign key."""
    with engine.begin() as c:
        korean = c.execute(
            text(
                """INSERT INTO entrants (competition_id, country_id, kind, name)
                   VALUES (:comp, :c, 'Team', 'Korea A') RETURNING id"""
            ),
            {"comp": world.comp, "c": world.country["KOR"]},
        ).scalar_one()
    with pytest.raises(IntegrityError), engine.begin() as c:
        _apply(c, world, "Gold", 1, "IND", entrant=korean)  # India credited, Korean entrant


def test_unknown_entrant_is_allowed(engine, world):
    with engine.begin() as c:
        assert _apply(c, world, "Silver", 1, "KOR", entrant=None) == "created"


def test_a_closed_placing_must_have_valid_to_and_a_current_one_must_not(engine, world):
    for is_current, valid_to in ((False, None), (True, T0)):
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(
                text(
                    """INSERT INTO placings (event_id, medal, slot, country_id, valid_from, valid_to,
                                             is_current, raw_version_id, source)
                       VALUES (:e, 'Gold', 1, :c, :t, :vt, :cur, :rv, 'x')"""
                ),
                {"e": world.event, "c": world.country["IND"], "t": T0, "vt": valid_to,
                 "cur": is_current, "rv": world.rv},
            )  # fmt: skip


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("UPDATE events SET gender = 'Other' WHERE id = :e", {}),
        ("UPDATE events SET participation = 'Crowd' WHERE id = :e", {}),
        ("UPDATE events SET status = 'maybe' WHERE id = :e", {}),
    ],
)
def test_event_enum_checks(engine, world, sql, params):
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text(sql), {"e": world.event, **params})


def test_medal_type_and_slot_checks(engine, world):
    for medal, slot in (("Platinum", 1), ("Gold", 0)):
        with pytest.raises(IntegrityError), engine.begin() as c:
            _apply(c, world, medal, slot, "IND")


def test_a_sweep_counts_as_two_country_medals(engine, world):
    """DOMAIN_MODEL counting rule 3: one country holding two placings = two country medals."""
    with engine.begin() as c:
        _apply(c, world, "Gold", 1, "IND")
        _apply(c, world, "Silver", 1, "IND")
        assert _count(c, "SELECT count(*) FROM v_medal_facts WHERE country_code = 'IND'") == 2


# --- raw versions (docs/DATA_PIPELINE.md section 4) ---------------------------------------------


def test_same_content_is_never_stored_twice_for_a_document(engine, world):
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                   VALUES (:d, 2, 'aa', 'p2', :t)"""
            ),
            {"d": world.doc, "t": T0},
        )


def test_version_numbers_are_unique_per_document_and_start_at_one(engine, world):
    for version_no in (1, 0):
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(
                text(
                    """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                       VALUES (:d, :n, 'new-hash', 'p3', :t)"""
                ),
                {"d": world.doc, "n": version_no, "t": T0},
            )


def test_a_new_hash_makes_a_new_version_and_latest_can_point_back(engine, world):
    with engine.begin() as c:
        v2 = c.execute(
            text(
                """INSERT INTO raw_versions (document_id, version_no, sha256, path, first_fetched_at)
                   VALUES (:d, 2, 'bb', 'p2', :t) RETURNING id"""
            ),
            {"d": world.doc, "t": T0},
        ).scalar_one()
        c.execute(
            text("UPDATE raw_documents SET latest_version_id = :v WHERE id = :d"),
            {"v": v2, "d": world.doc},
        )
        # content reverted to version 1: latest points back, no third version
        c.execute(
            text("UPDATE raw_documents SET latest_version_id = :v WHERE id = :d"),
            {"v": world.rv, "d": world.doc},
        )
        assert (
            _count(c, "SELECT count(*) FROM raw_versions WHERE document_id = :d", d=world.doc) == 2
        )
        assert (
            _count(c, "SELECT latest_version_id FROM raw_documents WHERE id = :d", d=world.doc)
            == world.rv
        )


def test_unknown_fetch_outcome_is_rejected(engine, world):
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO raw_fetches (document_id, fetched_at, outcome) VALUES (:d, :t, 'weird')"
            ),
            {"d": world.doc, "t": T0},
        )
