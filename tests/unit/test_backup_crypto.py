"""age encryption of backup dumps: key handling, round trip, failure modes. Needs the ``age`` tool.

In CI ``REQUIRE_AGE=1`` turns a missing tool into a failure, so these tests cannot be skipped there.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sie.ops.backup_crypto import (
    AGE_MAGIC,
    EncryptionError,
    _safe,
    decrypt_file,
    encrypt_file,
    looks_encrypted,
    looks_like_plaintext_dump,
    validate_recipient,
)


@pytest.fixture(scope="module")
def keypair(tmp_path_factory) -> tuple[str, Path]:
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        if os.environ.get("REQUIRE_AGE"):
            pytest.fail("age is required in CI: install it (apt-get install age)")
        pytest.skip("age is not installed")
    identity = tmp_path_factory.mktemp("keys") / "identity.txt"
    done = subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True
    )
    match = re.search(r"(age1[0-9a-z]+)", done.stderr + done.stdout)
    assert match, "age-keygen did not print a public key"
    return match.group(1), identity


def test_a_generated_public_key_is_accepted(keypair):
    recipient, _ = keypair
    assert validate_recipient(f"  {recipient}\n") == recipient


@pytest.mark.parametrize("bad", ["", "age1short", "ssh-ed25519 AAAA", "age1" + "b" * 58])
def test_junk_is_not_a_recipient(bad):
    with pytest.raises(EncryptionError, match="not a valid age public key"):
        validate_recipient(bad)


def test_a_private_key_pasted_as_recipient_is_refused_without_echoing_it(keypair):
    _, identity = keypair
    secret = next(
        line for line in identity.read_text().splitlines() if line.startswith("AGE-SECRET-KEY")
    )
    with pytest.raises(EncryptionError) as raised:
        validate_recipient(secret)
    assert "PRIVATE key" in str(raised.value) and secret not in str(raised.value)


def test_round_trip_hides_the_plaintext_and_restores_it_exactly(keypair, tmp_path):
    recipient, identity = keypair
    plain = tmp_path / "x.dump"
    plain.write_bytes(b"PGDMP" + os.urandom(4096))
    sealed = tmp_path / "x.dump.age"
    encrypt_file(plain, sealed, recipient)
    assert sealed.read_bytes().startswith(AGE_MAGIC)
    assert looks_encrypted(sealed) and not looks_like_plaintext_dump(sealed)
    assert looks_like_plaintext_dump(plain) and not looks_encrypted(plain)
    assert plain.read_bytes()[5:] not in sealed.read_bytes()
    assert not list(tmp_path.glob("*.partial"))
    out = tmp_path / "back.dump"
    decrypt_file(sealed, out, identity)
    assert out.read_bytes() == plain.read_bytes()


def test_another_key_cannot_decrypt_and_leaves_nothing_behind(keypair, tmp_path):
    recipient, _ = keypair
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    plain = tmp_path / "x.dump"
    plain.write_bytes(b"PGDMP data")
    sealed = tmp_path / "x.dump.age"
    encrypt_file(plain, sealed, recipient)
    target = tmp_path / "out.dump"
    with pytest.raises(EncryptionError, match="failed to decrypt"):
        decrypt_file(sealed, target, other)
    assert not target.exists()


def test_a_missing_identity_or_source_fails_cleanly(keypair, tmp_path):
    recipient, identity = keypair
    with pytest.raises(EncryptionError, match="identity file does not exist"):
        decrypt_file(tmp_path / "a.age", tmp_path / "a", tmp_path / "nope.txt")
    with pytest.raises(EncryptionError, match="failed to encrypt"):
        encrypt_file(tmp_path / "missing.dump", tmp_path / "m.dump.age", recipient)
    assert not list(tmp_path.glob("m.dump.age*"))


def test_a_tampered_ciphertext_does_not_decrypt(keypair, tmp_path):
    recipient, identity = keypair
    plain = tmp_path / "x.dump"
    plain.write_bytes(b"PGDMP" + os.urandom(2048))
    sealed = tmp_path / "x.dump.age"
    encrypt_file(plain, sealed, recipient)
    data = bytearray(sealed.read_bytes())
    data[-10] ^= 0xFF
    sealed.write_bytes(bytes(data))
    with pytest.raises(EncryptionError):
        decrypt_file(sealed, tmp_path / "out", identity)


def test_error_messages_never_carry_key_text():
    noisy = "failed: AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ end"
    assert "AGE-SECRET-KEY" not in _safe(noisy) and "[key removed]" in _safe(noisy)
