"""The free research loop: reuse stored data, else discover, fetch (cache first), collect, store.

Order of preference for a question: (1) datasets already collected that cover it (no network at all),
(2) cached discovery and fetches, (3) live requests to registered free catalogues and the pages they point
to, through the safe fetcher. A failed provider is reported and skipped; the loop has no paid endpoint to
fall back to. When nothing usable is found it says so and asks for a URL or file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sie.evidence.dataset_store import rows_permission
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
from sie.research.reuse import ReuseDecision, assess
from sie.research.store import ResearchStore, SaveResult, StoreRules


@dataclass
class ResearchRun:
    question: str
    reused: bool = False
    tables: list[CollectedTable] = field(default_factory=list)
    stored: list[SaveResult] = field(default_factory=list)
    discovery: DiscoveryResult | None = None
    outcomes: list[ProviderOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # one plain line per skipped lead or failure
    stale_urls: list[str] = field(
        default_factory=list
    )  # cached copy used because a live fetch failed
    stale_datasets: list[str] = field(default_factory=list)  # saved data past the freshness limit
    not_saved: list[str] = field(default_factory=list)  # tables used this run but not kept, and why
    reuse_rejected: list[ReuseDecision] = field(default_factory=list)
    tiers: dict[str, int] = field(default_factory=dict)
    lead_limitations: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )  # url -> what was not checked
    references: list[str] = field(default_factory=list)  # papers found: context, never collected

    @property
    def needs_manual_input(self) -> bool:
        return not self.tables


def make_store(settings, project: str = "default", *, now=None) -> ResearchStore:
    """The one dataset store: configurable location (never committed), per-project, with the rules."""
    rules = StoreRules.load(settings.data_dir / "evidence", settings.evidence_policy_file)
    kw = {} if now is None else {"now": now}
    return ResearchStore(
        settings.evidence_dataset_dir, project=project, rules=rules,
        retention_days=settings.evidence_dataset_retention_days, **kw,
    )  # fmt: skip


def build_fetchers(settings, *, now=None) -> tuple[CachedFetcher, CachedFetcher]:
    """(discovery fetcher, data fetcher): same safe fetcher, cache lifetimes differ.

    Only catalogue answers and hosts with a recorded policy are cached at all (see ``FetchCache``)."""
    live = WebFetcher(
        user_agent=settings.http_user_agent,
        interval=settings.evidence_fetch_interval_seconds,
        max_bytes=settings.evidence_fetch_max_bytes,
        timeout=settings.evidence_fetch_timeout_seconds,
    )
    kw = {} if now is None else {"now": now}
    cache = FetchCache(
        settings.research_dir / "cache",
        retention=timedelta(days=settings.research_cache_retention_days), **kw,
    )  # fmt: skip
    evidence_dir = settings.data_dir / "evidence"
    catalogue_hosts = {e.host for e in load_catalogues(evidence_dir / "catalogues.csv")}
    rules = StoreRules.load(evidence_dir, settings.evidence_policy_file)

    def metadata_only(url: str) -> bool:
        return (urlsplit(url).hostname or "") in catalogue_hosts

    def keepable(url: str) -> bool:
        return rows_permission(
            kind="web", url=url, licence=None, row_policies=rules.row_policies,
            open_licences={}, source_policies=rules.source_policies,
        ).allowed  # fmt: skip

    return (
        CachedFetcher(live, cache, max_age=timedelta(hours=settings.research_discovery_cache_hours), cacheable=metadata_only, **kw),
        CachedFetcher(live, cache, max_age=timedelta(hours=settings.research_data_cache_hours), cacheable=keepable, **kw),
    )  # fmt: skip


def collect_url(
    url: str,
    fetcher: CachedFetcher,
    store: ResearchStore,
    *,
    title: str = "",
    licence: str | None = None,
    catalogue: str = "",
    limitations: Sequence[str] = (),
) -> tuple[list[SaveResult], list[CollectedTable], bool]:
    """Fetch one URL (cache first), collect its tables and keep the permitted ones.

    ``licence`` and ``catalogue`` come from the catalogue entry that listed the URL and travel with every
    table into the manifest and the report. Returns (save results, tables, stale)."""
    fetched = fetcher.fetch(url)
    tables = [
        dataclasses.replace(
            t, licence=licence, catalogue=catalogue, lead_limitations=tuple(limitations)
        )
        for t in collect_page(fetched.page)
    ]
    saved = [store.save(t, title=title or t.label) for t in tables]
    return saved, tables, fetched.stale


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
    spec: dict | None = None,
    refresh: bool = False,
    max_fetch: int = 3,
    limit: int = 10,
    max_age_days: int = 30,
    check_columns: bool = True,
    now: datetime | None = None,
) -> ResearchRun:
    """Saved data first, but only if it can really answer; otherwise discover and collect more.

    Saved data is used when it passes ``reuse.assess`` (fresh, covers the question, has the columns the
    operation needs). Stale but otherwise fitting data is a disclosed last resort."""
    run = ResearchRun(question)
    spec = spec or {"op": "describe"}
    when = now or datetime.now(UTC)
    fallback: list[ReuseDecision] = []
    if not refresh:
        decisions = [
            d
            for m in store.list_datasets()
            if (d := assess(m, question, spec, now=when, max_age_days=max_age_days,
                            check_columns=check_columns)) is not None
        ]  # fmt: skip
        good = sorted((d for d in decisions if d.sufficient), key=lambda d: (-d.share, d.id))
        if good:
            run.reused = True
            run.tables += [store.load(d.id) for d in good[:max_fetch]]
            for t in run.tables:
                run.lead_limitations[t.url] = t.lead_limitations
            return run
        run.reuse_rejected = [d for d in decisions if not d.sufficient]
        fallback = sorted(
            (d for d in run.reuse_rejected if d.stale and not any("lacks" in r for r in d.reasons)),
            key=lambda d: (-d.share, d.id),
        )
    result, run.outcomes = discover_free(
        question, discovery_fetcher, catalogues_path, authority_path, limit=limit
    )
    run.discovery = result
    run.references = [f"{c.title} ({c.url})" for c in result.references]
    for c in result.usable:
        if len(run.tables) >= max_fetch:
            break
        run.tiers[c.url] = c.tier
        run.lead_limitations[c.url] = (
            "matched to the question by its title, description and address only, not by its contents",
            *c.limitations,
        )
        try:
            saved, tables, stale = collect_url(
                c.url,
                data_fetcher,
                store,
                title=c.title,
                licence=c.licence,
                catalogue=c.source_name,
                limitations=c.limitations,
            )
        except (FetchRefused, NothingCollected, EvidenceError) as exc:
            run.problems.append(f"{c.url}: {exc}")
            continue
        run.stored += saved
        run.tables += tables
        run.not_saved += [
            f"{r.table.url} ({r.table.label}): {r.basis}" for r in saved if not r.stored
        ]
        if stale:
            run.stale_urls.append(c.url)
    if not run.tables and fallback:
        for d in fallback[:max_fetch]:
            table = store.load(d.id)
            run.tables.append(table)
            run.stale_datasets.append(f"{table.url} (retrieved {table.retrieved_at})")
        run.reused = True
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
    "make_store",
    "run_research",
]
