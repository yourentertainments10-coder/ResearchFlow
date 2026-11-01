# Roadmap

> **Historical phase-plan warning (2026-10-30):** The original sports-engine phase headings below preserve the project's chronology; some individual phase descriptions predate later merges and must not be read as the current workspace status. For the active product sequence and acceptance gates, use the canonical summary above and [WORKSPACE_PRODUCT_DIRECTION.md](WORKSPACE_PRODUCT_DIRECTION.md). The generic dataset library, answer contract, evidence ledger and project-health foundation have since been merged; the visual workspace and complete CSV-to-export journey remain open.

> **Navigation:** [Documentation map](README.md) · [Canonical workspace direction](WORKSPACE_PRODUCT_DIRECTION.md) · [Acceptance evidence](../ACCEPTANCE.md).

> **Product-level roadmap alignment (2026-10-29):** The phase history below contains the original sports-engine plan and intermediate general-research phases. The owner-approved current delivery order is in [WORKSPACE_PRODUCT_DIRECTION.md](WORKSPACE_PRODUCT_DIRECTION.md): (1) reconcile documentation, (2) complete the CSV upload → profiling → multiple charts → natural-language refinement → verification → export journey, (3) build the visual workspace, (4) implement source-choice/fallback controls, (5) prove free-first live web research, (6) connect research results to the workspace, and (7) harden end-to-end security/accessibility/export tests. Do not mark a phase complete based only on design documents or simulated CI.

**Current gate:** Phase 4 web research remains on draft PR #31. Live structured-data discover → permitted fetch → collect/save → deterministic analysis → report/reuse, and target-Windows save/reuse, have not yet passed acceptance. The complete generic visual workspace is not built. Preserve the existing sports acceptance baseline.

**Slice 2 (2026-10-30):** the CSV workspace request planner and session replay are implemented on `feature/workspace-planner` in a separate PR awaiting owner approval (headless library only; no UI, server, filters or model). Not merged until approved.

**Execution update (2026-10-30):** PR #35 merged after CI completed successfully. It fixes a false `no non-null values` warning emitted by the validation-only pass in grouped dataset analytics and adds regression coverage. This is a correctness fix to the existing generic dataset library, not completion of the visual workspace. The next active work is the verified repository/documentation audit followed by the CSV-first visual workspace design and end-to-end acceptance path.


Work phase by phase. A phase is finished only when its acceptance criteria are met and evidence is saved. Sizes are relative effort (S, M, L), not promises.

## Phase 0: Source discovery (gate)  [M]  (status: done, 2026-10-04; D1 cleared by the owner 2026-10-07; API found, decodable, and event-level data confirmed with 469 events, see `SOURCE_DISCOVERY.md` sections 8 to 10. The live fetcher was built in Phase 6, ADR-031)
Tasks
1. Identify the official results site, any API or JSON feed, and the official medal table.
2. Fill the checklist in `DATA_PIPELINE.md` section 2 for each candidate.
3. Save trimmed samples as fixtures.
4. Confirm competition facts (dates, sports, event total) from the source and record them.
5. Choose the primary source and a fallback. Record in `docs/SOURCE_DISCOVERY.md` and `DECISIONS.md`.

Acceptance: a written decision stating how event-level data will be obtained (automatic, partly manual, or fully manual import), with robots/terms status and sample files.

