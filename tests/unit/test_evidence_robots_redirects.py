"""robots.txt redirects: bounded, each hop validated, fail closed (ADR-044, review of live discovery)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sie.evidence.errors import FetchRefused
from sie.evidence.web import MAX_ROBOTS_REDIRECTS, RawResponse, WebFetcher

UA = "SIE-research/0.1 (contact: owner@example.org)"
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
HOST = "data.example.org"
ROBOTS = f"https://{HOST}/robots.txt"
PAGE = f"https://{HOST}/results.csv"
ALLOW = RawResponse(200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n")
CSV = RawResponse(200, {"content-type": "text/csv"}, b"a,b\n1,2\n")


def moved(to: str, status: int = 301) -> RawResponse:
    return RawResponse(status, {"location": to}, b"")


class Net:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def get(self, url, headers, timeout, max_bytes):
        self.requests.append(url)
        return self.routes.get(url, RawResponse(404, {}, b""))


class Clock:
    t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def fetcher(net, resolver=lambda h, p: ["93.184.216.34"]):
    c = Clock()
    return WebFetcher(
        user_agent=UA, get=net.get, resolver=resolver, clock=c, sleep=c.sleep, now=lambda: NOW
    )


def test_a_same_host_robots_redirect_is_followed():
    net = Net({ROBOTS: moved(f"https://{HOST}/other-robots.txt"),
               f"https://{HOST}/other-robots.txt": ALLOW, PAGE: CSV})  # fmt: skip
    assert fetcher(net).fetch(PAGE).body == b"a,b\n1,2\n"
    assert net.requests[:2] == [ROBOTS, f"https://{HOST}/other-robots.txt"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_status_to_www_is_followed(status):
    www = f"https://www.{HOST}/robots.txt"
    net = Net({ROBOTS: moved(www, status), www: ALLOW, PAGE: CSV})
    assert fetcher(net).allowed(PAGE) is True


def test_the_redirected_robots_rules_are_obeyed():
    www = f"https://www.{HOST}/robots.txt"
    deny = RawResponse(200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /\n")
    net = Net({ROBOTS: moved(www), www: deny, PAGE: CSV})
    assert fetcher(net).allowed(PAGE) is False
    with pytest.raises(FetchRefused) as e:
        fetcher(net).fetch(PAGE)
    assert e.value.code == "robots" and PAGE not in net.requests


def test_a_relative_location_is_resolved():
    net = Net({ROBOTS: moved("/new/robots.txt"), f"https://{HOST}/new/robots.txt": ALLOW})
    assert fetcher(net).allowed(PAGE) is True


def test_five_redirects_are_followed_a_sixth_is_refused():
    def chain(n):
        routes = {ROBOTS: moved(f"https://{HOST}/r1")}
        for i in range(1, n):
            routes[f"https://{HOST}/r{i}"] = moved(f"https://{HOST}/r{i + 1}")
        routes[f"https://{HOST}/r{n}"] = ALLOW
        return Net(routes)

    assert MAX_ROBOTS_REDIRECTS == 5
    assert fetcher(chain(5)).allowed(PAGE) is True
    with pytest.raises(FetchRefused) as e:
        fetcher(chain(6)).allowed(PAGE)
    assert e.value.code == "robots" and "more than 5" in str(e.value)


def test_a_redirect_loop_is_refused():
    net = Net({ROBOTS: moved(f"https://{HOST}/a"), f"https://{HOST}/a": moved(ROBOTS)})
    with pytest.raises(FetchRefused) as e:
        fetcher(net).allowed(PAGE)
    assert e.value.code == "robots" and "loop" in str(e.value)


@pytest.mark.parametrize(
    "target",
    [
        f"http://{HOST}/robots.txt",  # downgrade to http
        "https://user:pw@data.example.org/robots.txt",  # credentials
        "https://data.example.org:8443/robots.txt",  # port outside the allow-list
        "ftp://data.example.org/robots.txt",
    ],
)
def test_an_unsafe_redirect_target_is_refused_not_read_as_no_robots(target):
    net = Net({ROBOTS: moved(target), PAGE: CSV})
    with pytest.raises(FetchRefused) as e:
        fetcher(net).fetch(PAGE)
    assert e.value.code == "robots" and PAGE not in net.requests


def test_a_redirect_to_a_private_address_is_refused_before_any_request_to_it():
    internal = "https://internal.example.net/robots.txt"
    net = Net({ROBOTS: moved(internal), internal: ALLOW})
    resolver = lambda h, p: ["10.0.0.5"] if h == "internal.example.net" else ["93.184.216.34"]  # noqa: E731
    with pytest.raises(FetchRefused) as e:
        fetcher(net, resolver).allowed(PAGE)
    assert e.value.code == "robots" and internal not in net.requests


def test_a_redirect_with_no_location_is_refused():
    net = Net({ROBOTS: RawResponse(301, {}, b"")})
    with pytest.raises(FetchRefused) as e:
        fetcher(net).allowed(PAGE)
    assert e.value.code == "robots"


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_a_forbidden_or_failing_robots_after_a_redirect_fails_closed(status):
    www = f"https://www.{HOST}/robots.txt"
    net = Net({ROBOTS: moved(www), www: RawResponse(status, {}, b""), PAGE: CSV})
    with pytest.raises(FetchRefused) as e:
        fetcher(net).fetch(PAGE)
    assert e.value.code == "robots" and PAGE not in net.requests


def test_a_redirect_to_a_missing_robots_file_means_no_rules_like_a_direct_404():
    www = f"https://www.{HOST}/robots.txt"
    net = Net({ROBOTS: moved(www), www: RawResponse(404, {}, b""), PAGE: CSV})
    assert fetcher(net).allowed(PAGE) is True


def test_the_redirected_robots_file_is_cached_per_host():
    www = f"https://www.{HOST}/robots.txt"
    net = Net({ROBOTS: moved(www), www: ALLOW, PAGE: CSV})
    f = fetcher(net)
    f.allowed(PAGE)
    f.allowed(PAGE)
    assert net.requests.count(ROBOTS) == 1


def test_a_page_redirect_to_another_host_is_still_refused():
    net = Net({ROBOTS: ALLOW, PAGE: moved("https://other.example.org/x.csv")})
    with pytest.raises(FetchRefused) as e:
        fetcher(net).fetch(PAGE)
    assert e.value.code == "redirect"
