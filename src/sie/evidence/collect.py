"""Collect a fetched or uploaded page into a table with provenance (ADR-044, Phase 4c).

Fetch and parse stay separate (AGENTS.md rule 14): this module only reads bytes that are already saved.
Nothing is guessed. A page with several tables must say which one it wants; a table with merged cells is
refused with its reason; a value is never filled in. CSV and JSON go through ``sie.datasets`` as they are;
HTML tables are read with the standard library and wrapped with ``sie.datasets.from_frame``.

Provenance per row: the dataset's ``SourceRef`` carries the URL, retrieval time and a hash; the row id is
the 1-based position in the source. ``row_provenance`` joins them into one citable record.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pandas as pd

from sie.datasets import Dataset, DatasetError, Limits, from_frame, open_dataset
from sie.evidence.errors import EvidenceError

MAX_TABLES = 200
MAX_CELL_CHARS = 32_768


class AmbiguousTable(EvidenceError):
    """The page holds several usable tables and the caller did not say which one."""

    def __init__(self, choices: list[dict]) -> None:
        super().__init__(f"the page has {len(choices)} tables; choose one by index")
        self.choices = choices


class NoTable(EvidenceError):
    """No table could be read from the page; ``skipped`` says why each candidate was refused."""

    def __init__(self, message: str, skipped: list[dict]) -> None:
        super().__init__(message)
        self.skipped = skipped


@dataclass
class HtmlTable:
    index: int
    caption: str
    header: list[str]
    rows: list[list[str]]
    refusal: str | None = None  # why this table cannot be used, if it cannot

    def summary(self) -> dict:
        return {
            "index": self.index,
            "caption": self.caption,
            "columns": self.header,
            "rows": len(self.rows),
            "refusal": self.refusal,
        }


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._stack: list[dict] = []  # nested tables are tracked and refused
        self._cell: list[str] | None = None
        self._cell_is_header = False
        self._caption: list[str] | None = None
        self._skip = 0  # inside script or style

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            t = {
                "caption": "",
                "rows": [],
                "header_flags": [],
                "row": None,
                "refusal": None,
                "nested": False,
            }
            if self._stack:
                self._stack[-1]["nested"] = True
                self._stack[-1]["refusal"] = "contains a nested table"
            self._stack.append(t)
            self.tables.append(t)
        elif not self._stack:
            return
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._stack[-1]["row"] = ([], [])
        elif tag in ("td", "th") and self._stack[-1]["row"] is not None:
            for key in ("rowspan", "colspan"):
                try:
                    if int(a.get(key, "1") or "1") > 1:
                        self._stack[-1]["refusal"] = f"uses {key} (merged cells)"
                except ValueError:
                    self._stack[-1]["refusal"] = f"has an invalid {key}"
            self._cell = []
            self._cell_is_header = tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif not self._stack:
            return
        elif tag == "table":
            self._stack.pop()
        elif tag == "caption" and self._caption is not None:
            self._stack[-1]["caption"] = " ".join("".join(self._caption).split())
            self._caption = None
        elif tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            row = self._stack[-1]["row"]
            row[0].append(text)
            row[1].append(self._cell_is_header)
            self._cell = None
        elif tag == "tr" and self._stack[-1]["row"] is not None:
            cells, flags = self._stack[-1]["row"]
            if cells:
                self._stack[-1]["rows"].append(cells)
                self._stack[-1]["header_flags"].append(all(flags))
            self._stack[-1]["row"] = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._caption is not None:
            self._caption.append(data)
        elif self._cell is not None:
            self._cell.append(data)


def html_tables(data: bytes, *, limit: int = MAX_TABLES) -> list[HtmlTable]:
    """Every table in the page, in document order, each usable or carrying the reason it is not."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    parser = _Tables()
    parser.feed(text)
    parser.close()
    out: list[HtmlTable] = []
    for i, raw in enumerate(parser.tables[:limit]):
        rows, flags = raw["rows"], raw["header_flags"]
        refusal = raw["refusal"]
        header: list[str] = []
        body = rows
        if refusal is None:
            if not rows:
                refusal = "has no rows"
            elif flags[0]:
                header, body = rows[0], rows[1:]
            else:
                refusal = "has no header row (a row of <th> cells)"
        if refusal is None:
            width = len(header)
            if width == 0 or any(not h.strip() for h in header):
                refusal = "has an empty column header"
            elif len(set(header)) != width:
                refusal = "has duplicate column headers"
            elif any(len(r) != width for r in body):
                refusal = "has rows with a different number of cells than the header"
            elif not body:
                refusal = "has a header but no data rows"
            elif any(len(c) > MAX_CELL_CHARS for r in body for c in r):
                refusal = "has a cell that is too long"
        out.append(HtmlTable(i, raw["caption"], header, body if refusal is None else [], refusal))
    return out


