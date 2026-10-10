"""Backup and a tested restore (docs/DEPLOYMENT.md section 3, item 5).

``backup`` writes a compressed ``pg_dump`` (custom format) plus a manifest that records what the
database held at that moment. ``restore_test`` proves the dump is usable: it restores into a scratch
database, compares the result with the manifest, re-hashes the raw evidence, and drops the scratch
database. A backup that was never restored is not a backup, so ``sie backup --verify`` does both.

How it stays consistent: the dump and the manifest counts are taken from the *same* exported
snapshot (``pg_export_snapshot`` + ``pg_dump --snapshot``), so a pipeline run that commits during the
backup cannot make them disagree. Use a direct connection, not a pooler (snapshots need one session).

Limits, stated plainly:
* Raw bytes stored on disk (``RAW_STORE_BACKEND=fs``) are not in a database dump. The manifest says
  how many versions that affects and the restore test reports it as a warning. Hosted runs use the
  ``db`` backend (ADR-016), which is inside the dump.
* Dumps are written without owners or grants so they restore on any host; run ``sie db-roles`` after.
* Encrypting and uploading the dump off-host is a step of the workflow, not of this code.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from sie.ops.backup_crypto import (
    ENCRYPTED_SUFFIX,
    EncryptionError,
    decrypt_file,
    encrypt_file,
    find_age,
    validate_recipient,
)

MANIFEST_FORMAT = 1
DEFAULT_KEEP = 14  # docs/DEPLOYMENT.md: keep the last 14 dumps
SCHEMAS = ("public", "reporting")
SCRATCH_PREFIX = "sie_restore_"
_DUMP_RE = re.compile(r"^sie-(?P<comp>[\w.-]+)-(?P<stamp>\d{8}T\d{6}Z)\.dump(\.age)?$")


class BackupError(RuntimeError):
    """A backup or restore step failed. The message says which and why."""


@dataclass(frozen=True)
class BackupResult:
    dump: Path
    manifest: Path
    data: dict[str, Any]
    pruned: list[Path] = field(default_factory=list)
    encrypted: bool = False  # ``dump`` is then ``*.dump.age`` and no plaintext dump was kept


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class RestoreReport:
    dump: Path
    checks: list[Check] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dump": str(self.dump),
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "warnings": self.warnings,
        }


def manifest_for(dump: Path) -> Path:
    """``sie-c-STAMP.dump`` and ``sie-c-STAMP.dump.age`` share ``sie-c-STAMP.manifest.json``."""
    name = dump.name.removesuffix(ENCRYPTED_SUFFIX).removesuffix(".dump")
    return dump.with_name(name + ".manifest.json")


def find_tool(name: str, bin_dir: Path | None) -> str:
    if bin_dir is not None:
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)
        raise BackupError(f"{name} not found in PG_BIN_DIR {bin_dir}")
    found = shutil.which(name)
    if found is None:
        raise BackupError(
            f"{name} not found on PATH; install the PostgreSQL client tools or set PG_BIN_DIR"
        )
    return found


def libpq_env(url: URL, database: str | None = None) -> dict[str, str]:
    """Connection settings as PG* variables, so the password never appears on a command line."""
    env = dict(os.environ)
    host = url.query.get("host") or url.host
    for key, value in {
        "PGHOST": host,
        "PGPORT": str(url.port) if url.port else None,
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": database or url.database,
        "PGSSLMODE": url.query.get("sslmode"),
    }.items():
        if value:
            env[key] = str(value)
    return env


def _run(args: list[str], env: dict[str, str], what: str) -> str:
    done = subprocess.run(args, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        raise BackupError(f"{what} failed (exit {done.returncode}): {done.stderr.strip()[:500]}")
    return done.stdout


def _tool_version(tool: str) -> str:
    return subprocess.run(
        [tool, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()  # noqa: S603


def table_counts(conn) -> dict[str, int]:
    names = (
        conn.execute(
            text(
                """SELECT schemaname || '.' || tablename FROM pg_tables
               WHERE schemaname = ANY(:s) ORDER BY 1"""
            ),
            {"s": list(SCHEMAS)},
        )
        .scalars()
        .all()
    )
    return {n: conn.execute(text(f"SELECT count(*) FROM {n}")).scalar_one() for n in names}  # noqa: S608


def backup(
    url: URL | str,
    out_dir: Path,
    competition: str,
    now: datetime,
    *,
    keep: int = DEFAULT_KEEP,
    bin_dir: Path | None = None,
    encrypt_to: str | None = None,
) -> BackupResult:
    """Dump the database to ``out_dir``. With ``encrypt_to`` (an age public key) the dump is written to a
    private temporary directory, encrypted into ``out_dir`` as ``*.dump.age``, and the plaintext is
    removed: ``out_dir`` never holds a readable dump."""
    url = make_url(url) if isinstance(url, str) else url
    pg_dump = find_tool("pg_dump", bin_dir)
    if encrypt_to is not None:
        encrypt_to = validate_recipient(encrypt_to)  # fail before touching the database
        find_age()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    stored = out_dir / f"sie-{competition}-{stamp}.dump{ENCRYPTED_SUFFIX if encrypt_to else ''}"
    manifest_path = manifest_for(stored)
    workdir = Path(tempfile.mkdtemp(prefix="sie_dump_")) if encrypt_to else out_dir
    dump = workdir / f"sie-{competition}-{stamp}.dump"
    partial = workdir / f"sie-{competition}-{stamp}.dump.partial"
    try:
        return _backup_into(
            url, pg_dump, competition, now, dump, partial, stored, manifest_path, out_dir,
            keep, encrypt_to,
        )  # fmt: skip
    finally:
        if encrypt_to:
            shutil.rmtree(workdir, ignore_errors=True)  # the plaintext never outlives the call


def _backup_into(
    url: URL,
    pg_dump: str,
    competition: str,
    now: datetime,
    dump: Path,
    partial: Path,
    stored: Path,
    manifest_path: Path,
    out_dir: Path,
    keep: int,
    encrypt_to: str | None,
) -> BackupResult:

    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            counts = table_counts(conn)
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            raw = dict(
                conn.execute(
                    text("SELECT storage_backend, count(*) FROM raw_versions GROUP BY 1")
                ).all()
            )
            server_version = conn.execute(text("SHOW server_version")).scalar_one()
            try:  # the snapshot must stay open until pg_dump has joined it
                _run(
                    [
                        pg_dump, "--format=custom", "--no-owner", "--no-acl",
                        f"--snapshot={snapshot}", "--file", str(partial),
                    ],
                    libpq_env(url),
                    "pg_dump",
                )  # fmt: skip
            except BackupError:
                partial.unlink(missing_ok=True)
                raise
    finally:
        engine.dispose()

    os.replace(partial, dump)
    plain_digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    plain_size = dump.stat().st_size
    if encrypt_to:
        encrypt_file(dump, stored, encrypt_to)
    digest = hashlib.sha256(stored.read_bytes()).hexdigest()
    on_disk = sum(n for backend, n in raw.items() if backend != "db")
    data: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "competition": competition,
        "created_at": now.isoformat(),
        "database": url.database,
        "alembic_revision": revision,
        "server_version": server_version,
        "pg_dump_version": _tool_version(pg_dump),
        "tables": counts,
        "raw_versions": {"by_backend": raw, "outside_dump": on_disk},
        "dump": {"file": stored.name, "size_bytes": stored.stat().st_size, "sha256": digest},
    }
    if encrypt_to:
        # The stored file is ciphertext; the plaintext hash lets a restore prove what it decrypted.
        data["plaintext"] = {"size_bytes": plain_size, "sha256": plain_digest}
        data["encryption"] = {"tool": "age", "recipient": encrypt_to}
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return BackupResult(
        stored,
        manifest_path,
        data,
        prune(out_dir, competition, keep, protect=stored),
        encrypted=bool(encrypt_to),
    )


def prune(out_dir: Path, competition: str, keep: int, protect: Path | None = None) -> list[Path]:
    """Delete the oldest dumps (and their manifests) beyond ``keep``. Touches only our own file names."""
    dumps = sorted(
        (
            p
            for p in out_dir.iterdir()
            if (m := _DUMP_RE.match(p.name)) and m["comp"] == competition
        ),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    for old in dumps[: max(0, len(dumps) - max(keep, 1))]:
        if old == protect:
            continue
        for path in (old, manifest_for(old)):
            if path.exists():
                path.unlink()
                removed.append(path)
    return removed


def verify_raw_bytes(conn) -> tuple[int, list[int]]:
    """Re-hash every raw blob held in the database. Returns (checked, ids whose bytes no longer match)."""
    bad, checked = [], 0
    for version_id, sha, blob in conn.execute(
        text(
            """SELECT v.id, v.sha256, b.content FROM raw_versions v
               JOIN raw_blobs b ON b.version_id = v.id ORDER BY v.id"""
        )
    ):
        checked += 1
        if hashlib.sha256(gzip.decompress(bytes(blob))).hexdigest() != sha:
            bad.append(version_id)
    return checked, bad


def restore_test(
    dump: Path,
    admin_url: URL | str,
    *,
    manifest: Path | None = None,
    bin_dir: Path | None = None,
    identity: Path | None = None,
) -> RestoreReport:
    """Restore ``dump`` into a throwaway database on the server of ``admin_url`` and check it.

    ``admin_url`` must be a scratch server, never the production database. An encrypted dump
    (``*.dump.age``) needs ``identity`` (the private key file): it is decrypted into a private temporary
    directory, checked against the manifest's plaintext hash, restored, and the plaintext is removed.
    """
    admin_url = make_url(admin_url) if isinstance(admin_url, str) else admin_url
    report = RestoreReport(dump)
    manifest = manifest or manifest_for(dump)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.checks.append(Check("manifest readable", False, str(exc)))
        return report
    report.checks.append(Check("manifest readable", True, manifest.name))

    actual = hashlib.sha256(dump.read_bytes()).hexdigest() if dump.exists() else None
    expected = data["dump"]["sha256"]
    report.checks.append(
        Check(
            "dump matches its recorded hash",
            actual == expected,
            "" if actual == expected else "file changed or truncated",
        )
    )
    if actual != expected:
        return report

    if not dump.name.endswith(ENCRYPTED_SUFFIX):
        return _restore_into_scratch(report, dump, data, admin_url, bin_dir)

    if identity is None:
        report.checks.append(
            Check("decrypts with the identity", False, "an encrypted backup needs --identity-file")
        )
        return report
    workdir = Path(tempfile.mkdtemp(prefix="sie_restore_plain_"))
    try:
        plain = workdir / dump.name.removesuffix(ENCRYPTED_SUFFIX)
        try:
            decrypt_file(dump, plain, identity)
        except EncryptionError as exc:
            report.checks.append(Check("decrypts with the identity", False, str(exc)))
            return report
        report.checks.append(Check("decrypts with the identity", True, plain.name))
        same = hashlib.sha256(plain.read_bytes()).hexdigest() == data.get("plaintext", {}).get(
            "sha256"
        )
        report.checks.append(
            Check(
                "decrypted dump matches its recorded plaintext hash",
                same,
                "" if same else "decrypted bytes differ from what was backed up",
            )
        )
        if not same:
            return report
        return _restore_into_scratch(report, plain, data, admin_url, bin_dir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _restore_into_scratch(
    report: RestoreReport, dump: Path, data: dict[str, Any], admin_url: URL, bin_dir: Path | None
) -> RestoreReport:
    pg_restore = find_tool("pg_restore", bin_dir)
    scratch = f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:10]}"
    admin = create_engine(
        admin_url.set(drivername="postgresql+psycopg"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))
        try:
            _run(
                [
                    pg_restore,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--dbname",
                    scratch,
                    str(dump),
                ],
                libpq_env(admin_url, database=scratch),
                "pg_restore",
            )
        except BackupError as exc:
            report.checks.append(Check("restore completes", False, str(exc)))
            return report
        report.checks.append(Check("restore completes", True, scratch))
        _compare(report, data, admin_url.set(database=scratch))
    finally:
        assert scratch.startswith(SCRATCH_PREFIX)  # never drop anything that is not ours
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
        admin.dispose()
    return report


def _compare(report: RestoreReport, data: dict[str, Any], scratch_url: URL) -> None:
    engine = create_engine(scratch_url.set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            ok = revision == data["alembic_revision"]
            report.checks.append(
                Check("schema revision matches", ok, f"{revision} vs {data['alembic_revision']}")
            )

            restored = table_counts(conn)
            expected = data["tables"]
            diffs = [
                f"{t}: {restored.get(t)} != {n}"
                for t, n in expected.items()
                if restored.get(t) != n
            ] + [f"{t}: unexpected table" for t in restored if t not in expected]
            report.checks.append(
                Check(f"row counts match ({len(expected)} tables)", not diffs, "; ".join(diffs[:5]))
            )

            checked, bad = verify_raw_bytes(conn)
            report.checks.append(
                Check(
                    f"raw evidence intact ({checked} blobs re-hashed)",
                    not bad,
                    f"version ids with changed bytes: {bad[:5]}" if bad else "",
                )
            )
    finally:
        engine.dispose()
    outside = data["raw_versions"]["outside_dump"]
    if outside:
        report.warnings.append(
            f"{outside} raw version(s) are stored outside the database (RAW_STORE_BACKEND=fs) and are "
            "not in this dump; back up the raw directory too"
        )
