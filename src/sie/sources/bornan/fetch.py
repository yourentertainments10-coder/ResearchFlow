"""Fetch a full portal capture and hand it to the pipeline. Glue between adapter, assembler and parser.

``fetch_capture`` does the network part (polite, cached, one request at a time); ``portal_source_input``
turns the result into the ``SourceInput`` the runner and the scheduler's ``SourceTask.fetch`` expect, so
a fetched capture goes through exactly the same ingestion path as a capture file (ADR-021). It is not
registered with the scheduler or any workflow here: that belongs to the operations work (Phase 6).
"""

from __future__ import annotations

from sie.config import Settings
from sie.pipeline.runner import SourceInput
from sie.sources.bornan.adapter import PortalAdapter
from sie.sources.bornan.capture import assemble_capture
from sie.sources.bornan.keys import HOST, NAME, SEASON
from sie.sources.bornan.to_parsed import capture_to_parsed
from sie.sources.http import PoliteClient

RAW_URL = (
    f"portal:{SEASON}"  # the raw document key: every fetch is a new version of this one document
)


def make_client(settings: Settings, **overrides) -> PoliteClient:
    return PoliteClient(
        user_agent=settings.http_user_agent,
        allowed_hosts={HOST},
        cache_dir=settings.data_dir / "cache" / "http",
        min_interval=settings.http_min_interval_seconds,
        **overrides,
    )


def fetch_capture(
    settings: Settings, *, client: PoliteClient | None = None, with_time: bool
) -> bytes:
    adapter = PortalAdapter(client or make_client(settings))
    documents = adapter.fetch_all()
    at = max(d.fetched_at for d in documents.values()) if with_time else None
    return assemble_capture(documents, at=at)


def portal_source_input(settings: Settings, *, client: PoliteClient | None = None) -> SourceInput:
    """Fetch now and return the runner input. Raises ``FetchError`` (or ``FetchBlocked``) on failure."""
    competition = settings.competition_id
    return SourceInput(
        source=NAME,
        url=RAW_URL,
        content=fetch_capture(settings, client=client, with_time=False),
        content_type="application/json",
        extension="json",
        parse=lambda raw: capture_to_parsed(raw, competition),
    )
