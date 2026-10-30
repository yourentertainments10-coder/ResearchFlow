"""Write a workspace to a directory, atomically, exporting only what verified.

Files: ``workspace.json`` (everything shown, without the tables of unverified analyses), per verified
analysis ``analysis_<id>.csv`` and (for charts) ``analysis_<id>.svg``, and ``manifest.json`` with the size
and SHA-256 of every other file. An analysis whose independent check failed is listed under
``not_exported`` with the reason and no data. The directory name is ``ws-<input sha 12>-<workspace sha 8>``,
so the same request finds its existing directory instead of writing again.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sie.datasets.export import table_csv_bytes
from sie.datasets.models import SERIALISATION_VERSION
from sie.datasets.store import publish, remove_stale_temp
from sie.workspace.build import Workspace

MANIFEST_VERSION = 1


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def exportable_document(ws: Workspace) -> dict[str, Any]:
    """The workspace dict with every unverified analysis and card stripped of its numbers."""
    doc = ws.to_dict()
    for item in doc["analyses"]:
        if not item["exportable"]:
            item.pop("result", None)
    doc["cards"] = [c for c in doc["cards"] if c["verified"]]
    return doc


def directory_name(ws: Workspace) -> str:
    return f"ws-{ws.source.sha256[:12]}-{ws.sha256()[:8]}"


def write_workspace(ws: Workspace, out_root: Path) -> tuple[Path, bool, list[dict[str, str]]]:
    """Publish the directory. Returns ``(directory, already_present, not_exported)``."""
    skipped = [
        {
            "id": i.planned.id,
            "title": i.planned.title,
            "reason": i.error
            or i.verification.reason
            or "; ".join(i.verification.mismatches[:3])
            or "not verified",
        }
        for i in ws.items
        if not i.exportable
    ]

    def writer(temp: Path) -> None:
        files: dict[str, bytes] = {"workspace.json": _json_bytes(exportable_document(ws))}
        for item in ws.items:
            if not item.exportable:
                continue
            csv_bytes, _ = table_csv_bytes(item.result.table)
            files[f"analysis_{item.planned.id}.csv"] = csv_bytes
            if item.svg:
                files[f"analysis_{item.planned.id}.svg"] = item.svg.encode("utf-8")
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "serialisation_version": SERIALISATION_VERSION,
            "input_sha256": ws.source.sha256,
            "workspace_sha256": ws.sha256(),
            "files": {
                name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(files.items())
            },
            "not_exported": skipped,
            "limitations": ws.limitations,
        }
        files["manifest.json"] = _json_bytes(manifest)
        for name, data in files.items():
            with open(temp / name, "xb") as handle:
                handle.write(data)

    remove_stale_temp(out_root)
    directory, present = publish(out_root, directory_name(ws), writer)
    return directory, present, skipped


def verify_written(directory: Path) -> dict[str, Any]:
    """Re-hash every file against ``manifest.json``. Raises ``ValueError`` on any difference."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for name, info in manifest["files"].items():
        data = (directory / name).read_bytes()
        if len(data) != info["bytes"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise ValueError(f"{name} does not match the manifest")
    extra = {p.name for p in directory.iterdir()} - set(manifest["files"]) - {"manifest.json"}
    if extra:
        raise ValueError(f"files not in the manifest: {sorted(extra)}")
    return manifest
