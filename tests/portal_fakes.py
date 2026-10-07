"""A fake results portal for tests: the saved fixtures, served the way the real API serves them."""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from sie.sources.http import Response

BORNAN = Path(__file__).resolve().parent / "fixtures" / "sources" / "bornan"
BASE = "https://back.results.asiangames2026.org/s/AG2026/en"
CONTACT_AGENT = "SIE-research/0.1 (owner@example.org)"


def encode(value: object) -> bytes:
    """The portal's body format: JSON -> zlib -> one character per byte -> UTF-8 text."""
    deflated = zlib.compress(json.dumps(value).encode("utf-8"))
    return deflated.decode("latin-1").encode("utf-8")


def _load(name: str) -> object:
    return json.loads((BORNAN / name).read_text(encoding="utf-8"))


def portal_bodies(disciplines: tuple[str, ...] = ("ARC", "SWM")) -> dict[str, object]:
    """URL -> decoded JSON value, for the disciplines saved as fixtures."""
    listing = [d for d in _load("ALL_disc_data.trimmed.json") if d["Disc"] in disciplines]
    bodies: dict[str, object] = {
        f"{BASE}/ALL/disc/data": listing,
        f"{BASE}/ALL/medals/standings": _load("ALL_medals_standings.decoded.json"),
    }
    for code in disciplines:
        bodies[f"{BASE}/{code}/medals/discipline"] = _load(f"{code}_medals_discipline.json")
        bodies[f"{BASE}/{code}/medals/standings"] = _load(f"{code}_medals_standings.json")
    return bodies


class FakePortal:
    """A transport that answers from ``bodies`` and records every request it receives."""

    def __init__(self, bodies: dict[str, object] | None = None, etag: str | None = None) -> None:
        self.bodies = bodies if bodies is not None else portal_bodies()
        self.etag = etag
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.overrides: dict[str, Response] = {}

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append((url, dict(headers)))
        if url in self.overrides:
            return self.overrides[url]
        if url not in self.bodies:
            return Response(404, b"")
        if self.etag and headers.get("If-None-Match") == self.etag:
            return Response(304, b"", {"etag": self.etag})
        resp_headers = {"content-type": "application/json; charset=utf-8"}
        if self.etag:
            resp_headers["etag"] = self.etag
        return Response(200, encode(self.bodies[url]), resp_headers)
