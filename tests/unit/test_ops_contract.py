"""Phase 6 foundations that need no database: failure categories, retry rules, outcome, freshness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sie.pipeline.failure import (
    ERROR_SUMMARY_CHARS,
    RETRY_RULES,
    FailureCategory,
    RetryPolicy,
    format_error_summary,
    parse_error_summary,
    retry_rule,
)
from sie.pipeline.freshness import Freshness, RawArtifact, classify_freshness, fingerprint
from sie.pipeline.observe import RunOutcome, derive_outcome

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(minutes=30)


def test_every_failure_category_has_a_retry_rule_and_none_is_unsafe():
    assert set(RETRY_RULES) == set(FailureCategory)
    assert all(rule.safe and rule.why for rule in RETRY_RULES.values())


@pytest.mark.parametrize(
    ("category", "policy", "attempts"),
    [
        (FailureCategory.FETCH, RetryPolicy.AUTO, 3),
        (FailureCategory.LOAD, RetryPolicy.AUTO, 1),
        (FailureCategory.PARSE, RetryPolicy.AFTER_FIX, 0),
        (FailureCategory.VALIDATION, RetryPolicy.AFTER_FIX, 0),
        (FailureCategory.RAW_STORE, RetryPolicy.MANUAL, 0),
    ],
)
def test_the_retry_contract(category, policy, attempts):
    rule = retry_rule(category)
    assert (rule.policy, rule.max_auto_attempts) == (policy, attempts)


def test_the_error_summary_round_trips_and_is_bounded():
    text = format_error_summary(FailureCategory.PARSE, ValueError("bad shape"))
    assert text == "[parse_failure] ValueError: bad shape"
    assert parse_error_summary(text) == (FailureCategory.PARSE, "ValueError: bad shape")
    assert len(format_error_summary(FailureCategory.LOAD, "x" * 5000)) == ERROR_SUMMARY_CHARS


@pytest.mark.parametrize(
    "stored", [None, "", "plain old error", "[nonsense] detail", "[parse_failure]"]
)
def test_text_without_a_known_category_has_none(stored):
    category, _ = parse_error_summary(stored)
    assert category is None


@pytest.mark.parametrize(
    ("status", "changed", "quarantined", "expected"),
    [
        ("running", None, None, RunOutcome.RUNNING),
        ("failed", 1, 0, RunOutcome.FAILED),
        ("success", 1, 0, RunOutcome.SUCCEEDED),
        ("success", 0, 0, RunOutcome.UNCHANGED),
        ("success", 1, 3, RunOutcome.SUCCEEDED_WITH_QUARANTINE),
        ("success", 0, 3, RunOutcome.SUCCEEDED_WITH_QUARANTINE),  # attention beats "unchanged"
    ],
)
def test_the_run_outcome_is_a_pure_function_of_what_was_recorded(
    status, changed, quarantined, expected
):
    assert derive_outcome(status, changed, quarantined) == expected


@pytest.mark.parametrize(
    ("success_ago", "last_status", "expected"),
    [
        (None, None, Freshness.NEVER_SUCCEEDED),
        (None, "failed", Freshness.NEVER_SUCCEEDED),
        (timedelta(minutes=5), "success", Freshness.FRESH),
        (timedelta(minutes=30), "success", Freshness.FRESH),  # the threshold itself is still fresh
        (timedelta(minutes=31), "success", Freshness.STALE),
        (
            timedelta(minutes=5),
            "failed",
            Freshness.FAILING,
        ),  # a recent success does not hide a failure
        (timedelta(hours=9), "failed", Freshness.FAILING),
    ],
)
def test_freshness_classification(success_ago, last_status, expected):
    last_success = NOW - success_ago if success_ago is not None else None
    assert classify_freshness(last_success, last_status, NOW, MAX_AGE) == expected


def _artifact(url, sha):
    return RawArtifact(url, 1, 1, sha, "fs", f"k/{sha}", 10, NOW)


def test_the_fingerprint_ignores_order_and_changes_with_content():
    a, b = _artifact("u1", "aa"), _artifact("u2", "bb")
    assert fingerprint([a, b]) == fingerprint([b, a])
    assert fingerprint([a, b]) != fingerprint([a, _artifact("u2", "cc")])
    assert fingerprint([a]) != fingerprint([a, b])
    assert fingerprint([]) is None
