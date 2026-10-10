"""Encrypt backup dumps with age (public-key encryption), so a dump can leave the database host safely.

Why public-key: the machine that makes the backup (a GitHub Actions runner) only needs the *recipient*
(a public key, ``age1...``), which is not a secret and can be a repository variable. The matching private
key, the *identity* (``AGE-SECRET-KEY-...``), stays with the owner offline and is needed only to restore.
Someone who steals the runner, the workflow or an uploaded file cannot decrypt anything.

Rules this module keeps:
* the identity is never printed, logged, put in an exception message or written next to a backup;
* the plaintext dump is deleted after a successful encryption (``seal_backup``), and never uploaded;
* the manifest records the SHA-256 of the plaintext (what the restore test checks) and of the
  ciphertext, plus the public recipient, and nothing secret;
* age is authenticated: a changed ciphertext fails to decrypt instead of decrypting to garbage.

Limit: encryption happens in memory (the whole dump), which suits the free-plan database size (1 GB
at most, far less compressed). A larger database needs a streaming tool.
Needs the optional dependency ``pyrage`` (``pip install -e ".[backup]"``).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from sie.ops.backup import BackupError, BackupResult

ENCRYPTED_SUFFIX = ".dump.age"
AGE_HEADER = b"age-encryption.org/v1"


def _pyrage():
    try:
        import pyrage
        from pyrage import x25519
    except ImportError as exc:
        raise BackupError(
            "encrypted backups need the 'pyrage' package: pip install -e \".[backup]\""
        ) from exc
    return pyrage, x25519


def parse_recipient(value: str):
    """A public age key. The error never repeats the value (it may be a mistyped secret)."""
    _, x25519 = _pyrage()
    value = value.strip()
    if not value.startswith("age1"):
        raise BackupError("backup recipient must be a public age key starting with 'age1'")
    try:
        return x25519.Recipient.from_str(value)
    except Exception as exc:  # noqa: BLE001
        raise BackupError("backup recipient is not a valid age public key") from exc


def load_identity(path: Path):
    """Read an age identity file (the first ``AGE-SECRET-KEY-`` line). Never echoes its content."""
    _, x25519 = _pyrage()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BackupError(f"cannot read the identity file {path.name}: {exc.strerror}") from exc
    keys = [ln.strip() for ln in lines if ln.strip().startswith("AGE-SECRET-KEY-")]
    if not keys:
        raise BackupError(f"{path.name} holds no age identity")
    try:
        return x25519.Identity.from_str(keys[0])
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"{path.name} does not hold a valid age identity") from exc


def generate_identity(out: Path) -> str:
    """Write a new identity to ``out`` (mode 0600, never overwriting) and return the public recipient."""
    _, x25519 = _pyrage()
    identity = x25519.Identity.generate()
    recipient = str(identity.to_public())
    out.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL  # fails if the file exists: never replace a key
    try:
        fd = os.open(out, flags, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError as exc:
        raise BackupError(f"{out} already exists; refusing to overwrite a key file") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"# public key: {recipient}\n{identity}\n")
    return recipient


def encrypt_file(src: Path, dest: Path, recipient: str) -> str:
    """Encrypt ``src`` to ``dest`` (written atomically, mode 0600). Returns the ciphertext SHA-256."""
    pyrage, _ = _pyrage()
    sealed = pyrage.encrypt(src.read_bytes(), [parse_recipient(recipient)])
    if not sealed.startswith(AGE_HEADER):
        raise BackupError("encryption produced unexpected output")
    partial = dest.with_name(dest.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(sealed)
    os.replace(partial, dest)
    return hashlib.sha256(sealed).hexdigest()


def decrypt_file(src: Path, dest: Path, identity_file: Path) -> None:
    """Decrypt ``src`` into ``dest`` (mode 0600). A wrong key or a changed file raises ``BackupError``."""
    pyrage, _ = _pyrage()
    identity = load_identity(identity_file)
    try:
        plain = pyrage.decrypt(src.read_bytes(), [identity])
    except Exception as exc:  # noqa: BLE001
        raise BackupError("cannot decrypt: wrong identity, or the backup file was changed") from exc
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(plain)


def seal_backup(result: BackupResult, recipient: str) -> Path:
    """Encrypt a finished backup, update its manifest, then delete the plaintext dump.

    Call this *after* the restore test, which needs the plaintext. Returns the encrypted file.
    """
    plain = result.dump
    sealed = plain.with_name(plain.name + ".age")
    cipher_sha = encrypt_file(plain, sealed, recipient)
    data: dict[str, Any] = dict(result.data)
    data["encryption"] = {
        "format": "age",
        "recipient": recipient.strip(),  # public key: not a secret
        "file": sealed.name,
        "ciphertext_sha256": cipher_sha,
        "ciphertext_bytes": sealed.stat().st_size,
        "plaintext_deleted": True,
    }
    result.manifest.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    plain.unlink()  # the plaintext must not stay where an upload step could pick it up
    return sealed


def manifest_for(dump: Path) -> Path:
    name = dump.name
    base = name[: -len(ENCRYPTED_SUFFIX)] if name.endswith(ENCRYPTED_SUFFIX) else dump.stem
    return dump.with_name(base + ".manifest.json")
