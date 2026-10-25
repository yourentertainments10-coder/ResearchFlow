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

DATA_FORMATS = frozenset(["csv", "json"])  # the collector parses these (and HTML tables)
MIN_TOPIC_RELEVANCE = 0.5  # share of the question's topic words a lead must show to be kept
_YEAR = re.compile(r"(?<![0-9])(19[5-9][0-9]|20[0-9]{2})(?![0-9])")

# Geography groups: a question that names one group is not answered by a lead that names a different one.
# Small and explicit on purpose; a place not listed here is simply not checked (and that is disclosed).
GEOGRAPHIES: dict[str, tuple[str, ...]] = {
    "united kingdom": ("uk", "u.k.", "united kingdom", "britain", "great britain", "british",
                       "england", "scotland", "wales", "northern ireland", "gb"),
    "united states": ("us", "u.s.", "usa", "united states", "america", "american"),
    "india": ("india", "indian"),
    "canada": ("canada", "canadian"),
    "australia": ("australia", "australian"),
    "ireland": ("ireland", "irish"),
    "france": ("france", "french"),
    "germany": ("germany", "german"),
    "new zealand": ("new zealand",),
}  # fmt: skip


def geographies(text: str) -> frozenset[str]:
    """The geography groups a text names (whole words or phrases only)."""
    low = f" {' '.join(_WORD.findall(text.lower().replace('u.s.', 'us').replace('u.k.', 'uk')))} "
    found = set()
    for group, names in GEOGRAPHIES.items():
        for name in names:
            plain = " ".join(_WORD.findall(name.replace("u.s.", "us").replace("u.k.", "uk")))
            if f" {plain} " in low:
                found.add(group)
    return frozenset(found)


def years(text: str) -> frozenset[str]:
    return frozenset(_YEAR.findall(text))


def _geo_words(text: str) -> frozenset[str]:
    low = text.lower().replace("u.s.", "us").replace("u.k.", "uk")
    words = set()
    for names in GEOGRAPHIES.values():
        for name in names:
            words.update(_WORD.findall(name.replace("u.s.", "us").replace("u.k.", "uk")))
    return frozenset(w for w in _WORD.findall(low) if w in words)


def topic_terms(question: str) -> frozenset[str]:
    """The question's words once geography words and years are taken out: what the data must be about."""
    geo = _geo_words(question)
    return frozenset(w for w in terms(question) if w not in geo and not _YEAR.fullmatch(w))


def assess_lead(question: str, text: str) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    """(topic relevance, limitations, mismatches) of a lead's text against the question.

    A mismatch means the lead is about another place or another year than the one asked for. A limitation
    is something the lead does not state (so it could not be checked). Nothing is guessed."""
    wanted = topic_terms(question)
    have = terms(text)
    relevance = len(wanted & have) / len(wanted) if wanted else 1.0
    limits: list[str] = []
    bad: list[str] = []
    want_geo, have_geo = geographies(question), geographies(text)
    if want_geo:
        if have_geo and not (want_geo & have_geo):
            bad.append(f"geography mismatch: asked for {', '.join(sorted(want_geo))}, lead names {', '.join(sorted(have_geo))}")  # fmt: skip
        elif not have_geo:
            limits.append("geography not stated by the lead; not checked against the question")
    want_years, have_years = years(question), years(text)
    if want_years:
        if have_years and not (want_years & have_years):
            bad.append(f"year mismatch: asked for {', '.join(sorted(want_years))}, lead names {', '.join(sorted(have_years))}")  # fmt: skip
        elif not have_years:
            limits.append("year not stated by the lead; not checked against the question")
    return relevance, tuple(limits), tuple(bad)


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    published: str | None = None  # as reported by the provider; informational only
    licence: str | None = None  # as reported by the catalogue; never checked here
    source_name: str | None = None  # which free catalogue or provider proposed the lead
    kind: str = (
        "dataset"  # "dataset": may hold usable data; "paper": a reference to read, never data
    )
    format: str | None = None  # file format the catalogue declares (csv, json, ...), if any


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
    kind: str = "dataset"  # "paper" is a reference to read, never a data source
    format: str | None = None
    limitations: tuple[str, ...] = ()  # what the lead does not show; carried into the report

    @property
    def robots_status(self) -> str:
        """``not_checked`` until robots.txt was read for this host; never "verified" or "fetchable"."""
        if self.allowed_by_robots is None:
            return "not_checked"
        return "allowed" if self.allowed_by_robots else "disallowed"


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
        """Dataset leads robots.txt has not forbidden. Still unverified until fetched and parsed."""
        return tuple(
            c for c in self.candidates if c.kind == "dataset" and c.allowed_by_robots is not False
        )

    @property
    def references(self) -> tuple[Candidate, ...]:
        """Papers and other references: context to read, never collected as data."""
        return tuple(c for c in self.candidates if c.kind != "dataset")

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "note": "A lead is not evidence. robots_status 'not_checked' means fetch permission has not "
            "been looked up; nothing here is verified, fetched or known to contain the answer.",
            "candidates": [
                c.__dict__
                | {
                    "reasons": list(c.reasons),
                    "limitations": list(c.limitations),
                    "robots_status": c.robots_status,
                    "role": "reference" if c.kind != "dataset" else "data_lead",
                }
                for c in self.candidates
            ],
            "rejected": [r.__dict__ for r in self.rejected],
        }