## Phase 1: Project skeleton and database  [S]  (status: done, 2026-10-04)
**Design-freeze gate: migration `001_initial_schema` is not written until all of these are confirmed in the docs.** (Confirmed before migration 001 was written.)
- [ ] `DOMAIN_MODEL.md`: event, medal placing, entrant, country medal and their counting rules
- [ ] Placing versioning and the reallocation procedure (`DATABASE.md` sections 3 and 6), with tests 4, 5, 21 to 24 written as failing tests first
- [ ] Raw versioning semantics (`DATA_PIPELINE.md` section 4), with tests 15 to 17
- [ ] Conflict policy table (`DATA_PIPELINE.md` section 7), with test 18
- [ ] Snapshot identity (`DATABASE.md` section 8), with test 19
- [ ] Adapter and parser split and import-rule test (`ARCHITECTURE.md` section 6, test 20)
- [ ] Open decisions D1 to D4 reviewed (`DECISIONS.md`)
Tasks: repository, `pyproject.toml`, ruff, pytest, pre-commit, CI workflow, settings, logging, Alembic, models, migration `001_initial_schema`, seed reference tables from CSV.
Delivered: migrations 001 and 002 (explicit SQL), `reporting` schema and least-privilege roles (`sie db-roles`), `sie migrate`, `sie seed-reference`, tests on real PostgreSQL, CI workflow, Docker Compose database.
Acceptance: `alembic upgrade head` creates the schema; `pytest` and `ruff` pass; `sie seed-reference` loads countries and aliases (sports and disciplines are confirmed in Phase 0).

## Phase 2: Manual import and loader  [M]  (status: done for placings, 2026-10-05: acceptance demonstrated by `tests/integration/test_ingest.py`; the statement below that conflict policy and analytics read path are not built is historical and superseded by later merged work (see `DATA_PIPELINE.md` and `ANALYTICS_SPEC.md`))
Tasks: define `ParsedResult`, normaliser, validator with quarantine, idempotent loader, `sie import-csv`, `ingest_runs` logging.
Acceptance: the golden CSV loads; the same file loaded twice changes nothing; bad rows appear in quarantine with reasons; a reallocation in the golden data produces the closed and new placing versions plus one history row.
Why first: it lets analytics and the dashboard be built even before automation of collection works.

## Phase 3: Source adapter(s)  [M to L]  (historical sports-pipeline phase status: the portal fetcher and conflict policy were implemented later; verify production reconciliation status in `ACCEPTANCE.md` before relying on this phase label)
Tasks: `SourceAdapter` and `SourceParser` protocols, raw store with version semantics, rate limiter with cache, adapter (fetch only) for the chosen primary source, separate parser tested on fixtures, change detection by hash, conflict policy engine and `sie resolve-conflict`, official medal table adapter and parser, reconciliation.
Acceptance: one command ingests a full day of results; reconciliation report produced; parser failure on a changed fixture produces a clear error.

## Phase 4: Analytics engine  [M]  (status: built 2026-10-08: metrics, invariants, snapshots and change detection, insights, `sie analyze`; see `ANALYTICS_SPEC.md` section 14 and ADR-033. Acceptance: hand-computed golden country table and invariants are tested; `sie analyze` ran against the production database (manual run 38025265668, success) and `sie acceptance` passed 16 of 16 on 2026-10-11 (owner-reported))
Tasks: `v_medal_facts`, metric functions per `ANALYTICS_SPEC.md`, invariants tests, snapshots, change detection, exports (CSV, Excel), rule-based insights.
Acceptance: golden dataset matches expected outputs exactly; invariants pass; `sie analyze` writes all tables listed in the spec.

## Phase 5: Dashboard  [M]
Tasks: pages listed in `ARCHITECTURE.md`, filters (country, sport, gender, medal, date), completeness and reconciliation banners, export buttons.
Acceptance: each page renders against the test database and the real database; a viewer can answer "which sports power country X and how do its women compare" within a minute.

## Phase 6: Automation and operations  [S to M]  (status: foundations built, 2026-10-06: failure categories and retry contract, run summary and outcome, source freshness, `sie run-summary` and `sie source-status`; see `DATA_PIPELINE.md` section 12. 6A built 2026-10-06: scheduler command and workflow, retry executor, advisory lock, stuck-run recovery. 6B built 2026-10-06: `sie health`, alert contract, notifier abstraction. 6C/6D built 2026-10-07: `sie backup` with restore test, `sie publish`, webhook delivery with de-duplication, first-failure alert. The portal fetcher and daily refresh built 2026-10-07 after the owner cleared D1 (ADR-031); running daily since 2026-10-08; encrypted backup with restore test (ADR-034), alert lifecycle test (ADR-035), production acceptance command (ADR-037) and seed diagnostics (ADR-038) added 2026-10-10 and 11. Remaining: webhook delivery drill, key-based restore check, durable alert state)
Tasks: scheduler on the chosen runner (ADR-016), single-writer lock (Postgres advisory lock), failure and staleness notifications, source health check, nightly backup with a tested restore, `sie publish` export bundle, run history page.
Acceptance: three consecutive unattended scheduled runs succeed; a deliberately broken source triggers an alert.

