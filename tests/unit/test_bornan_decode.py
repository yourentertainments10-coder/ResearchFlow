"""The decoder is tested on a real capture (tests/fixtures/sources/bornan/README.md)."""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from sie.sources.bornan.decode import PayloadDecodeError, decode_payload, inflate_payload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "bornan"
RAW = (FIXTURES / "ALL_medals_standings.raw").read_bytes()
DECODED = (FIXTURES / "ALL_medals_standings.decoded.json").read_bytes()


def _wrap(payload: bytes) -> bytes:
    """Build a body the way the portal does: bytes -> one character each -> UTF-8."""
    return zlib.compress(payload).decode("latin-1").encode("utf-8")


def test_the_real_capture_is_what_the_docs_say():
    assert len(RAW) == 3167
    assert RAW[:2] == b"x\xc2"  # 0x78 then the UTF-8 form of 0x9C


def test_inflate_reproduces_the_decoded_fixture_exactly():
    assert inflate_payload(RAW) == DECODED


def test_decode_payload_returns_the_standings():
    data = decode_payload(RAW)
    assert isinstance(data, list) and len(data) == 40
    ind = next(e for e in data if e["Org"] == "IND")
    assert ind["Count"]["total"]["total"] == 85


def test_the_captured_table_is_internally_consistent():
    for entry in decode_payload(RAW):
        for medal, counts in entry["Count"].items():
            assert counts["M"] + counts["W"] + counts["X"] == counts["total"], (entry["Org"], medal)
        c = entry["Count"]
        assert (
            c["ME_GOLD"]["total"] + c["ME_SILVER"]["total"] + c["ME_BRONZE"]["total"]
            == (c["total"]["total"])
        )


def test_round_trip_with_the_same_wrapping():
    value = {"a": [1, 2, "é", "日本"]}
    assert decode_payload(_wrap(json.dumps(value).encode("utf-8"))) == value


@pytest.mark.parametrize(
    "body",
    [
        b"",  # empty
        b'[{"plain": "json"}]',  # not compressed: no zlib header
        b"\xff\xfe\xfa",  # not UTF-8 text
        "日本語".encode(),  # characters above U+00FF
        RAW[:-20],  # truncated stream
        _wrap(b"not json at all"),  # decompresses, but is not JSON
    ],
)
def test_unknown_shapes_fail_loudly(body):
    with pytest.raises(PayloadDecodeError):
        decode_payload(body)


def test_oversized_output_is_refused(monkeypatch):
    monkeypatch.setattr("sie.sources.bornan.decode.MAX_DECODED_BYTES", 100)
    with pytest.raises(PayloadDecodeError, match="exceeds"):
        inflate_payload(_wrap(b"0" * 10_000))
