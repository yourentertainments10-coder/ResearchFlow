"""The portal adapter, the capture assembler and the import rules, on the saved fixtures. No network."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from portal_fakes import BASE, BORNAN, CONTACT_AGENT, FakePortal, encode, portal_bodies
from sie.sources.base import DocumentRef
from sie.sources.bornan.adapter import PortalAdapter
from sie.sources.bornan.capture import assemble_capture
from sie.sources.bornan.decode import decode_payload
from sie.sources.bornan.to_parsed import capture_to_parsed
from sie.sources.http import FetchError, PoliteClient

SRC = Path(__file__).resolve().parents[2] / "src" / "sie" / "sources"
HOST = "back.results.asiangames2026.org"


def adapter(tmp_path, transport=None) -> tuple[PortalAdapter, FakePortal]:
    transport = transport or FakePortal()
    client = PoliteClient(
        user_agent=CONTACT_AGENT,
        allowed_hosts={HOST},
        cache_dir=tmp_path / "cache",
        transport=transport,
        sleep=lambda _s: None,
    )
    return PortalAdapter(client), transport


def test_the_documents_of_a_full_capture_are_listed_in_a_fixed_order(tmp_path):
    portal, _ = adapter(tmp_path)
    refs = portal.list_documents()
    assert [r.key for r in refs] == [
        "ALL:disc/data",
        "ALL:medals/standings",
        "ARC:medals/discipline",
        "ARC:medals/standings",
        "SWM:medals/discipline",
        "SWM:medals/standings",
    ]
    assert refs[2] == DocumentRef(key="ARC:medals/discipline", url=f"{BASE}/ARC/medals/discipline")


def test_a_document_is_asked_for_once_per_run(tmp_path):
    portal, transport = adapter(tmp_path)
    portal.fetch_all()
    portal.fetch_all()
    urls = [u for u, _ in transport.requests]
    assert len(urls) == len(set(urls)) == 6


def test_fetch_returns_the_bytes_exactly_as_received(tmp_path):
    portal, _ = adapter(tmp_path)
    document = portal.fetch(portal.ref("ARC:medals/discipline"))
    assert document.content == encode(portal_bodies()[f"{BASE}/ARC/medals/discipline"])
    assert document.http_status == 200 and document.fetched_at.tzinfo is not None
    assert "application/json" in document.content_type


def test_an_unexpected_discipline_list_is_a_fetch_error_not_a_guess(tmp_path):
    transport = FakePortal({f"{BASE}/ALL/disc/data": {"not": "a list"}})
    portal, _ = adapter(tmp_path, transport)
    with pytest.raises(FetchError, match="unexpected shape"):
        portal.list_documents()


def test_a_missing_discipline_document_fails_the_whole_capture(tmp_path):
    bodies = portal_bodies()
    del bodies[f"{BASE}/SWM/medals/standings"]
    portal, _ = adapter(tmp_path, FakePortal(bodies))
    with pytest.raises(FetchError) as caught:
        portal.fetch_all()
    assert caught.value.http_status == 404  # a partial capture is never assembled


# --- the assembled capture ---------------------------------------------------------------------------


def assembled(tmp_path, at=None) -> bytes:
    portal, _ = adapter(tmp_path)
    return assemble_capture(portal.fetch_all(), at=at)


def test_the_capture_has_the_shape_the_pipeline_already_loads(tmp_path):
    capture = json.loads(assembled(tmp_path))
    assert set(capture) == {"medals", "standings", "all_standings"}
    assert set(capture["medals"]) == set(capture["standings"]) == {"ARC", "SWM"}
    assert len(capture["all_standings"]) == 40


def test_without_a_time_two_fetches_are_byte_identical(tmp_path):
    """So an unchanged portal is detected as 'unchanged' by the raw store (change detection)."""
    assert assembled(tmp_path / "a") == assembled(tmp_path / "b")


def test_with_a_time_the_capture_carries_it_in_the_snippets_format(tmp_path):
    at = datetime(2026, 10, 4, 16, 21, 31, 114000, tzinfo=UTC)
    assert json.loads(assembled(tmp_path, at))["at"] == "2026-10-04T16:21:31.114Z"


def test_assembled_rows_equal_the_rows_of_the_owners_capture_format(tmp_path):
    """The fetched path and the file path produce exactly the same parsed rows."""
    owner = {
        "at": "2026-10-04T16:21:31.114Z",
        "medals": {
            d: json.loads((BORNAN / f"{d}_medals_discipline.json").read_text())
            for d in ("ARC", "SWM")
        },
        "standings": {
            d: json.loads((BORNAN / f"{d}_medals_standings.json").read_text())
            for d in ("ARC", "SWM")
        },
    }
    from_file = capture_to_parsed(json.dumps(owner).encode(), "asiad-2026")
    from_fetch = capture_to_parsed(assembled(tmp_path), "asiad-2026")
    assert from_fetch == from_file and len(from_fetch) == 153


def test_a_capture_missing_a_standings_document_is_refused(tmp_path):
    portal, _ = adapter(tmp_path)
    documents = portal.fetch_all()
    del documents["SWM:medals/standings"]
    with pytest.raises(ValueError, match="SWM"):
        assemble_capture(documents)


def test_the_fixture_bodies_survive_the_portal_encoding():
    value = {"a": [1, 2, "é"]}
    assert decode_payload(encode(value)) == value


# --- import rules (docs/ARCHITECTURE.md section 6) ----------------------------------------------------


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names |= {f"{module}.{a.name}" for a in node.names}
    return names


def _hit(imports: set[str], prefixes: set[str]) -> set[str]:
    return {i for i in imports if any(i == p or i.startswith(p + ".") for p in prefixes)}


def test_the_adapter_imports_no_parser_assembler_or_database_code():
    forbidden = {
        "sie.sources.bornan.parse_medals",
        "sie.sources.bornan.to_parsed",
        "sie.sources.bornan.capture",
        "sie.sources.bornan.fetch",
        "sie.pipeline",
        "sie.db",
        "sqlalchemy",
        "psycopg",
    }
    assert _hit(_imports(SRC / "bornan" / "adapter.py"), forbidden) == set()
    assert _hit(_imports(SRC / "http.py"), forbidden | {"sie.sources.bornan"}) == set()


def test_parsers_and_the_assembler_import_no_network_or_database_code():
    forbidden = {
        "sie.sources.http",
        "sie.sources.bornan.adapter",
        "urllib",
        "httpx",
        "requests",
        "sqlalchemy",
        "psycopg",
        "sie.db",
        "sie.pipeline",
    }
    for name in ("parse_medals.py", "to_parsed.py", "decode.py"):
        assert _hit(_imports(SRC / "bornan" / name), forbidden) == set(), name
    # The assembler needs the adapter's two key constants and nothing network-related.
    assert (
        _hit(_imports(SRC / "bornan" / "capture.py"), forbidden - {"sie.sources.bornan.adapter"})
        == set()
    )