def score(tier: int, relevance: float, structured: bool, *, reference: bool = False) -> float:
    """Authority dominates, then relevance, then a small preference for downloadable data.

    A reference (paper) never outranks a structured data lead of the same relevance."""
    base = (5 - tier) * 10 + relevance * 5 + (1.0 if structured else 0.0)
    return round(base - (3.0 if reference else 0.0), 6)


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
    if not terms(query):
        raise EvidenceError("the question has no searchable words")
    wanted_topic = topic_terms(query)
    if not 1 <= limit <= 50:
        raise EvidenceError("limit must be between 1 and 50")
    rejected: list[Rejected] = []
    kept: list[Candidate] = []
    best: dict[str, tuple[tuple[float, str], SearchHit]] = {}
    unique: list[tuple[str, str, SearchHit]] = []
    for hit in provider.search(query, limit * 3):
        try:
            url, host, _ = check_url(hit.url)
        except FetchRefused as exc:
            rejected.append(Rejected(hit.url[:200], exc.code, str(exc)))
            continue
        rank = (assess_lead(query, f"{hit.title} {hit.snippet}")[0], hit.title)
        if url in best:  # one address listed twice: the better-matching listing is kept, any order
            rejected.append(Rejected(url, "duplicate", "the same address was already listed"))
            if rank > best[url][0]:
                best[url] = (rank, hit)
            continue
        best[url] = (rank, hit)
        unique.append((url, host, hit))
    for url, host, _first in unique:
        hit = best[url][1]
        tier, category = authority_for(rules, host)
        path_text = urlsplit(url).path.replace("/", " ").replace("_", " ").replace("-", " ")
        relevance, limits, mismatches = assess_lead(query, f"{hit.title} {hit.snippet} {path_text}")
        if mismatches:
            rejected.append(Rejected(url, "mismatch", "; ".join(mismatches)))
            continue
        if wanted_topic and relevance < MIN_TOPIC_RELEVANCE:
            rejected.append(
                Rejected(
                    url,
                    "irrelevant" if relevance == 0 else "low_relevance",
                    f"only {relevance:.0%} of the question's topic words appear in the title, snippet "
                    f"or path (at least {MIN_TOPIC_RELEVANCE:.0%} needed)",
                )
            )
            continue
        reference = hit.kind != "dataset"
        fmt = (hit.format or "").strip().lower() or None
        structured = (
            urlsplit(url).path.lower().endswith(DATA_SUFFIXES) or fmt in DATA_FORMATS
        ) and not reference
        reasons = [f"tier {tier} ({category})", f"matches {relevance:.0%} of the question's topic words"]  # fmt: skip
        if reference:
            reasons.append("academic reference: context to read, not a data source")
            limits = (
                *limits,
                "an academic reference, not a structured dataset; it is not collected",
            )
        elif structured:
            reasons.append("downloadable data file")
        else:
            limits = (*limits, "no downloadable CSV/JSON file declared; a page may hold no usable table")  # fmt: skip
        if tier == DEFAULT_TIER:
            reasons.append("host not in the authority list: treated as lowest tier")
        cand = (
            Candidate(url, host, hit.title[:200], tier, category, round(relevance, 4), structured,
                      score(tier, relevance, structured, reference=reference), None, hit.published,
                      tuple(reasons), hit.licence, hit.source_name, hit.kind, fmt, limits)
        )  # fmt: skip
        kept.append(cand)
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
