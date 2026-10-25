# Changelog

## Fixes from the first live run (Phase 4)
- A page or API redirect may now go to the `www.` variant or a sub-domain of the same host (data.gov.uk's API answered with a redirect to another host and was refused); the new host's robots.txt, interval and address checks apply, any other host is still refused and the refusal names it.
- Windows: saving a dataset crashed in `os.fsync` (EBADF on a read-only descriptor, directories cannot be opened). Files are now opened read-write there and directory sync is skipped; the rename that publishes the dataset is still atomic.
- Live smoke test is still not passed: see PR #31.

## Live discovery review fixes (ADR-046 amendment, 2026-10-25)
- Ranking judges topic, place and year separately: a US or 2011 dataset no longer answers a "UK 2024" question (rejected with `mismatch`); weak topic overlap is `low_relevance`; unstated place or year is a disclosed limitation. Academic papers are references (`role: reference`), never collected, ranked below structured datasets.
- robots.txt may redirect up to 5 times with every hop validated (https, public address, port, pacing, loop check); anything unsafe or 403/5xx fails closed.
- Discovery output says `robots_status: not_checked` until permission was looked up; leads are never called verified.
- Lead limitations become UNVERIFIED gaps in the report. New tests: robots redirects, end-to-end regression (simulated network).
- Not run: a live-source smoke test (the build environment cannot reach public sites).

## Phase 4 review fixes (ADR-046 amendment)
- One dataset store: `sie research` now saves through `sie.evidence.dataset_store` (project label via `--project`; only normalised table + manifest; rows only where an open licence or recorded policy permits; expires after `EVIDENCE_DATASET_RETENTION_DAYS`, default 90). Replaces the earlier content-keyed store that kept source files without scope, licence or expiry.
- The fetch cache is short-lived (`RESEARCH_CACHE_RETENTION_DAYS`, default 7) and keeps bodies only for catalogue API answers and hosts with a recorded policy; `sie research prune` removes expired data and cache files.
- Reuse checks freshness, project, question coverage and the columns the operation needs; stale data is a disclosed last resort; otherwise it asks for a URL or file.
- `sie research ask` compares shared columns across two or more sources (or `--conflict-key/--conflict-values`) and the report lists all conflicting values (previously only two per key), states what was compared, or says agreement could not be checked.
- Catalogue licence id travels lead -> manifest -> report; missing licence is a disclosed gap. Data.gov.uk request pinned to the documented `package_search` GET; live behaviour still unverified.
- Still unfinished: metric definitions, natural-language query planning, live test.

## Free discovery, collection, cache and report (ADR-046, Phase 4c)
- `sie.research` and `sie research discover|collect|ask|datasets`: registry of free keyless catalogues (data.gov.uk CKAN, OpenAlex), file cache, CSV/JSON/HTML table collection through `sie.datasets` (HTML parsing reuses `sie.evidence.collect`) with row-level provenance and flagged missing, rejected and conflicting values, a content-keyed dataset store reused for follow-ups, deterministic analysis, a number-locked answer with disclosed gaps, SVG chart, markdown report.
- Cost guard: only `free_no_key` catalogues load; quota exhaustion, timeouts and failures are reported and skipped, never replaced by a paid or unregistered endpoint (tests, simulated network).
- Fix in `sie.datasets`: group and top-N operations no longer emit a false "no non-null values" warning from their validation probe.
- Not verified live: no real catalogue was contacted from the build environment. Not built: catalog.data.gov, open.canada.ca, data.gov.in, Wikimedia, Common Crawl, a general web-search provider, and linking collected datasets to the evidence ledger.

## Free-source-first research policy (ADR-045)
- General research must start with free public catalogues, official/government data sources, documented open APIs and eligible free search options. No paid search provider, billable model or automatic paid fallback is allowed without a separate explicit owner decision.
- When free quotas are exhausted or coverage is unavailable, report the limitation and use cached evidence, another eligible free source or manual URL/file import.
- Phase 4 design clarifies public-host fetching: no fixed host allow-list, but generic network safety checks, robots.txt, source terms/licences, rate limits and access controls still apply. The candidate catalogue is documented in docs/FREE_SOURCE_CATALOGUE.md. Documentation decision only; no provider adapter or Phase 4c code was added in this change.

All notable changes to this project and its documentation. Format follows Keep a Changelog. Versions of the planning pack use `docs-x.y.z` until code exists.

## Project-scoped freshness and health (ADR-043)
- `sie.evidence.health` and `sie project health <slug> [--json]` (read only, no migration): source states `current`, `stale`, `expired`, `pruned`, `upload`; informational `old_upload`; claim counts by status, claims on pruned or expired sources, pending XLSX claims, open conflicts; `gaps_from_health` for `sie.answer`. New settings `EVIDENCE_WEB_MAX_AGE_DAYS` (30) and `EVIDENCE_OLD_UPLOAD_DAYS` (180). Empty project gives an explicit no-sources report.

## Collection into tables with provenance (ADR-044, Phase 4c, first part)
- `sie.evidence.collect`: CSV and JSON go through `sie.datasets` unchanged; HTML tables are read with the standard library and refused, with a stated reason, when they have merged cells, nested tables, no header row, duplicate or empty headers, ragged rows or no data. A page with several usable tables must be told which one (no guessing); empty cells stay missing. `row_provenance` gives URL, retrieval time, page hash, table and row id for any row. No dependency, no migration, no storage yet.

## Web research: safe fetching and source discovery (ADR-044, Phase 4a and 4b)
- `sie.evidence.web` (SSRF guard, robots.txt, 2 s interval, stop on 403/429/5xx, one same-host redirect, type and size caps) and `sie.evidence.discovery` (authority-tier ranking with stated reasons, robots pre-check, manual leads-file provider). New commands `sie evidence fetch` (dry run unless `--execute`) and `sie evidence discover`. No migration, no real free-search/catalogue adapter yet, no automation. `PHASE4_WEB_RESEARCH_DESIGN.md`.

## Evidence ledger and upload storage (ADR-042)
- Migration 005 (additive) and package `sie.evidence`: per-project uploads in the raw store, claims with source locators, append-only reviews (`source_located` and `human_confirmed` never mean "true"), conflicts, adapters to `sie.answer`, retention modes (web default excerpt-only), owner-only dry-run purge and prune with tombstones, integrity check, `sie project` and `sie evidence` commands. XLSX claims stay unverified until the datasets loader exists. Backups and the provider history are not erased by purge; the Neon window is unverified. `EVIDENCE_LEDGER_DESIGN.md`, `EVIDENCE_RETENTION.md`.


## Generalisation audit (proposal only)
- `docs/GENERALISATION_AUDIT.md`, ADR-039 (accepted by the owner 2026-10-11; supersedes ADR-024 points 1 and 3) and `docs/DATASET_ANALYTICS_DESIGN.md` (design and test plan for step 2, revision 2 with determinism, operation, cleaning, parsing, safety, atomic-publish and type rules and a library-first interface; awaiting approval): what is generic, what is sports-specific and what is missing for a general research and analytics engine; cost categories; a suggested order. Documentation only; ADR-024 stays in force until the owner decides.

## Answer contract and number lock (ADR-041)
- New library `sie.answer`: layered statements, exact figures with their source, evidence and gap records, refusals, a number lock (digits, number words, ordinals and other numeric characters are rejected unless declared), layer and disclosure rules, fail-closed deterministic rendering. 41 tests. No command, table, dependency or change to existing code. `docs/ANSWER_CONTRACT_DESIGN.md`.
- `GENERALISATION_AUDIT.md` corrected: `ops/publish.py` is sports-specific (only the backup code is domain-neutral).

## Status docs brought up to date (2026-10-11)
- `README.md` status table, `docs/ROADMAP.md` phase notes and the `ACCEPTANCE.md` ledger now record: production acceptance 16/16 (owner-reported), encrypted backup runs succeeding, daily refresh running, and what is still open. No code change.

## Seed reference: bounded waits and progress (ADR-038)
- `sie seed-reference` gets `--lock-timeout`, `--statement-timeout`, `--heartbeat-seconds`, exits 3 on a timeout with the blocking sessions listed; new read-only `sie db-activity`. `db/diagnostics.py`. No change to what the seed writes.

## Official event total loaded by the seed
- `data/reference/competitions.csv` gains `official_event_total` (the portal's event list, 469) and `seed_reference` writes it, so `competitions.official_event_total` is no longer NULL. Fixes the `sie acceptance` check "official event total on the competition" (observed null). The load already refused more events than this total. No migration (the column exists since 001); production is repaired by the next `refresh` run, which seeds first.

## `sie acceptance` (ADR-037)
- Read-only database check against 469 events / 1,568 placings (470/469/629), freshness, open conflicts and snapshots; exit 1 on any failed check. Ported from PR #15 onto the encrypted-backup branch; the rest of PR #15 (its own `pyrage` backup encryption, `backup-keygen`, its workflow, `scripts/smoke_dashboard.py`) is superseded by PR #16 (age binary, scratch restore) and PR #19 (browser smoke test, usable against a live URL).

## Cloudflare preview builds
- `wrangler.jsonc` gets an empty `"previews": {}` block: `npx wrangler preview` (Workers Builds, non-production branches) refused to run without it. Production deploy settings are unchanged. Test: `tests/test_wrangler_config.py`.

## Alert lifecycle test (ADR-035)
- `refresh.yml`: the health check and alert-state steps now run after a failed refresh, so an outage sends the alert. New `tests/integration/test_alert_lifecycle.py` (outage, single delivery, persistence across processes, failed-channel retry, interrupted run, recovery, relapse, corrupt state) and `tests/unit/test_refresh_workflow.py`. Operations: `DEPLOYMENT.md` section 7a. No application code change.

## Dashboard browser smoke test (ADR-036)
- `tests/dashboard_smoke.py` (reusable, also against a URL) and `tests/unit/test_dashboard_smoke.py`: all routes at desktop and mobile, light and dark, console and network errors, blank-page and overflow detection, figures checked against the data, nine negative cases including the PR #14 syntax error. CI now installs Chromium and sets `REQUIRE_BROWSER=1`. Documentation in `TESTING.md` section 9. No dashboard code change.

## Backup encryption (ADR-034)
- Security fix: `backup.yml` no longer uploads a plaintext dump. `sie backup` encrypts with age (`--encrypt-to`, `--require-encryption`), `sie restore-test --identity-file --scratch-url` decrypts and verifies in a scratch database, the workflow fails closed without `BACKUP_AGE_RECIPIENT` / `BACKUP_AGE_IDENTITY`, restores into a scratch service container instead of production, and guards the artifact contents. Fixed `--out` vs `--out-dir` (backup runs on 8-10 Oct failed and uploaded nothing). New settings `BACKUP_AGE_RECIPIENT`, `RESTORE_TEST_DATABASE_URL`. Exposure audit: no plaintext dump was ever published.

## Acceptance evidence ledger
- New `ACCEPTANCE.md`: statuses (PASS, FAIL, NOT RUN, BLOCKED) with GitHub Actions run IDs, commits and times for the official analysis, the three scheduled refreshes (8, 9, 10 Oct, event `schedule`), alert and dashboard tests, Cloudflare builds and backups. Records the failed backup runs and that no unencrypted dump was ever uploaded. Documentation only; no code change.

## Portal fetcher and daily refresh
- `sie scheduled-run portal`, `sie fetch-portal --out FILE`, `sources/bornan/fetch.py`; `refresh.yml` now runs daily and then `sie health`. Guards: `PORTAL_FETCH_ENABLED`, honest User-Agent, 2 s gap, fixed host, no redirects, size cap, all or nothing. New settings `PORTAL_FETCH_ENABLED`, `HTTP_TIMEOUT_SECONDS`. ADR-031. Tested with a fake portal; not yet run against the live portal.

## analyze workflow
- `.github/workflows/analyze.yml` (manual): `sie analyze --official` on the production database, uploads the tables, fails on a reconciliation mismatch.

## Phase 4 analytics engine
- `sie analyze` writes the twelve tables of the spec (plus discipline, women-only, insights and rank trajectory tables), a workbook and a manifest with completeness and reconciliation status. Pure metrics (`analytics/metrics.py`), invariants, rule-based insights, snapshots and change detection (no migration). ADR-033, `ANALYTICS_SPEC.md` section 14.
- Cleanup: statements that `site/` and `reports/` were not regenerated corrected (ADR-023, changelog, README); the Methodology validation table is headed "Discipline" (it lists the source's 59 disciplines), in the template and in `site/index.html`.

## Phase 3 conflict policy engine
- Observations recorded for every loaded placing; freshness-aware policy (`pipeline/conflicts.py`) holds or applies disagreeing claims; `events.is_disputed` derived from open conflicts; `sie conflicts` and `sie resolve-conflict`. No migration. ADR-032.

## Phase 6C/6D backup, publish, delivery
- `sie backup` / `sie restore-test` (snapshot-consistent dump, scratch restore, hash and count checks), `sie publish` export bundle, `WebhookNotifier` for `NOTIFY_WEBHOOK_URL`, file-based alert de-duplication, `needs_attention` alert on a first parse, validation or raw-store failure, `backup.yml` workflow. New settings `PG_BIN_DIR`, `ALERT_STATE_PATH`, `ALERT_RENOTIFY_MINUTES`. ADR-030. No schema, analytics, report or dashboard changes. Portal fetcher and cron still blocked on D1.

## Phase 6B source health and alerts
- `sie health`: per-source health (fresh, stale, failing, never_succeeded) with last success and failure, consecutive failures, stuck runs, fingerprint and raw artifact references.
- Alert contract (never_succeeded, repeated_failures, stale, stuck_runs) using existing thresholds; notifier abstraction with a log channel; new setting `SCHEDULED_SOURCES`. ADR-029. No schema, analytics, report or dashboard changes.

## Phase 6A scheduler, retries, locking
- `sie scheduled-run` and `sie recover-stuck`; per-source PostgreSQL advisory lock; fetch retry (3 attempts, backoff) and one load retry, applied from the existing retry rules; no automatic retry for parse, validation or raw-store failures; stuck runs closed as failed.
- `refresh.yml` workflow (manual dispatch only until the portal terms are cleared). ADR-028. No schema, analytics, report or dashboard changes.

## Phase 6 operational foundation
- Failure categories (fetch, raw store, parse, validation, load) with a retry contract; failed runs store `[category] detail`.
- `record_fetch_failure` records unreachable sources without touching raw versions; the run is now committed before the raw store step so even a raw-store failure leaves a closed, failed run.
- Deterministic run outcome and structured run summary; source freshness with fingerprint and raw artifact references; stuck-run detection; `sie run-summary` and `sie source-status`.
- No migration, no analytics, report or dashboard changes. ADR-027.

## Product direction (documentation only)
- Added `docs/PRODUCT_VISION.md` and ADR-024: competition-agnostic engine, extension contract, second-competition proof criteria, research query and evidence layers, constrained role of AI. Roadmap Phase 7 gains acceptance criteria; a research query layer follows it. Everything beyond Asian Games 2026 is marked planned. No code changes.

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
- Analytics decisions (ADR-025, ADR-026, ADR-019/020 applied): migration 004 adds `placings.result_date` and `placings.source_country` (also in `v_medal_facts` and `reporting.medal_facts`); the loader fills them; the medal timeline uses each medal's own date (53 multi-date events, 106 re-dated medals, 3 undated medals on an explicit `undated` row); `sport` in analytics is the 49 official sports with the 59 disciplines kept as `discipline`; Open stays separate, with `reconcile_official_table` folding it only for the official-table comparison; analytics use reference country names and keep the source's name. Tests: frozen discipline tables still match, 49-sport tables equal the frozen ones summed by the reference mapping, official country table reconciles at zero mismatches, timeline ends at 1568. `site/` and `reports/` were regenerated from the full capture on 2026-10-07 (PR #5).
- Event-level analytics read `reporting.medal_facts` (`src/sie/analytics/facts.py`); `full_report.load` takes the facts frame and uses the capture file only for the official per-discipline standings it reconciles against. Metric functions unchanged. Regression tests load the 1568 frozen placings through the real ingestion path and compare every metric table with the frozen verified report (`tests/fixtures/expected/`). Fixes found on the way: a tied bronze (Women's Pole Vault) would have been quarantined; the portal converter judged ties and team events per event code although codes repeat across disciplines. `site/` and `reports/` were regenerated from the full capture on 2026-10-07 (PR #5).
- One canonical ingestion path (ADR-021): `pipeline/raw.py` (versioned raw store, `fs` or `db`), `pipeline/load.py`, `pipeline/runner.py`; `sie load-capture` and the new `sie import-csv` share it. `sie/load.py` removed. Unknown country, sport or gender now quarantine the row instead of failing the load; `ingest_runs` records source, counts and errors. Migration 003 (ADR-022) adds `raw_blobs`, `storage_backend`, `placings.source_note`, `ingest_runs.source`. Golden CSVs in `tests/fixtures/manual/`. `COMPETITION` is now the `COMPETITION_ID` setting. ADR-020 (sport grouping) and ADR-023 (static dashboard) recorded.
- Open events are now a separate gender category in the event-level reports (`gender_tables`) and the dashboard instead of being folded into Mixed (ADR-019). Reconciliation still folds Open into the official Mixed bucket. The committed `site/index.html` and `reports/` were regenerated from the full capture on 2026-10-07 (PR #5).
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
