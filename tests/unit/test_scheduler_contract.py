"""Scheduler building blocks that need no database."""

from __future__ import annotations

import pytest

from sie.db.locks import LOCK_NAMESPACE, lock_key
from sie.pipeline.failure import RETRY_RULES, FailureCategory, RetryPolicy
from sie.pipeline.scheduler import backoff_seconds


def test_the_scheduler_applies_the_locked_retry_contract_and_does_not_redefine_it():
    # If someone edits these numbers, the contract changed: update DATA_PIPELINE.md section 12 and ADR-027.
    assert RETRY_RULES[FailureCategory.FETCH].max_auto_attempts == 3
    assert RETRY_RULES[FailureCategory.LOAD].max_auto_attempts == 1
    for category in (FailureCategory.PARSE, FailureCategory.VALIDATION):
        assert RETRY_RULES[category].policy == RetryPolicy.AFTER_FIX
        assert RETRY_RULES[category].max_auto_attempts == 0
    assert RETRY_RULES[FailureCategory.RAW_STORE].policy == RetryPolicy.MANUAL
    assert RETRY_RULES[FailureCategory.RAW_STORE].max_auto_attempts == 0


def test_backoff_doubles_and_is_deterministic():
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert backoff_seconds(2, base=0.5) == 1.0


def test_lock_keys_are_stable_distinct_per_source_and_fit_in_int32():
    a = lock_key("asiad-2026", "official")
    assert a == lock_key("asiad-2026", "official")
    assert a != lock_key("asiad-2026", "manual")
    assert a != lock_key("asiad-2030", "official")
    for key in (a, lock_key("x", "y")):
        assert all(-(2**31) <= part < 2**31 for part in key)
    assert a[0] == lock_key("other", "thing")[0] and a[0] != 0  # one fixed namespace
    assert LOCK_NAMESPACE > 0


@pytest.mark.parametrize("pair", [("a", "bc"), ("ab", "c")])
def test_the_key_separates_competition_and_source(pair):
    assert lock_key(*pair) != lock_key("abc", "")
