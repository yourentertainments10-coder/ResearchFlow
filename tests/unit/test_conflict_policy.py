"""The freshness-aware conflict policy table (docs/DATA_PIPELINE.md section 7), row by row. No database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sie.config import Settings
from sie.pipeline.conflicts import (
    Claim,
    ConflictPolicy,
    Rule,
    Status,
    conflict_policy,
    decide,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
POLICY = ConflictPolicy("official", timedelta(minutes=30))
KOR, JPN, CHN = 1, 2, 3


def claim(source: str, country: int, minutes_ago: float = 0) -> Claim:
    return Claim(source, country, NOW - timedelta(minutes=minutes_ago), observation_id=1)


def run(new, others, *, accepted=True, open_conflict=False):
    return decide(
        new, others, has_accepted=accepted, open_conflict=open_conflict, policy=POLICY, now=NOW
    )


def test_only_one_source_has_the_slot_accepts_without_a_conflict():
    d = run(claim("official", KOR), [])
    assert d.apply and d.rule is None and not d.disputed


def test_all_sources_agree_accepts_without_a_conflict():
    d = run(claim("manual", KOR), [claim("official", KOR, 5)])
    assert d.apply and d.rule is None and not d.disputed


def test_nothing_accepted_yet_takes_the_claim_even_if_sources_disagree():
    d = run(claim("manual", JPN), [claim("official", KOR, 5)], accepted=False)
    assert d.apply and d.rule is None


def test_official_fetched_now_beats_a_disagreeing_source_automatically():
    d = run(claim("official", KOR), [claim("manual", JPN, 5)])
    assert d.apply and d.rule is Rule.OFFICIAL_FRESH and d.status is Status.AUTO_RESOLVED
    assert not d.disputed


def test_official_fresh_holds_back_an_older_disagreeing_claim():
    # An older non-official observation (for example a backlog) arriving after a fresh official fetch.
    new = claim("manual", JPN, minutes_ago=20)
    d = run(new, [claim("official", KOR, minutes_ago=5)])
    assert not d.apply and d.rule is Rule.OFFICIAL_FRESH and d.status is Status.AUTO_RESOLVED
    assert not d.disputed and not d.refetch_official


def test_official_stale_holds_the_value_asks_for_a_refetch_and_flags_the_event():
    d = run(claim("manual", JPN), [claim("official", KOR, minutes_ago=5)])
    assert not d.apply and d.rule is Rule.OFFICIAL_STALE and d.status is Status.NEEDS_REVIEW
    assert d.disputed and d.refetch_official


def test_official_older_than_the_threshold_is_stale_even_if_nothing_newer_exists():
    new = claim("manual", JPN, minutes_ago=300)
    d = run(new, [claim("official", KOR, minutes_ago=120)])
    assert d.rule is Rule.OFFICIAL_STALE and not d.apply


def test_refetch_that_now_agrees_resolves_the_open_conflict_and_applies():
    d = run(claim("official", JPN), [claim("manual", JPN, 1)], open_conflict=True)
    assert d.apply and d.rule is Rule.AGREEMENT and d.status is Status.RESOLVED


def test_refetch_unchanged_and_still_disagreeing_keeps_official_and_stays_disputed():
    d = run(claim("official", KOR), [claim("manual", JPN, 1)], open_conflict=True)
    assert d.apply and d.rule is Rule.OFFICIAL_STILL_DISAGREES and d.disputed


def test_two_non_official_sources_disagreeing_hold_the_value_and_flag_the_event():
    d = run(claim("scraper", JPN), [claim("manual", KOR, 2)])
    assert not d.apply and d.rule is Rule.NON_OFFICIAL and d.disputed and not d.refetch_official


def test_a_source_changing_its_own_claim_is_not_a_conflict():
    # No other source claims anything: a reallocation by the official source itself.
    d = run(claim("official", JPN), [])
    assert d.apply and d.rule is None


def test_the_official_source_comes_from_the_first_entry_of_the_source_priority():
    s = Settings(_env_file=None, source_priority="official,manual", freshness_threshold_minutes=45)
    p = conflict_policy(s)
    assert p.official_source == "official" and p.freshness_threshold == timedelta(minutes=45)
