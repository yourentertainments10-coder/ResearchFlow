"""A fake network for the free-research tests. No real request is ever made (simulated, not live)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sie.evidence.web import RawResponse, WebFetcher

UA = "SIE-research/0.1 (contact: owner@example.org)"
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
CKAN = "https://data.gov.uk/api/action/package_search"
OPENALEX = "https://api.openalex.org/works"


class Ticker:
    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def json_page(obj, status=200) -> RawResponse:
    return RawResponse(
        status, {"content-type": "application/json; charset=utf-8"}, json.dumps(obj).encode()
    )


def csv_page(text: str) -> RawResponse:
    return RawResponse(200, {"content-type": "text/csv"}, text.encode())


def html_page(text: str) -> RawResponse:
    return RawResponse(200, {"content-type": "text/html; charset=utf-8"}, text.encode())


class Site:
    """Maps URL prefixes to responses (or exceptions to raise); records every request made."""

    def __init__(self, pages=None, robots="User-agent: *\nAllow: /\n") -> None:
        self.pages: dict[str, object] = dict(pages or {})
        self.robots = robots
        self.requests: list[str] = []

    def hosts(self) -> set[str]:
        return {urlsplit(u).hostname for u in self.requests}

    def get(self, url, headers, timeout, max_bytes):
        self.requests.append(url)
        assert headers["User-Agent"] == UA
        if url.endswith("/robots.txt"):
            return RawResponse(200, {"content-type": "text/plain"}, self.robots.encode())
        for prefix, resp in self.pages.items():
            if url.startswith(prefix):
                if isinstance(resp, BaseException):
                    raise resp
                return resp
        return RawResponse(404, {}, b"no")

    def api_requests(self) -> list[str]:
        return [u for u in self.requests if not u.endswith("/robots.txt")]


def web_fetcher(site: Site, ticker: Ticker | None = None, redirect_hosts=None) -> WebFetcher:
    clock = ticker or Ticker()
    return WebFetcher(
        user_agent=UA,
        get=site.get,
        resolver=lambda host, port: ["93.184.216.34"],
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
        redirect_hosts=redirect_hosts,
    )


def ckan_result(*datasets) -> dict:
    return {"success": True, "result": {"count": len(datasets), "results": list(datasets)}}


ELECTION_DATASET = {
    "name": "general-election-results",
    "title": "General election results by party",
    "notes": "Seats won by each party in the general election",
    "license_id": "uk-ogl",
    "license_title": "UK Open Government Licence (OGL)",
    "metadata_modified": "2026-05-01T00:00:00",
    "resources": [
        {
            "url": "https://data.example.gov.uk/election/results.csv",
            "format": "CSV",
            "name": "Results",
        },
        {
            "url": "https://data.example.gov.uk/election/results.pdf",
            "format": "PDF",
            "name": "Report",
        },
    ],
}

RESULTS_CSV = "party,seats,votes\nAlpha,300,1000\nBeta,150,800\nGamma,90,400\nAlpha,10,50\n"



def write_test_catalogues(path, *, redirect_hosts=()):
    """Enable the synthetic CKAN fixture without changing the production source registry."""
    from sie.research.catalogues import load_catalogues

    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "name", "kind", "base_url", "access", "verified_on", "terms_ref", "quota_note",
        "redirect_hosts", "status", "status_note",
    ]
    source_registry = Path(__file__).resolve().parents[1] / "data" / "evidence" / "catalogues.csv"
    rows = []
    for entry in load_catalogues(source_registry):
        row = {name: getattr(entry, name) for name in columns}
        if entry.name == "data.gov.uk":
            row["status"] = "active"
            row["status_note"] = ""
            row["redirect_hosts"] = ";".join(redirect_hosts)
        rows.append(row)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path
