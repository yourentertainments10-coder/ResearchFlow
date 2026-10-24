"""Source discovery and ranking (ADR-044). Deterministic: the same hits always rank the same way.

A search provider (injected) only proposes addresses. Everything else is decided here by rules that can be
read and tested: which tier a host earns (``data/evidence/source_authority.csv``), whether the address is
fetchable at all, whether robots.txt allows it, and how well it matches the question. Every rejected
candidate keeps a reason, so the owner sees why a source was skipped instead of a silent gap.

No model is used. A hit is a lead, not evidence: it becomes evidence only when fetched and registered.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import WebFetcher, check_url

DEFAULT_TIER = 4
_WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "and",
        "or",
        "to",
        "for",
        "by",
        "is",
        "are",
        "was",
        "were",
        "how",
        "many",
        "what",
        "where",
        "when",
        "which",
        "who",
        "with",
        "from",
    ]
)
DATA_SUFFIXES = (".csv", ".json")


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    published: str | None = None  # as reported by the provider; informational only
    licence: str | None = None  # as reported by the catalogue; never checked here
    source_name: str | None = None  # which free catalogue or provider proposed the lead


class SearchProvider(Protocol):
    """Anything that turns a query into leads. No concrete provider ships yet (cost and key need an owner decision)."""

    def search(self, query: str, limit: int) -> Sequence[SearchHit]: ...


@dataclass(frozen=True)
class AuthorityRule:
    pattern: str
    tier: int
    category: str
    note: str


def load_authority(path: Path) -> tuple[AuthorityRule, ...]:
    """Longest pattern wins at match time. A missing file means every host is the default tier."""
    if not path.exists():
        return ()
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["pattern", "tier", "category", "note"]:
            raise EvidenceError("source_authority.csv must have columns pattern,tier,category,note")
        rules = []
        for row in reader:
            pattern = (row["pattern"] or "").strip().lower()
            try:
                tier = int(row["tier"])
            except ValueError:
                raise EvidenceError(f"bad tier for {pattern!r}") from None
            if not pattern.startswith(".") or not 1 <= tier <= 4:
                raise EvidenceError(
                    f"bad authority rule {pattern!r}: patterns start with '.', tiers are 1 to 4"
                )
            rules.append(AuthorityRule(pattern, tier, row["category"].strip(), row["note"].strip()))
    return tuple(sorted(rules, key=lambda r: (-len(r.pattern), r.pattern)))


def authority_for(rules: Sequence[AuthorityRule], host: str) -> tuple[int, str]:
    host = "." + host.lower().rstrip(".")
    for rule in rules:  # longest first
        if host.endswith(rule.pattern):
            return rule.tier, rule.category
    return DEFAULT_TIER, "unclassified"


def terms(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in STOP and len(w) > 1)


@dataclass(frozen=True)
class Candidate:
    url: str
    host: str
    title: str
    tier: int
    category: str
    relevance: float
    structured: bool
    score: float
    allowed_by_robots: bool | None  # None: not checked
    published: str | None
    reasons: tuple[str, ...]
    licence: str | None = None
    source_name: str | None = None


@dataclass(frozen=True)
class Rejected:
    url: str
    code: str
    reason: str


@dataclass(frozen=True)
class DiscoveryResult:
    query: str
    candidates: tuple[Candidate, ...]
    rejected: tuple[Rejected, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.allowed_by_robots is not False)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "candidates": [c.__dict__ | {"reasons": list(c.reasons)} for c in self.candidates],
            "rejected": [r.__dict__ for r in self.rejected],
        }


def score(tier: int, relevance: float, structured: bool) -> float:
    """Authority dominates, then relevance, then a small preference for downloadable data."""
    return round((5 - tier) * 10 + relevance * 5 + (1.0 if structured else 0.0), 6)


def discover(
    provider: SearchProvider,
    query: str,
    *,
    rules: Sequence[AuthorityRule],
    fetcher: WebFetcher | None = None,
    limit: int = 10,
    max_per_host: int = 2,
) -> DiscoveryResult:
    """Ask the provider, then keep, rank or reject each lead with a stated reason.

    With a ``fetcher``, each kept lead is pre-checked against robots.txt (one robots request per host,
    no page request). A host that forbids automated access is listed as not allowed, never worked around.
    """
    wanted = terms(query)
    if not wanted:
        raise EvidenceError("the question has no searchable words")
    if not 1 <= limit <= 50:
        raise EvidenceError("limit must be between 1 and 50")
    rejected: list[Rejected] = []
    kept: list[Candidate] = []
    seen: set[str] = set()
    for hit in provider.search(query, limit * 3):
        try:
            url, host, _ = check_url(hit.url)
        except FetchRefused as exc:
            rejected.append(Rejected(hit.url[:200], exc.code, str(exc)))
            continue
        if url in seen:
            rejected.append(Rejected(url, "duplicate", "the same address was already listed"))
            continue
        seen.add(url)
        tier, category = authority_for(rules, host)
        have = terms(f"{hit.title} {hit.snippet} {urlsplit(url).path.replace('/', ' ')}")
        relevance = len(wanted & have) / len(wanted)
        if relevance == 0:
            rejected.append(
                Rejected(
                    url,
                    "irrelevant",
                    "no word of the question appears in the title, snippet or path",
                )
            )
            continue
        structured = urlsplit(url).path.lower().endswith(DATA_SUFFIXES)
        reasons = [f"tier {tier} ({category})", f"matches {relevance:.0%} of the question's words"]
        if structured:
            reasons.append("downloadable data file")
        if tier == DEFAULT_TIER:
            reasons.append("host not in the authority list: treated as lowest tier")
        kept.append(
            Candidate(url, host, hit.title[:200], tier, category, round(relevance, 4), structured,
                      score(tier, relevance, structured), None, hit.published, tuple(reasons),
                      hit.licence, hit.source_name)
        )  # fmt: skip
    kept.sort(key=lambda c: (-c.score, c.url))
    per_host: dict[str, int] = {}
    final: list[Candidate] = []
    for c in kept:
        if per_host.get(c.host, 0) >= max_per_host:
            rejected.append(
                Rejected(c.url, "host_cap", f"already listing {max_per_host} results from {c.host}")
            )
            continue
        per_host[c.host] = per_host.get(c.host, 0) + 1
        final.append(c)
        if len(final) == limit:
            break
    if fetcher is not None:
        checked = []
        for c in final:
            try:
                ok = fetcher.allowed(c.url)
                reasons = c.reasons if ok else (*c.reasons, "robots.txt disallows automated access")
                checked.append(_with(c, allowed_by_robots=ok, reasons=reasons))
            except FetchRefused as exc:
                checked.append(
                    _with(
                        c,
                        allowed_by_robots=False,
                        reasons=(*c.reasons, f"not fetchable ({exc.code})"),
                    )
                )
        final = checked
    return DiscoveryResult(query, tuple(final), tuple(rejected))


def _with(c: Candidate, **changes) -> Candidate:
    data = c.__dict__ | changes
    return Candidate(**data)


class FileProvider:
    """Leads read from a JSON file: a list of {"url", "title", "snippet", "published"} objects.

    Lets an owner paste results from any search tool until a real provider is chosen (it needs a key
    and a spend limit, which are an owner decision). It makes no network call.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def search(self, query: str, limit: int) -> Sequence[SearchHit]:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvidenceError(f"cannot read the leads file: {exc}") from exc
        if not isinstance(data, list):
            raise EvidenceError("the leads file must be a JSON list")
        hits = []
        for item in data[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise EvidenceError("every lead needs a string 'url'")
            hits.append(
                SearchHit(
                    item["url"],
                    str(item.get("title", "")),
                    str(item.get("snippet", "")),
                    item.get("published"),
                )
            )
        return hits
