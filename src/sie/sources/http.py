"""Polite HTTP for source adapters: network access and nothing else (docs/DATA_PIPELINE.md section 10).

Every rule in AGENTS.md rule 9 is enforced here, once, so no adapter can forget one:

* https only, and only hosts the adapter was configured with;
* an honest ``User-Agent`` that carries a contact address (the default placeholder is refused);
* at least ``min_interval`` seconds between requests to one host, never parallel;
* every response is cached on disk; a repeat request is conditional (``If-None-Match`` or
  ``If-Modified-Since``) and a 304 serves the cached bytes;
* HTTP 403 or 429 stops fetching for good in this client (``FetchBlocked``): no retry, no workaround;
* a response larger than ``max_bytes`` is refused.

Pure standard library (``urllib``), like ``pipeline/notify.py``: no new dependency. The transport is
injectable so tests never touch the network. Adapters import this module; parsers never do.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
PLACEHOLDER_MARKER = "set HTTP_USER_AGENT"


class FetchError(Exception):
    """A request failed. ``http_status`` is set when the server answered."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class FetchBlocked(FetchError):
    """The server said 403 or 429. Fetching from this client stops: we do not retry or work around."""


class FetchRefused(FetchError):
    """We refused to send the request (not https, host not allowed, no contact in the User-Agent)."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


# (url, request headers, timeout) -> Response. Raises FetchError on network errors.
Transport = Callable[[str, dict[str, str], float], Response]


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return Response(
                response.status,
                response.read(DEFAULT_MAX_BYTES + 1),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        # 304 (not modified) and error statuses are answers, not network failures.
        return Response(exc.code, b"", {k.lower(): v for k, v in exc.headers.items()})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"network error: {exc}") from exc


def _has_contact(user_agent: str) -> bool:
    return PLACEHOLDER_MARKER not in user_agent and ("@" in user_agent or "http" in user_agent)


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str,
        allowed_hosts: set[str],
        cache_dir: Path,
        min_interval: float = 2.0,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.user_agent = user_agent
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._last_request: dict[str, float] = {}
        self.blocked: str | None = None  # set once the server answers 403 or 429
        self.requests_sent = 0

    def get(self, url: str) -> tuple[Response, bool, datetime]:
        """Return ``(response, from_cache, fetched_at)``. A 304 is returned as the cached 200."""
        if self.blocked:
            raise FetchBlocked(f"fetching is stopped for this run: {self.blocked}")
        host = self._check_allowed(url)
        cached = self._read_cache(url)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json, text/plain, */*"}
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]

        self._wait_for_turn(host)
        response = self._transport(url, headers, self.timeout)
        self.requests_sent += 1
        fetched_at = self._now()

        if response.status in (403, 429):
            self.blocked = f"HTTP {response.status} from {host}"
            raise FetchBlocked(
                f"HTTP {response.status} for {url}: the server asked us to stop", response.status
            )
        if response.status == 304 and cached:
            return Response(200, cached["body"], cached["headers"]), True, fetched_at
        if response.status != 200:
            raise FetchError(f"HTTP {response.status} for {url}", response.status)
        if len(response.body) > self.max_bytes:
            raise FetchError(f"response for {url} is larger than {self.max_bytes} bytes")
        self._write_cache(url, response)
        return response, False, fetched_at

    # --- rules ---------------------------------------------------------------------------------------

    def _check_allowed(self, url: str) -> str:
        parts = urlparse(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            raise FetchRefused(f"refusing {url!r}: only https is allowed")
        if host not in self.allowed_hosts:
            raise FetchRefused(f"refusing {url!r}: host {host!r} is not configured for this source")
        if not _has_contact(self.user_agent):
            raise FetchRefused(
                "refusing to fetch: set HTTP_USER_AGENT to an honest agent name with a contact "
                "address (an email or a URL), for example 'SIE-research/0.1 (you@example.org)'"
            )
        return host

    def _wait_for_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    # --- cache ---------------------------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, url: str) -> dict | None:
        path = self._cache_path(url)
        body_path = path.with_suffix(".body")
        if not (path.exists() and body_path.exists()):
            return None
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            return {**meta, "body": body_path.read_bytes()}
        except (OSError, ValueError):
            return None  # a damaged cache entry is ignored, never trusted

    def _write_cache(self, url: str, response: Response) -> None:
        etag = response.headers.get("etag", "")
        modified = response.headers.get("last-modified", "")
        if not (etag or modified):
            return  # nothing to make a conditional request with: do not keep a stale copy around
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(url)
        path.with_suffix(".body").write_bytes(response.body)
        path.write_text(
            json.dumps(
                {
                    "url": url,
                    "etag": etag,
                    "last_modified": modified,
                    "headers": response.headers,
                }
            ),
            encoding="utf-8",
        )
