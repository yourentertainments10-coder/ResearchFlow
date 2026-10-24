"""Collected datasets on disk: written once, deduplicated by content, reused for follow-up questions.

The id is a hash of the page's content hash, the table label and the exact bytes given to the loader, so
collecting the same page again never creates a second copy and never changes the first. Files are plain
and read-only: ``source.csv|json`` (what the loader read) and ``meta.json`` (where it came from and what
was flagged). Loading re-hashes the source and refuses a changed file. No database and no migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sie.datasets import LoadOptions, open_dataset
from sie.datasets.provenance import sha256_bytes
from sie.evidence.discovery import terms
from sie.evidence.errors import EvidenceError
from sie.research.collect import CSV_OPTIONS, CollectedTable

JSON_OPTIONS = LoadOptions(rename_duplicates=True)


@dataclass(frozen=True)
class StoredDataset:
    id: str
    path: Path
    already_present: bool
    meta: dict


def dataset_id(table: CollectedTable) -> str:
    text = "|".join([table.raw_sha256, table.label, sha256_bytes(table.source_bytes)])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ResearchStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, ds_id: str) -> Path:
        if not ds_id.isalnum() or len(ds_id) != 16:
            raise EvidenceError("bad dataset id")
        return self.root / "datasets" / ds_id

    def save(self, table: CollectedTable, *, title: str = "") -> StoredDataset:
        ds_id = dataset_id(table)
        final = self._dir(ds_id)
        if final.exists():
            return StoredDataset(ds_id, final, True, json.loads((final / "meta.json").read_text()))
        suffix = Path(table.source_name).suffix
        meta: dict[str, Any] = {
            "id": ds_id,
            "title": title or table.label,
            "url": table.url,
            "retrieved_at": table.retrieved_at,
            "content_type": table.content_type,
            "raw_sha256": table.raw_sha256,
            "label": table.label,
            "source_file": f"source{suffix}",
            "source_sha256": sha256_bytes(table.source_bytes),
            "rows": table.rows,
            "columns": [str(c) for c in table.dataset.frame.columns],
            "flags": list(table.flags),
        }
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=final.parent, prefix=".tmp-"))
        try:
            (tmp / meta["source_file"]).write_bytes(table.source_bytes)
            (tmp / "meta.json").write_text(
                json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
            )
            for f in tmp.iterdir():
                os.chmod(f, stat.S_IREAD)
            try:
                os.rename(tmp, final)
            except OSError:
                if not final.exists():
                    raise
                shutil.rmtree(tmp, ignore_errors=True)  # a concurrent writer won: same content
                return StoredDataset(
                    ds_id, final, True, json.loads((final / "meta.json").read_text())
                )
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return StoredDataset(ds_id, final, False, meta)

    def meta(self, ds_id: str) -> dict:
        path = self._dir(ds_id) / "meta.json"
        if not path.exists():
            raise EvidenceError(f"no stored dataset {ds_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def load(self, ds_id: str) -> CollectedTable:
        meta = self.meta(ds_id)
        data = (self._dir(ds_id) / meta["source_file"]).read_bytes()
        if sha256_bytes(data) != meta["source_sha256"]:
            raise EvidenceError(f"stored dataset {ds_id} no longer matches its recorded hash")
        options = JSON_OPTIONS if meta["source_file"].endswith(".json") else CSV_OPTIONS
        name = f"page{Path(meta['source_file']).suffix}"
        ds = open_dataset(
            data, name, options, kind="fetched", url=meta["url"], retrieved_at=meta["retrieved_at"]
        )
        return CollectedTable(
            ds, meta["label"], meta["raw_sha256"], meta["url"], meta["retrieved_at"],
            meta["content_type"], name, data, tuple(meta["flags"]),
        )  # fmt: skip

    def list_datasets(self) -> list[dict]:
        base = self.root / "datasets"
        if not base.exists():
            return []
        return [
            json.loads((d / "meta.json").read_text(encoding="utf-8"))
            for d in sorted(base.iterdir())
            if d.is_dir() and not d.name.startswith(".tmp-") and (d / "meta.json").exists()
        ]

    def reuse(self, question: str, *, min_share: float = 0.5) -> list[tuple[dict, float]]:
        """Stored datasets whose title, address or column names cover enough of the question's words."""
        wanted = terms(question)
        if not wanted:
            return []
        scored = []
        for meta in self.list_datasets():
            have = terms(" ".join([meta["title"], meta["url"], *meta["columns"]]))
            share = len(wanted & have) / len(wanted)
            if share >= min_share:
                scored.append((meta, round(share, 4)))
        return sorted(scored, key=lambda x: (-x[1], x[0]["id"]))
