"""Parse a manual results CSV. Pure function over bytes: no file access, no network, no database.

Format (docs/DATA_PIPELINE.md section 9). Required columns: competition, sport, event, medal, country.
Optional columns: discipline, gender, athlete_or_team, date, source_url, source_note, participation,
slot, is_tie. Column order does not matter. An unknown column, a missing required column, a row with
the wrong number of cells, an oversized cell or too many rows is a ``CsvFormatError``: the whole file is
refused rather than partly guessed at.
"""

from __future__ import annotations

import csv
import io

from sie.sources.base import ParsedResult

SOURCE = "manual"  # the source name stored on runs, raw documents and placings
REQUIRED_COLUMNS = frozenset({"competition", "sport", "event", "medal", "country"})
OPTIONAL_COLUMNS = frozenset(
    {
        "discipline",
        "gender",
        "athlete_or_team",
        "date",
        "source_url",
        "source_note",
        "participation",
        "slot",
        "is_tie",
    }
)
SOURCE_NAME = "manual"


class CsvFormatError(ValueError):
    """The file is not a manual results CSV in the documented format."""


def parse_manual_csv(content: bytes, *, max_rows: int, max_cell_chars: int) -> list[ParsedResult]:
    """Return one ``ParsedResult`` per non-blank data row, in file order."""
    try:
        text = content.decode("utf-8-sig")  # tolerate the BOM that Excel adds
    except UnicodeDecodeError as exc:
        raise CsvFormatError(f"file is not valid UTF-8 text: {exc}") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [cell.strip().casefold() for cell in next(reader)]
    except StopIteration:
        raise CsvFormatError("file is empty: a header row is required") from None

    duplicates = sorted({h for h in header if header.count(h) > 1})
    if duplicates:
        raise CsvFormatError(f"duplicate columns: {duplicates}")
    unknown = sorted(set(header) - REQUIRED_COLUMNS - OPTIONAL_COLUMNS)
    if unknown:
        raise CsvFormatError(
            f"unknown columns {unknown}; allowed: {sorted(REQUIRED_COLUMNS | OPTIONAL_COLUMNS)}"
        )
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise CsvFormatError(f"missing required columns: {missing}")

    results: list[ParsedResult] = []
    for cells in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in cells):
            continue  # blank line
        if len(cells) != len(header):
            raise CsvFormatError(
                f"line {line}: {len(cells)} cells but the header has {len(header)} columns"
            )
        if len(results) >= max_rows:
            raise CsvFormatError(f"more than {max_rows} data rows")
        row = {name: cell.strip() for name, cell in zip(header, cells, strict=True)}
        too_long = [name for name, value in row.items() if len(value) > max_cell_chars]
        if too_long:
            raise CsvFormatError(
                f"line {line}: cell longer than {max_cell_chars} characters in {too_long}"
            )
        results.append(
            ParsedResult(
                row_number=line,
                competition=row.get("competition", ""),
                sport=row.get("sport", ""),
                discipline=row.get("discipline", ""),
                event=row.get("event", ""),
                gender=row.get("gender", ""),
                medal=row.get("medal", ""),
                country=row.get("country", ""),
                entrant=row.get("athlete_or_team", ""),
                participation=row.get("participation", ""),
                slot=row.get("slot", ""),
                is_tie=row.get("is_tie", ""),
                date=row.get("date", ""),
                source_url=row.get("source_url", ""),
                source_note=row.get("source_note", ""),
            )
        )
    return results
