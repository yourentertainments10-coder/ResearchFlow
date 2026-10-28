"""Track F: raw copy, name sanitising, atomic publish, stale temp cleanup."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from sie.datasets.models import DatasetError, IntegrityError, LimitExceeded
from sie.datasets.provenance import sha256_bytes
from sie.datasets.store import (
    publish,
    read_regular_file,
    remove_stale_temp,
    run_name,
    sanitise_filename,
    store_raw,
)

# --- sanitise_filename ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("data.csv", "data.csv"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\sales 2026.csv", "sales_2026.csv"),
        ("/abs/path/file.xlsx", "file.xlsx"),
        (".hidden", "hidden"),
        ("...", "upload"),
        ("", "upload"),
        ("a\x00b.csv", "a_b.csv"),
        ("tab\tname\n.csv", "tab_name_.csv"),
        ("données.csv", "données.csv"),
        ("dir/", "upload"),
    ],
)
def test_sanitise_filename(raw, expected):
    assert sanitise_filename(raw) == expected


def test_sanitised_names_never_contain_separators_or_dotdot_components():
    for raw in ("a/../b", "..\\..\\x", "../", "x/..", ".. ", "\u2215etc\u2215passwd"):
        out = sanitise_filename(raw)
        assert "/" not in out and "\\" not in out and out not in ("..", ".")
        assert not out.startswith(".")


def test_long_names_are_cut_but_keep_their_extension():
    name = sanitise_filename("x" * 300 + ".csv")
    assert len(name) == 100 and name.endswith(".csv")
    assert len(sanitise_filename("y" * 300)) == 100


def test_sanitising_is_idempotent_and_normalises_unicode():
    decomposed = "e\u0301.csv"
    once = sanitise_filename(decomposed)
    assert once == "\u00e9.csv"
    assert sanitise_filename(once) == once


# --- read_regular_file ---------------------------------------------------------------------------


def test_read_regular_file_returns_bytes(tmp_path: Path):
    f = tmp_path / "a.csv"
    f.write_bytes(b"a,b\n1,2\n")
    assert read_regular_file(f, 100) == b"a,b\n1,2\n"


def test_read_refuses_oversized_file(tmp_path: Path):
    f = tmp_path / "big.csv"
    f.write_bytes(b"x" * 11)
    with pytest.raises(LimitExceeded) as info:
        read_regular_file(f, 10)
    assert info.value.limit == "max_bytes"
    f.write_bytes(b"x" * 10)
    assert len(read_regular_file(f, 10)) == 10


def test_read_refuses_symlink_directory_and_missing(tmp_path: Path):
    real = tmp_path / "real.csv"
    real.write_bytes(b"a\n")
    link = tmp_path / "link.csv"
    link.symlink_to(real)
    with pytest.raises(DatasetError, match="symbolic link"):
        read_regular_file(link, 100)
    with pytest.raises(DatasetError, match="not a regular file"):
        read_regular_file(tmp_path, 100)
    with pytest.raises(DatasetError, match="cannot read"):
        read_regular_file(tmp_path / "missing.csv", 100)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_read_refuses_a_pipe_without_blocking(tmp_path: Path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(DatasetError, match="not a regular file"):
        read_regular_file(fifo, 100)


# --- store_raw -----------------------------------------------------------------------------------


def test_store_raw_writes_hash_addressed_read_only_copy(tmp_path: Path):
    data = b"a,b\n1,2\n"
    stored = store_raw(data, "My file.csv", tmp_path / "uploads")
    sha = sha256_bytes(data)
    assert stored.sha256 == sha
    assert stored.path == tmp_path / "uploads" / sha / "My_file.csv"
    assert stored.safe_name == "My_file.csv"
    assert stored.path.read_bytes() == data
    assert stat.S_IMODE(os.stat(stored.path).st_mode) == 0o444
    assert stat.S_IMODE(os.stat(stored.path.parent).st_mode) == 0o555


def test_store_raw_is_idempotent_and_never_overwrites_with_another_name(tmp_path: Path):
    root = tmp_path / "uploads"
    first = store_raw(b"same", "one.csv", root)
    second = store_raw(b"same", "two.csv", root)
    assert second.path == first.path and second.safe_name == "one.csv"
    assert [p.name for p in (root / first.sha256).iterdir()] == ["one.csv"]
    assert not [p for p in root.iterdir() if p.name.startswith(".tmp-")]


def test_store_raw_hostile_name_stays_inside_root(tmp_path: Path):
    root = tmp_path / "uploads"
    stored = store_raw(b"x", "../../outside.csv", root)
    assert root in stored.path.parents
    assert not (tmp_path / "outside.csv").exists()


def test_store_raw_detects_tampering(tmp_path: Path):
    root = tmp_path / "uploads"
    stored = store_raw(b"original", "a.csv", root)
    os.chmod(stored.path.parent, 0o755)
    os.chmod(stored.path, 0o644)
    stored.path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store_raw(b"original", "a.csv", root)


def test_store_raw_detects_extra_files_and_non_directories(tmp_path: Path):
    root = tmp_path / "uploads"
    stored = store_raw(b"data", "a.csv", root)
    os.chmod(stored.path.parent, 0o755)
    (stored.path.parent / "extra.txt").write_text("x")
    with pytest.raises(IntegrityError):
        store_raw(b"data", "a.csv", root)
    (root / sha256_bytes(b"other")).write_text("not a dir")
    with pytest.raises(IntegrityError):
        store_raw(b"other", "a.csv", root)


def test_store_raw_failure_leaves_no_temp_directory(tmp_path: Path, monkeypatch):
    root = tmp_path / "uploads"

    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr("sie.datasets.store._fsync_path", boom)
    with pytest.raises(OSError, match="disk full"):
        store_raw(b"data", "a.csv", root)
    assert list(root.iterdir()) == []


# --- run_name ------------------------------------------------------------------------------------


def test_run_name_is_deterministic():
    a, b = "a" * 64, "b" * 64
    assert run_name(a, b) == "aaaaaaaaaaaa-bbbbbbbb"
    assert run_name(a, b) == run_name(a, b)
    assert run_name(a, "c" * 64) != run_name(a, b)


@pytest.mark.parametrize(
    ("inp", "par"),
    [("a" * 11, "b" * 8), ("a" * 12, "b" * 7), ("A" * 12, "b" * 8), ("../" + "a" * 12, "b" * 8), ("a" * 12, "g" * 8)],
)  # fmt: skip
def test_run_name_rejects_bad_hashes(inp, par):
    with pytest.raises(DatasetError):
        run_name(inp, par)


# --- publish -------------------------------------------------------------------------------------


def _writer(content: str):
    def write(tmp: Path) -> None:
        (tmp / "result.txt").write_text(content)
        (tmp / "sub").mkdir()
        (tmp / "sub" / "x.txt").write_text("x")

    return write


def test_publish_creates_directory_atomically(tmp_path: Path):
    final, present = publish(tmp_path / "out", "run-1", _writer("hello"))
    assert not present and final == tmp_path / "out" / "run-1"
    assert (final / "result.txt").read_text() == "hello"
    assert (final / "sub" / "x.txt").read_text() == "x"
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["run-1"]


def test_publish_is_idempotent_and_skips_the_writer(tmp_path: Path):
    out = tmp_path / "out"
    first, _ = publish(out, "run-1", _writer("one"))
    calls = []

    def writer(tmp: Path) -> None:
        calls.append(tmp)

    second, present = publish(out, "run-1", writer)
    assert present and second == first and calls == []
    assert (first / "result.txt").read_text() == "one"


def test_publish_failure_publishes_nothing_and_cleans_up(tmp_path: Path):
    out = tmp_path / "out"

    def failing(tmp: Path) -> None:
        (tmp / "partial.txt").write_text("half")
        raise RuntimeError("writer failed")

    with pytest.raises(RuntimeError, match="writer failed"):
        publish(out, "run-1", failing)
    assert list(out.iterdir()) == []


def test_publish_cleans_up_on_keyboard_interrupt(tmp_path: Path):
    out = tmp_path / "out"

    def interrupted(tmp: Path) -> None:
        (tmp / "partial.txt").write_text("half")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        publish(out, "run-1", interrupted)
    assert list(out.iterdir()) == []


def test_publish_failure_during_flush_publishes_nothing(tmp_path: Path, monkeypatch):
    out = tmp_path / "out"

    def boom(_path):
        raise OSError("fsync failed")

    monkeypatch.setattr("sie.datasets.store._fsync_tree", boom)
    with pytest.raises(OSError, match="fsync failed"):
        publish(out, "run-1", _writer("x"))
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "a\\b", "x\x00y", ".tmp-1"])
def test_publish_rejects_unsafe_names(bad, tmp_path: Path):
    with pytest.raises(DatasetError):
        publish(tmp_path / "out", bad, _writer("x"))


def test_publish_leaves_an_existing_directory_untouched_even_if_it_is_a_file(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "run-1").write_text("occupied")
    final, present = publish(out, "run-1", _writer("new"))
    assert present and final.read_text() == "occupied"


# --- remove_stale_temp ---------------------------------------------------------------------------


def test_remove_stale_temp_only_removes_old_tmp_directories(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    old = out / ".tmp-old"
    old.mkdir()
    (old / "f").write_text("x")
    fresh = out / ".tmp-fresh"
    fresh.mkdir()
    keep = out / "run-1"
    keep.mkdir()
    old_other = out / "other-old"
    old_other.mkdir()
    a_file = out / ".tmp-file"
    a_file.write_text("x")
    target = tmp_path / "victim"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = out / ".tmp-link"
    link.symlink_to(target, target_is_directory=True)
    past = time.time() - 7200
    for p in (old, keep, old_other, a_file):
        os.utime(p, (past, past))
    os.utime(link, (past, past), follow_symlinks=False)

    assert remove_stale_temp(out, 3600) == [".tmp-old"]
    assert not old.exists() and fresh.exists() and keep.exists() and old_other.exists()
    assert a_file.exists() and link.is_symlink() and (target / "keep.txt").exists()


def test_remove_stale_temp_handles_a_missing_root(tmp_path: Path):
    assert remove_stale_temp(tmp_path / "nope") == []


def test_fsync_on_windows_opens_files_read_write_and_skips_directories(tmp_path, monkeypatch):
    """Windows refuses fsync on a read-only descriptor (EBADF) and cannot open a directory."""
    import os

    from sie.datasets import store

    opened = []
    monkeypatch.setattr(os, "open", lambda p, flags: opened.append(flags) or 3)
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    monkeypatch.setattr(os, "close", lambda fd: None)
    f = tmp_path / "a.txt"
    f.write_text("x")
    store._fsync_path(f, windows=True)
    store._fsync_path(tmp_path, windows=True)
    assert len(opened) == 1 and opened[0] & os.O_RDWR
    store._fsync_path(f, windows=False)
    assert opened[1] == os.O_RDONLY
