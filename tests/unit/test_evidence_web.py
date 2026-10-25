"""Web fetching rules (ADR-044), offline: every network call is an injected fake."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sie.evidence.errors import FetchRefused
from sie.evidence.web import (
    RawResponse,
    WebFetcher,
    check_url,
    check_user_agent,
    is_public_address,
)

UA = "SIE-research/0.1 (contact: owner@example.org)"
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


class Site:
    """A fake network: maps URL to a response; records requests and resolved names."""

    def __init__(self, pages=None, robots="User-agent: *\nAllow: /\n", robots_status=200):
        self.pages = pages or {}
        self.robots = robots
        self.robots_status = robots_status
        self.requests: list[str] = []

    def get(self, url, headers, timeout, max_bytes):
        self.requests.append(url)
        assert headers["User-Agent"] == UA and "Accept-Encoding" not in headers
        if url.endswith("/robots.txt"):
            return RawResponse(
                self.robots_status, {"content-type": "text/plain"}, self.robots.encode()
            )
        return self.pages.get(url, RawResponse(404, {}, b"no"))


def page(body=b"<html>x</html>", ctype="text/html; charset=utf-8", status=200, **headers):
    return RawResponse(status, {"content-type": ctype, **headers}, body)


def fetcher(site, resolver=lambda h, p: ["93.184.216.34"], **kw):
    clock = kw.pop("clock", None) or Ticker()
    return WebFetcher(
        user_agent=UA,
        get=site.get,
        resolver=resolver,
        clock=clock,
        sleep=clock.sleep,
        now=lambda: NOW,
        **kw,
    )


class Ticker:
    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


U = "https://data.example.org/results"


@pytest.mark.parametrize(
    ("addr", "public"),
    [
        ("93.184.216.34", True), ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False), ("10.0.0.5", False), ("172.16.3.1", False), ("192.168.1.1", False),
        ("169.254.169.254", False), ("0.0.0.0", False), ("224.0.0.1", False), ("100.64.0.1", False),
        ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:127.0.0.1", False),
        ("::ffff:10.1.1.1", False), ("not-an-ip", False), ("", False),
    ],
)  # fmt: skip
def test_only_globally_routable_addresses_are_public(addr, public):
    assert is_public_address(addr) is public


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.org/x", "bad_scheme"), ("ftp://example.org/x", "bad_scheme"),
        ("file:///etc/passwd", "bad_scheme"), ("https://u:p@example.org/x", "credentials"),
        ("https://localhost/x", "bad_host"), ("https://intranet/x", "bad_host"), ("https:///x", "bad_host"),
        ("https://example.org:8443/x", "bad_port"), ("https://example.org:99999/x", "bad_url"),
        ("", "bad_url"), ("https://example.org/" + "a" * 2100, "bad_url"),
    ],
)  # fmt: skip
def test_unsafe_urls_are_refused(url, code):
    with pytest.raises(FetchRefused) as e:
        check_url(url)
    assert e.value.code == code


def test_a_good_url_is_normalised_without_its_fragment():
    assert check_url("https://Data.Example.ORG/a?b=1#top") == (
        "https://data.example.org/a?b=1",
        "data.example.org",
        443,
    )


def test_the_placeholder_user_agent_is_refused():
    with pytest.raises(FetchRefused) as e:
        check_user_agent("SIE-research/0.1 (set HTTP_USER_AGENT with a contact address)")
    assert e.value.code == "user_agent"
    with pytest.raises(FetchRefused):
        WebFetcher(user_agent="  ")


def test_an_interval_below_two_seconds_cannot_be_configured():
    with pytest.raises(ValueError):
        WebFetcher(user_agent=UA, interval=1.0)


def test_a_normal_fetch_returns_the_page_and_the_robots_check_time():
    site = Site({U: page(b"a,b\n1,2\n", "text/csv")})
    got = fetcher(site).fetch(U + "#frag")
    assert (got.status, got.content_type, got.body, got.final_url) == (
        200,
        "text/csv",
        b"a,b\n1,2\n",
        U,
    )
    assert got.requested_url == U and got.robots_checked_at == NOW
    assert site.requests == ["https://data.example.org/robots.txt", U]


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_a_host_that_resolves_to_a_private_address_is_never_requested(addr):
    site = Site({U: page()})
    with pytest.raises(FetchRefused) as e:
        fetcher(site, resolver=lambda h, p: [addr]).fetch(U)
    assert e.value.code == "ssrf" and site.requests == []


def test_one_private_address_among_public_ones_still_refuses():
    site = Site({U: page()})
    with pytest.raises(FetchRefused) as e:
        fetcher(site, resolver=lambda h, p: ["93.184.216.34", "10.0.0.1"]).fetch(U)
    assert e.value.code == "ssrf" and site.requests == []


def test_a_connection_that_lands_on_a_private_peer_is_refused_even_if_dns_looked_fine():
    site = Site()
    site.pages[U] = RawResponse(200, {"content-type": "text/html"}, b"x", peer_ip="127.0.0.1")
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "ssrf"


def test_robots_disallow_blocks_the_page_request():
    site = Site({U: page()}, robots="User-agent: *\nDisallow: /results\n")
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "robots" and U not in site.requests


def test_robots_that_names_this_client_is_obeyed():
    site = Site({U: page()}, robots="User-agent: SIE-research\nDisallow: /\n")
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "robots"


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_an_unreadable_robots_file_stops_the_fetch(status):
    site = Site({U: page()}, robots_status=status)
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "robots" and U not in site.requests


def test_a_missing_robots_file_means_allowed():
    site = Site({U: page()}, robots_status=404)
    assert fetcher(site).fetch(U).status == 200


def test_robots_is_fetched_once_per_host():
    site = Site({U: page(), U + "2": page()})
    f = fetcher(site)
    f.fetch(U)
    f.fetch(U + "2")
    assert site.requests.count("https://data.example.org/robots.txt") == 1


def test_requests_to_one_host_are_at_least_two_seconds_apart():
    clock = Ticker()
    site = Site({U: page(), U + "2": page()})
    f = fetcher(site, clock=clock)
    f.fetch(U)
    f.fetch(U + "2")
    assert clock.slept and all(s >= 1.99 for s in clock.slept)
    assert clock.slept[0] == pytest.approx(
        2.0
    )  # robots then page: the second request waits the full interval


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_a_403_429_or_5xx_stops_the_host_and_is_not_retried(status):
    site = Site({U: page(status=status), U + "2": page()})
    f = fetcher(site)
    with pytest.raises(FetchRefused) as e:
        f.fetch(U)
    assert e.value.code == "status"
    n = len(site.requests)
    with pytest.raises(FetchRefused) as e2:
        f.fetch(U + "2")
    assert e2.value.code == "stopped" and len(site.requests) == n  # nothing more was sent


def test_a_404_is_a_refusal_but_does_not_stop_the_host():
    site = Site({U + "2": page()})
    f = fetcher(site)
    with pytest.raises(FetchRefused):
        f.fetch(U)
    assert f.fetch(U + "2").status == 200


def test_one_same_host_redirect_is_followed_and_recorded():
    site = Site(
        {U: page(status=301, location="/final"), "https://data.example.org/final": page(b"ok")}
    )
    got = fetcher(site).fetch(U)
    assert (
        got.final_url == "https://data.example.org/final" and got.requested_url == U and got.notes
    )


def test_a_redirect_to_another_host_is_not_followed():
    site = Site({U: page(status=302, location="https://evil.example.net/x")})
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "redirect" and all("evil" not in r for r in site.requests)


def test_a_redirect_to_http_or_a_private_literal_is_refused():
    for target in ("http://data.example.org/x", "https://127.0.0.1/x"):
        site = Site({U: page(status=302, location=target)})
        with pytest.raises(FetchRefused):
            fetcher(site).fetch(U)


def _chain(n, final=True):
    pages = {U: page(status=302, location="/h1")}
    for i in range(1, n):
        pages[f"https://data.example.org/h{i}"] = page(status=302, location=f"/h{i + 1}")
    if final:
        pages[f"https://data.example.org/h{n}"] = page(b"ok")
    return pages


def test_up_to_three_redirects_are_followed_and_recorded():
    got = fetcher(Site(_chain(3))).fetch(U)
    assert got.final_url.endswith("/h3") and len(got.notes) == 3


def test_a_fourth_redirect_is_refused_and_the_message_lists_the_hosts():
    site = Site(_chain(4))
    with pytest.raises(FetchRefused, match="more than 3 redirects") as e:
        fetcher(site).fetch(U)
    assert e.value.code == "redirect" and "data.example.org -> data.example.org" in str(e.value)
    assert "https://data.example.org/h4" not in site.requests


def test_a_redirect_loop_is_refused():
    site = Site({U: page(status=302, location="/a"),
                 "https://data.example.org/a": page(status=302, location=U)})  # fmt: skip
    with pytest.raises(FetchRefused, match="loop"):
        fetcher(site).fetch(U)


def test_a_multi_hop_chain_through_the_www_host_checks_robots_for_each_host():
    www = "https://www.data.example.org/x"
    site = Site({U: page(status=301, location=www), www: page(status=302, location="/y"),
                 "https://www.data.example.org/y": page(b"ok")})  # fmt: skip
    assert fetcher(site).fetch(U).final_url.endswith("/y")
    assert site.requests.count("https://www.data.example.org/robots.txt") == 1


@pytest.mark.parametrize(
    "ctype", ["application/pdf", "image/png", "application/octet-stream", "", "text/javascript"]
)
def test_content_types_outside_the_allow_list_are_refused(ctype):
    site = Site({U: page(ctype=ctype)})
    with pytest.raises(FetchRefused) as e:
        fetcher(site).fetch(U)
    assert e.value.code == "content_type"


def test_the_size_cap_and_empty_and_compressed_bodies_are_refused():
    site = Site({U: page(b"x" * 2000)})
    with pytest.raises(FetchRefused) as e:
        fetcher(site, max_bytes=1000).fetch(U)
    assert e.value.code == "too_large"
    with pytest.raises(FetchRefused) as e:
        fetcher(Site({U: page(b"")})).fetch(U)
    assert e.value.code == "empty"
    with pytest.raises(FetchRefused) as e:
        fetcher(Site({U: page(b"zz", **{"content-encoding": "gzip"})})).fetch(U)
    assert e.value.code == "encoding"


def test_an_unresolvable_host_is_a_clean_refusal():
    def nxdomain(h, p):
        raise FetchRefused("dns", "does not resolve")

    with pytest.raises(FetchRefused) as e:
        fetcher(Site()).__class__(user_agent=UA, get=Site().get, resolver=nxdomain).fetch(U)
    assert e.value.code == "dns"


def test_a_redirect_to_the_www_variant_is_followed_and_its_robots_file_is_read():
    www = "https://www.data.example.org/final"
    seen = []

    class Net(Site):
        def get(self, url, headers, timeout, max_bytes):
            seen.append(url)
            return super().get(url, headers, timeout, max_bytes)

    site = Net({U: page(status=301, location=www), www: page(b"ok")})
    got = fetcher(site).fetch(U)
    assert got.final_url == www and any("related host" in n for n in got.notes)
    assert "https://www.data.example.org/robots.txt" in seen  # the new host's rules were consulted


def test_a_redirect_to_a_subdomain_is_followed_but_lookalikes_are_not():
    sub = "https://api.data.example.org/x"
    assert (
        fetcher(Site({U: page(status=302, location=sub), sub: page(b"ok")})).fetch(U).status == 200
    )
    for bad in ("https://evildata.example.org/x", "https://data.example.org.evil.net/x",
                "https://example.org/x"):  # fmt: skip
        site = Site({U: page(status=302, location=bad)})
        with pytest.raises(FetchRefused, match="another host") as e:
            fetcher(site).fetch(U)
        assert e.value.code == "redirect" and bad not in site.requests


def test_a_related_host_that_resolves_to_a_private_address_is_refused():
    www = "https://www.data.example.org/final"
    site = Site({U: page(status=301, location=www), www: page(b"ok")})
    f = fetcher(site, resolver=lambda h, p: ["10.0.0.9"] if h.startswith("www.") else ["93.184.216.34"])  # fmt: skip
    with pytest.raises(FetchRefused) as e:
        f.fetch(U)
    assert e.value.code == "ssrf" and www not in site.requests


def test_a_redirect_host_named_for_the_origin_is_followed_and_scoped_to_that_origin():
    target = "https://ckan.example.gov.uk/api/x"
    routes = {U: page(status=301, location=target), target: page(b"ok")}
    ok = fetcher(Site(routes), redirect_hosts={"data.example.org": ["ckan.example.gov.uk"]})
    assert ok.fetch(U).final_url == target
    with pytest.raises(FetchRefused, match="another host"):  # no entry: still refused
        fetcher(Site(routes)).fetch(U)
    with pytest.raises(FetchRefused, match="another host"):  # an entry for a different origin
        fetcher(Site(routes), redirect_hosts={"other.example.org": ["ckan.example.gov.uk"]}).fetch(U)  # fmt: skip


def test_a_named_redirect_host_is_still_checked_for_robots_and_private_addresses():
    target = "https://ckan.example.gov.uk/api/x"
    allow = {"data.example.org": ["ckan.example.gov.uk"]}
    site = Site({U: page(status=301, location=target), target: page(b"ok")})
    f = fetcher(site, resolver=lambda h, p: ["10.0.0.9"] if h.startswith("ckan.") else ["93.184.216.34"], redirect_hosts=allow)  # fmt: skip
    with pytest.raises(FetchRefused) as e:
        f.fetch(U)
    assert e.value.code == "ssrf" and target not in site.requests
    seen = Site({U: page(status=301, location=target), target: page(b"ok")})
    fetcher(seen, redirect_hosts=allow).fetch(U)
    assert "https://ckan.example.gov.uk/robots.txt" in seen.requests
