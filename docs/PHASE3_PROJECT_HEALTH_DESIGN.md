# Phase 3: Project-scoped freshness and health (design, for owner approval)

> **Current implementation correction (2026-10-30):** Project-scoped health is implemented in `src/sie/evidence/health.py` and covered by tests. The original design questions and branch sequencing below are retained as historical context. XLSX claim verification remains pending in `src/sie/evidence/verify.py` (`PENDING_XLSX`); do not describe the whole document as unimplemented.

Status: **design approved 2026-10-15; project-health implementation merged.** The original design text below preserves the initial proposal; check the current code and tests for implementation status. Decisions: web max age 30 days; uploads never stale, informational `old_upload` only; `expired` = deadline passed, `pruned` = bytes removed; empty project reports no sources. Rules: AGENTS.md sections 1, 2 (rules 2, 5, 6, 7, 12, 15) and 7.

## 1. Requirement (restated)
`sie health` and `sie source-status` answer "is this competition's source current?" from `ingest_runs`, `raw_fetches` and `raw_versions`, keyed by `competition`. A research project (ADR-042) has no such answer. The owner needs, per project: which sources are old, expired or pruned, which claims are unreviewed, which conflicts are open, and the disclosed `Gap`s an answer must carry because of them (ADR-039, audit section 8 step 3).

## 2. Assumptions (please confirm or correct)
1. Read-only: no new table and no migration. Everything is computed from `projects`, `evidence_sources`, `claims`, `claim_reviews` (via `evidence_claims_current`) and `claim_conflicts`.
2. The sports freshness and health code is left as it is. Its four statuses keep their meaning (no silent metric change).
3. An **upload** has no refresh cycle, so it is never "stale" by age. It reports `retrieved_at` and age only. A **web** source is stale after `EVIDENCE_WEB_MAX_AGE_DAYS` (new setting, default 30; blank is not allowed to mean "never"). This default is my guess; the owner should set it.
4. Health says nothing about truth. It reports process state only, using the status meanings of `EVIDENCE_LEDGER_DESIGN.md` section 3.
5. Alerts are not sent. Health returns data and an exit code; delivery stays with the existing notifier, wired in a later phase.

## 3. Impact analysis
| Area | Impact |
|------|--------|
| Database | None. New read queries only, each parametrised and filtered by `project_id` |
| Pipeline | None. Existing health, freshness and alerts untouched (a test pins their output) |
| Analytics / dashboard | None |
| `sie.answer` | New adapter `gaps_from_health(report) -> tuple[Gap, ...]` using existing `GapKind` (`stale`, `conflict`, `unverified`, `missing_data`). No change to `sie.answer` models |
| Config | One new setting, documented in `.env.example` |
| Tests | Unit (pure classification), integration on PostgreSQL (CI), isolation case per scoped function |
| Docs | `EVIDENCE_LEDGER_DESIGN.md` section 8, `TESTING.md`, `CHANGELOG`, ADR-043 |

## 4. Design
New module `src/sie/evidence/health.py`, public functions take a `ProjectScope` (`@scoped_api`, so the isolation test insists on a cross-project case).

- `classify_source(kind, retrieved_at, retain_until, pruned_at, now, max_age) -> SourceState` (pure): `pruned`, `expired` (full web copy past `retain_until`), `stale` (web only, older than max age), `current`, `upload` (age reported, never stale).
- `project_health(conn, scope, now, max_age) -> ProjectHealth`: per-source states; claim counts by current status; claims whose source is pruned or expired (cannot be re-located); XLSX claims awaiting the datasets loader; open `claim_conflicts` with ids; `completeness` = claims by status over all claims, shown as counts, never as a score; `checked_at`.
- `gaps_from_health(report) -> tuple[Gap, ...]`: one disclosed gap per problem class, with counts from the database only.
- CLI `sie project health <slug> [--json]`: exit 0 when no source is stale, expired or pruned and no conflict is open; exit 1 otherwise; exit 2 for an unknown or archived-for-write project. Read-only role is enough.
- No number comes from prose or a model (rule 2). Empty project returns an explicit "no sources" report, not zero-as-fine (rule 12).

## 5. Test plan
Unit: classification table (every state and boundary, including exactly at max age); gap mapping (each problem class, none when healthy); JSON shape is stable and sorted. Integration (PostgreSQL): mixed project (upload, fresh web, stale web, pruned, expired) gives the expected report; open conflict listed and resolved conflict not; second project's data never appears (one case per scoped function); running twice returns identical results (idempotent, read-only: row counts unchanged); sports health output unchanged (golden). Lint: ruff check and format, mypy on the new module.

## 6. Open questions for the owner
1. Default web max age 30 days: keep, or choose another?
2. Should an upload older than some age be flagged as "old" (informational), or never?
3. PR target: stack on `feature/evidence-ledger` (so the diff shows only Phase 3) or wait for #28 to merge? Recommendation: stack, retarget to `main` after #28 merges.

## 7. Datasets (merged in PR #29) and XLSX claim verification
**Still pending:** the current claim verifier returns `PENDING_XLSX` for `xlsx_cell` locators. XLSX claim verification needs a separately reviewed implementation using the shared dataset loader, with explicit cached-value/formula rules. Until implemented and tested, XLSX claims stay unverified.
