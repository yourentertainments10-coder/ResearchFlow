"""Persisted collected datasets (ADR-044, Phase 4c): reuse a fetched table for follow-up questions.

Built on ``sie.datasets.store`` (``publish``, ``run_name``, ``remove_stale_temp``): no second storage
mechanism. What is kept is only the normalised table and its manifest, never the page it came from.

Layout, under a configurable root that is never committed (``data/evidence_datasets/`` is git-ignored)::

    <root>/p<project_id>/<table hash 12>-<provenance hash 8>/
        table.json      {"columns": [...], "rows": [[...], ...]}  native JSON values, row ids are 1..n
        manifest.json   source url, retrieval time, page hash, table index and caption, schema, licence,
                        permission basis, created and expiry time, evidence source id

* project-scoped: every function takes a ``ProjectScope`` and looks only in that project's directory;
* hash-addressed and idempotent: the same table from the same page gives the same directory, which is
  returned as it is; the table hash is re-checked on every open;
* atomic: ``publish`` renames a finished directory into place, so a reader never sees half a dataset;
* permitted: rows are stored only when a recorded host policy or an open licence covers the source
  (or the file is the project's own upload); otherwise ``RowsNotPermitted``;
* not kept forever: every dataset has an expiry (default 90 days, never longer than the policy says);
  an expired dataset is never reused silently; ``prune_expired`` and ``delete_*`` remove it.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from sie.datasets import Dataset, SourceRef
from sie.datasets.models import DatasetError
from sie.datasets.provenance import sha256_bytes, table_sha256
from sie.datasets.store import publish, remove_stale_temp, run_name
from sie.evidence.collect import CollectedDataset
from sie.evidence.errors import DatasetExpired, EvidenceError, NotFound, RowsNotPermitted
from sie.evidence.retention import SourcePolicy, policy_for
from sie.evidence.scope import ProjectScope, scoped_api

FORMAT_VERSION = 1
MAX_STORED_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class RowsPermission:
    allowed: bool
    basis: (
        str  # why: "own_upload", "host_policy: ...", "open_licence: cc-by", or the refusal reason
    )
    licence: str | None = None
    terms_ref: str | None = None
    retain_days: int | None = None  # a policy may only shorten the default


def load_open_licences(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["licence_id", "name"]:
            raise EvidenceError("open_licences.csv must have the columns licence_id,name")
        return {(r["licence_id"] or "").strip().lower(): (r["name"] or "").strip() for r in reader}


@dataclass(frozen=True)
class RowPolicy:
    host: str
    terms_ref: str
    allows_rows: bool
    licence: str
    retain_days: int | None


def load_row_policies(path: Path) -> dict[str, RowPolicy]:
    cols = ["host", "terms_ref", "recorded_on", "allows_rows", "licence", "retain_days", "notes"]
    if not path.exists():
        return {}
    out: dict[str, RowPolicy] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != cols:
            raise EvidenceError(f"dataset_policies.csv must have the columns {', '.join(cols)}")
        for n, r in enumerate(reader, start=2):
            host = (r["host"] or "").strip().lower()
            allows = (r["allows_rows"] or "").strip().lower()
            terms = (r["terms_ref"] or "").strip()
            if not host or allows not in ("true", "false") or (allows == "true" and not terms):
                raise EvidenceError(
                    f"dataset_policies.csv line {n}: host, allows_rows and (for true) terms_ref needed"
                )
            days = (r["retain_days"] or "").strip()
            if days and (not days.isdigit() or int(days) < 1):
                raise EvidenceError(
                    f"dataset_policies.csv line {n}: retain_days must be a positive whole number"
                )
            if host in out:
                raise EvidenceError(f"dataset_policies.csv line {n}: host {host} is listed twice")
            out[host] = RowPolicy(
                host,
                terms,
                allows == "true",
                (r["licence"] or "").strip(),
                int(days) if days else None,
            )
    return out


def rows_permission(
    *,
    kind: str,
    url: str | None,
    licence: str | None,
    row_policies: dict[str, RowPolicy],
    open_licences: dict[str, str],
    source_policies: dict[str, SourcePolicy] | None = None,
) -> RowsPermission:
    """May the rows extracted from this source be stored? Default is no."""
    if kind == "upload":
        return RowsPermission(True, "own_upload")
    host = (urlsplit(url or "").hostname or "").lower()
    parts = host.split(".")
    for i in range(len(parts) - 1):
        pol = row_policies.get(".".join(parts[i:]))
        if pol is not None:
            if pol.allows_rows:
                return RowsPermission(
                    True,
                    f"host_policy: {pol.host}",
                    pol.licence or licence,
                    pol.terms_ref,
                    pol.retain_days,
                )
            return RowsPermission(False, f"host_policy: {pol.host} does not allow storing rows")
    if source_policies:
        sp = policy_for(source_policies, url or "")
        if sp is not None and sp.allows_full:
            return RowsPermission(
                True, f"source_policy: {sp.host}", sp.licence or licence, sp.terms_ref
            )
    lic = (licence or "").strip().lower()
    if lic and lic in open_licences:
        return RowsPermission(
            True, f"open_licence: {lic}", lic, f"licence reported by the source: {lic}"
        )
    return RowsPermission(False, "no recorded permission or open licence covers this source")


@dataclass(frozen=True)
class StoredDataset:
    key: str
    path: Path
    created: bool
    expires_at: datetime


@dataclass(frozen=True)
class OpenedDataset:
    key: str
    dataset: Dataset
    manifest: dict[str, Any]


Namespace = int | str  # a database project id ("p<id>"), or a research project label (as given)
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def research_namespace(label: str) -> str:
    """The directory name for a database-free research project. Cannot collide with ``p<id>``."""
    if not LABEL_RE.match(label):
        raise EvidenceError("project label must match [a-z0-9][a-z0-9-]{0,62}")
    return f"r-{label}"


def _root(root: Path, ns: Namespace) -> Path:
    return root / (f"p{ns}" if isinstance(ns, int) else ns)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False, default=str))


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


@scoped_api
def save_dataset(
    root: Path,
    scope: ProjectScope,
    collected: CollectedDataset,
    *,
    now: datetime,
    permission: RowsPermission,
    default_retention_days: int = 90,
    evidence_source_id: int | None = None,
    kind: str = "web",
    extra: dict[str, Any] | None = None,
) -> StoredDataset:
    """Persist one collected table. Raises RowsNotPermitted when nothing permits it."""
    return save_in(
        root, scope.project_id, collected, now=now, permission=permission,
        default_retention_days=default_retention_days, evidence_source_id=evidence_source_id,
        kind=kind, extra=extra,
    )  # fmt: skip


def save_in(
    root: Path,
    ns: Namespace,
    collected: CollectedDataset,
    *,
    now: datetime,
    permission: RowsPermission,
    default_retention_days: int = 90,
    evidence_source_id: int | None = None,
    kind: str = "web",
    extra: dict[str, Any] | None = None,
) -> StoredDataset:
    """The one writer, for a database project (via ``save_dataset``) or a research project label."""
    if not permission.allowed:
        raise RowsNotPermitted(permission.basis)
    ds = collected.dataset
    days = (
        min(default_retention_days, permission.retain_days)
        if permission.retain_days
        else default_retention_days
    )
    if days < 1:
        raise EvidenceError("retention days must be at least 1")
    frame = ds.frame
    columns = [str(c) for c in frame.columns]
    rows = [[_jsonable(v) for v in row] for row in frame.itertuples(index=False, name=None)]
    thash = table_sha256(frame)
    provenance = {
        "page_url": collected.page_url, "page_sha256": collected.page_sha256, "table_index": collected.table_index,
        "table_caption": collected.table_caption, "retrieved_at": collected.retrieved_at,
    }  # fmt: skip
    key = run_name(thash, sha256_bytes(json.dumps(provenance, sort_keys=True).encode()))
    project_dir = _root(root, ns)
    existing = project_dir / key
    if existing.exists():
        try:
            manifest = json.loads((existing / "manifest.json").read_text(encoding="utf-8"))
            if _parse(manifest["expires_at"]) > now:
                return StoredDataset(key, existing, False, _parse(manifest["expires_at"]))
        except (OSError, ValueError, KeyError):
            pass  # unreadable: replaced below
        _remove(existing)  # expired or damaged: store afresh with a new expiry
    expires = now + timedelta(days=days)
    manifest = {
        "format_version": FORMAT_VERSION, "project_id": ns, "key": key, "kind": kind,
        "created_at": _stamp(now), "expires_at": _stamp(expires), "retention_days": days,
        "table_sha256": thash, "rows": len(rows), "columns": columns,
        "source": _jsonable(ds.source.model_dump()), "provenance": provenance,
        "evidence_source_id": evidence_source_id,
        "licence": permission.licence, "terms_ref": permission.terms_ref, "permission_basis": permission.basis,
        "skipped_tables": _jsonable(list(collected.skipped_tables)), "loader_notes": _jsonable(ds.notes),
        "extra": _jsonable(extra or {}),
    }  # fmt: skip
    body = json.dumps({"columns": columns, "rows": rows}, allow_nan=False, ensure_ascii=False)
    if len(body.encode()) > MAX_STORED_BYTES:
        raise EvidenceError("the table is too large to store")

    def write(tmp: Path) -> None:
        (tmp / "table.json").write_text(body, encoding="utf-8")
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

    path, present = publish(project_dir, key, write)
    return StoredDataset(key, path, not present, expires)


def _remove(path: Path) -> None:
    from sie.datasets.store import _remove_tree

    _remove_tree(path)


def _load(root: Path, project_id: Namespace, key: str) -> tuple[Path, dict[str, Any]]:
    if not key or "/" in key or "\\" in key or key.startswith("."):
        raise NotFound("no such dataset")
    path = _root(root, project_id) / key
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotFound("no such dataset") from None
    if manifest.get("project_id") != project_id or manifest.get("key") != key:
        raise NotFound("no such dataset")
    return path, manifest


@scoped_api
def open_stored(
    root: Path, scope: ProjectScope, key: str, *, now: datetime, allow_expired: bool = False
) -> OpenedDataset:
    """Reopen a stored dataset. Re-hashes the table; an altered file is refused, an expired one too."""
    return open_in(root, scope.project_id, key, now=now, allow_expired=allow_expired)


def open_in(
    root: Path, ns: Namespace, key: str, *, now: datetime, allow_expired: bool = False
) -> OpenedDataset:
    path, manifest = _load(root, ns, key)
    if not allow_expired and _parse(manifest["expires_at"]) <= now:
        raise DatasetExpired("the stored dataset passed its retention date; collect it again")
    try:
        data = json.loads((path / "table.json").read_text(encoding="utf-8"))
        frame = pd.DataFrame(data["rows"], columns=data["columns"], dtype=object)
    except (OSError, ValueError, KeyError) as exc:
        raise DatasetError(f"the stored table cannot be read: {exc}") from exc
    frame = frame.where(frame.notna(), None)
    frame.index = pd.Index(range(1, len(frame) + 1), name="row_id", dtype="int64")
    if table_sha256(frame) != manifest["table_sha256"]:
        raise DatasetError("the stored table no longer matches its recorded hash")
    ds = Dataset(
        frame=frame, source=SourceRef(**manifest["source"]), notes=manifest.get("loader_notes", [])
    )
    return OpenedDataset(key, ds, manifest)


@scoped_api
def list_datasets(root: Path, scope: ProjectScope, *, now: datetime) -> list[dict[str, Any]]:
    return list_in(root, scope.project_id, now=now)


def list_in(root: Path, ns: Namespace, *, now: datetime) -> list[dict[str, Any]]:
    out = []
    base = _root(root, ns)
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        try:
            _, m = _load(root, ns, entry.name)
        except NotFound:
            continue
        out.append({
            "key": m["key"], "rows": m["rows"], "columns": m["columns"], "page_url": m["provenance"]["page_url"],
            "retrieved_at": m["provenance"]["retrieved_at"], "expires_at": m["expires_at"],
            "state": "expired" if _parse(m["expires_at"]) <= now else "active",
            "licence": m.get("licence"), "terms_ref": m.get("terms_ref"), "extra": m.get("extra", {}),
            "table_caption": m["provenance"].get("table_caption"),
        })  # fmt: skip
    return out


@scoped_api
def find_cached(root: Path, scope: ProjectScope, *, page_url: str, now: datetime) -> list[str]:
    """Keys of active datasets this project already holds for this page, newest retrieval first."""
    hits = [
        d
        for d in list_datasets(root, scope, now=now)
        if d["page_url"] == page_url and d["state"] == "active"
    ]
    hits.sort(key=lambda d: (d["retrieved_at"] or "", d["key"]), reverse=True)
    return [d["key"] for d in hits]


@scoped_api
def delete_dataset(root: Path, scope: ProjectScope, key: str) -> None:
    delete_in(root, scope.project_id, key)


def delete_in(root: Path, ns: Namespace, key: str) -> None:
    path, _ = _load(root, ns, key)
    _remove(path)


def count_project_datasets(root: Path, project_id: int) -> int:
    base = _root(root, project_id)
    return sum(1 for e in base.iterdir() if not e.name.startswith(".")) if base.is_dir() else 0


def delete_project_datasets(root: Path, project_id: int) -> int:
    """Remove every stored dataset of a project (used by ``sie project purge``). Returns how many."""
    base = _root(root, project_id)
    if not base.is_dir():
        return 0
    n = sum(1 for e in base.iterdir() if not e.name.startswith("."))
    shutil.rmtree(base, ignore_errors=True)
    return n


@dataclass(frozen=True)
class PruneResult:
    executed: bool
    expired: tuple[str, ...]
    removed: int


def prune_expired(
    root: Path, *, now: datetime, execute: bool = False, confirm: str = ""
) -> PruneResult:
    """Owner maintenance across all projects: expired datasets, dry run unless confirmed with 'prune'."""
    if execute and confirm != "prune":
        raise EvidenceError("deleting datasets needs --confirm prune")
    found: list[str] = []
    if root.is_dir():
        for pdir in sorted(root.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            for entry in sorted(pdir.iterdir()):
                if entry.name.startswith("."):
                    continue
                try:
                    expires = _parse(
                        json.loads((entry / "manifest.json").read_text(encoding="utf-8"))[
                            "expires_at"
                        ]
                    )
                except (OSError, ValueError, KeyError):
                    expires = datetime.min.replace(tzinfo=UTC)  # unreadable: treat as expired
                if expires <= now:
                    found.append(f"{pdir.name}/{entry.name}")
    removed = 0
    if execute:
        for item in found:
            _remove(root / item)
            removed += 1
        for pdir in root.iterdir():
            if pdir.is_dir():
                remove_stale_temp(pdir, 0)
    return PruneResult(execute, tuple(found), removed)
