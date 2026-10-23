"""Track F: reading an input once, the raw copy, name sanitising and atomic publish.

See docs/DATASET_ANALYTICS_DESIGN.md section 10. Nothing here knows about tables: it moves bytes and
directories safely. Read-only permissions on the raw copy are a guard against accidents, not
immutability; integrity rests on the hash being re-checked on every use.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sie.datasets.models import DatasetError, IntegrityError, LimitExceeded
from sie.datasets.provenance import sha256_bytes

MAX_NAME_CHARS = 100
TEMP_PREFIX = ".tmp-"
_HEX = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True)
class StoredRaw:
    path: Path
    sha256: str
    safe_name: str


def sanitise_filename(name: str) -> str:
    """Last path component, NFC, only letters, digits, ``.``, ``_``, ``-``, no leading dots, at most
    100 characters (the extension is kept), ``upload`` if nothing is left.

    Letters and digits are Unicode ``str.isalnum`` characters, so ``données.csv`` keeps its letters;
    every other character (spaces, separators, NUL, control characters) becomes ``_``. A name can
    therefore never contain a path separator or ``..`` and cannot leave the directory it is joined to.
    """
    text = unicodedata.normalize("NFC", name)
    last = re.split(r"[\\/]", text)[-1]
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in last)
    cleaned = cleaned.lstrip(".")
    if len(cleaned) > MAX_NAME_CHARS:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 10 and stem:
            cleaned = stem[: MAX_NAME_CHARS - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:MAX_NAME_CHARS]
    return cleaned or "upload"


def read_regular_file(path: Path | str, max_bytes: int) -> bytes:
    """Read an input file ONCE. Refuses symlinks, directories, pipes and devices, and a file larger
    than ``max_bytes`` (at most ``max_bytes + 1`` bytes are read). The caller hashes, stores and
    analyses these bytes; the path is never read again, so there is no check-then-use gap."""
    target = Path(path)
    try:
        before = os.lstat(target)
    except OSError as exc:
        raise DatasetError(f"cannot read {target.name!r}: {exc.strerror}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise DatasetError(f"{target.name!r} is a symbolic link; refusing to follow it")
    if not stat.S_ISREG(before.st_mode):
        raise DatasetError(f"{target.name!r} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise DatasetError(f"cannot open {target.name!r}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        after = os.fstat(handle.fileno())
        if not stat.S_ISREG(after.st_mode):
            raise DatasetError(f"{target.name!r} is not a regular file")
        if after.st_size > max_bytes:
            raise LimitExceeded("max_bytes", f"file is {after.st_size} bytes, limit {max_bytes}")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise LimitExceeded("max_bytes", f"file is larger than the limit of {max_bytes} bytes")
    return data


def _make_writable(path: Path) -> None:
    for current, dirs, files in os.walk(path):
        with contextlib.suppress(OSError):
            os.chmod(current, 0o700)
        for item in dirs + files:
            full = os.path.join(current, item)
            if not os.path.islink(full):
                with contextlib.suppress(OSError):
                    os.chmod(full, 0o600 if item in files else 0o700)


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if path.exists():
        _make_writable(path)
        shutil.rmtree(path, ignore_errors=True)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_existing(directory: Path, sha256: str) -> StoredRaw:
    """An existing ``<sha256>`` directory must hold exactly one regular file with that hash."""
    if directory.is_symlink() or not directory.is_dir():
        raise IntegrityError(f"{directory} exists but is not a directory")
    files = sorted(p for p in directory.iterdir())
    if len(files) != 1 or files[0].is_symlink() or not files[0].is_file():
        raise IntegrityError(f"{directory} does not hold exactly one regular file")
    actual = sha256_bytes(files[0].read_bytes())
    if actual != sha256:
        raise IntegrityError(f"stored raw copy {files[0]} has hash {actual}, expected {sha256}")
    return StoredRaw(files[0], sha256, files[0].name)


def store_raw(data: bytes, name: str, root: Path) -> StoredRaw:
    """Write ``data`` to ``root/<sha256>/<safe name>`` and return where it is.

    The file is written to a temporary directory with exclusive creation, flushed, made read-only and
    renamed into place in one step, then re-read and re-hashed. An existing copy for the same hash is
    re-hashed and returned unchanged (never overwritten, even when ``name`` differs). A mismatch
    raises ``IntegrityError``. The read-only mode (0444 file, 0555 directory) guards against accidents
    only; the owner or root can still change it.
    """
    sha = sha256_bytes(data)
    safe = sanitise_filename(name)
    root.mkdir(parents=True, exist_ok=True)
    final = root / sha
    if os.path.lexists(final):
        return _verify_existing(final, sha)

    temp = root / f"{TEMP_PREFIX}raw-{uuid.uuid4().hex}"
    temp.mkdir(mode=0o700)
    try:
        target = temp / safe
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o444)
        _fsync_path(temp)
        os.chmod(temp, 0o555)
        try:
            os.rename(temp, final)
        except OSError:
            # another writer created the same hash first: keep theirs, verified below
            if not os.path.lexists(final):
                raise
            _remove_tree(temp)
            return _verify_existing(final, sha)
    except BaseException:
        _remove_tree(temp)
        raise
    return _verify_existing(final, sha)


def run_name(input_sha256: str, parameters_sha256: str) -> str:
    """``<input sha first 12>-<parameters sha first 8>``: the same request always gets the same name."""
    for label, value, need in (
        ("input hash", input_sha256, 12),
        ("parameters hash", parameters_sha256, 8),
    ):
        if len(value) < need or not _HEX.match(value):
            raise DatasetError(f"{label} must be lowercase hex of at least {need} characters")
    return f"{input_sha256[:12]}-{parameters_sha256[:8]}"


def _fsync_tree(path: Path) -> None:
    for current, _dirs, files in os.walk(path):
        for item in files:
            full = Path(current) / item
            if not full.is_symlink():
                _fsync_path(full)
        _fsync_path(Path(current))


def publish(out_root: Path, final_name: str, writer: Callable[[Path], None]) -> tuple[Path, bool]:
    """Publish a directory atomically.

    ``writer(tmp_dir)`` fills ``out_root/.tmp-<id>/``; everything is flushed to disk and the
    directory is renamed to ``out_root/final_name`` in one step. If ``final_name`` already exists the
    writer is not called and the existing directory is returned untouched (``already_present`` True),
    so rerunning a request is idempotent. Any failure removes the temporary directory and publishes
    nothing; a process killed in between leaves only a ``.tmp-`` directory (``remove_stale_temp``).
    """
    if (
        not final_name
        or final_name.startswith(".")
        or "/" in final_name
        or "\\" in final_name
        or "\x00" in final_name
    ):
        raise DatasetError(f"invalid output directory name {final_name!r}")
    out_root.mkdir(parents=True, exist_ok=True)
    final = out_root / final_name
    if os.path.lexists(final):
        return final, True

    temp = out_root / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    temp.mkdir(mode=0o700)
    try:
        writer(temp)
        _fsync_tree(temp)
        if os.path.lexists(final):  # lost a race with another run of the same request
            _remove_tree(temp)
            return final, True
        os.rename(temp, final)
        _fsync_path(out_root)
    except BaseException:
        _remove_tree(temp)
        raise
    return final, False


def remove_stale_temp(out_root: Path, older_than_seconds: float = 3600.0) -> list[str]:
    """Delete ``.tmp-*`` directories directly inside ``out_root`` whose mtime is older than the limit.

    Symlinks, files and any other name are never touched. Returns the removed names, sorted.
    """
    if not out_root.is_dir():
        return []
    cutoff = time.time() - older_than_seconds
    removed: list[str] = []
    for entry in sorted(out_root.iterdir(), key=lambda p: p.name):
        if not entry.name.startswith(TEMP_PREFIX):
            continue
        info = os.lstat(entry)
        if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= cutoff:
            continue
        _remove_tree(entry)
        removed.append(entry.name)
    return sorted(removed)
