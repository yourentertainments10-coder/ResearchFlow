"""Fetch the official results portal's API (``back.results.asiangames2026.org``). Fetch only.

It lists what to fetch, asks the portal politely (``sources/http.py`` enforces the rules) and hands back
the bytes exactly as received. It never parses medal rows and never imports the parser or the database.
The one thing it reads from a response is the list of discipline codes in ``ALL/disc/data``, because
that list decides which documents exist.

Documents (docs/SOURCE_DISCOVERY.md sections 8 to 11), under ``/s/AG2026/en/``:
``ALL/disc/data``, ``ALL/medals/standings``, and for every discipline ``{D}/medals/discipline`` and
``{D}/medals/standings``. The decision to fetch them automatically is D1 (ADR-031).
"""

from __future__ import annotations

from datetime import datetime

from sie.sources.base import DocumentRef, RawDocument
from sie.sources.bornan.decode import PayloadDecodeError, decode_payload
from sie.sources.bornan.keys import (
    ALL_STANDINGS_KEY,
    DISCIPLINES_KEY,
    HOST,
    LANGUAGE,
    NAME,
    SEASON,
)
from sie.sources.http import FetchError, PoliteClient


def document_key(discipline: str, path: str) -> str:
    return f"{discipline}:{path}"


class PortalAdapter:
    name = NAME

    def __init__(
        self,
        client: PoliteClient,
        *,
        host: str = HOST,
        season: str = SEASON,
        language: str = LANGUAGE,
    ) -> None:
        self.client = client
        self._base = f"https://{host}/s/{season}/{language}"
        self._fetched: dict[str, RawDocument] = {}

    def ref(self, key: str) -> DocumentRef:
        discipline, _, path = key.partition(":")
        return DocumentRef(key=key, url=f"{self._base}/{discipline}/{path}")

    def list_documents(self, since: datetime | None = None) -> list[DocumentRef]:
        """Every document of a full results capture. ``since`` is accepted for the protocol; the
        portal has no change feed, so change detection is by content hash after fetching."""
        refs = [self.ref(DISCIPLINES_KEY), self.ref(ALL_STANDINGS_KEY)]
        for code in self.discipline_codes():
            refs.append(self.ref(document_key(code, "medals/discipline")))
            refs.append(self.ref(document_key(code, "medals/standings")))
        return refs

    def discipline_codes(self) -> list[str]:
        document = self.fetch(self.ref(DISCIPLINES_KEY))
        try:
            data = decode_payload(document.content)
            return sorted({str(item["Disc"]) for item in data})
        except (PayloadDecodeError, KeyError, TypeError) as exc:
            raise FetchError(f"the discipline list has an unexpected shape: {exc}") from exc

    def fetch(self, ref: DocumentRef) -> RawDocument:
        if ref.key in self._fetched:  # never ask twice in one run
            return self._fetched[ref.key]
        response, from_cache, fetched_at = self.client.get(ref.url)
        document = RawDocument(
            ref=ref,
            content=response.body,
            content_type=response.headers.get("content-type", ""),
            http_status=response.status,
            fetched_at=fetched_at,
            from_cache=from_cache,
        )
        self._fetched[ref.key] = document
        return document

    def fetch_all(self) -> dict[str, RawDocument]:
        """Fetch every listed document (in order, one at a time) and return them by key."""
        return {ref.key: self.fetch(ref) for ref in self.list_documents()}