## Phase 7: Expansion  [S to M each, none required for v1]
Direction and constraints: `PRODUCT_VISION.md`, ADR-024. Each item below has its own acceptance and is built separately.
- **Second competition (the multi-competition proof)**. Acceptance: a small fixture competition is registered; its data enters the same canonical model; the same analytics functions run unchanged; source parsing stays in the adapter/parser; Asian Games 2026 outputs are unchanged (tests prove all five).
- **Cross-competition analytics** (after the proof). Acceptance: a comparison (for example a country across two editions) uses canonical IDs only and states when editions are not comparable.
- **LLM explanation with number-lock verification**. Acceptance: every number in generated text matches a verified result; insufficient evidence yields an explicit refusal, never a guess.
- **Hindi and English report generation.**
- **Power BI template reading the exports.**
- **Athlete-level data and entries** (enables true medal efficiency), if a source provides it.

## After Phase 7: Research query layer  (planned, not started)
Question to intent to required data to filters to analytical operations to evidence to answer, over the canonical analytics. Starts with a constrained set of question types and a deterministic, testable query plan; never an unrestricted agent. Entry condition: reliable multi-competition data and Phase 4 analytics. Its design contract is in `PRODUCT_VISION.md` sections 5 to 7.

## Risks and mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| No machine-readable event-level source | Blocks automation | Manual CSV path from Phase 2; semi-automatic helpers |
| Source forbids automated access | Legal and ethical issue | Respect it; manual import |
| Site structure changes mid-competition | Stale data | Health checks, fail-loud parsers, fixtures, fast fix procedure |
| Name inconsistency across sources | Wrong aggregation | Reference tables, quarantine, no auto-creation |
| Partial data misread as final | Wrong conclusions | Completeness indicators, small-sample flags |
| Medal reallocations | Counts drift | History table, reconciliation on every run |
| Scope growth | Never finishes | Phase gates, non-goals list, decision records |
| An authoritative source is stale | Wrong value kept or a good correction ignored | Freshness-aware conflict policy, `disputed` flag, immediate re-fetch |
| Free-tier limits | Automation stops | Local scheduler fallback |

## Change management
Every change request follows: requirement, impact analysis (DB, pipeline, analytics, dashboard, tests, docs), design, tests, implementation, review, docs, commit. Major decisions are logged in `DECISIONS.md`. Version tags: `v0.1` after Phase 2, `v0.2` after Phase 4, `v1.0` after Phase 6.

## General research engine (ADR-039, ADR-041)
Phase 1 (answer contract and number lock, `sie.answer`) done 2026-10-11. Next, each with its own proposal: 2 evidence ledger and uploads in `raw_documents` (migration), 3 project-scoped freshness and health, 4 web research with citations, 5 router and model layer behind the number lock, 6 conversation backend, authentication and quotas. Order and acceptance criteria: the architecture proposal of 2026-10-11 and `GENERALISATION_AUDIT.md` section 8. `sie.datasets` (file analytics) is developed separately on `feature/datasets-library`.

Phase 2 (evidence ledger and uploads, ADR-042) built on `feature/evidence-ledger`; production migration is gated on the owner's backup and approval (`DEPLOYMENT.md` section 10).

