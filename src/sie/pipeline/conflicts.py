"""The freshness-aware conflict policy of docs/DATA_PIPELINE.md section 7. Pure: no database, no clock.

A source's claim about one placing slot (event, medal, slot) is compared with the latest claim of every
*other* source. A source changing its own claim is never a conflict: that is a reallocation or a
correction, handled by ``apply_placing`` with history. Source priority is a tie-break input only: it
says which source is "official", and even the official source is trusted over another only while its
observation is fresh.

``decide`` returns what to do with the new claim; the caller (``db/conflicts.py``, ``load.py``) does it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sie.config import Settings


class Rule(StrEnum):
    """Which row of the policy table applied. Stored in ``source_conflicts.policy_rule``."""

    OFFICIAL_FRESH = "official_fresh"  # official fresh, another source disagrees: prefer official
    OFFICIAL_STALE = "official_stale"  # official stale, another source disagrees: hold, re-fetch
    OFFICIAL_STILL_DISAGREES = "official_still_disagrees"  # re-fetched, unchanged, still disagrees
    NON_OFFICIAL = "non_official"  # two non-official sources disagree, no official claim
    AGREEMENT = "agreement"  # the sources agree again: the open conflict is resolved


class Status(StrEnum):
    AUTO_RESOLVED = "auto_resolved"
    NEEDS_REVIEW = "needs_review"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class Claim:
    source: str
    country_id: int
    observed_at: datetime
    observation_id: int | None = None


@dataclass(frozen=True)
class ConflictPolicy:
    official_source: str  # the highest-priority source (settings.source_priority_list[0])
    freshness_threshold: timedelta

    def official_is_fresh(self, official: Claim, newest_other: datetime, now: datetime) -> bool:
        """Fetched after the most recent claim of any other source, and recently enough."""
        return official.observed_at > newest_other and now - official.observed_at <= (
            self.freshness_threshold
        )


@dataclass(frozen=True)
class Decision:
    apply: bool  # write the new claim to ``placings`` (through apply_placing)
    rule: Rule | None = None  # set when a conflict row is recorded or an open one is closed
    status: Status | None = None  # the conflict's status; None when there is no conflict
    against: Claim | None = None  # the claim the new one conflicts with (or agrees with, to resolve)
    refetch_official: bool = False  # ask for an immediate re-fetch of the official source

    @property
    def disputed(self) -> bool:
        return self.status is Status.NEEDS_REVIEW


def decide(
    new: Claim,
    others: list[Claim],
    *,
    has_accepted: bool,
    open_conflict: bool,
    policy: ConflictPolicy,
    now: datetime,
) -> Decision:
    """What to do with ``new`` given the latest claim of each other source.

    ``others`` holds at most one claim per other source. ``has_accepted``: a current placing exists for
    the slot. ``open_conflict``: a ``needs_review`` conflict is already open on this slot.
    """
    disagreeing = [o for o in others if o.country_id != new.country_id]

    if not disagreeing:
        if open_conflict and others:
            return Decision(apply=True, rule=Rule.AGREEMENT, status=Status.RESOLVED, against=others[0])
        return Decision(apply=True)

    if not has_accepted:  # nothing to protect: the first accepted value is this claim
        return Decision(apply=True)

    official_other = next((o for o in disagreeing if o.source == policy.official_source), None)
    newest_other = max(o.observed_at for o in disagreeing)

    if new.source == policy.official_source:
        # The official source was just fetched, so it is fresh by construction.
        rule = Rule.OFFICIAL_STILL_DISAGREES if open_conflict else Rule.OFFICIAL_FRESH
        status = Status.NEEDS_REVIEW if open_conflict else Status.AUTO_RESOLVED
        return Decision(apply=True, rule=rule, status=status, against=disagreeing[0])

    if official_other is not None:
        if policy.official_is_fresh(official_other, new.observed_at, now):
            return Decision(
                apply=False, rule=Rule.OFFICIAL_FRESH, status=Status.AUTO_RESOLVED, against=official_other
            )
        return Decision(
            apply=False,
            rule=Rule.OFFICIAL_STALE,
            status=Status.NEEDS_REVIEW,
            against=official_other,
            refetch_official=True,
        )

    return Decision(
        apply=False, rule=Rule.NON_OFFICIAL, status=Status.NEEDS_REVIEW, against=disagreeing[0]
    )


def conflict_policy(settings: Settings) -> ConflictPolicy:
    """The policy from configuration: the first source in ``source_priority`` is the official one."""
    priority = settings.source_priority_list
    if not priority:
        raise ValueError("SOURCE_PRIORITY must name at least one source")
    return ConflictPolicy(
        official_source=priority[0],
        freshness_threshold=timedelta(minutes=settings.freshness_threshold_minutes),
    )
