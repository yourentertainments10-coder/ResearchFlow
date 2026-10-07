"""Types shared by all sources. Pure data: no network, no database (docs/ARCHITECTURE.md section 6)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

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
    external_key: str = ""  # the source's own event id when it has one (portal event code)
    country_label: str = ""  # the source's own name for the country, kept as provenance only


# --- adapter and parser interfaces (docs/ARCHITECTURE.md section 6) --------------------------------------


class DocumentRef(BaseModel):
    """Something an adapter can fetch: a stable key (``ARC:medals/discipline``) and its URL."""

    model_config = ConfigDict(frozen=True)

    key: str
    url: str


class RawDocument(BaseModel):
    """What a fetch returns: the bytes exactly as received, plus how and when they arrived."""

    model_config = ConfigDict(frozen=True)

    ref: DocumentRef
    content: bytes
    content_type: str = ""
    http_status: int = 200
    fetched_at: datetime
    from_cache: bool = False


class SourceAdapter(Protocol):
    """Fetch only: network access, nothing else. It never parses and never touches the database."""

    name: str

    def list_documents(self, since: datetime | None = None) -> list[DocumentRef]: ...

    def fetch(self, ref: DocumentRef) -> RawDocument: ...


class SourceParser(Protocol):
    """Parse only: a pure function over bytes that were already saved."""

    source: str

    def parse(self, content: bytes) -> list[ParsedResult]: ...
