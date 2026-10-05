# Changelog

All notable changes to this project and its documentation. Format follows Keep a Changelog. Versions of the planning pack use `docs-x.y.z` until code exists.

## Dashboard usability pass
- Timeline: picking a 6th country replaces the oldest; any country can be added.
- Gender: neutral title; per-country view has Men/Women/Mixed(/Open) toggle.
- Searchable dropdowns show a chevron; compare bars use a fixed-width track; table filters on Countries and Sports; type scale and radii consolidated; one button and selected style; info icons no longer add a stray "i" to headings.

## Dashboard P0-P3 review fixes
- Trend lines end at the table totals (341/150): 3 undated medals shown as a labelled last point.
- Open vs Mixed toggle (4 gender buckets), corrected Open-events wording.
- Methodology: source link, 3-step pipeline, per-sport validation table (59/59).
- New sports leaderboard, all-country overview, gender page, richer timeline (gold toggle, rank over time, peak days), explorer filters, searchable dropdowns, route aliases, not-found state, nav/KPI/disclaimer cleanup.

## [Unreleased]
- One canonical ingestion path (ADR-021): `pipeline/raw.py` (versioned raw store, `fs` or `db`), `pipeline/load.py`, `pipeline/runner.py`; `sie load-capture` and the new `sie import-csv` share it. `sie/load.py` removed. Unknown country, sport or gender now quarantine the row instead of failing the load; `ingest_runs` records source, counts and errors. Migration 003 (ADR-022) adds `raw_blobs`, `storage_backend`, `placings.source_note`, `ingest_runs.source`. Golden CSVs in `tests/fixtures/manual/`. `COMPETITION` is now the `COMPETITION_ID` setting. ADR-020 (sport grouping) and ADR-023 (static dashboard) recorded.
- Open events are now a separate gender category in the event-level reports (`gender_tables`) and the dashboard instead of being folded into Mixed (ADR-019). Reconciliation still folds Open into the official Mixed bucket. The committed `site/index.html` and `reports/` were not regenerated in this change.
- Phase 0 (source discovery) done except the portal's terms of use. The owner's event-level capture confirmed 59 disciplines and 469 events (Men 221, Women 207, Mixed 21, Open 20), one medal row per placing with ties as `Order` 2 and teams as one row per country, and an exact match between medal rows and the portal's standings for Swimming and Archery. Findings, the personal-data rule (birth dates are never stored) and the D1 decision are in `docs/SOURCE_DISCOVERY.md` section 10. Trimmed fixtures added under `tests/fixtures/sources/bornan/`.
- Fixed CI on `main`: `ruff format --check` failed because current ruff also formats Python code blocks in Markdown and the example in `docs/ARCHITECTURE.md` was not formatted. Whitespace only.
- Phase 0 (source discovery) mostly done: `docs/SOURCE_DISCOVERY.md` sections 8 and 9. The official results portal is a JavaScript app; its API (`back.results.asiangames2026.org/s/AG2026/en/...`) was found from the owner's network capture. Responses are zlib-compressed JSON mislabelled as `application/json` (no key involved). Still open: the portal's terms of use and event-level samples.
- `src/sie/sources/bornan/decode.py` (pure decoder, size-capped, fails loudly on unknown shapes) with tests on a real capture stored in `tests/fixtures/sources/bornan/`. 126 tests, ruff clean.

- `src/sie/analytics/` : country x medal x gender analytics (shares, HHI concentration, gender split, 3-2-1 points) from the official standings feed, validated against the feed's own totals; Excel and HTML report in `reports/`. 130 tests.
- Event-level pipeline pieces: `parse_medals.py` (drops personal data, event gender from event code), `analytics/events.py` (reconciliation, country x sport, concentration, specialisation LQ), `analytics/full_report.py` (HTML, Excel, `placings.csv`). Full capture of 59 disciplines reconciles with the official table: 1568 medals, 469 events, 0 mismatches. See `docs/SOURCE_DISCOVERY.md` section 11.
- Phase 2 loader `src/sie/load.py` and `sie load-capture`: idempotent load into PostgreSQL through `apply_placing` (raw version by sha256, reallocation on change, unknown countries refused). The real capture loads to 469 events / 1568 placings, rerun writes nothing, and `v_medal_facts` equals the official table for all 40 countries.
- Static dashboard `src/sie/dashboard.py` -> `site/index.html`: filters by gender, sport and country; four charts (stacked medals by country, men/women/mixed split, country x sport heatmap, cumulative medals by day) with tooltips, light and dark mode, table view; palette checked with the dataviz validator. `reports/placings.csv` now carries the medal date. Hosts free on Cloudflare Pages or GitHub Pages.
- `docs/SOURCE_DISCOVERY.md` section 12: no terms of use exist on the portal; conservative rules recorded.
- Dashboard redesign (presentation layer only; data and analytics untouched): hero with snapshot badge and validation card, navigation (Overview, Countries, Sports, Gender, Timeline, Data Explorer, Methodology), country explorer with a deterministic "Why this sport matters" panel, country comparison, sport and gender explorers, data explorer with CSV and Excel download, empty and error states, dark mode toggle, mobile layout. Template in `src/sie/web/dashboard.html`. Found and fixed a crash in 6 of 7 pages in the first draft; `tests/unit/test_dashboard_pages.py` opens every page in a real browser (skipped where Chromium is missing).
- Fixed CI: `pandas` and `openpyxl` were used but not declared, so a clean install failed at test collection (exit code 2). They are now dependencies; `playwright` is an optional `browser` extra; the dashboard template ships as package data. Verified in a fresh virtualenv with the exact CI commands: 141 passed, 1 skipped.

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
