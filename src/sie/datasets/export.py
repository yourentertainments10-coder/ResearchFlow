"""Track E: write result tables and the manifest into a directory. See design sections 9 and 11."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sie.datasets.models import CleanResult, Dataset, Limits, OperationResult, ProfileResult


def neutralise_text(value: str) -> tuple[str, bool]:
    """CSV text-cell protection: a leading ``=``, ``+``, ``-``, ``@``, tab or CR gets a ``'`` prefix.
    Returns (value, changed). Only for text values; numbers are never passed here."""
    raise NotImplementedError


def build_manifest(
    ds: Dataset,
    limits: Limits,
    profile: ProfileResult | None,
    cleaning: CleanResult | None,
    results: list[OperationResult],
    *,
    generated_at: str,
    outputs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The manifest dict (design section 11). Pure; ``generated_at`` is supplied by the caller."""
    raise NotImplementedError


def write_outputs(
    directory: Path,
    ds: Dataset,
    limits: Limits,
    profile: ProfileResult | None,
    cleaning: CleanResult | None,
    results: list[OperationResult],
    *,
    generated_at: str,
) -> dict[str, dict[str, Any]]:
    """Write CSV tables, one XLSX workbook and ``manifest.json`` into the existing empty ``directory``.
    Returns {file name: {"sha256": ..., "size_bytes": ...}}. ``directory`` is the caller's temp
    directory; publishing atomically is ``store.publish``."""
    raise NotImplementedError
