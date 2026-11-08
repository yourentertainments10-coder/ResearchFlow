"""Read a CSV too large to load whole: one pass, bounded memory, a seeded random sample, the whole file's hash.

The file is read once in chunks and never held in memory. Every data row gets a pseudo-random key from a
seeded generator, in file order, and the rows with the smallest keys are kept (at most ``max_rows`` rows, at
most ``limits.max_cells`` / ``SAMPLE_MAX_CELLS`` cells and ``limits.max_bytes`` bytes), so the result depends
only on the file's rows, the seed and the caps, never on chunk size. The sample is written back out as a
normal UTF-8 CSV and goes through the same loader, profile, calculations and independent checks as any file.

What it does NOT do: it does not scale anything up. Every number computed afterwards describes the sample;
``SampleNote`` says so, with the row counts, the seed and the SHA-256 of the whole file.

No clock, no network, no files; the caller supplies the byte stream.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import heapq
import io
import random
from dataclasses import dataclass
from typing import Any, BinaryIO

from sie.datasets import DatasetError, Limits, LoadOptions
from sie.datasets.load import LimitExceeded, ParseError

DEFAULT_SAMPLE_ROWS = 100_000
SAMPLE_MAX_CELLS = 1_000_000  # memory bound while sampling: kept rows x columns
MAX_STREAM_BYTES = 2 * 1024**3  # largest file read (2 GiB); more is refused, not truncated
MAX_SCAN_ROWS = 50_000_000
CHUNK_BYTES = 1024 * 1024
_UTF8 = ("utf-8", "utf8", "utf-8-sig", "utf8-sig")


@dataclass(frozen=True)
class SampleNote:
    rows_in_file: int
    rows_in_sample: int
    seed: int
    file_sha256: str
    file_bytes: int
    method: str = "seeded random sample: every data row had the same chance of being kept"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in_file": self.rows_in_file,
            "rows_in_sample": self.rows_in_sample,
            "seed": self.seed,
            "file_sha256": self.file_sha256,
            "file_bytes": self.file_bytes,
            "method": self.method,
        }

    def text(self) -> str:
        """The sentence shown wherever numbers are shown. Digits here are this note's own figures."""
        return (
            f"The file is too large to load whole, so everything here is calculated on a random sample of "
            f"{self.rows_in_sample:,} of its {self.rows_in_file:,} rows (seed {self.seed}). Counts, sums and "
            "averages describe the sample, not the whole file, and nothing is scaled up. "
            f"SHA-256 of the whole file: {self.file_sha256}."
        )


@dataclass(frozen=True)
class Sampled:
    data: bytes  # the sample as a UTF-8 CSV, header first, rows in file order
    options: LoadOptions  # the options to load ``data`` with (encoding is now UTF-8)
    note: SampleNote


