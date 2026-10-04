"""Validate normalised rows (docs/DATA_PIPELINE.md section 6). Pure: no database access.

Rows that break a rule are rejected for quarantine, never fixed silently. Exact duplicates are skipped
and counted. Slots are assigned here when the source gave none: the first row of a medal in an event
takes slot 1, the next free slot after that, and the medal rules below decide whether a second slot is
allowed (a marked tie, or a second bronze in a sport that awards two).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sie.pipeline.models import CompetitionRef, NormalisedResult, Reason, Rejection
from sie.pipeline.normalise import ReferenceIndex

MEDAL_ORDER = ("Gold", "Silver", "Bronze")


@dataclass(frozen=True)
class Placed:
    """A row that passed validation, with the slot it will occupy."""

    row: NormalisedResult
    slot: int


@dataclass
class ValidationOutcome:
    placed: list[Placed] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    duplicates_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def is_complete(gold: int, silver: int, bronze: int) -> bool:
    """Whether an event's podium looks finished, tie-aware (docs/DOMAIN_MODEL.md section 4, rule 5).

    A tie for gold omits silver; a tie for silver omits bronze. Anything else needs all three medals.
    """
    if gold >= 2:
        return bronze >= 1
    if gold == 1 and silver >= 2:
        return True
    return gold >= 1 and silver >= 1 and bronze >= 1


def validate(
    rows: Sequence[NormalisedResult], ref: ReferenceIndex, competition: CompetitionRef
) -> ValidationOutcome:
    out = ValidationOutcome()

    # 1. Exact duplicates are skipped, not quarantined (a re-pasted line is not an error).
    seen: set[tuple[object, ...]] = set()
    unique: list[NormalisedResult] = []
    for row in rows:
        key = (row.event_key, row.medal, row.slot, row.country_id, (row.entrant or "").casefold())
        if key in seen:
            out.duplicates_skipped += 1
            continue
        seen.add(key)
        unique.append(row)

    by_event: dict[tuple[int, str, str], list[NormalisedResult]] = defaultdict(list)
    for row in unique:
        by_event[row.event_key].append(row)

    accepted: list[Placed] = []
    for group in by_event.values():
        accepted.extend(_validate_event(group, ref, out))

    for placed in accepted:
        _check_date(placed.row, competition, out)
    out.placed = sorted(accepted, key=lambda p: p.row.row_number)
    return out


def _validate_event(
    group: list[NormalisedResult], ref: ReferenceIndex, out: ValidationOutcome
) -> list[Placed]:
    def reject(row: NormalisedResult, reason: Reason, detail: str) -> None:
        out.rejections.append(Rejection(row.row_number, reason, detail, row.parsed))

    # An event has one participation. Later rows that disagree with the first are rejected.
    participation = group[0].participation
    rows: list[NormalisedResult] = []
    for row in group:
        if row.participation == participation:
            rows.append(row)
        else:
            reject(
                row,
                Reason.EVENT_PARTICIPATION_CONFLICT,
                f"event {row.event_name!r} is {participation} in earlier rows but {row.participation} here",
            )

    dates = {r.event_date for r in rows if r.event_date}
    if len(dates) > 1:
        shown = ", ".join(sorted(d.isoformat() for d in dates))
        out.warnings.append(f"event {rows[0].event_name!r} has several dates in the file: {shown}")

    # 2. Slots: explicit ones first (a repeat is a conflict), then the next free slot for the rest.
    slot_of: dict[int, int] = {}
    taken: set[tuple[str, int]] = set()
    for row in rows:
        if row.slot is None:
            continue
        if (row.medal, row.slot) in taken:
            reject(
                row,
                Reason.SLOT_CONFLICT,
                f"{row.medal} slot {row.slot} of {row.event_name!r} is already taken by another row",
            )
            continue
        taken.add((row.medal, row.slot))
        slot_of[row.row_number] = row.slot
    for row in rows:
        if row.slot is not None:
            continue
        slot = 1
        while (row.medal, slot) in taken:
            slot += 1
        taken.add((row.medal, slot))
        slot_of[row.row_number] = slot

    # 3. How many placings of each medal are allowed.
    allowed: list[Placed] = []
    for medal in MEDAL_ORDER:
        items = sorted(
            (
                (slot_of[r.row_number], r)
                for r in rows
                if r.medal == medal and r.row_number in slot_of
            ),
            key=lambda item: item[0],
        )
        by_slot = dict(items)
        for slot, row in items:
            if slot == 1 or slot == 2 and _second_slot_allowed(medal, row, by_slot.get(1), ref):
                allowed.append(Placed(row, slot))
            else:
                reject(
                    row,
                    Reason.MEDAL_COUNT_EXCEEDED,
                    f"a {medal} in slot {slot} of {row.event_name!r} needs a marked tie"
                    + (" or a sport that awards two bronzes" if medal == "Bronze" else "")
                    + ": set is_tie to yes on both rows",
                )

    # 4. A country appears at most once in a team event (invariant I5).
    if participation != "Team":
        return allowed
    countries: set[int] = set()
    kept: list[Placed] = []
    for placed in allowed:  # already in medal order, then slot order
        if placed.row.country_id in countries:
            reject(
                placed.row,
                Reason.DUPLICATE_COUNTRY_IN_TEAM_EVENT,
                f"the same country already holds a placing in team event {placed.row.event_name!r}",
            )
            continue
        countries.add(placed.row.country_id)
        kept.append(placed)
    return kept


def _second_slot_allowed(
    medal: str, row: NormalisedResult, first: NormalisedResult | None, ref: ReferenceIndex
) -> bool:
    if medal == "Bronze" and row.sport_id in ref.double_bronze:
        return True
    return row.is_tie and first is not None and first.is_tie


def _check_date(row: NormalisedResult, competition: CompetitionRef, out: ValidationOutcome) -> None:
    if row.event_date is None:
        return
    start, end = competition.start_date, competition.end_date
    if (start and row.event_date < start) or (end and row.event_date > end):
        out.warnings.append(
            f"row {row.row_number}: date {row.event_date.isoformat()} is outside the competition "
            f"dates {start}..{end}"
        )
