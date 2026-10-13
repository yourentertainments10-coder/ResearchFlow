"""Source locations: validate a locator, and resolve CSV and JSON locators against stored bytes.

This module is pure (bytes in, text out) so it is easy to test with hostile input. It checks only that a
value *is present at a stated place*; it never decides whether the value is true.

XLSX is deliberately absent. XLSX verification needs the shared datasets loader (cached values, merged
cells, epoch handling, ZIP-bomb defences), which is not implemented yet, and a second reader here would
duplicate it. Until then an XLSX claim can be stored but is never ``source_located``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sie.evidence.errors import EvidenceError

LOCATOR_KINDS = ("table_cell", "json_pointer", "xlsx_cell", "char_range", "page", "none")
MAX_ROWS = 1_000_000  # the datasets limits (docs/DATASET_ANALYTICS_DESIGN.md section 8)
MAX_COLUMNS = 500
MAX_CELL_CHARS = 32_768
DEADLINE_CHECK_EVERY = 10_000
ENCODINGS = ("utf-8", "utf-8-sig")
_CELL_REF = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_PLAIN_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class LocatorInvalid(EvidenceError):
    """The locator is malformed for its kind."""


class NotLocated(EvidenceError):
    """The locator is well formed but nothing usable is at that place. ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TimeBudgetExceeded(EvidenceError):
    """Reading the source ran past its time budget."""


