"""Types passed between pipeline stages. Terms follow docs/DOMAIN_MODEL.md."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sie.sources.base import ParsedResult


class Reason(StrEnum):
    """Why a row went to quarantine. Stored as text in ``quarantine.reason``."""

    # normalise
    MISSING_FIELD = "MISSING_FIELD"
    UNKNOWN_COMPETITION = "UNKNOWN_COMPETITION"
    COMPETITION_MISMATCH = "COMPETITION_MISMATCH"
    INVALID_MEDAL = "INVALID_MEDAL"
    UNKNOWN_COUNTRY = "UNKNOWN_COUNTRY"
    MULTI_COUNTRY_ENTRANT = "MULTI_COUNTRY_ENTRANT"
    UNKNOWN_SPORT = "UNKNOWN_SPORT"
    UNKNOWN_DISCIPLINE = "UNKNOWN_DISCIPLINE"
    SPORT_DISCIPLINE_MISMATCH = "SPORT_DISCIPLINE_MISMATCH"
    UNKNOWN_GENDER = "UNKNOWN_GENDER"
    INVALID_PARTICIPATION = "INVALID_PARTICIPATION"
    INVALID_SLOT = "INVALID_SLOT"
    INVALID_FLAG = "INVALID_FLAG"
    INVALID_DATE = "INVALID_DATE"
    # validate
    SLOT_CONFLICT = "SLOT_CONFLICT"
    MEDAL_COUNT_EXCEEDED = "MEDAL_COUNT_EXCEEDED"
    DUPLICATE_COUNTRY_IN_TEAM_EVENT = "DUPLICATE_COUNTRY_IN_TEAM_EVENT"
    EVENT_PARTICIPATION_CONFLICT = "EVENT_PARTICIPATION_CONFLICT"
    # load
    EVENT_TOTAL_EXCEEDED = "EVENT_TOTAL_EXCEEDED"


@dataclass(frozen=True)
class CompetitionRef:
    id: int
    code: str
    start_date: date | None
    end_date: date | None
    official_event_total: int | None


@dataclass(frozen=True)
class Rejection:
    """A row that cannot be loaded. It goes to quarantine with a reason; it is never silently dropped."""

    row_number: int
    reason: Reason
    detail: str
    row: ParsedResult

    def key(self) -> str:
        """Identity of this problem, stable across re-runs of the same file."""
        return f"{self.reason}#{self.row_number}"

    def payload(self) -> dict[str, object]:
        return {"row_number": self.row_number, "detail": self.detail, "row": self.row.model_dump()}


@dataclass(frozen=True)
class NormalisedResult:
    """A row whose names are resolved to reference ids and whose values have their final types."""

    row_number: int
    competition_id: int
    sport_id: int
    discipline_id: int
    event_name: str  # cleaned
    event_name_raw: str
    gender: str  # Men | Women | Mixed | Open
    participation: str  # Individual | Team | Pair
    medal: str  # Gold | Silver | Bronze
    slot: int | None  # None: the validator assigns it
    is_tie: bool
    country_id: int
    entrant: str | None
    event_date: date | None
    source_url: str
    source_note: str
    parsed: ParsedResult

    @property
    def event_key(self) -> tuple[int, str, str]:
        """Matches the unique key of the events table, within one competition."""
        return (self.discipline_id, self.event_name, self.gender)
