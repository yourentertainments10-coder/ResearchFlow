"""Cost guard (ADR-045): a provider that fails is reported and skipped; nothing replaces it with a paid call.

``search_all`` asks each free provider in turn. Quota exhaustion (HTTP 429), a stop after 403 or 5xx,
timeouts, network errors, robots refusals and malformed responses each end that provider's turn with a
recorded outcome and a plain next step. The function has no other endpoint to call, so the failure path
cannot reach one. Cached results are used by ``CachedFetcher`` before any live request is made.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sie.evidence.discovery import SearchHit
from sie.evidence.errors import EvidenceError, FetchRefused

NEXT_STEP = (
    "Nothing was bought or switched to a paid service. Use cached results, another free source, or "
    "supply a URL or file yourself (sie research collect <url> or sie datasets profile <file>)."
)


@dataclass(frozen=True)
class ProviderOutcome:
    provider: str
    status: str  # ok, quota_exhausted, unavailable, refused, bad_response
    detail: str = ""
    hits: int = 0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "hits": self.hits,
        }


def classify_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, FetchRefused):
        text = str(exc)
        if exc.code == "status" and "HTTP 429" in text:
            return "quota_exhausted", text
        if exc.code == "stopped":
            return "quota_exhausted" if "429" in text else "unavailable", text
        if exc.code in ("robots", "unregistered_host", "ssrf", "bad_url"):
            return "refused", text
        return "unavailable", text  # network, timeout, status 403/5xx, dns, content type, size
    return "bad_response", str(exc)


def search_all(
    providers: Sequence, query: str, limit: int
) -> tuple[list[SearchHit], list[ProviderOutcome]]:
    hits: list[SearchHit] = []
    outcomes: list[ProviderOutcome] = []
    for provider in providers:
        name = getattr(getattr(provider, "entry", None), "name", type(provider).__name__)
        try:
            found = list(provider.search(query, limit))
        except (FetchRefused, EvidenceError) as exc:
            status, detail = classify_failure(exc)
            outcomes.append(ProviderOutcome(name, status, f"{detail}. {NEXT_STEP}"))
            continue
        hits.extend(found)
        outcomes.append(ProviderOutcome(name, "ok", "", len(found)))
    return hits, outcomes


class SearchProviderAdapter:
    """Presents the combined free providers as one ``SearchProvider`` for ``discover``, keeping outcomes."""

    def __init__(self, providers: Sequence) -> None:
        self.providers = list(providers)
        self.outcomes: list[ProviderOutcome] = []

    def search(self, query: str, limit: int) -> Sequence[SearchHit]:
        hits, self.outcomes = search_all(self.providers, query, limit)
        return hits
