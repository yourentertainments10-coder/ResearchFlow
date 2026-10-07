"""The rules every adapter inherits (AGENTS.md rule 9, docs/DATA_PIPELINE.md section 10)."""

from __future__ import annotations

import pytest

from portal_fakes import BASE, CONTACT_AGENT, FakePortal
from sie.sources.http import (
    FetchBlocked,
    FetchError,
    FetchRefused,
    PoliteClient,
    Response,
)

URL = f"{BASE}/ALL/medals/standings"
HOST = "back.results.asiangames2026.org"


class Clock:
    """A fake clock whose sleep advances it, so spacing is testable without waiting."""

    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make(tmp_path, transport=None, clock=None, **kw) -> PoliteClient:
    clock = clock or Clock()
    return PoliteClient(
        user_agent=kw.pop("user_agent", CONTACT_AGENT),
        allowed_hosts={HOST},
        cache_dir=tmp_path / "cache",
        transport=transport or FakePortal(),
        sleep=clock.sleep,
        clock=clock.now,
        **kw,
    )


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/s/AG2026/en/ALL/medals/standings",
        "https://example.org/s/AG2026/en/ALL/medals/standings",
        "ftp://back.results.asiangames2026.org/x",
    ],
)
def test_only_https_and_only_the_configured_host(tmp_path, url):
    transport = FakePortal()
    with pytest.raises(FetchRefused):
        make(tmp_path, transport).get(url)
    assert transport.requests == []  # nothing was sent


@pytest.mark.parametrize(
    "agent",
    [
        "SIE-research/0.1 (set HTTP_USER_AGENT with a contact address)",
        "SIE-research/0.1",
        "",
    ],
)
def test_an_agent_without_a_contact_address_is_refused(tmp_path, agent):
    transport = FakePortal()
    with pytest.raises(FetchRefused, match="contact"):
        make(tmp_path, transport, user_agent=agent).get(URL)
    assert transport.requests == []


def test_the_user_agent_is_sent_and_a_url_contact_is_enough(tmp_path):
    transport = FakePortal()
    agent = "SIE-research/0.1 (+https://github.com/yourentertainments10-coder/ResearchFlow)"
    make(tmp_path, transport, user_agent=agent).get(URL)
    assert transport.requests[0][1]["User-Agent"] == agent


def test_requests_to_one_host_are_spaced_by_the_minimum_interval(tmp_path):
    clock = Clock()
    client = make(tmp_path, clock=clock, min_interval=2.0)
    client.get(URL)
    client.get(f"{BASE}/ARC/medals/standings")
    client.get(f"{BASE}/SWM/medals/standings")
    assert clock.slept == [2.0, 2.0]  # the first request waits for nothing


def test_time_already_passed_counts_towards_the_interval(tmp_path):
    clock = Clock()
    client = make(tmp_path, clock=clock, min_interval=2.0)
    client.get(URL)
    clock.t += 1.5
    client.get(f"{BASE}/ARC/medals/standings")
    assert clock.slept == [pytest.approx(0.5)]


@pytest.mark.parametrize("status", [403, 429])
def test_403_and_429_stop_all_fetching_with_no_retry(tmp_path, status):
    transport = FakePortal()
    transport.overrides[URL] = Response(status, b"")
    client = make(tmp_path, transport)
    with pytest.raises(FetchBlocked) as first:
        client.get(URL)
    assert first.value.http_status == status
    sent = len(transport.requests)
    with pytest.raises(FetchBlocked):  # even a different, fine URL is not tried any more
        client.get(f"{BASE}/ARC/medals/standings")
    assert len(transport.requests) == sent == 1
    assert client.blocked


def test_other_errors_are_failures_but_do_not_block(tmp_path):
    transport = FakePortal()
    transport.overrides[URL] = Response(500, b"")
    client = make(tmp_path, transport)
    with pytest.raises(FetchError) as caught:
        client.get(URL)
    assert not isinstance(caught.value, FetchBlocked) and caught.value.http_status == 500
    assert client.blocked is None
    response, _, _ = client.get(f"{BASE}/ARC/medals/standings")
    assert response.status == 200


def test_an_unknown_url_is_a_404_failure(tmp_path):
    with pytest.raises(FetchError) as caught:
        make(tmp_path).get(f"{BASE}/NOPE/medals/standings")
    assert caught.value.http_status == 404


def test_a_conditional_request_serves_the_cached_bytes_on_304(tmp_path):
    transport = FakePortal(etag='"v1"')
    first, from_cache, _ = make(tmp_path, transport).get(URL)
    assert not from_cache and "If-None-Match" not in transport.requests[0][1]

    again = make(tmp_path, transport)  # a new process: the cache is on disk
    second, from_cache, _ = again.get(URL)
    assert from_cache and second.status == 200 and second.body == first.body
    assert transport.requests[1][1]["If-None-Match"] == '"v1"'


def test_nothing_is_cached_when_the_server_gives_no_validator(tmp_path):
    transport = FakePortal()  # no ETag, no Last-Modified
    make(tmp_path, transport).get(URL)
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())


def test_a_damaged_cache_entry_is_ignored_not_trusted(tmp_path):
    transport = FakePortal(etag='"v1"')
    client = make(tmp_path, transport)
    client.get(URL)
    for meta in (tmp_path / "cache").glob("*.json"):
        meta.write_text("{not json", encoding="utf-8")
    response, from_cache, _ = make(tmp_path, transport).get(URL)
    assert response.status == 200 and not from_cache
    assert "If-None-Match" not in transport.requests[-1][1]


def test_an_oversized_response_is_refused(tmp_path):
    transport = FakePortal()
    client = make(tmp_path, transport, max_bytes=10)
    with pytest.raises(FetchError, match="larger"):
        client.get(URL)
