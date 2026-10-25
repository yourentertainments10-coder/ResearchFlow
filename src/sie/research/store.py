"""Collected datasets for ``sie research``: one storage mechanism, shared with the evidence ledger.

This is a thin door onto ``sie.evidence.dataset_store`` (which builds on ``sie.datasets.store``): the same
atomic publish, hash check, expiry and permission rules. A research *project label* (``--project``) takes
the place of a database project id, so the research commands stay free of the database. What is kept is
only the normalised table and its manifest (source URL, retrieval time, page hash, table label, schema,
licence and terms as reported by the catalogue and as permitted, retention); never the page itself
(ADR-042, ADR-044). Rows are written only when a recorded policy or an open licence permits it; otherwise
the table is used for this run and not saved, and the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sie.evidence.collect import CollectedDataset
from sie.evidence.dataset_store import (
    RowPolicy,
    delete_in,
    list_in,
    load_open_licences,
    load_row_policies,
    open_in,
    prune_expired,
    research_namespace,
    rows_permission,
    save_in,
)
from sie.evidence.errors import DatasetExpired, EvidenceError
from sie.evidence.retention import SourcePolicy, load_policies
from sie.research.collect import CollectedTable


@dataclass(frozen=True)
class StoreRules:
    open_licences: dict[str, str]
    row_policies: dict[str, RowPolicy]
    source_policies: dict[str, SourcePolicy]

    @classmethod
    def load(cls, evidence_dir: Path, source_policies: Path | None = None) -> StoreRules:
        return cls(
            load_open_licences(evidence_dir / "open_licences.csv"),
            load_row_policies(evidence_dir / "dataset_policies.csv"),
            load_policies(source_policies or evidence_dir / "source_policies.csv"),
        )

    @classmethod
    def empty(cls) -> StoreRules:
        return cls({}, {}, {})


@dataclass(frozen=True)
class SaveResult:
    table: CollectedTable
    id: str | None  # None: the rows were not saved
    already_present: bool
    expires_at: datetime | None
    basis: str  # why it may be kept, or why it was not saved

    @property
    def stored(self) -> bool:
        return self.id is not None


def _extra(table: CollectedTable, title: str) -> dict:
    return {
        "label": table.label, "content_type": table.content_type, "source_name": table.source_name,
        "flags": list(table.flags), "catalogue": table.catalogue, "reported_licence": table.licence,
        "title": title or table.label,
    }  # fmt: skip


class ResearchStore:
    def __init__(
        self,
        root: Path,
        *,
        project: str = "default",
        rules: StoreRules | None = None,
        retention_days: int = 90,
        now=lambda: datetime.now(UTC),
    ) -> None:
        self.root, self.project = root, project
        self.ns = research_namespace(project)
        self.rules = rules or StoreRules.empty()
        self.retention_days, self._now = retention_days, now

    def save(self, table: CollectedTable, *, title: str = "") -> SaveResult:
        """Keep the normalised table if its source's terms allow it; otherwise say why not."""
        perm = rows_permission(
            kind="web",
            url=table.url,
            licence=table.licence,
            row_policies=self.rules.row_policies,
            open_licences=self.rules.open_licences,
            source_policies=self.rules.source_policies,
        )
        if not perm.allowed:
            return SaveResult(table, None, False, None, perm.basis)
        collected = CollectedDataset(
            table.dataset, table.raw_sha256, table.url, table.retrieved_at, None, table.label
        )
        stored = save_in(
            self.root, self.ns, collected, now=self._now(), permission=perm,
            default_retention_days=self.retention_days,
            extra=_extra(table, title),
        )  # fmt: skip
        return SaveResult(table, stored.key, not stored.created, stored.expires_at, perm.basis)

    def load(self, ds_id: str) -> CollectedTable:
        opened = open_in(self.root, self.ns, ds_id, now=self._now())
        m = opened.manifest
        x = m.get("extra", {})
        prov = m["provenance"]
        return CollectedTable(
            opened.dataset, x.get("label", ""), prov["page_sha256"], prov["page_url"],
            prov["retrieved_at"], x.get("content_type", ""), x.get("source_name", ""), b"",
            tuple(x.get("flags", [])), x.get("reported_licence") or m.get("licence"),
            m.get("terms_ref") or "", x.get("catalogue", ""), ds_id, m["expires_at"],
        )  # fmt: skip

    def list_datasets(self, *, include_expired: bool = False) -> list[dict]:
        rows = list_in(self.root, self.ns, now=self._now())
        return rows if include_expired else [r for r in rows if r["state"] == "active"]

    def delete(self, ds_id: str) -> None:
        delete_in(self.root, self.ns, ds_id)

    def prune(self, *, execute: bool = False):
        """Expired datasets of every project under this root (dry run unless ``execute``)."""
        return prune_expired(
            self.root, now=self._now(), execute=execute, confirm="prune" if execute else ""
        )


__all__ = ["DatasetExpired", "EvidenceError", "ResearchStore", "SaveResult", "StoreRules"]
