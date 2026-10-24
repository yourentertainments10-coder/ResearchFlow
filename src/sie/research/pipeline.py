"""The free research loop: reuse stored data, else discover, fetch (cache first), collect, store.

Order of preference for a question: (1) datasets already collected that cover it (no network at all),
(2) cached discovery and fetches, (3) live requests to registered free catalogues and the pages they point
to, through the safe fetcher. A failed provider is reported and skipped; the loop has no paid endpoint to
fall back to. When nothing usable is found it says so and asks for a URL or file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sie.evidence.discovery import (
    Candidate,
    DiscoveryResult,
    authority_for,
    discover,
    load_authority,
)
from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import WebFetcher
from sie.research.cache import CachedFetcher, FetchCache
from sie.research.catalogues import build_providers, load_catalogues
from sie.research.collect import CollectedTable, NothingCollected, collect_page
from sie.research.guard import ProviderOutcome, SearchProviderAdapter
from sie.research.store import ResearchStore, StoredDataset


@dataclass
class ResearchRun:
    question: str
    reused: bool = False
    tables: list[CollectedTable] = field(default_factory=list)
    stored: list[StoredDataset] = field(default_factory=list)
    discovery: DiscoveryResult | None = None
    outcomes: list[ProviderOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # one plain line per skipped lead or failure
    stale_urls: list[str] = field(default_factory=list)
    tiers: dict[str, int] = field(default_factory=dict)

    @property
    def needs_manual_input(self) -> bool:
        return not self.tables


def build_fetchers(settings, *, now=None) -> tuple[CachedFetcher, CachedFetcher]:
    """(discovery fetcher, data fetcher): same safe fetcher and cache, different cache lifetimes."""
    live = WebFetcher(
        user_agent=settings.http_user_agent,
        interval=settings.evidence_fetch_interval_seconds,
        max_bytes=settings.evidence_fetch_max_bytes,
        timeout=settings.evidence_fetch_timeout_seconds,
    )
    cache = FetchCache(settings.research_dir / "cache")
    kw = {} if now is None else {"now": now}
    return (
        CachedFetcher(live, cache, max_age=timedelta(hours=settings.research_discovery_cache_hours), **kw),
        CachedFetcher(live, cache, max_age=timedelta(hours=settings.research_data_cache_hours), **kw),
    )  # fmt: skip


def collect_url(
    url: str, fetcher: CachedFetcher, store: ResearchStore, *, title: str = ""
) -> tuple[list[StoredDataset], list[CollectedTable], bool]:
    """Fetch one URL (cache first), collect its tables and store them. Returns (stored, tables, stale)."""
    fetched = fetcher.fetch(url)
    tables = collect_page(fetched.page)
    stored = [store.save(t, title=title or t.label) for t in tables]
    return stored, tables, fetched.stale


def discover_free(
    question: str,
    discovery_fetcher: CachedFetcher,
    catalogues_path: Path,
    authority_path: Path,
    *,
    limit: int = 10,
) -> tuple[DiscoveryResult, list[ProviderOutcome]]:
    entries = load_catalogues(catalogues_path)
    adapter = SearchProviderAdapter(build_providers(entries, discovery_fetcher))
    rules = load_authority(authority_path)
    result = discover(adapter, question, rules=rules, limit=limit)
    return result, adapter.outcomes


def run_research(
    question: str,
    *,
    store: ResearchStore,
    discovery_fetcher: CachedFetcher,
    data_fetcher: CachedFetcher,
    catalogues_path: Path,
    authority_path: Path,
    refresh: bool = False,
    max_fetch: int = 3,
    limit: int = 10,
) -> ResearchRun:
    run = ResearchRun(question)
    if not refresh:
        matches = store.reuse(question)
        if matches:
            run.reused = True
            for meta, _share in matches[:max_fetch]:
                run.tables.append(store.load(meta["id"]))
            return run
    result, run.outcomes = discover_free(
        question, discovery_fetcher, catalogues_path, authority_path, limit=limit
    )
    run.discovery = result
    for c in result.usable:
        if len(run.tables) >= max_fetch:
            break
        run.tiers[c.url] = c.tier
        try:
            stored, tables, stale = collect_url(c.url, data_fetcher, store, title=c.title)
        except (FetchRefused, NothingCollected) as exc:
            run.problems.append(f"{c.url}: {exc}")
            continue
        except EvidenceError as exc:
            run.problems.append(f"{c.url}: {exc}")
            continue
        run.stored += stored
        run.tables += tables
        if stale:
            run.stale_urls.append(c.url)
    return run


def candidates_summary(candidates: Sequence[Candidate]) -> list[str]:
    return [
        f"tier {c.tier} {c.url} ({c.source_name or 'lead'}; licence {c.licence or 'not stated'})"
        for c in candidates
    ]


__all__ = [
    "ResearchRun",
    "authority_for",
    "build_fetchers",
    "collect_url",
    "discover_free",
    "run_research",
]
