"""Free public catalogues as discovery providers (ADR-045).

Each adapter turns a question into leads by calling a documented, keyless search API through the safe
fetcher. The registry (``data/evidence/catalogues.csv``) records, per catalogue, where its terms were read
and when; an entry whose access is not ``free_no_key`` is refused at load time, so no key, account, paid
plan or paid endpoint can enter through configuration. Adapters only build URLs from the registered base
URL, and parse strictly: an unexpected response shape is an error, never a guess.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from sie.evidence.discovery import SearchHit
from sie.evidence.errors import EvidenceError, FetchRefused
from sie.evidence.web import check_url
from sie.research.cache import Fetched

FREE_ACCESS = "free_no_key"
COLUMNS = ["name", "kind", "base_url", "access", "verified_on", "terms_ref", "quota_note"]
DATA_FORMATS = {"csv", "json"}
KINDS = ("ckan", "openalex")


class PaidAccessRefused(EvidenceError):
    """A catalogue entry asks for anything other than free keyless access."""


@dataclass(frozen=True)
class CatalogueEntry:
    name: str
    kind: str
    base_url: str
    access: str
    verified_on: str
    terms_ref: str
    quota_note: str

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""


def load_catalogues(path: Path) -> tuple[CatalogueEntry, ...]:
    if not path.exists():
        return ()
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != COLUMNS:
            raise EvidenceError(f"catalogues.csv must have columns {','.join(COLUMNS)}")
        entries = []
        for row in reader:
            entry = CatalogueEntry(**{c: (row[c] or "").strip() for c in COLUMNS})
            if entry.access != FREE_ACCESS:
                raise PaidAccessRefused(
                    f"catalogue {entry.name!r}: access {entry.access!r} is refused; only "
                    f"{FREE_ACCESS!r} is allowed (ADR-045)"
                )
            if entry.kind not in KINDS:
                raise EvidenceError(f"catalogue {entry.name!r}: unknown kind {entry.kind!r}")
            if not entry.verified_on or not entry.terms_ref.startswith("https://"):
                raise EvidenceError(
                    f"catalogue {entry.name!r} needs verified_on and a terms_ref link"
                )
            _, host, _ = check_url(entry.base_url)
            if all(part.isdigit() for part in host.split(".")) or ":" in host:
                raise EvidenceError(f"catalogue {entry.name!r}: use a host name, not an IP address")
            if "?" in entry.base_url or "key" in entry.base_url.lower():
                raise PaidAccessRefused(f"catalogue {entry.name!r}: no query or key in base_url")
            entries.append(entry)
    names = [e.name for e in entries]
    if len(set(names)) != len(names):
        raise EvidenceError("catalogue names must be unique")
    return tuple(entries)


class RegisteredHostFetcher:
    """Lets provider calls reach only the hosts of registered free catalogues."""

    def __init__(self, inner, entries: Sequence[CatalogueEntry]) -> None:
        self.inner, self.hosts = inner, {e.host for e in entries}

    def fetch(self, url: str) -> Fetched:
        host = urlsplit(url).hostname or ""
        if host not in self.hosts:
            raise FetchRefused(
                "unregistered_host", f"{host or url!r} is not a registered free catalogue"
            )
        return self.inner.fetch(url)


def _json(fetched: Fetched, who: str) -> dict:
    if fetched.page.content_type != "application/json":
        raise EvidenceError(f"{who}: expected JSON, got {fetched.page.content_type}")
    try:
        data = json.loads(fetched.page.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError(f"{who}: the response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"{who}: unexpected response shape")
    return data


class CkanCatalogue:
    """A CKAN ``package_search`` catalogue. Gives one lead per CSV or JSON resource, else one per dataset."""

    def __init__(self, entry: CatalogueEntry, fetcher) -> None:
        if entry.kind != "ckan":
            raise EvidenceError(f"{entry.name} is not a CKAN catalogue")
        self.entry, self.fetcher = entry, fetcher

    def search(self, query: str, limit: int) -> Sequence[SearchHit]:
        rows = max(1, min(limit, 100))
        url = f"{self.entry.base_url}/package_search?q={quote(query)}&rows={rows}"
        data = _json(self.fetcher.fetch(url), self.entry.name)
        result = data.get("result")
        if (
            data.get("success") is not True
            or not isinstance(result, dict)
            or not isinstance(result.get("results"), list)
        ):
            raise EvidenceError(f"{self.entry.name}: unexpected CKAN response (no result list)")
        hits: list[SearchHit] = []
        for ds in result["results"]:
            if not isinstance(ds, dict) or not isinstance(ds.get("title"), str):
                raise EvidenceError(f"{self.entry.name}: a dataset has no title")
            licence = ds.get("license_title") or ds.get("license_id")
            common = {
                "published": ds.get("metadata_modified"),
                "licence": licence if isinstance(licence, str) else None,
                "source_name": self.entry.name,
            }
            snippet = str(ds.get("notes") or "")[:300]
            added = False
            for res in ds.get("resources") or []:
                fmt = str(res.get("format") or "").strip().lower() if isinstance(res, dict) else ""
                if fmt in DATA_FORMATS and isinstance(res.get("url"), str):
                    label = str(res.get("name") or fmt.upper())
                    hits.append(
                        SearchHit(res["url"], f"{ds['title']} - {label}", snippet, **common)
                    )
                    added = True
            if not added and isinstance(ds.get("name"), str):
                host = self.entry.host
                hits.append(
                    SearchHit(
                        f"https://{host}/dataset/{quote(ds['name'])}",
                        ds["title"],
                        snippet,
                        **common,
                    )
                )
        return hits[: limit * 3]


class OpenAlexCatalogue:
    """OpenAlex works search, keyless. Leads are papers (context), not data files."""

    def __init__(self, entry: CatalogueEntry, fetcher) -> None:
        if entry.kind != "openalex":
            raise EvidenceError(f"{entry.name} is not an OpenAlex entry")
        self.entry, self.fetcher = entry, fetcher

    def search(self, query: str, limit: int) -> Sequence[SearchHit]:
        per = max(1, min(limit, 50))
        url = (
            f"{self.entry.base_url}/works?search={quote(query)}&per-page={per}"
            "&select=id,title,doi,publication_date,primary_location"
        )
        data = _json(self.fetcher.fetch(url), self.entry.name)
        results = data.get("results")
        if not isinstance(results, list):
            raise EvidenceError(f"{self.entry.name}: unexpected response (no results list)")
        hits = []
        for work in results:
            if not isinstance(work, dict) or not isinstance(work.get("title"), str):
                continue  # a work without a title is not a usable lead; counted nowhere else
            loc = (
                work.get("primary_location")
                if isinstance(work.get("primary_location"), dict)
                else {}
            )
            target = work.get("doi") or (loc or {}).get("landing_page_url")
            if isinstance(target, str):
                hits.append(
                    SearchHit(
                        target,
                        work["title"],
                        "",
                        work.get("publication_date"),
                        None,
                        self.entry.name,
                    )
                )
        return hits


ADAPTERS: dict[str, Callable] = {"ckan": CkanCatalogue, "openalex": OpenAlexCatalogue}


def build_providers(entries: Sequence[CatalogueEntry], fetcher) -> list:
    """One provider per registered catalogue, all restricted to registered hosts."""
    guarded = RegisteredHostFetcher(fetcher, entries)
    return [ADAPTERS[e.kind](e, guarded) for e in entries]
