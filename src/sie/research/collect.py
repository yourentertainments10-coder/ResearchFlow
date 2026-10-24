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

import pandas as pd

from sie.datasets import Dataset, DatasetError, Limits, LoadOptions, open_dataset
from sie.datasets.provenance import sha256_bytes
from sie.evidence.collect import html_tables
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


# --- HTML tables: parsed by sie.evidence.collect (one parser in the code base) -----------------------


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


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
        tables = html_tables(page.body)
        usable = [t for t in tables if t.refusal is None]
        refused = [f"table_refused:{t.index}:{t.refusal}" for t in tables if t.refusal is not None]
        if not usable:
            raise NothingCollected(
                f"the page has no usable HTML table ({len(tables)} found"
                + (f"; {'; '.join(refused)}" if refused else "")
                + ")"
            )
        extra = list(refused)
        if len(usable) > max_tables:
            extra.append(f"tables_not_collected:{len(usable) - max_tables}")
        return [
            build(
                f"table-{t.index}",
                f"table-{t.index}.csv",
                _csv_bytes(t.header, t.rows),
                CSV_OPTIONS,
                extra,
            )
            for t in usable[:max_tables]
        ]
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
