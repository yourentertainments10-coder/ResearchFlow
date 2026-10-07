"""The portal fetcher's rules, with no network: every request goes through an injected getter."""

from __future__ import annotations

import json

import pytest

from portal_fake import BORNAN, UA, Portal, listing, real_pages, wire  # noqa: F401
from sie.pipeline.scheduler import FetchError
from sie.sources.bornan.fetch import (
    MAX_BODY_BYTES,
    PortalClient,
    Response,
    check_user_agent,
    fetch_capture,
)
from sie.sources.bornan.to_parsed import capture_to_parsed


def client(portal, **kw):
    return PortalClient(UA, get=portal, sleep=kw.pop("sleep", lambda s: None), **kw)


def test_capture_has_every_discipline_and_parses_like_a_browser_capture():
    portal = Portal(real_pages())
    capture = json.loads(fetch_capture(client(portal)))
    assert sorted(capture["medals"]) == ["ARC", "SWM"]
    assert portal.calls == ["ALL/disc/data", "ARC/medals/discipline", "SWM/medals/discipline"]
    rows = capture_to_parsed(json.dumps(capture).encode(), "asiad-2026")
    assert len(rows) == 30 + 123


def test_same_data_gives_identical_bytes():
    a = fetch_capture(client(Portal(real_pages())))
    b = fetch_capture(client(Portal(real_pages())))
    assert a == b


def test_only_public_result_endpoints_are_requested():
    portal = Portal(real_pages())
    fetch_capture(client(portal))
    assert all(c == "ALL/disc/data" or c.endswith("/medals/discipline") for c in portal.calls)
    assert not any("entries" in c for c in portal.calls)


def test_identifies_itself_and_requires_a_contact():
    portal = Portal(real_pages())
    fetch_capture(client(portal))
    assert {h["User-Agent"] for h in portal.headers} == {UA}
    with pytest.raises(FetchError, match="HTTP_USER_AGENT is not set"):
        check_user_agent("SIE-research/0.1 (set HTTP_USER_AGENT with a contact address)")
    with pytest.raises(FetchError):
        PortalClient("   ", get=portal)


def test_waits_between_requests():
    now, sleeps = [100.0], []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    portal = Portal(real_pages())
    c = PortalClient(UA, min_interval=2.0, get=portal, sleep=sleep, clock=lambda: now[0])
    fetch_capture(c)
    assert len(portal.calls) == 3 and sleeps == [2.0, 2.0]  # no wait before the first request


@pytest.mark.parametrize("status", [429, 503, 500, 404, 403, 302])
def test_a_bad_status_stops_immediately_and_keeps_the_status(status):
    pages = real_pages()
    pages["ARC/medals/discipline"] = status
    portal = Portal(pages)
    with pytest.raises(FetchError) as err:
        fetch_capture(client(portal))
    assert err.value.http_status == status
    assert portal.calls == ["ALL/disc/data", "ARC/medals/discipline"]  # SWM never requested


@pytest.mark.parametrize("bad", ["../x", "swm", "SW", "SWMM", "S W", None, 5])
def test_a_discipline_code_is_checked_before_it_reaches_a_url(bad):
    portal = Portal({"ALL/disc/data": [{"Disc": bad}]})
    with pytest.raises(FetchError, match="unexpected discipline code"):
        fetch_capture(client(portal))
    assert portal.calls == ["ALL/disc/data"]


@pytest.mark.parametrize("listing_", [[], {}, "x", [{"Disc": "ARC"}, {"Disc": "ARC"}]])
def test_an_unusable_discipline_list_fails(listing_):
    with pytest.raises(FetchError):
        fetch_capture(client(Portal({"ALL/disc/data": listing_})))


def test_a_missing_discipline_means_no_capture_at_all():
    pages = real_pages()
    pages["SWM/medals/discipline"] = 500
    with pytest.raises(FetchError):
        fetch_capture(client(Portal(pages)))


def test_a_payload_that_is_not_a_list_of_rows_fails():
    pages = real_pages()
    pages["ARC/medals/discipline"] = {"error": "x"}
    with pytest.raises(FetchError, match="expected a list of medal rows"):
        fetch_capture(client(Portal(pages)))


def test_an_undecodable_body_names_the_endpoint():
    def get(url, headers, timeout):
        return Response(200, b"<html>not the api</html>")

    with pytest.raises(FetchError, match="ALL/disc/data"):
        fetch_capture(PortalClient(UA, get=get, sleep=lambda s: None))


def test_an_oversized_body_is_refused():
    def get(url, headers, timeout):
        return Response(200, b"x" * (MAX_BODY_BYTES + 1))

    with pytest.raises(FetchError, match="larger than"):
        fetch_capture(PortalClient(UA, get=get, sleep=lambda s: None))


def test_the_real_getter_does_not_follow_redirects_and_reports_timeouts():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from sie.sources.bornan.fetch import urllib_get

    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = urllib_get(f"http://127.0.0.1:{server.server_port}/start", {"User-Agent": UA}, 5)
    finally:
        server.shutdown()
    assert resp.status == 302 and hits == ["/start"]
    with pytest.raises(FetchError, match="request failed"):
        urllib_get("http://127.0.0.1:1/", {"User-Agent": UA}, 1)
