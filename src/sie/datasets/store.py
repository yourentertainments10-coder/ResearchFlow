"""Track F: raw copy, name sanitising and atomic publish. See design section 10."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredRaw:
    path: Path
    sha256: str
    safe_name: str


def sanitise_filename(name: str) -> str:
    """Last path component, NFC, only letters digits ``.`` ``_`` ``-``, no leading dots, at most 100
    characters, ``upload`` if nothing is left."""
    raise NotImplementedError


def store_raw(data: bytes, name: str, root: Path) -> StoredRaw:
    """Write ``data`` to ``root/<sha256>/<safe name>`` (exclusive create + rename), verify the hash
    after writing, re-verify an existing copy. Never overwrite. Raises ``IntegrityError``."""
    raise NotImplementedError


def run_name(input_sha256: str, parameters_sha256: str) -> str:
    """``<input sha first 12>-<parameters sha first 8>``."""
    raise NotImplementedError


def publish(out_root: Path, final_name: str, writer: Callable[[Path], None]) -> tuple[Path, bool]:
    """Write into ``out_root/.tmp-<id>/`` via ``writer(tmp_dir)``, fsync, then rename to
    ``out_root/final_name``. Returns (path, already_present). Any failure removes the temp directory
    and publishes nothing. An existing final directory is returned untouched (idempotent)."""
    raise NotImplementedError


def remove_stale_temp(out_root: Path, older_than_seconds: float = 3600.0) -> list[str]:
    """Delete ``.tmp-*`` directories inside ``out_root`` older than the limit. Nothing else."""
    raise NotImplementedError
