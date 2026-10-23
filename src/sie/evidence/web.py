"""Polite, safe web fetching for evidence (ADR-044). Network access is injected, so every rule is tested offline.

Rules, all enforced here:

* https only, no credentials in the URL, port 443 unless a caller allows another port explicitly;
* the host is resolved first and every address must be globally routable (no loopback, private,
  link-local, multicast, reserved or metadata addresses); the address actually connected to is checked
  again, which closes the DNS-rebinding gap;
* ``robots.txt`` is fetched and obeyed per host; an unreachable or forbidden robots file stops the fetch;
* honest User-Agent with a contact (the placeholder is refused); at least ``interval`` seconds between
  requests to one host; a 403, 429 or 5xx stops at once, nothing is retried here;
* at most one redirect, and only to the same host (re-validated like any other URL);
* identity encoding only, a content-type allow-list, a byte cap and a timeout;
* nothing is executed or rendered; no JavaScript, no logins, no paywalls.

This module only fetches. Parsing is elsewhere (AGENTS.md rule 14).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from sie.evidence.errors import FetchRefused

PLACEHOLDER_UA = "set HTTP_USER_AGENT"
ALLOWED_TYPES = ("text/html", "text/plain", "text/csv", "application/json", "application/csv")
MAX_URL_CHARS = 2000


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes
    peer_ip: str | None = None  # the address actually connected to, when known


# get(url, headers, timeout, max_bytes) -> RawResponse. Must not follow redirects.
Getter = Callable[[str, dict[str, str], float, int], RawResponse]
Resolver = Callable[[str, int], list[str]]


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    retrieved_at: datetime
    robots_checked_at: datetime
    notes: tuple[str, ...] = field(default_factory=tuple)


def is_public_address(address: str) -> bool:
    """True only for a globally routable address. Unparseable text is never public."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str, *, allow_ports: tuple[int, ...] = (443,)) -> tuple[str, str, int]:
    """Validate a URL for fetching; returns (url without fragment, host, port). Raises FetchRefused."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_URL_CHARS:
        raise FetchRefused("bad_url", "the URL is empty or too long")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise FetchRefused("bad_scheme", "only https URLs are fetched")
    if parts.username or parts.password:
        raise FetchRefused("credentials", "a URL with credentials is never fetched")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host and ":" not in host:
        raise FetchRefused("bad_host", "the URL has no usable public host name")
    try:
        port = parts.port or 443
    except ValueError:
        raise FetchRefused("bad_url", "the URL has an invalid port") from None
    if port not in allow_ports:
        raise FetchRefused("bad_port", f"port {port} is not allowed")
    clean = parts._replace(fragment="", netloc=parts.netloc.lower()).geturl()
    return clean, host, port


def system_resolver(host: str, port: int) -> list[str]:
    try:
        return sorted(
            {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        )
    except socket.gaierror as exc:
        raise FetchRefused("dns", f"the host name does not resolve: {exc.strerror or exc}") from exc


def check_user_agent(user_agent: str) -> None:
    if PLACEHOLDER_UA in user_agent or not user_agent.strip():
        raise FetchRefused(
            "user_agent",
            "HTTP_USER_AGENT is not set: web fetching must identify itself with a contact address",
        )


def urllib_getter(url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> RawResponse:
    """The real network call: https, no redirects, identity encoding, peer address re-checked.

    Not exercised by CI (no network there); the rules around it are, through injected getters.
    """
    parts = urlsplit(url)
    conn = http.client.HTTPSConnection(
        parts.hostname, parts.port or 443, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        conn.connect()
        peer = conn.sock.getpeername()[0] if conn.sock else None
        if peer is None or not is_public_address(peer):
            raise FetchRefused("ssrf", "the connection went to a non-public address")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", path, headers={**headers, "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        body = resp.read(max_bytes + 1)
        return RawResponse(resp.status, {k.lower(): v for k, v in resp.getheaders()}, body, peer)
    except (OSError, http.client.HTTPException) as exc:
        raise FetchRefused("network", f"the request failed: {type(exc).__name__}") from exc
    finally:
        conn.close()


class WebFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        get: Getter = urllib_getter,
        resolver: Resolver = system_resolver,
        interval: float = 2.0,
        max_bytes: int = 5 * 1024 * 1024,
        timeout: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        allow_ports: tuple[int, ...] = (443,),
    ) -> None:
        check_user_agent(user_agent)
        if interval < 2.0:
            raise ValueError("the interval between requests to one host is at least 2 seconds")
        self.user_agent, self._get, self._resolve = user_agent, get, resolver
        self.interval, self.max_bytes, self.timeout = interval, max_bytes, timeout
        self._clock, self._sleep, self._now, self._ports = clock, sleep, now, allow_ports
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[RobotFileParser, datetime]] = {}
        self._stopped: dict[str, str] = {}  # host -> code, after a 403, 429 or 5xx

    # -- one request ---------------------------------------------------------------------------
    def _request(self, url: str, host: str, port: int, accept: str) -> RawResponse:
        if host in self._stopped:
            raise FetchRefused(
                "stopped", f"requests to {host} were stopped earlier: {self._stopped[host]}"
            )
        addresses = self._resolve(host, port)
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise FetchRefused("ssrf", "the host resolves to a non-public address")
        wait = self._last.get(host, float("-inf")) + self.interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last[host] = self._clock()
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        resp = self._get(url, headers, self.timeout, self.max_bytes)
        if resp.peer_ip is not None and not is_public_address(resp.peer_ip):
            raise FetchRefused("ssrf", "the connection went to a non-public address")
        if resp.status in (403, 429) or resp.status >= 500:
            self._stopped[host] = f"HTTP {resp.status}"
            raise FetchRefused("status", f"HTTP {resp.status} from {host}: stopped, not retried")
        if len(resp.body) > self.max_bytes:
            raise FetchRefused("too_large", f"the response is larger than {self.max_bytes} bytes")
        return resp

    # -- robots ---------------------------------------------------------------------------------
    def _robots_for(self, host: str, port: int) -> tuple[RobotFileParser, datetime]:
        if host in self._robots:
            return self._robots[host]
        url = f"https://{host}/robots.txt"
        parser = RobotFileParser()
        try:
            resp = self._request(url, host, port, "text/plain")
        except FetchRefused as exc:
            if exc.code == "status":
                raise FetchRefused(
                    "robots", f"robots.txt could not be read ({exc}); not fetching"
                ) from exc
            raise
        if resp.status in (401,):
            raise FetchRefused("robots", "robots.txt requires a login; not fetching")
        if resp.status == 404 or resp.status == 410:
            parser.parse([])  # no robots file: everything allowed
        elif 200 <= resp.status < 300:
            parser.parse(resp.body.decode("utf-8", errors="replace").splitlines())
        else:
            raise FetchRefused("robots", f"robots.txt answered HTTP {resp.status}; not fetching")
        self._robots[host] = (parser, self._now())
        return self._robots[host]

    # -- public ---------------------------------------------------------------------------------
    def fetch(self, url: str) -> FetchedPage:
        """Fetch one page. Raises FetchRefused with a code when any rule says no."""
        current, host, port = check_url(url, allow_ports=self._ports)
        original = current
        notes: list[str] = []
        for hop in range(2):  # the page, then at most one same-host redirect
            parser, checked_at = self._robots_for(host, port)
            if not parser.can_fetch(self.user_agent, current):
                raise FetchRefused("robots", "robots.txt disallows this URL for automated clients")
            resp = self._request(current, host, port, ", ".join(ALLOWED_TYPES))
            if resp.status in (301, 302, 303, 307, 308):
                if hop == 1:
                    raise FetchRefused("redirect", "more than one redirect")
                target = urljoin(current, resp.headers.get("location", ""))
                nxt, nhost, _ = check_url(target, allow_ports=self._ports)
                if nhost != host:
                    raise FetchRefused("redirect", "redirect to another host is not followed")
                notes.append(f"redirected once to {nxt}")
                current = nxt
                continue
            if not 200 <= resp.status < 300:
                raise FetchRefused("status", f"HTTP {resp.status}")
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_TYPES:
                raise FetchRefused(
                    "content_type", f"content type {ctype or 'missing'} is not accepted"
                )
            if not resp.body:
                raise FetchRefused("empty", "the response body is empty")
            if resp.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise FetchRefused("encoding", "compressed responses are not accepted")
            return FetchedPage(
                original,
                current,
                resp.status,
                ctype,
                resp.body,
                self._now(),
                checked_at,
                tuple(notes),
            )
        raise FetchRefused("redirect", "more than one redirect")  # pragma: no cover


def fetch_into_project(
    engine,
    scope,
    fetcher: WebFetcher,
    url: str,
    *,
    title: str,
    tier: int,
    now: datetime,
    retention_mode: str = "excerpt",
    policies=None,
    added_by: str | None = None,
):
    """Fetch one page and record it as a web evidence source of the project (excerpt by default)."""
    from sie.evidence.retention import register_fetched_source

    page = fetcher.fetch(url)
    return page, register_fetched_source(
        engine,
        scope,
        content=page.body,
        requested_url=page.requested_url,
        final_url=page.final_url,
        http_status=page.status,
        content_type=page.content_type,
        title=title,
        tier=tier,
        now=now,
        retention_mode=retention_mode,
        policies=policies,
        added_by=added_by,
    )
