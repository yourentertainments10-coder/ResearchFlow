"""Phase 4c: turn a fetched page into normalised tables through ``sie.datasets`` (parse only, no network).

CSV and JSON go to the datasets loader as they are (a JSON object that wraps exactly one list of records is
unwrapped, and that is flagged). HTML tables are read with the standard library parser. Nothing is filled in
or silently dropped: ragged rows become rejects, duplicate or empty headers are renamed and flagged, merged
cells are flagged, and the loader's own notes travel with the table as flags. Every table keeps the page's
URL, retrieval time and content hash, and every row keeps its 1-based ``row_id``.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pandas as pd

from sie.datasets import Dataset, DatasetError, Limits, LoadOptions, open_dataset
from sie.datasets.provenance import sha256_bytes
from sie.evidence.errors import EvidenceError
from sie.evidence.web import FetchedPage

MAX_TABLES = 10
WRAP_KEYS = ("records", "results", "items", "data", "rows", "result")
CSV_OPTIONS = LoadOptions(ragged="reject", rename_duplicates=True)


class NothingCollected(EvidenceError):
    """The page has no usable table. Said plainly; nothing is guessed."""


@dataclass(frozen=True)
class CollectedTable:
    dataset: Dataset
    label: str  # e.g. "table-1", "csv", "json"
    raw_sha256: str  # hash of the page as received
    url: str
    retrieved_at: str
    content_type: str
    source_name: str  # the file name handed to the loader (.csv or .json)
    source_bytes: bytes  # exactly what the loader read (the page itself, or the derived CSV/JSON)
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rows(self) -> int:
        return self.dataset.rows

    def provenance_frame(self) -> pd.DataFrame:
        """One row per table row: where it came from. Index is the row_id."""
        frame = pd.DataFrame(
            {
                "source_url": self.url,
                "retrieved_at": self.retrieved_at,
                "raw_sha256": self.raw_sha256,
                "table": self.label,
            },
            index=self.dataset.frame.index,
        )
        return frame


# --- HTML tables ------------------------------------------------------------------------------------


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._stack: list[dict] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self.nested = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            if self._stack:
                self.nested += 1
            self._stack.append({"rows": [], "header_row": False, "spans": False})
        elif not self._stack:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th" and not self._stack[-1]["rows"]:
                self._stack[-1]["header_row"] = True
            for span in ("colspan", "rowspan"):
                if str(a.get(span, "1")).strip() not in ("", "1"):
                    self._stack[-1]["spans"] = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if not self._stack:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self._stack[-1]["rows"].append(self._row)
            self._row = None
        elif tag == "table":
            self.tables.append(self._stack.pop())


def html_tables(body: bytes) -> tuple[list[dict], int]:
    parser = _Tables()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.tables, parser.nested


def _csv_bytes(rows: list[list[str]]) -> tuple[bytes, list[str]]:
    flags: list[str] = []
    header = list(rows[0])
    for i, h in enumerate(header):
        if not h.strip():
            header[i] = f"column_{i + 1}"
            flags.append(f"empty_header_named:column_{i + 1}")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows[1:])
    return buf.getvalue().encode("utf-8"), flags


# --- JSON -------------------------------------------------------------------------------------------


def _records(document, path: str = "$", depth: int = 0):
    """The one list of objects inside a wrapper, with its path. Several candidates is an error."""
    if isinstance(document, list):
        return document, path
    if not isinstance(document, dict) or depth > 3:
        raise NothingCollected("the JSON has no list of records")
    found = []
    for k in WRAP_KEYS:
        if k in document and isinstance(document[k], (list, dict)):
            try:
                found.append(_records(document[k], f"{path}.{k}", depth + 1))
            except NothingCollected:
                continue
    if len(found) != 1:
        keys = sorted(k for k, v in document.items() if isinstance(v, (list, dict)))
        raise NothingCollected(
            f"cannot tell which part of the JSON is the table ({len(found)} candidates; keys: {keys})"
        )
    return found[0]


# --- public -----------------------------------------------------------------------------------------


def _flags(ds: Dataset, extra: list[str]) -> tuple[str, ...]:
    flags = list(extra)
    for note in ds.notes:
        flags.append(f"{note.get('code', 'note')}:{note.get('count', '')}".rstrip(":"))
    if ds.rejects is not None and len(ds.rejects):
        flags.append(f"rejected_rows:{len(ds.rejects)}")
    return tuple(flags)


def collect_page(
    page: FetchedPage, *, limits: Limits | None = None, max_tables: int = MAX_TABLES
) -> list[CollectedTable]:
    """Parse one fetched page into tables. Raises ``NothingCollected`` or a ``DatasetError``."""
    raw = sha256_bytes(page.body)
    when = page.retrieved_at.isoformat()
    ctype = page.content_type
    path = page.final_url.lower().split("?")[0]

    def build(label, name, data, options, extra):
        try:
            ds = open_dataset(
                data, name, options, limits, kind="fetched", url=page.final_url, retrieved_at=when
            )
        except DatasetError as exc:
            raise NothingCollected(f"{label}: {exc}") from exc
        return CollectedTable(
            ds, label, raw, page.final_url, when, ctype, name, data, _flags(ds, extra)
        )

    if ctype in ("text/csv", "application/csv") or (
        ctype == "text/plain" and path.endswith(".csv")
    ):
        return [build("csv", "page.csv", page.body, CSV_OPTIONS, [])]
    if ctype == "application/json" or (ctype == "text/plain" and path.endswith(".json")):
        try:
            document = json.loads(page.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise NothingCollected(f"malformed JSON: {exc}") from exc
        records, where = _records(document)
        extra = [] if where == "$" else [f"unwrapped_json:{where}"]
        data = page.body if where == "$" else json.dumps(records, sort_keys=False).encode("utf-8")
        return [build("json", "page.json", data, LoadOptions(rename_duplicates=True), extra)]
    if ctype == "text/html":
        tables, nested = html_tables(page.body)
        usable = [t for t in tables if len(t["rows"]) >= 2 and max(map(len, t["rows"])) >= 2]
        if not usable:
            raise NothingCollected(
                f"the page has no usable HTML table ({len(tables)} found, none with a header and a row)"
            )
        out = []
        skipped = len(usable) - max_tables
        for i, t in enumerate(usable[:max_tables], start=1):
            data, extra = _csv_bytes(t["rows"])
            if not t["header_row"]:
                extra.append("header_assumed_first_row")
            if t["spans"]:
                extra.append("merged_cells_present")
            if nested:
                extra.append(f"nested_tables_on_page:{nested}")
            if skipped > 0:
                extra.append(f"tables_not_collected:{skipped}")
            out.append(build(f"table-{i}", f"table-{i}.csv", data, CSV_OPTIONS, extra))
        return out
    raise NothingCollected(f"content type {ctype} cannot be collected")


# --- coverage and conflicts -------------------------------------------------------------------------


def coverage(table: CollectedTable) -> dict:
    """Plain counts for the report: what is there, what is missing, what was rejected. No estimates."""
    frame = table.dataset.frame
    nulls = {str(c): int(frame[c].isna().sum()) for c in frame.columns}
    return {
        "table": table.label,
        "url": table.url,
        "retrieved_at": table.retrieved_at,
        "raw_sha256": table.raw_sha256,
        "rows": int(len(frame)),
        "columns": [str(c) for c in frame.columns],
        "missing_values_by_column": {c: n for c, n in nulls.items() if n},
        "rejected_rows": 0 if table.dataset.rejects is None else int(len(table.dataset.rejects)),
        "flags": list(table.flags),
    }


def _norm(value) -> str:
    return " ".join(str(value).split()).casefold()


def find_conflicts(
    tables: list[CollectedTable], key: list[str], value_columns: list[str]
) -> pd.DataFrame:
    """Rows that share a key but state different values, across tables or within one.

    Nothing is resolved here: both claims are listed with where they came from. Values are compared as
    trimmed, case-folded text, so ``6`` and ``6.0`` are reported as different rather than guessed equal.
    """
    parts = []
    for t in tables:
        frame = t.dataset.frame
        missing = [c for c in key + value_columns if c not in frame.columns]
        if missing:
            raise EvidenceError(f"table {t.label} ({t.url}) has no column {missing}")
        part = frame[key + value_columns].copy()
        part["_row_id"] = frame.index
        part["_source"] = f"{t.url}#{t.label}"
        parts.append(part)
    if not parts:
        return pd.DataFrame(columns=[*key, "column", "value_a", "source_a", "value_b", "source_b"])
    allrows = pd.concat(parts, ignore_index=True)
    out = []
    for _, group in allrows.groupby(key, dropna=True, sort=True):
        for col in value_columns:
            seen: dict[str, tuple] = {}
            for _, r in group.iterrows():
                v = r[col]
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    continue
                seen.setdefault(_norm(v), (v, f"{r['_source']} row {r['_row_id']}"))
            if len(seen) > 1:
                (va, sa), (vb, sb) = list(seen.values())[:2]
                out.append({**{k: group.iloc[0][k] for k in key}, "column": col,
                            "value_a": va, "source_a": sa, "value_b": vb, "source_b": sb})  # fmt: skip
    return pd.DataFrame(out, columns=[*key, "column", "value_a", "source_a", "value_b", "source_b"])