Update 2026-10-15: `sie.datasets` (file analytics, ADR-040) merged as PR #29. Phase 1 (#27) merged. Phase 2 is PR #28 (open, owner review). Planned next, each with its own design approval and PR, built one at a time:
- Phase 3 project-scoped freshness and health: design `PHASE3_PROJECT_HEALTH_DESIGN.md` (awaiting approval).
- Follow-up after #28 merges: XLSX claim verification through `sie.datasets` (replaces `xlsx_verification_pending_datasets_loader`).
- Phase 4 web research with citations (fetcher under AGENTS.md rule 9), Phase 5 router and model layer behind the number lock (D4 still open), Phase 6 conversation backend, authentication and quotas.
- Phase 7 (second competition, cross-competition analytics, Hindi/English reports, Power BI template, athlete-level data) stays optional.

## Update: general research engine, free-source-first (2026-10-23)

- **Phase 2 / PR #28:** merged to `main` at `4008ab1` (owner-reported); the main CI was green at that point. Migration 005 still needs the migration step from the next scheduled refresh to be confirmed successful; do not assume production has applied it until that run is inspected.
- **Phase 4a/4b:** safe fetching and deterministic lead ranking/discovery are implemented on `feature/phase-4-web-research` at `7106c75` (owner-reported CI green on that commit). The free-source policy/design updates are additional documentation commits after that SHA, so checks for the current branch head must be re-verified. Nothing in Phase 4 is merged.
- **Source-cost rule (ADR-045):** start with free public catalogues, government/official sources, documented open APIs and eligible free search options. Do not add a paid search service, billable model, paid dependency or automatic paid fallback without a separate explicit owner decision. Quota exhaustion must yield a clear limitation and fallback to cache, another eligible free source or manual URL/file import.
- **Next implementation order:** (1) verify the current Phase 4 branch CI and open review PRs only after the owner asks; (2) controlled manual live-fetch test on the owner's machine; (3) Phase 4c: parse accessible HTML tables, CSV and JSON into the existing `sie.datasets` pipeline with row-level provenance and validation; (4) deterministic analytics/charts/reports and same-dataset follow-up reuse; (5) free discovery adapters after verifying their current terms and quotas. No migration, paid provider or new dependency is presumed.
- **Not done:** no concrete live search provider, no 4c collection pipeline, no follow-up analytics over fetched datasets, no live network test, and no Phase 4 PR opened as part of this documentation update. Phase 3 PR also remains uncreated according to the latest owner-provided status.

## Update 2026-10-24: Phase 4c built on a simulated network (ADR-046)
- **Built:** free catalogue adapters (data.gov.uk, OpenAlex keyless), cache, cost guard, collection of CSV/JSON/HTML tables with row-level provenance, content-keyed dataset store with follow-up reuse, deterministic analysis, number-locked report with coverage gaps, SVG chart, `sie research discover|collect|ask|datasets`.
- **Not done:** controlled live fetch by the owner; linking collected datasets to the evidence ledger (no migration was added); other free catalogues pending a terms check (see `FREE_SOURCE_CATALOGUE.md`); metric definitions for questions like party wins; Phase 5 (router and model layer, D4 still open) and Phase 6 (conversation backend, authentication, quotas). Phase 7 stays optional.
- **PRs:** none opened; nothing merged. Phase 3 is contained in this branch's history.



## Update 2026-10-28: Phase 4 catalogue safety and acceptance status (ADR-047)

- The production catalogue registry now distinguishes active and disabled providers. The data.gov.uk CKAN route is disabled because its redirect target's robots.txt disallows `/api/`; this is not bypassed. OpenAlex remains active for scholarly references only.
- The existing safe fetcher honors published Crawl-delay (minimum 2 seconds between requests, with the current 60-second upper safety cap); explicit tests cover a 10-second delay and a delay above the cap.
- Test fixtures re-enable data.gov.uk only in an isolated temporary registry for simulated CKAN ranking/redirect/cost-guard tests. Production config remains closed.
- Remaining acceptance gates: identify and verify at least one permitted free structured-data catalogue; run live discover -> fetch -> collect -> save -> analyse -> report/reuse; confirm save/reuse on the target Windows machine. PR #31 is not merge-ready until these pass.
