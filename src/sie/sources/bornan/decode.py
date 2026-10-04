"""Decode the portal's API payloads. Pure functions: no network, no database.

Observed on 2026-10-04 (docs/SOURCE_DISCOVERY.md section 8): the API answers with
``Content-Type: application/json; charset=utf-8`` but the body is not JSON. It is a zlib stream whose
bytes were written out as text: every byte 0x00-0xFF became one character (U+0000-U+00FF) and the text
was then encoded as UTF-8. Decoding reverses those steps:

    UTF-8 text -> one byte per character (latin-1) -> zlib inflate -> UTF-8 JSON

This is ordinary compression with no key. Anything that does not match this shape raises
``PayloadDecodeError`` instead of being guessed at (AGENTS.md rule: parsers fail loudly).
"""

from __future__ import annotations

import json
import zlib
from typing import Any

# A real payload is a few kB. The cap stops a corrupt or hostile stream from exhausting memory.
MAX_DECODED_BYTES = 50 * 1024 * 1024


class PayloadDecodeError(ValueError):
    """The response body is not in the shape this decoder knows."""


def inflate_payload(raw: bytes) -> bytes:
    """Return the decompressed bytes of a raw response body."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadDecodeError(f"body is not valid UTF-8 text: {exc}") from exc
    try:
        compressed = text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise PayloadDecodeError(
            "body contains characters above U+00FF, so it is not a byte-per-character stream"
        ) from exc
    if (
        len(compressed) < 2
        or (compressed[0] * 256 + compressed[1]) % 31 != 0
        or compressed[0] != 0x78
    ):
        raise PayloadDecodeError("data does not start with a zlib header (0x78 ..)")
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(compressed, MAX_DECODED_BYTES)
    except zlib.error as exc:
        raise PayloadDecodeError(f"zlib could not inflate the data: {exc}") from exc
    if inflater.unconsumed_tail:
        raise PayloadDecodeError(f"decoded data exceeds {MAX_DECODED_BYTES} bytes")
    if not inflater.eof:
        raise PayloadDecodeError("zlib stream is truncated")
    return out


def decode_payload(raw: bytes) -> Any:
    """Return the parsed JSON value of a raw response body."""
    inflated = inflate_payload(raw)
    try:
        return json.loads(inflated.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadDecodeError(f"decoded data is not valid JSON: {exc}") from exc
