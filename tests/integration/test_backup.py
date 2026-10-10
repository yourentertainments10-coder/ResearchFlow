"""6C: backup and restore test on a real PostgreSQL (pgserver's own pg_dump/pg_restore)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pgserver
import pytest
from sqlalchemy import create_engine, text
from test_ops_foundation import csv_source

from sie.config import Settings
from sie.ops.backup import SCRATCH_PREFIX, BackupError, backup, manifest_for, prune, restore_test
from sie.ops.backup_crypto import AGE_MAGIC, PG_CUSTOM_MAGIC, EncryptionError
from sie.pipeline.runner import run_ingest
from sie.reference import seed_reference

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


@pytest.fixture()
def settings(db_url, tmp_path):
    shutil.copytree(ROOT / "data" / "reference", tmp_path / "reference")
    return Settings(
        _env_file=None,
        database_url=db_url.render_as_string(hide_password=False),
        data_dir=tmp_path,
        raw_store_backend="db",
    )


@pytest.fixture()
def loaded(engine, settings):
    with engine.begin() as c:
        seed_reference(c, settings.reference_dir)
    run_ingest(engine, csv_source(), settings, now=NOW)
    return engine


def scratch_dbs(admin_url):
    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        names = (
            c.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                {"p": SCRATCH_PREFIX + "%"},
            )
            .scalars()
            .all()
        )
    eng.dispose()
    return names


def test_backup_then_restore_verifies(loaded, db_url, admin_url, tmp_path):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    assert result.dump.exists() and result.manifest.exists()
    assert result.data["tables"]["public.placings"] > 0
    assert result.data["alembic_revision"]
    assert result.data["raw_versions"]["outside_dump"] == 0

    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert report.ok, report.to_dict()
    names = [c.name for c in report.checks]
    assert any(n.startswith("raw evidence intact (1 blobs") for n in names)
    assert scratch_dbs(admin_url) == []  # scratch database is dropped


def test_restore_detects_tampered_dump(loaded, db_url, admin_url, tmp_path):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    data = bytearray(result.dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    result.dump.write_bytes(bytes(data))
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert not report.ok
    assert report.checks[-1].name == "dump matches its recorded hash"
    assert scratch_dbs(admin_url) == []


def test_restore_detects_count_mismatch(loaded, db_url, admin_url, tmp_path):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    manifest = json.loads(result.manifest.read_text())
    manifest["tables"]["public.placings"] += 1
    result.manifest.write_text(json.dumps(manifest))
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert not report.ok
    bad = [c for c in report.checks if not c.ok]
    assert bad and bad[0].name.startswith("row counts match")
    assert "public.placings" in bad[0].detail
    assert scratch_dbs(admin_url) == []


def test_restore_detects_corrupted_raw_blob(loaded, db_url, admin_url, tmp_path):
    """Dump taken while a blob's bytes no longer match their recorded sha256 must fail the restore test."""
    with loaded.begin() as c:
        c.execute(text("UPDATE raw_versions SET sha256 = repeat('0', 64)"))
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert not report.ok
    assert [c.name for c in report.checks if not c.ok][0].startswith("raw evidence intact")


def test_missing_manifest_fails_cleanly(loaded, db_url, admin_url, tmp_path):
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    result.manifest.unlink()
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert not report.ok and report.checks[0].name == "manifest readable"


def test_fs_backend_is_flagged_as_outside_the_dump(engine, db_url, admin_url, settings, tmp_path):
    fs = settings.model_copy(update={"raw_store_backend": "fs"})
    with engine.begin() as c:
        seed_reference(c, fs.reference_dir)
    run_ingest(engine, csv_source(), fs, now=NOW)
    result = backup(db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN)
    assert result.data["raw_versions"]["outside_dump"] == 1
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert report.ok and report.warnings and "outside the database" in report.warnings[0]


def test_prune_keeps_newest_and_only_our_files(loaded, db_url, tmp_path):
    out = tmp_path / "b"
    for i in range(4):
        backup(db_url, out, "asiad-2026", NOW + timedelta(hours=i), keep=2, bin_dir=BIN)
    (out / "notes.txt").write_text("mine")
    other = backup(db_url, out, "other-comp", NOW, keep=2, bin_dir=BIN)
    dumps = sorted(p.name for p in out.glob("sie-asiad-2026-*.dump"))
    assert dumps == ["sie-asiad-2026-20261006T120000Z.dump", "sie-asiad-2026-20261006T130000Z.dump"]
    assert len(list(out.glob("sie-asiad-2026-*.manifest.json"))) == 2
    assert (out / "notes.txt").exists() and other.dump.exists()
    prune(out, "asiad-2026", keep=0)  # keep is floored at 1: never delete everything
    assert [p.name for p in out.glob("sie-asiad-2026-*.dump")] == [
        "sie-asiad-2026-20261006T130000Z.dump"
    ]


def test_missing_tool_and_failed_dump_leave_no_partial(db_url, tmp_path):
    with pytest.raises(BackupError, match="not found in PG_BIN_DIR"):
        backup(db_url, tmp_path / "b", "x", NOW, bin_dir=tmp_path)
    bad = db_url.set(database="does_not_exist")
    with pytest.raises(Exception):  # noqa: B017  (connection error before pg_dump runs)
        backup(bad, tmp_path / "b", "x", NOW, bin_dir=BIN)
    assert list((tmp_path / "b").glob("*.partial")) == []


