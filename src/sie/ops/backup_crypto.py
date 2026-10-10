"""Encryption of backup dumps with ``age`` (https://age-encryption.org), before anything leaves the host.

Why: the repository and its Actions artifacts can be public, and a database dump holds the raw evidence
and every table. A dump is therefore only ever stored or uploaded as ``*.dump.age``.

Model: asymmetric. The backup job needs only the **recipient** (a public key, ``age1...``); the
**identity** (private key, ``AGE-SECRET-KEY-...``) is needed only to decrypt, for restores and for the
decrypt-and-restore check. The identity is read from a file path, never from the command line or the
log, and a decrypted dump only ever lives in a private temporary directory that is removed afterwards.

``age`` is an external tool (like ``pg_dump``), so this adds no Python dependency.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ENCRYPTED_SUFFIX = ".age"
AGE_MAGIC = b"age-encryption.org/v1"  # first bytes of every age file
PG_CUSTOM_MAGIC = b"PGDMP"  # first bytes of a plaintext pg_dump custom-format file
# An age X25519 recipient: "age1" followed by 58 bech32 characters (no 1, b, i or o).
_RECIPIENT_RE = re.compile(r"^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$")


class EncryptionError(RuntimeError):
    """Encryption or decryption failed, or the key material is unusable. Never contains key text."""


def find_age(name: str = "age") -> str:
    found = shutil.which(name)
    if found is None:
        raise EncryptionError(f"{name} not found on PATH; install it (apt install age)")
    return found


def validate_recipient(recipient: str) -> str:
    """Return the recipient, or raise. Refuses a pasted private key without echoing it."""
    value = recipient.strip()
    if value.startswith("AGE-SECRET-KEY"):
        raise EncryptionError(
            "the backup recipient is a PRIVATE key; give the public key (age1...) instead and "
            "keep the private key only as a secret used for restores"
        )
    if not _RECIPIENT_RE.match(value):
        raise EncryptionError("the backup recipient is not a valid age public key (age1...)")
    return value


def encrypt_file(source: Path, target: Path, recipient: str) -> None:
    """Encrypt ``source`` to ``target`` for ``recipient``. ``target`` appears only when complete."""
    recipient = validate_recipient(recipient)
    age = find_age()
    partial = target.with_name(target.name + ".partial")
    done = subprocess.run(  # noqa: S603
        [age, "--encrypt", "--recipient", recipient, "--output", str(partial), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        partial.unlink(missing_ok=True)
        raise EncryptionError(
            f"age failed to encrypt (exit {done.returncode}): {_safe(done.stderr)}"
        )
    os.replace(partial, target)


def decrypt_file(source: Path, target: Path, identity: Path) -> None:
    """Decrypt ``source`` into ``target`` with the private key in the file ``identity``."""
    if not identity.is_file():
        raise EncryptionError("the identity file does not exist")
    age = find_age()
    done = subprocess.run(  # noqa: S603
        [age, "--decrypt", "--identity", str(identity), "--output", str(target), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        target.unlink(missing_ok=True)
        raise EncryptionError(
            f"age failed to decrypt (exit {done.returncode}): {_safe(done.stderr)}"
        )


def looks_encrypted(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(AGE_MAGIC)) == AGE_MAGIC


def looks_like_plaintext_dump(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(PG_CUSTOM_MAGIC)) == PG_CUSTOM_MAGIC


def _safe(stderr: str) -> str:
    """age's own message, trimmed and with any key-looking text removed."""
    text = re.sub(r"AGE-SECRET-KEY-[A-Z0-9]+", "[key removed]", stderr.strip())
    return text[:300]