@dataclass(frozen=True)
class Located:
    text: str  # the value exactly as stored in the source, NFC normalised
    kind: str  # 'string', 'number_literal' (JSON) or 'cell' (CSV)


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _int(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise LocatorInvalid(f"{name} must be an integer between {low} and {high}")
    return value


def validate_locator(kind: str, locator: dict[str, Any]) -> None:
    """Raise ``LocatorInvalid`` unless the locator has exactly the shape its kind needs."""
    if kind not in LOCATOR_KINDS:
        raise LocatorInvalid(f"unknown locator kind {kind!r}")
    if not isinstance(locator, dict):
        raise LocatorInvalid("locator must be an object")
    if kind == "none":
        if locator:
            raise LocatorInvalid("a 'none' locator must be empty")
    elif kind == "table_cell":
        _int(locator.get("row_id"), 1, MAX_ROWS, "row_id")
        column, index = locator.get("column"), locator.get("column_index")
        if column is None and index is None:
            raise LocatorInvalid("table_cell needs column or column_index")
        if column is not None and (not isinstance(column, str) or not 1 <= len(column) <= 300):
            raise LocatorInvalid("column must be a name of 1 to 300 characters")
        if index is not None:
            _int(index, 0, MAX_COLUMNS - 1, "column_index")
        delimiter = locator.get("delimiter")
        if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n"':
            raise LocatorInvalid("delimiter is required and must be one character (never sniffed)")
        if locator.get("encoding", "utf-8") not in ENCODINGS:
            raise LocatorInvalid(f"encoding must be one of {ENCODINGS}")
        fmt = locator.get("number_format")
        if fmt is not None:
            if not isinstance(fmt, dict) or set(fmt) - {"decimal_separator", "thousands_separator"}:
                raise LocatorInvalid(
                    "number_format has decimal_separator and thousands_separator only"
                )
            if fmt.get("decimal_separator", ".") not in (".", ","):
                raise LocatorInvalid("decimal_separator must be '.' or ','")
            if fmt.get("thousands_separator") not in (None, ",", ".", " ", "'"):
                raise LocatorInvalid("thousands_separator must be one of , . space '")
    elif kind == "json_pointer":
        pointer = locator.get("pointer")
        if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 1000:
            raise LocatorInvalid("pointer must be an RFC 6901 pointer starting with '/'")
    elif kind == "xlsx_cell":
        sheet, cell = locator.get("sheet"), locator.get("cell")
        if not isinstance(sheet, str) or not 1 <= len(sheet) <= 31:
            raise LocatorInvalid("sheet must be a name of 1 to 31 characters")
        if not isinstance(cell, str) or not _CELL_REF.match(cell):
            raise LocatorInvalid("cell must look like B7")
        if locator.get("epoch") not in (None, 1900, 1904):
            raise LocatorInvalid("epoch must be 1900 or 1904")
    elif kind == "char_range":
        start = _int(locator.get("start"), 0, 10**9, "start")
        if _int(locator.get("end"), 1, 10**9, "end") <= start:
            raise LocatorInvalid("end must be greater than start")
    elif kind == "page":
        _int(locator.get("page"), 1, 100_000, "page")


def parse_number(text: str, number_format: dict[str, str] | None = None) -> Decimal | None:
    """A deterministic parse of a numeral under a *declared* format; ``None`` if it is not one.

    Nothing is guessed: ``1,234`` is not a number unless the locator declares ``,`` as the thousands
    separator, and ``(5)`` or ``5%`` are never numbers here.
    """
    fmt = number_format or {}
    s = text.strip()
    thousands = fmt.get("thousands_separator")
    if thousands:
        s = s.replace(thousands, "")
    if fmt.get("decimal_separator", ".") == ",":
        s = s.replace(",", ".")
    if not _PLAIN_DECIMAL.match(s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:  # pragma: no cover - the regex already rules this out
        return None


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise TimeBudgetExceeded("reading the source exceeded its time budget")


def locate_csv(content: bytes, locator: dict[str, Any], deadline: float | None = None) -> Located:
    """The text of one CSV cell, read with the delimiter and encoding the locator records."""
    validate_locator("table_cell", locator)
    encoding = locator.get("encoding", "utf-8")
    try:
        decoded = content.decode("utf-8-sig" if encoding == "utf-8-sig" else "utf-8")
    except UnicodeDecodeError as exc:
        raise NotLocated("undecodable", f"the file is not valid {encoding}") from exc
    if encoding == "utf-8" and decoded.startswith("﻿"):
        decoded = decoded[1:]  # a BOM is allowed, as in the datasets design
    if "\x00" in decoded:
        raise NotLocated("nul_byte", "the file contains NUL bytes")
    try:
        reader = csv.reader(
            io.StringIO(decoded, newline=""), delimiter=locator["delimiter"], strict=True
        )
        header: list[str] | None = None
        target = locator["row_id"]
        for position, row in enumerate(reader):
            if position % DEADLINE_CHECK_EVERY == 0:
                _check_deadline(deadline)
            if len(row) > MAX_COLUMNS:
                raise NotLocated("too_many_columns", f"more than {MAX_COLUMNS} columns")
            if position == 0:
                header = row
                continue
            if position != target:
                continue
            column = _resolve_column(header or [], locator)
            if column >= len(row):
                raise NotLocated("ragged_row", "that row has no such column")
            if len(row[column]) > MAX_CELL_CHARS:
                raise NotLocated(
                    "cell_too_long", f"cell is longer than {MAX_CELL_CHARS} characters"
                )
            return Located(nfc(row[column]), "cell")
    except csv.Error as exc:
        raise NotLocated("malformed_csv", f"malformed CSV: {exc}") from exc
    raise NotLocated("row_not_found", f"no data row {locator['row_id']}")


def _resolve_column(header: list[str], locator: dict[str, Any]) -> int:
    name, index = locator.get("column"), locator.get("column_index")
    if name is not None:
        matches = [i for i, h in enumerate(header) if nfc(h) == nfc(name)]
        if not matches:
            raise NotLocated("column_not_found", "no column has that header")
        if len(matches) > 1 and index is None:
            raise NotLocated(
                "ambiguous_column", "several columns share that header; give column_index"
            )
        if index is not None:
            if index not in matches:
                raise NotLocated("column_mismatch", "column_index does not carry that header")
            return index
        return matches[0]
    assert index is not None  # validate_locator guarantees one of the two
    if index >= len(header):
        raise NotLocated("column_not_found", "no column at that index")
    return index


class _Literal(str):
    """A JSON number kept as its source text, so 1.50 stays 1.50 and 1e2 is not silently 100.0."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise NotLocated("duplicate_key", f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise NotLocated("invalid_number", f"{name} is not valid JSON")


def locate_json(content: bytes, locator: dict[str, Any], deadline: float | None = None) -> Located:
    """The scalar at an RFC 6901 pointer. Numbers are returned as their literal text."""
    validate_locator("json_pointer", locator)
    _check_deadline(deadline)
    try:
        document = json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=_no_duplicate_keys,
            parse_float=_Literal,
            parse_int=_Literal,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise NotLocated("undecodable", "the file is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise NotLocated(
            "malformed_json", f"malformed JSON at line {exc.lineno} column {exc.colno}"
        ) from exc
    except RecursionError as exc:
        raise NotLocated("too_deep", "the JSON is nested too deeply") from exc
    node: Any = document
    for raw in locator["pointer"].split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                raise NotLocated("pointer_not_found", "no such key")
            node = node[token]
        elif isinstance(node, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(node):
                raise NotLocated("pointer_not_found", "no such array index")
            node = node[int(token)]
        else:
            raise NotLocated("pointer_not_found", "the pointer goes past a scalar")
    if isinstance(node, _Literal):
        return Located(str(node), "number_literal")
    if isinstance(node, str):
        return Located(nfc(node), "string")
    if node is None or isinstance(node, bool):
        raise NotLocated("value_not_comparable", "true, false and null cannot back a claim")
    raise NotLocated("value_not_comparable", "the pointer names an object or array, not a value")