# --- encrypted backups (ADR-034): nothing readable leaves the host, and the stored file restores ----------


@pytest.fixture()
def keys(tmp_path):
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        if os.environ.get("REQUIRE_AGE"):
            pytest.fail("age is required in CI: install it (apt-get install age)")
        pytest.skip("age is not installed")
    identity = tmp_path / "identity.txt"
    done = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    recipient = re.search(r"(age1[0-9a-z]+)", done.stderr + done.stdout).group(1)
    return recipient, identity


def encrypted(db_url, tmp_path, recipient, **kw):
    return backup(
        db_url, tmp_path / "b", "asiad-2026", NOW, bin_dir=BIN, encrypt_to=recipient, **kw
    )


def test_an_encrypted_backup_leaves_only_ciphertext_and_a_manifest(loaded, db_url, tmp_path, keys):
    recipient, _ = keys
    result = encrypted(db_url, tmp_path, recipient)
    files = sorted(p.name for p in (tmp_path / "b").iterdir())
    assert files == [
        "sie-asiad-2026-20261006T100000Z.dump.age",
        "sie-asiad-2026-20261006T100000Z.manifest.json",
    ]
    assert result.encrypted and result.dump.name.endswith(".dump.age")
    head = result.dump.read_bytes()[:64]
    assert head.startswith(AGE_MAGIC) and PG_CUSTOM_MAGIC not in head
    assert manifest_for(result.dump) == result.manifest
    data = json.loads(result.manifest.read_text())
    assert data["encryption"] == {"tool": "age", "recipient": recipient}
    assert data["plaintext"]["sha256"] != data["dump"]["sha256"]
    assert "AGE-SECRET-KEY" not in result.manifest.read_text()


def test_the_plaintext_dump_never_outlives_the_call(loaded, db_url, tmp_path, keys, monkeypatch):
    recipient, _ = keys
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    encrypted(db_url, tmp_path, recipient)
    assert list(scratch.iterdir()) == []  # the private working directory is gone


def test_decrypt_and_restore_verifies_schema_counts_and_raw_evidence(
    loaded, db_url, admin_url, tmp_path, keys
):
    recipient, identity = keys
    result = encrypted(db_url, tmp_path, recipient)
    report = restore_test(result.dump, admin_url, bin_dir=BIN, identity=identity)
    assert report.ok, report.to_dict()
    names = [c.name for c in report.checks]
    assert "decrypts with the identity" in names
    assert "decrypted dump matches its recorded plaintext hash" in names
    assert "schema revision matches" in names
    assert any(n.startswith("row counts match") for n in names)
    assert any(n.startswith("raw evidence intact") for n in names)
    assert scratch_dbs(admin_url) == []
    assert not [p for p in tmp_path.rglob("*.dump")]  # no plaintext dump anywhere under the output


def test_an_encrypted_backup_cannot_be_restored_without_the_identity(
    loaded, db_url, admin_url, tmp_path, keys
):
    recipient, _ = keys
    result = encrypted(db_url, tmp_path, recipient)
    report = restore_test(result.dump, admin_url, bin_dir=BIN)
    assert not report.ok and report.checks[-1].name == "decrypts with the identity"
    assert scratch_dbs(admin_url) == []


def test_the_wrong_identity_fails_before_anything_is_restored(
    loaded, db_url, admin_url, tmp_path, keys
):
    recipient, _ = keys
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    result = encrypted(db_url, tmp_path, recipient)
    report = restore_test(result.dump, admin_url, bin_dir=BIN, identity=other)
    assert not report.ok and report.checks[-1].name == "decrypts with the identity"
    assert "AGE-SECRET-KEY" not in json.dumps(report.to_dict())
    assert scratch_dbs(admin_url) == []


def test_a_tampered_ciphertext_is_caught_by_the_recorded_hash(
    loaded, db_url, admin_url, tmp_path, keys
):
    recipient, identity = keys
    result = encrypted(db_url, tmp_path, recipient)
    data = bytearray(result.dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    result.dump.write_bytes(bytes(data))
    report = restore_test(result.dump, admin_url, bin_dir=BIN, identity=identity)
    assert not report.ok and report.checks[-1].name == "dump matches its recorded hash"


def test_a_bad_recipient_fails_before_the_database_is_touched(loaded, db_url, tmp_path, keys):
    _, identity = keys
    secret = next(
        line for line in identity.read_text().splitlines() if line.startswith("AGE-SECRET-KEY")
    )
    with pytest.raises(EncryptionError, match="PRIVATE key"):
        encrypted(db_url, tmp_path, secret)
    assert not (tmp_path / "b").exists() or list((tmp_path / "b").iterdir()) == []


def test_prune_handles_encrypted_dumps_and_their_manifests(loaded, db_url, tmp_path, keys):
    recipient, _ = keys
    out = tmp_path / "b"
    for i in range(3):
        backup(
            db_url, out, "asiad-2026", NOW + timedelta(hours=i), keep=2, bin_dir=BIN,
            encrypt_to=recipient,
        )  # fmt: skip
    assert sorted(p.name for p in out.glob("*.dump.age")) == [
        "sie-asiad-2026-20261006T110000Z.dump.age",
        "sie-asiad-2026-20261006T120000Z.dump.age",
    ]
    assert len(list(out.glob("*.manifest.json"))) == 2
