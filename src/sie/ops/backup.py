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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

MANIFEST_FORMAT = 1
DEFAULT_KEEP = 14  # docs/DEPLOYMENT.md: keep the last 14 dumps
SCHEMAS = ("public", "reporting")
SCRATCH_PREFIX = "sie_restore_"
_DUMP_RE = re.compile(
    r"^sie-(?P<comp>[\w.-]+)-(?P<stamp>\d{8}T\d{6}Z)\.(?:dump|dump\.age|manifest\.json)$"
)


class BackupError(RuntimeError):
    """A backup or restore step failed. The message says which and why."""


@dataclass(frozen=True)
class BackupResult:
    dump: Path
    manifest: Path
    data: dict[str, Any]
    pruned: list[Path] = field(default_factory=list)


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
) -> BackupResult:
    url = make_url(url) if isinstance(url, str) else url
    pg_dump = find_tool("pg_dump", bin_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dump = out_dir / f"sie-{competition}-{stamp}.dump"
    manifest_path = dump.with_suffix(".manifest.json")
    partial = dump.with_suffix(".dump.partial")

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
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
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
        "dump": {"file": dump.name, "size_bytes": dump.stat().st_size, "sha256": digest},
    }
    manifest_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return BackupResult(dump, manifest_path, data, prune(out_dir, competition, keep, protect=dump))


def prune(out_dir: Path, competition: str, keep: int, protect: Path | None = None) -> list[Path]:
    """Delete the oldest backups beyond ``keep``: the dump (plain or encrypted) and its manifest.

    A backup is one timestamp, however many files it has. Only our own file names are touched.
    """
    by_stamp: dict[str, list[Path]] = {}
    for p in out_dir.iterdir():
        m = _DUMP_RE.match(p.name)
        if m and m["comp"] == competition:
            by_stamp.setdefault(m["stamp"], []).append(p)
    protected = None
    if protect is not None and (m := _DUMP_RE.match(protect.name)):
        protected = m["stamp"]
    stamps = sorted(by_stamp)
    removed: list[Path] = []
    for stamp in stamps[: max(0, len(stamps) - max(keep, 1))]:
        if stamp == protected:
            continue
        for path in sorted(by_stamp[stamp]):
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

    An encrypted dump (``.dump.age``) needs ``identity``, the private key file; it is decrypted into a
    private temporary directory that is removed afterwards.
    """
    if dump.name.endswith(".dump.age"):
        return _restore_encrypted(dump, admin_url, manifest, bin_dir, identity)
    return _restore_plain(dump, admin_url, manifest=manifest, bin_dir=bin_dir)


def _restore_encrypted(dump, admin_url, manifest, bin_dir, identity) -> RestoreReport:
    import tempfile

    from sie.ops.seal import decrypt_file, manifest_for

    report = RestoreReport(dump)
    manifest = manifest or manifest_for(dump)
    if identity is None:
        report.checks.append(
            Check("identity file given", False, "an encrypted backup needs --identity")
        )
        return report
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.checks.append(Check("manifest readable", False, str(exc)))
        return report
    recorded = data.get("encryption", {}).get("ciphertext_sha256")
    actual = hashlib.sha256(dump.read_bytes()).hexdigest() if dump.exists() else None
    report.checks.append(
        Check("encrypted file matches its recorded hash", actual is not None and actual == recorded)
    )
    if actual != recorded:
        return report
    with tempfile.TemporaryDirectory(prefix="sie_restore_") as tmp:  # mode 0700
        plain = Path(tmp) / (dump.name[: -len(".age")])
        try:
            decrypt_file(dump, plain, identity)
        except BackupError as exc:
            report.checks.append(Check("decrypts with the identity", False, str(exc)))
            return report
        report.checks.append(Check("decrypts with the identity", True))
        inner = _restore_plain(plain, admin_url, manifest=manifest, bin_dir=bin_dir)
    report.checks += inner.checks
    report.warnings += inner.warnings
    return report


def _restore_plain(
    dump: Path,
    admin_url: URL | str,
    *,
    manifest: Path | None = None,
    bin_dir: Path | None = None,
) -> RestoreReport:
    admin_url = make_url(admin_url) if isinstance(admin_url, str) else admin_url
    report = RestoreReport(dump)
    manifest = manifest or dump.with_suffix(".manifest.json")
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
