"""Failure categories and the retry contract for an ingestion run (docs/DATA_PIPELINE.md section 9).

A run fails at exactly one stage, and the stage decides what is safe to do next. The category is
stored at the start of ``ingest_runs.error_summary`` as ``[category] detail`` so it can be queried
(``error_summary LIKE '[load_failure]%'``) without a schema change.

Invariant behind every rule here: raw evidence is written once and never replaced (``raw.py``), and
parse, normalise, validate and load are all-or-nothing. So re-running any failed stage on the same
input is idempotent; the categories differ only in *whether retrying can help*.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

ERROR_SUMMARY_CHARS = 500


class FailureCategory(StrEnum):
    FETCH = "fetch_failure"  # the source could not be reached or answered with an error
    RAW_STORE = "raw_store_failure"  # the bytes could not be stored intact
    PARSE = "parse_failure"  # the bytes are not in the shape the parser expects
    VALIDATION = "validation_failure"  # normalise or validate stage broke (not a single bad row)
    LOAD = "load_failure"  # the all-or-nothing database load was rejected


class RetryPolicy(StrEnum):
    AUTO = "auto"  # a scheduler may retry by itself, within max_auto_attempts
    AFTER_FIX = (
        "after_fix"  # retrying unchanged input cannot help; fix code or reference data first
    )
    MANUAL = "manual"  # needs a person to look; never retried automatically


@dataclass(frozen=True)
class RetryRule:
    policy: RetryPolicy
    max_auto_attempts: int
    safe: bool  # retrying cannot corrupt data or raw evidence
    why: str


RETRY_RULES: dict[FailureCategory, RetryRule] = {
    FailureCategory.FETCH: RetryRule(
        RetryPolicy.AUTO,
        3,
        True,
        "nothing was stored or changed; transient network or server errors usually clear",
    ),
    FailureCategory.RAW_STORE: RetryRule(
        RetryPolicy.MANUAL,
        0,
        True,
        "a write-once conflict or storage fault; do not retry blindly, raw evidence is never overwritten",
    ),
    FailureCategory.PARSE: RetryRule(
        RetryPolicy.AFTER_FIX,
        0,
        True,
        "the same bytes parse the same way; the raw input is kept, fix the parser then re-run",
    ),
    FailureCategory.VALIDATION: RetryRule(
        RetryPolicy.AFTER_FIX,
        0,
        True,
        "normalise or validate broke on this input; fix rules or reference mappings then re-run",
    ),
    FailureCategory.LOAD: RetryRule(
        RetryPolicy.AUTO,
        1,
        True,
        "the load is one transaction and was rolled back; retry once for a transient database "
        "fault, then treat as manual",
    ),
}


def retry_rule(category: FailureCategory) -> RetryRule:
    return RETRY_RULES[category]


def format_error_summary(
    category: FailureCategory, error: BaseException | str, limit: int = ERROR_SUMMARY_CHARS
) -> str:
    detail = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return f"[{category}] {detail}"[:limit]


def parse_error_summary(summary: str | None) -> tuple[FailureCategory | None, str]:
    """Split a stored ``error_summary`` into its category and detail. Unknown text has no category."""
    if not summary:
        return None, ""
    if summary.startswith("["):
        tag, _, rest = summary[1:].partition("] ")
        try:
            return FailureCategory(tag), rest
        except ValueError:
            pass
    return None, summary
