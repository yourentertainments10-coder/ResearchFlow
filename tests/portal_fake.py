"""A fake portal for the fetcher tests, shared by unit and integration tests (no network)."""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from sie.sources.bornan.fetch import BASE_URL, Response

BORNAN = Path(__file__).resolve().parent / "fixtures/sources/bornan"
UA = "SIE-research/0.1 (contact: owner@example.org)"


def wire(obj) -> bytes:
    """What the portal sends: zlib bytes written out as text (see decode.py)."""
    return zlib.compress(json.dumps(obj).encode()).decode("latin-1").encode("utf-8")


class Portal:
    """A fake portal. ``pages`` maps a path under BASE_URL to a payload or to a status code."""

    def __init__(self, pages):
        self.pages, self.calls, self.headers = pages, [], []

    def __call__(self, url, headers, timeout):
        assert url.startswith(BASE_URL)
        path = url[len(BASE_URL) :]
        self.calls.append(path)
        self.headers.append(headers)
        page = self.pages[path]
        return Response(page, b"") if isinstance(page, int) else Response(200, wire(page))


def listing(*codes):
    return [{"Disc": c, "DiscDesc": c} for c in codes]


def real_pages():
    arc = json.loads((BORNAN / "ARC_medals_discipline.json").read_text())
    swm = json.loads((BORNAN / "SWM_medals_discipline.json").read_text())
    return {
        "ALL/disc/data": listing("SWM", "ARC"),
        "ARC/medals/discipline": arc,
        "SWM/medals/discipline": swm,
    }
