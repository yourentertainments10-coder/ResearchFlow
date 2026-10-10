"""P3: encrypted backups. Real pg_dump/pg_restore on the test database; throwaway age keys only."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pgserver
import pytest
from test_ops_foundation import csv_source

from sie.config import Settings
from sie.ops.backup import BackupError, backup, prune, restore_test
from sie.ops.seal import generate_identity, seal_backup
from sie.pipeline.runner import run_ingest
from sie.reference import seed_reference

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


@pytest.fixture()
def loaded(engine, db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    settings = Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    run_ingest(engine, csv_source(), settings, now=NOW)
    return engine


@pytest.fixture()
def key(tmp_path):
    ident = tmp_path / "keys" / "id.txt"
    return ident, generate_identity(ident)


def sealed_backup(db_url, tmp_path, recipient):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    return result, seal_backup(result, recipient)


def test_round_trip_restores_and_plaintext_is_gone(loaded, db_url, admin_url, tmp_path, key):
    identity, recipient = key
    result, sealed = sealed_backup(db_url, tmp_path, recipient)
    assert sealed.name.endswith(".dump.age") and sealed.exists()
    assert not result.dump.exists()
    assert [p.name for p in (tmp_path / "b").glob("*.dump")] == []
    report = restore_test(sealed, admin_url, bin_dir=BIN, identity=identity)
    assert report.ok, report.to_dict()


def test_ciphertext_is_not_a_pg_dump(loaded, db_url, tmp_path, key):
    _, recipient = key
    result, sealed = sealed_backup(db_url, tmp_path, recipient)
    blob = sealed.read_bytes()
    assert blob.startswith(b"age-encryption.org/v1")
    assert b"PGDMP" not in blob
    assert oct(sealed.stat().st_mode & 0o777) == "0o600"


def test_wrong_identity_fails(loaded, db_url, admin_url, tmp_path, key):
    _, recipient = key
    other, _ = (tmp_path / "other.txt", generate_identity(tmp_path / "other.txt"))
    _, sealed = sealed_backup(db_url, tmp_path, recipient)
    report = restore_test(sealed, admin_url, bin_dir=BIN, identity=other)
    assert not report.ok
    assert any("decrypt" in c.name for c in report.checks if not c.ok)


def test_missing_identity_fails(loaded, db_url, admin_url, tmp_path, key):
    _, recipient = key
    _, sealed = sealed_backup(db_url, tmp_path, recipient)
    assert not restore_test(sealed, admin_url, bin_dir=BIN).ok


def test_tampered_ciphertext_is_detected(loaded, db_url, admin_url, tmp_path, key):
    identity, recipient = key
    _, sealed = sealed_backup(db_url, tmp_path, recipient)
    blob = bytearray(sealed.read_bytes())
    blob[-5] ^= 0xFF
    sealed.write_bytes(bytes(blob))
    report = restore_test(sealed, admin_url, bin_dir=BIN, identity=identity)
    assert not report.ok  # caught by the recorded hash before any decryption


def test_manifest_and_files_hold_no_secret(loaded, db_url, tmp_path, key):
    identity, recipient = key
    secret = identity.read_text().split("AGE-SECRET-KEY-")[1].split()[0]
    result, sealed = sealed_backup(db_url, tmp_path, recipient)
    for path in (tmp_path / "b").iterdir():
        assert b"AGE-SECRET-KEY-" not in path.read_bytes()
        assert secret.encode() not in path.read_bytes()
    enc = json.loads(result.manifest.read_text())["encryption"]
    assert enc["recipient"] == recipient and enc["plaintext_deleted"] is True


def test_bad_recipient_is_refused_without_echoing_it(loaded, db_url, tmp_path):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    with pytest.raises(BackupError) as err:
        seal_backup(result, "AGE-SECRET-KEY-NOTAREALKEY")
    assert "NOTAREALKEY" not in str(err.value)


def test_keygen_never_overwrites(tmp_path):
    path = tmp_path / "id.txt"
    generate_identity(path)
    with pytest.raises(BackupError):
        generate_identity(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_prune_counts_encrypted_backups_and_removes_manifests(tmp_path):
    out = tmp_path / "b"
    out.mkdir()
    stamps = [(NOW + timedelta(days=d)).strftime("%Y%m%dT%H%M%SZ") for d in range(4)]
    for s in stamps:
        (out / f"sie-asiad-2026-{s}.dump.age").write_bytes(b"x")
        (out / f"sie-asiad-2026-{s}.manifest.json").write_text("{}")
    (out / "notes.txt").write_text("keep me")
    removed = prune(out, "asiad-2026", keep=2)
    left = sorted(p.name for p in out.iterdir())
    assert len(removed) == 4
    assert "notes.txt" in left
    assert all(stamps[0] not in n and stamps[1] not in n for n in left)
    assert any(stamps[3] in n for n in left)
