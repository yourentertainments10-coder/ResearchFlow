# Changelog

All notable changes to this project and its documentation. Format follows Keep a Changelog. Versions of the planning pack use `docs-x.y.z` until code exists.

## [Unreleased]
- Phase 0 (source discovery) mostly done: `docs/SOURCE_DISCOVERY.md` sections 8 and 9. The official results portal is a JavaScript app; its API (`back.results.asiangames2026.org/s/AG2026/en/...`) was found from the owner's network capture. Responses are zlib-compressed JSON mislabelled as `application/json` (no key involved). Still open: the portal's terms of use and event-level samples.
- `src/sie/sources/bornan/decode.py` (pure decoder, size-capped, fails loudly on unknown shapes) with tests on a real capture stored in `tests/fixtures/sources/bornan/`. 126 tests, ruff clean.

## [0.1.0] - 2026-10-04
Phase 1 complete: project skeleton and database. Documentation moves to docs-0.3.0.

### Added
- Code: `src/sie` (settings, logging, `sie` CLI with `migrate`, `seed-reference`, `db-roles`, `version`), Alembic with explicit-SQL migrations 001 (core schema) and 002 (competition scope on runs and snapshots, one daily snapshot per competition-local day, `reporting` schema).
- Least-privilege roles `sie_owner`, `sie_pipeline`, `sie_reader`; the reader can only `SELECT` from `reporting`.
- Reference data CSVs and a seed loader with validation.
- Tests on real PostgreSQL (embedded `pgserver` locally, a service container in CI): migrations, schema rules (placings, reallocation, uniqueness), roles and grants, seeding, normalisation, configuration, architecture import rules. 114 tests, ruff clean.
- `docker-compose.yml` (local Postgres), CI workflow, pre-commit config, `.env.example`.
- `docs/SCALABILITY.md` and ADR-015 to ADR-017.

### Changed
- PostgreSQL from day one. ADR-002 ("SQLite first") is superseded by ADR-015. SQLite and MongoDB are not used, including tests.
- Hosting decision (ADR-016): Neon free Postgres, GitHub Actions scheduler, Render or Streamlit Cloud for the dashboard, Cloudflare for DNS, CDN and WAF. Render's free Postgres (deleted after 30 days) and Cloudflare Workers (10 ms CPU) are explicitly not used for the database or the pipeline. `docs/DEPLOYMENT.md` rewritten.
- ADR-014's data-location decision is superseded by ADR-016.
- `docs/ARCHITECTURE.md`: removed a duplicated "Scaling path" section and the stale "SQLite to PostgreSQL" line.
- `docs/DATABASE.md` section 14 now describes the real test setup (template database copied per test).

### Fixed
- Test `test_reader_cannot_write_anything` asserted the wrong error for an insert into an identity column of a view (rejected by the rewriter before privileges are checked). It now uses a statement that reaches the privilege check and also asserts the reader's privileges through `has_table_privilege`.

## [docs-0.2.0] - 2026-10-02
Design review round. No code yet.

### Added
- `docs/DOMAIN_MODEL.md`: formal definitions of event, medal placing, entrant and country medal, with counting rules and testable invariants.
- `CHANGELOG.md`.
- Design-freeze gate before migration 001 (`docs/ROADMAP.md`, Phase 1).
- Source observations and a freshness-aware conflict policy (`docs/DATA_PIPELINE.md` section 7).
- Raw version semantics: same URL and same hash, same URL and new hash, reverted content (`docs/DATA_PIPELINE.md` section 4).
- Snapshot identity rules (`docs/DATABASE.md` section 8).
- ADR-009 to ADR-014 in `docs/DECISIONS.md`.

### Changed
- Source adapters now only fetch. Parsing moved to separate per-source parsers (Option A). `ARCHITECTURE.md` section 6 rewritten.
- Table `medals` replaced by `placings` with version rows and a partial unique index on current rows, so reallocation keeps its audit trail without violating uniqueness.
- `raw_documents` split into `raw_documents`, `raw_versions` and `raw_fetches`.
- "Higher-priority source wins automatically" replaced by the policy table. Priority is now a tie-break input only.
- Production data no longer lives in Git. Git carries code, reference data and a published export bundle only (`docs/DEPLOYMENT.md`, ADR-014).
- Validation rules made tie-aware.
- Test plan extended with reallocation, raw versioning, conflict, snapshot and architecture-rule tests.

## [docs-0.1.0] - 2026-10-01
Initial planning pack: README, AGENTS.md, product requirements, architecture, data pipeline, database, analytics specification, testing, security, deployment, roadmap, AI prompts, decisions.