class _Counting(io.RawIOBase):
    """A read-only view of a byte stream that hashes and counts what passes and enforces a byte cap."""

    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        self._stream, self._max = stream, max_bytes
        self.sha = hashlib.sha256()
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._stream.read(min(len(buffer), CHUNK_BYTES))
        if not chunk:
            return 0
        self.count += len(chunk)
        if self.count > self._max:
            raise LimitExceeded("max_stream_bytes", f"the file is larger than {self._max} bytes")
        self.sha.update(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _codec(options: LoadOptions) -> str:
    name = options.encoding.lower().replace("_", "-")
    try:
        return "utf-8-sig" if name in _UTF8 else codecs.lookup(options.encoding).name
    except LookupError as exc:
        raise DatasetError(f"unknown encoding {options.encoding!r}") from exc


def sample_csv(
    stream: BinaryIO,
    *,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    seed: int = 0,
    max_rows: int = DEFAULT_SAMPLE_ROWS,
    max_stream_bytes: int = MAX_STREAM_BYTES,
) -> Sampled:
    """One pass over ``stream``. Raises the loader's ``ParseError`` / ``LimitExceeded`` for a file it could
    not load either (bad header, ragged rows unless ``options.ragged == "reject"``, over-long cells, bytes
    that are not valid in the encoding, NUL bytes, a file past ``max_stream_bytes``)."""
    opts = options or LoadOptions()
    lim = limits or Limits()
    counting = _Counting(stream, max_stream_bytes)
    text = io.TextIOWrapper(
        io.BufferedReader(counting, CHUNK_BYTES), encoding=_codec(opts), newline="", errors="strict"
    )
    reader = csv.reader(text, delimiter=opts.delimiter, strict=True)

    def next_record() -> list[str] | None:
        try:
            return next(reader)
        except StopIteration:
            return None
        except csv.Error as exc:
            if "field larger than field limit" in str(exc):
                raise LimitExceeded(
                    "max_cell_chars", "a field is larger than the parser allows"
                ) from exc
            raise ParseError(f"malformed CSV: {exc}", f"line {reader.line_num}") from exc
        except UnicodeDecodeError as exc:
            raise ParseError(
                f"bytes are not valid {opts.encoding}; if the file uses another encoding pass it explicitly",
                f"byte offset {exc.start}",
            ) from exc

    header = next_record()
    while header is not None and header == []:
        header = next_record()
    if header is None:
        raise ParseError("file is empty: no header row")
    width = len(header)
    if any("\x00" in h for h in header):
        raise ParseError("NUL byte in file", "header")
    keep = max(
        1, min(max_rows, lim.max_rows, SAMPLE_MAX_CELLS // max(width, 1), lim.max_cells // width)
    )
    byte_cap = (
        lim.max_bytes // 2
    )  # headroom: CSV quoting and non-ASCII text make bytes exceed characters
    rng = random.Random(seed)
    heap: list[
        tuple[float, int, tuple[str, ...]]
    ] = []  # (-key, row number, fields): top = largest key
    kept_size = 0
    rows = 0
    record_no = 0
    skipped = 0
    while True:
        fields = next_record()
        if fields is None:
            break
        record_no += 1
        if fields == []:
            continue
        if len(fields) != width:
            if opts.ragged == "reject":
                skipped += 1
                continue
            raise ParseError(
                f"row has {len(fields)} fields but the header has {width}; "
                "pass ragged='reject' to set such rows aside",
                f"row {record_no}",
            )
        rows += 1
        if rows > MAX_SCAN_ROWS:
            raise LimitExceeded("max_scan_rows", f"more than {MAX_SCAN_ROWS} rows")
        key = rng.random()
        if len(heap) >= keep and key >= -heap[0][0]:
            joined = "".join(fields)
            if "\x00" in joined:
                raise ParseError("NUL byte in file", f"row {record_no}")
            if max(map(len, fields)) > lim.max_cell_chars:
                raise LimitExceeded("max_cell_chars", f"a cell in row {record_no} is too long")
            continue
        joined = "".join(fields)
        if "\x00" in joined:
            raise ParseError("NUL byte in file", f"row {record_no}")
        if max(map(len, fields)) > lim.max_cell_chars:
            raise LimitExceeded("max_cell_chars", f"a cell in row {record_no} is too long")
        size = len(joined) + width
        heapq.heappush(heap, (-key, rows, tuple(fields)))
        kept_size += size
        while len(heap) > keep or kept_size > byte_cap:
            _, _, dropped = heapq.heappop(heap)
            kept_size -= sum(map(len, dropped)) + width
    if rows == 0:
        raise ParseError("the file has a header but no data rows")
    # the longest run of the smallest keys whose CSV text fits the byte limit
    ranked = sorted(heap, key=lambda item: (-item[0], item[1]))
    while True:
        chosen = sorted(ranked, key=lambda item: item[1])
        out = io.StringIO(newline="")
        writer = csv.writer(out, delimiter=opts.delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(item[2] for item in chosen)
        data = out.getvalue().encode("utf-8")
        if len(data) <= lim.max_bytes or len(ranked) <= 1:
            break
        ranked = ranked[: max(1, len(ranked) * 3 // 4)]
    note = SampleNote(
        rows_in_file=rows,
        rows_in_sample=len(chosen),
        seed=seed,
        file_sha256=counting.sha.hexdigest(),
        file_bytes=counting.count,
    )
    return Sampled(data, opts.model_copy(update={"encoding": "utf-8", "ragged": "error"}), note)
