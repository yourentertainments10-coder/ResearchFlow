"""Types shared by all sources. Pure data: no network, no database (docs/ARCHITECTURE.md section 6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ParsedResult(BaseModel):
    """One medal placing exactly as a source claimed it.

    Every field is the source's own text, trimmed but otherwise untouched, and an empty string means
    "the source did not say". Matching to reference data, type conversion and validation happen later
    in ``sie.pipeline`` so that a parser never guesses (AGENTS.md: parsers fail loudly, never guess).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_number: int  # line number in the source file, so a quarantined row can be found again
    competition: str = ""
    sport: str = ""
    discipline: str = ""
    event: str = ""
    gender: str = ""
    medal: str = ""
    country: str = ""
    entrant: str = ""
    participation: str = ""
    slot: str = ""
    is_tie: str = ""
    date: str = ""
    source_url: str = ""
    source_note: str = ""
