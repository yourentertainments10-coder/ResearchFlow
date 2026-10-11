"""Track A: read bytes into a ``Dataset`` (CSV, XLSX, JSON) with limits. See design sections 8 and 10."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sie.datasets.models import Dataset, Limits, LoadOptions

if TYPE_CHECKING:
    import pandas as pd


def open_dataset(
    data: bytes,
    name: str,
    options: LoadOptions | None = None,
    limits: Limits | None = None,
    *,
    kind: str = "upload",
    url: str | None = None,
    retrieved_at: str | None = None,
) -> Dataset:
    """Detect the format from ``name`` (.csv, .xlsx, .json) and parse ``data``.

    The caller has already read the file once; this function never touches the file system.
    Raises ``LimitExceeded``, ``ParseError`` or ``DatasetError``; never returns a partial dataset.
    """
    raise NotImplementedError


def from_frame(
    frame: pd.DataFrame,
    name: str,
    *,
    kind: str = "derived",
    url: str | None = None,
    retrieved_at: str | None = None,
) -> Dataset:
    """Wrap a DataFrame a caller already holds (for example a future fetcher). Index becomes row_id
    1..n, columns become object dtype, ``SourceRef.sha256`` is the canonical table hash."""
    raise NotImplementedError
