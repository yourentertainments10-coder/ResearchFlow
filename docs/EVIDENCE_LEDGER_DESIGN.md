# Evidence Ledger and Upload Storage (ADR-042)

Status: implemented in `main` via merged PR #28; project-scoped health was added by merged PR #30. Code: `src/sie/evidence/`. DDL: `src/sie/db/sql/005_evidence_ledger.sql`.

## 1. What it is
A per-project store of **sources** (an uploaded file, or a fetched web page) and **claims** (one stated value taken from a source, with where it came from). It feeds `sie.answer` as `EvidenceRef` and evidence-layer `Figure` objects. It does not change any sports table or view.

## 2. Tables
| Table | Purpose |
|-------|---------|
| `projects` | A workspace. Slug, name, optional `archived_at` (read only) and `purged_at` (closed for good). No foreign key to `competitions` (avoids a lock on it) |
| `project_documents` | Links a project to the raw document that holds its bytes. A raw document belongs to exactly one project |
| `evidence_sources` | One row per source: kind `upload` or `web`, tier 1-4, SHA-256, size, retention mode, `raw_version_id` when bytes are kept, web fields (requested and final URL, HTTP status), `terms_ref` and `retain_until` for full web copies, `pruned_at` |
| `claims` | Immutable: subject, metric, value text and optional number, excerpt and its hash, locator, extraction method and version, `claim_hash` (unique per project) |
| `claim_reviews` | Append only. The current status is the latest row |
| `claim_conflicts` | Two claims in the same project that disagree; resolved by a named person |
| `purge_tombstones` | What was purged, by whom, counts, and the backup and history limits that applied |
| `evidence_claims_current` (view) | Claims joined to their latest review |

Project isolation is enforced in the database: child rows carry `project_id` and reference parents by `(id, project_id)`.

## 3. Status meanings
`unverified` (default) < `source_located` (system found the value where the locator says, in bytes still held) / `human_confirmed` (a named person confirmed the claim matches its source) / `disputed` / `rejected`. **None of them says the claim is true.** A source can be wrong. Answers present evidence figures in the evidence layer only.

## 4. Locators
`table_cell` (CSV: row, column, delimiter), `json_pointer`, `char_range`, `none`. Limits: 1,000,000 rows, 500 columns, 32,768 characters per cell, 60 s. XLSX: not read here; claims stay `unverified` with reason `xlsx_verification_pending_datasets_loader`. When that loader exists, verification will call it instead of adding a second reader. Cached values and formulas will need an explicit rule at that time.

## 5. Concurrency
One transaction at READ COMMITTED, `lock_timeout` 10 s, `statement_timeout` 60 s, project row `FOR SHARE` (purge takes `FOR UPDATE`, so it waits for in-flight ingests), advisory lock per document key, bounded retry (3 attempts) on unique violation, serialisation failure and deadlock, then `IngestBusy`. The raw store's own row lock keeps version numbers gap-free (tested with 8 threads).

## 6. API rules
Public functions take a `ProjectScope` obtained from `open_scope`, never a raw id or bare hash. `tests/integration/test_evidence_isolation.py` has one cross-project case per scoped function and fails when one is missing. A static test checks that every SQL string touching a project-owned table mentions `project_id`.

## 7. Commands
`sie project create|list|purge`, `sie evidence upload|check|prune`. Purge and prune are owner-only (the `pipeline` role is refused), dry-run by default, and need `--execute` plus `--confirm <slug>` (purge, and `--by` naming a person) or `--confirm prune`.

## 8. Project health (Phase 3, ADR-043)
Read-only; no table, no migration. `sie.evidence.health.project_health(conn, scope, now, web_max_age, old_upload_after)` and `sie project health <slug> [--json]`. Source states: `pruned` (the stored bytes were actually removed; never reported as current), `expired` (the retention deadline `retain_until` has passed, bytes may still be stored until `sie evidence prune`), `stale` (a web source older than `EVIDENCE_WEB_MAX_AGE_DAYS`, default 30: potentially outdated, not necessarily false), `current`, and `upload` (uploads never go stale by age; `old_upload` is an informational flag after `EVIDENCE_OLD_UPLOAD_DAYS`, default 180). The report also gives claims by current status, claims on pruned and expired sources, XLSX claims still unverified, and open conflict ids. Exit code 0 unless a source is pruned, expired or stale or a conflict is open (exit 1); an unknown project exits 2. An empty project prints an explicit `no_sources` report and exits 0; an old upload alone exits 0. `gaps_from_health(report)` turns the problems into `sie.answer` gaps (`stale`, `missing_data`, `conflict`, `unverified`). The 90-day full-copy retention is unchanged. Health says nothing about whether a claim is true.

## 9. Not built
Web fetching itself (Phase 4; `register_fetched_source` only records what a fetcher passes it), authentication and quotas, XLSX verification, any UI.