@dataclass(frozen=True)
class CollectedDataset:
    dataset: Dataset
    page_sha256: str  # hash of the page bytes as fetched: ties the table to the evidence source
    page_url: str | None
    retrieved_at: str | None
    table_index: int | None = None
    table_caption: str = ""
    skipped_tables: tuple[dict, ...] = field(default_factory=tuple)

    def manifest(self) -> dict:
        return {
            "page_sha256": self.page_sha256,
            "page_url": self.page_url,
            "retrieved_at": self.retrieved_at,
            "table_index": self.table_index,
            "table_caption": self.table_caption,
            "rows": self.dataset.rows,
            "columns": self.dataset.columns,
            "skipped_tables": list(self.skipped_tables),
            "loader_notes": self.dataset.notes,
        }


def _name_from(url: str | None, content_type: str) -> str:
    stem = (urlsplit(url).path.rsplit("/", 1)[-1] if url else "") or "page"
    ext = {"text/csv": ".csv", "application/csv": ".csv", "application/json": ".json"}.get(
        content_type
    )
    if ext and not stem.lower().endswith(ext):
        stem = stem.rsplit(".", 1)[0] + ext if "." in stem else stem + ext
    return stem


def collect(
    data: bytes,
    content_type: str,
    *,
    url: str | None = None,
    retrieved_at: str | None = None,
    table_index: int | None = None,
    limits: Limits | None = None,
) -> CollectedDataset:
    """Turn saved bytes into a dataset with provenance. Raises on anything it cannot read exactly."""
    ctype = content_type.split(";")[0].strip().lower()
    page_sha = hashlib.sha256(data).hexdigest()
    if ctype in ("text/csv", "application/csv", "application/json"):
        ds = open_dataset(
            data,
            _name_from(url, ctype),
            None,
            limits,
            kind="fetched",
            url=url,
            retrieved_at=retrieved_at,
        )
        return CollectedDataset(ds, page_sha, url, retrieved_at)
    if ctype != "text/html":
        raise EvidenceError(f"cannot collect a table from {ctype or 'an unknown content type'}")
    tables = html_tables(data)
    usable = [t for t in tables if t.refusal is None]
    skipped = tuple(t.summary() for t in tables if t.refusal is not None)
    if not usable:
        raise NoTable("no usable table on the page", list(skipped))
    if table_index is None:
        if len(usable) > 1:
            raise AmbiguousTable([t.summary() for t in usable])
        chosen = usable[0]
    else:
        chosen = next((t for t in tables if t.index == table_index), None)
        if chosen is None:
            raise EvidenceError(f"the page has no table with index {table_index}")
        if chosen.refusal is not None:
            raise NoTable(f"table {table_index} {chosen.refusal}", list(skipped))
    frame = pd.DataFrame(chosen.rows, columns=chosen.header)
    frame = frame.replace({"": None})
    try:
        ds = from_frame(
            frame, _name_from(url, ctype), kind="fetched", url=url, retrieved_at=retrieved_at
        )
    except DatasetError as exc:
        raise EvidenceError(str(exc)) from exc
    return CollectedDataset(ds, page_sha, url, retrieved_at, chosen.index, chosen.caption, skipped)


def row_provenance(c: CollectedDataset, row_id: int) -> dict:
    """The citable origin of one row: where it was fetched, when, from which bytes, which table, which row."""
    if row_id not in c.dataset.frame.index:
        raise EvidenceError(f"row {row_id} does not exist")
    return {
        "url": c.page_url,
        "retrieved_at": c.retrieved_at,
        "page_sha256": c.page_sha256,
        "table_index": c.table_index,
        "table_caption": c.table_caption,
        "row_id": int(row_id),
    }
