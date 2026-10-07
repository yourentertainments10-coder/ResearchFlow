"""Fetch the official portal's medal data and assemble the capture the parser already reads.

D1 (terms of use) was cleared by the owner on 2026-10-07 (ADR-031). The fetcher stays deliberately
polite and narrow, following AGENTS.md rule 9:

* one fixed host and path (``BASE_URL``), https only, no redirects followed, discipline codes checked
  before they go into a URL;
* an honest ``User-Agent`` with a contact address: it refuses to run with the placeholder default;
* at least ``min_interval`` seconds between requests (default 2);
* only public result endpoints: ``ALL/disc/data`` (the discipline list) and ``{DISC}/medals/discipline``.
  The ``entries/...`` endpoints carry birth dates and participant lists and are never requested;
* the response is size-capped; a 429 or 5xx stops the run at once (no further requests), and the
  scheduler's backoff and attempt limit apply (docs/DATA_PIPELINE.md section 12);
* all or nothing: a capture with a missing discipline is never produced, because a partial capture
  would look like withdrawn medals.

Network access is injected (``get``), so every rule above is tested without the network.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sie.pipeline.scheduler import FetchError
from sie.sources.bornan.decode import PayloadDecodeError, decode_payload

HOST = "back.results.asiangames2026.org"
BASE_URL = f"https://{HOST}/s/AG2026/en/"
MAX_BODY_BYTES = 5 * 1024 * 1024  # real payloads are a few kB up to a few hundred kB
PLACEHOLDER_UA = "set HTTP_USER_AGENT"
_DISC_CODE = re.compile(r"^[A-Z0-9]{3}$")


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


Getter = Callable[[str, dict[str, str], float], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect surfaces as an HTTPError and is reported, never followed


def urllib_get(url: str, headers: dict[str, str], timeout: float) -> Response:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310  (https, fixed host)
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            return Response(resp.status, body)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"request failed: {exc}") from exc


def check_user_agent(user_agent: str) -> None:
    if PLACEHOLDER_UA in user_agent or not user_agent.strip():
        raise FetchError(
            "HTTP_USER_AGENT is not set: the portal fetcher must identify itself with a contact "
            "address, for example 'SIE-research/0.1 (contact: you@example.org)'"
        )


class PortalClient:
    def __init__(
        self,
        user_agent: str,
        *,
        min_interval: float = 2.0,
        timeout: float = 30.0,
        get: Getter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        check_user_agent(user_agent)
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.min_interval, self.timeout = min_interval, timeout
        self._get, self._sleep, self._clock = get or urllib_get, sleep, clock
        self._last: float | None = None
        self.requests: list[str] = []

    def fetch(self, path: str) -> Any:
        """GET ``BASE_URL + path`` and return the decoded JSON. Raises ``FetchError`` on any problem."""
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        url = BASE_URL + path
        self._last = self._clock()
        self.requests.append(url)
        response = self._get(url, self.headers, self.timeout)
        if response.status != 200:
            raise FetchError(f"{path}: HTTP {response.status}", http_status=response.status)
        if len(response.body) > MAX_BODY_BYTES:
            raise FetchError(f"{path}: response larger than {MAX_BODY_BYTES} bytes")
        try:
            return decode_payload(response.body)
        except PayloadDecodeError as exc:
            raise FetchError(f"{path}: {exc}") from exc


def discipline_codes(listing: Any) -> list[str]:
    if not isinstance(listing, list) or not listing:
        raise FetchError("ALL/disc/data: expected a non-empty list of disciplines")
    codes = []
    for item in listing:
        code = item.get("Disc") if isinstance(item, dict) else None
        if not isinstance(code, str) or not _DISC_CODE.match(code):
            raise FetchError(f"ALL/disc/data: unexpected discipline code {code!r}")
        codes.append(code)
    if len(set(codes)) != len(codes):
        raise FetchError("ALL/disc/data: duplicate discipline codes")
    return sorted(codes)


def fetch_capture(client: PortalClient) -> bytes:
    """The capture JSON (``{"medals": {DISC: rows}}``) for every discipline, or ``FetchError``."""
    codes = discipline_codes(client.fetch("ALL/disc/data"))
    medals: dict[str, Any] = {}
    for code in codes:
        rows = client.fetch(f"{code}/medals/discipline")
        if not isinstance(rows, list):
            raise FetchError(f"{code}/medals/discipline: expected a list of medal rows")
        medals[code] = rows
    return json.dumps({"medals": medals}, sort_keys=True, separators=(",", ":")).encode("utf-8")
