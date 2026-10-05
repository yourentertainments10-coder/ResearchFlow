# Roadmap

Work phase by phase. A phase is finished only when its acceptance criteria are met and evidence is saved. Sizes are relative effort (S, M, L), not promises.

## Phase 0: Source discovery (gate)  [M]  (status: done except the portal's terms of use, 2026-10-04; API found, decodable, and event-level data confirmed with 469 events, see `SOURCE_DISCOVERY.md` sections 8 to 10. The terms gate only automated fetching in Phase 3)
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

## Phase 2: Manual import and loader  [M]  (status: done for placings, 2026-10-05: acceptance demonstrated by `tests/integration/test_ingest.py`; entrants, conflict policy and the analytics read path are not built, see ADR-021)
Tasks: define `ParsedResult`, normaliser, validator with quarantine, idempotent loader, `sie import-csv`, `ingest_runs` logging.
Acceptance: the golden CSV loads; the same file loaded twice changes nothing; bad rows appear in quarantine with reasons; a reallocation in the golden data produces the closed and new placing versions plus one history row.
Why first: it lets analytics and the dashboard be built even before automation of collection works.

## Phase 3: Source adapter(s)  [M to L]
Tasks: `SourceAdapter` and `SourceParser` protocols, raw store with version semantics, rate limiter with cache, adapter (fetch only) for the chosen primary source, separate parser tested on fixtures, change detection by hash, conflict policy engine and `sie resolve-conflict`, official medal table adapter and parser, reconciliation.
Acceptance: one command ingests a full day of results; reconciliation report produced; parser failure on a changed fixture produces a clear error.

## Phase 4: Analytics engine  [M]
Tasks: `v_medal_facts`, metric functions per `ANALYTICS_SPEC.md`, invariants tests, snapshots, change detection, exports (CSV, Excel), rule-based insights.
Acceptance: golden dataset matches expected outputs exactly; invariants pass; `sie analyze` writes all tables listed in the spec.

## Phase 5: Dashboard  [M]
Tasks: pages listed in `ARCHITECTURE.md`, filters (country, sport, gender, medal, date), completeness and reconciliation banners, export buttons.
Acceptance: each page renders against the test database and the real database; a viewer can answer "which sports power country X and how do its women compare" within a minute.

## Phase 6: Automation and operations  [S to M]
Tasks: scheduler on the chosen runner (ADR-016), single-writer lock (Postgres advisory lock), failure and staleness notifications, source health check, nightly backup with a tested restore, `sie publish` export bundle, run history page.
Acceptance: three consecutive unattended scheduled runs succeed; a deliberately broken source triggers an alert.

## Phase 7: Optional extras  [S each]
- LLM rewriting of insights with number-lock verification
- Hindi and English report generation
- Power BI template reading the exports
- Second competition adapter (proves extensibility)
- Athlete-level data and entries (enables true medal efficiency)

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
