# Evidence Retention, Purge and Backup Limits (ADR-042)

## 1. Retention modes
| Mode | Stored | Used for |
|------|--------|----------|
| `full` | the bytes (gzip in `raw_blobs`) | every upload; web pages only when allowed |
| `excerpt` | hash, size, metadata and the claim excerpts, no page bytes | default for web pages |
| `metadata` | hash, size, metadata | web copies after `prune` |

A claim on an `excerpt` or `metadata` source stays `unverified` (`bytes_not_retained`): the system cannot re-check bytes it does not hold.

## 2. Full web copies
Allowed only for a host listed in `data/evidence/source_policies.csv` with `allows_full=true` and a recorded `terms_ref` (where the permission or licence was read) and date. The file is committed empty, so the default is deny. Each full copy gets `retain_until = fetched + EVIDENCE_FULL_RETENTION_DAYS` (default 90). `sie evidence prune` drops the bytes of expired copies and keeps the source row and its claims. Rules are the owner's reading of a site's terms, not legal advice.

## 3. Purge
`sie project purge <slug>` deletes a project's sources, claims, reviews, conflicts, document links and its raw rows from the live database in one transaction, writes a tombstone, renames the project to `purged-<id>` and closes it. It refuses when another table references the raw rows, when another project links the same document, or when a document is not namespaced to the project. Sports data is never touched (tested).

## 4. What purge does NOT do
- It does **not** erase backups. Encrypted dumps made before the purge keep the data until they expire (`BACKUP_RETENTION_DAYS`, 14 in `backup.yml`).
- It does **not** erase the database provider's own history. Neon's point-in-time window: the owner reported **6 hours** for the production branch from the Neon console on 2026-10-14 (owner-reported; this repository cannot check it). Record it by setting `DB_HISTORY_WINDOW_HOURS`. Until then every purge report says so.
- Restoring a backup taken before a purge brings the data back. After any restore, re-run purge for each project in `purge_tombstones` that is not purged in the restored copy.
- Nothing in this repository claims complete erasure.

## 5. Settings
`EVIDENCE_MAX_UPLOAD_BYTES` (25 MiB), `EVIDENCE_MAX_DECOMPRESSED_BYTES` (200 MiB), `EVIDENCE_FULL_RETENTION_DAYS` (90), `EVIDENCE_POLICY_PATH`, `DB_HISTORY_WINDOW_HOURS` (hours; blank = not verified), `BACKUP_RETENTION_DAYS` (14).
