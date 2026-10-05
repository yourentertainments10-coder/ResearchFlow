# Architecture Decision Records

Format: Decision, Context, Options, Why, Consequences, Status. Add a new record for every significant change. Do not edit old records. Supersede them.

## ADR-001: Python as the single language
- **Context:** Scraping, data work, analytics and a dashboard are all needed by one developer.
- **Options:** Python; JavaScript/TypeScript full stack; mixed.
- **Why:** Best data ecosystem (pandas, SQLAlchemy), one language end to end.
- **Consequences:** If a public multi-user web app is ever needed, a separate front end may be added.
- **Status:** Accepted

## ADR-002: SQLite first, PostgreSQL later (superseded by ADR-015)
- **Context:** Single owner, a few thousand rows, free to run, easy to move around.
- **Options:** SQLite; PostgreSQL; MongoDB; DuckDB.
- **Why:** SQLite has zero setup and is enough for this volume. SQLAlchemy and Alembic keep the move to PostgreSQL to a configuration change. MongoDB is not suitable for relational, aggregated data. DuckDB is a good optional analytics engine over the same data but adds a second store.
- **Consequences:** Avoid SQLite-only SQL; concurrent writers are not supported (single scheduled writer by design).
- **Status:** Superseded by ADR-015 on 2026-10-04. Reason: the owner will deploy on a hosted platform and expects many future requirements, so starting on the production engine avoids a later migration.

## ADR-003: Deterministic analytics, LLM only explains
- **Context:** An LLM answering from web searches cannot be audited or reproduced.
- **Why:** Numbers must come from code over stored rows with source links. The LLM may rewrite already-computed text, with numbers verified afterwards.
- **Consequences:** More upfront work (collection, cleaning), but results are reproducible and trustworthy.
- **Status:** Accepted

## ADR-004: Source adapters behind one interface
- **Why:** Isolates site-specific code, lets sources be swapped when a site changes, and supports new competitions.
- **Consequences:** Adapters stay thin.
- **Status:** Accepted. The "fetch and parse" wording is superseded by ADR-009 (adapters only fetch).

## ADR-005: Immutable raw store plus rebuildable database
- **Why:** If parsing logic improves or a bug is found, the database can be rebuilt from raw data without re-fetching. Supports audits.
- **Consequences:** Storage grows; raw files are kept outside Git (except small fixtures).
- **Status:** Accepted

## ADR-006: Manual CSV import is a first-class path
- **Why:** Some sources may forbid automation or lack machine-readable data. The same validation applies, so quality is unchanged.
- **Status:** Accepted

## ADR-007: Streamlit for the dashboard
- **Options:** Streamlit; Power BI; FastAPI + React; static HTML.
- **Why:** Free hosting option, fast to build, Python only. Power BI remains an optional consumer of the exports.
- **Consequences:** Limited multi-user and design control. Revisit only if requirements grow.
- **Status:** Accepted

## ADR-008: No authentication in v1
- **Why:** Single owner, read-only dashboard, no personal data. Adding auth later requires a new record and the rules in `SECURITY.md` section 7.
- **Status:** Accepted

## ADR-009: Adapters fetch only; parsers are separate
- **Context:** Early docs said both "adapter = fetch only" and "adapter has parse()".
- **Options:** A) adapter fetches, a separate per-source parser parses. B) adapter does both.
- **Why:** A keeps parsing a pure function over saved raw bytes, so it is reproducible, testable on fixtures, and can be re-run after a bug fix without re-fetching.
- **Consequences:** Two protocols (`SourceAdapter`, `SourceParser`), an import-rule test, one more class per source.
- **Status:** Accepted

## ADR-010: Four distinct domain concepts
- **Context:** Analytics depend on not mixing events, podium slots, who stood on them and what is counted.
- **Decision:** Event, medal placing, entrant and country medal are formally defined in `DOMAIN_MODEL.md`. The table `medals` becomes `placings`; `entrants` is added; `v_medal_facts` is the country-medal view.
- **Consequences:** Counting rules and invariants I1 to I6 are tested. Gender belongs to the event, not the entrant.
- **Status:** Accepted

## ADR-011: Placings are versioned; uniqueness covers current rows only
- **Context:** A plain unique key on (event, medal, slot) breaks reallocation or forces overwriting history.
- **Decision:** Each correction closes the old placing version and inserts a new one in one transaction. A partial unique index applies to `is_current = 1` rows. `placing_history` is append-only.
- **Consequences:** Needs a database with partial indexes (PostgreSQL and SQLite both have them; this project uses PostgreSQL, ADR-015). The procedure is in `DATABASE.md` section 6 and is tested explicitly.
- **Status:** Accepted

## ADR-012: Raw documents are versioned by content hash
- **Decision:** `raw_documents` (logical), `raw_versions` (distinct content), `raw_fetches` (every attempt). Same hash means no new version; a new hash means a new version; a reverted hash points latest back to the old version.
- **Consequences:** Every fetch stays auditable without duplicating files.
- **Status:** Accepted

## ADR-013: Freshness-aware conflict resolution; priority is a tie-break only
- **Context:** "The higher-priority source wins automatically" can keep stale official data and ignore a newer correction.
- **Decision:** Source claims are stored as observations. The policy table in `DATA_PIPELINE.md` section 7 decides. A fresh official value is preferred; a stale one triggers a re-fetch and a `disputed` flag; competing non-official claims are held for review.
- **Consequences:** More tables (`source_observations`, `source_conflicts`) and a `sie resolve-conflict` command. Disputed events stay visible and flagged.
- **Status:** Accepted

## ADR-014: Where production data lives; snapshot identity
- **Decision (data):** The primary database and raw store live on durable disk controlled by the pipeline, backed up nightly to a second location. Git holds code, reference data, fixtures and a published export bundle only. GitHub Actions is used as the primary runner only if durable external state is available. This resolves D2.
- **Decision (snapshots):** Snapshot content identity is a data fingerprint. A `change` snapshot is written only on change; one `daily` snapshot per competition-timezone day; each records the analytics version.
- **Consequences:** The owner's machine or an always-on host is part of the operating model during the competition. The owner may override this by recording a new ADR.
- **Status:** Accepted. The data-location decision is superseded by ADR-016 (managed Postgres instead of the owner's disk); the snapshot decision stands.

## ADR-015: PostgreSQL from day one; MongoDB and SQLite not used
- **Context:** The owner will deploy on hosted platforms, wants it easy now and durable later, and expects many new requirements after v1. MongoDB or PostgreSQL were the two candidates.
- **Options:** PostgreSQL; MongoDB; SQLite then migrate.
- **Why:** The domain is relational (event, placing, entrant, country) and the key rule ("one current placing per slot", versioned history) needs a partial unique index, composite foreign keys and transactions, which PostgreSQL provides natively. Analytics are SQL aggregations. Raw payloads fit `JSONB`. Free and paid managed Postgres exists on every host, so moving between hosts is a `pg_dump` and a new `DATABASE_URL`. Data volume is tiny (about 2,000 placings per competition), so Postgres will not be the bottleneck. Starting on SQLite would mean a migration later for no gain. MongoDB would move integrity into application code.
- **Consequences:** Tests run on real PostgreSQL (`pgserver` locally, a service container in CI). Migrations are explicit SQL run by Alembic. PostgreSQL 16 is the target; migrations stay compatible with 15+. `SCALABILITY.md` records the comparison and growth stages.
- **Status:** Accepted (supersedes ADR-002)

## ADR-016: Hosting split; Render and Cloudflare are not the database
- **Context:** Render and Cloudflare were named as deployment targets. The Render, Neon and Cloudflare Workers limits below were re-read on 2026-10-04 from the providers' own pages; the remaining provider facts are in `DEPLOYMENT.md` with the date they were checked (2026-10-02).
- **Facts:** Render's free Postgres expires 30 days after creation (14-day grace, then deleted), 1 GB, no backups. Render free web services sleep after 15 minutes. Cloudflare Workers (free) allow 10 ms CPU per request, so a Python pandas pipeline does not fit; Cloudflare is for DNS, CDN, WAF and static front ends. Neon's free plan is permanent (1 GB per project, 100 compute-hours per month, suspends after 5 idle minutes; compute stops at the quota but data is not deleted; its restore history on the free plan is short, see `DEPLOYMENT.md`).
- **Decision:** Production database on Neon free Postgres (any Postgres works via `DATABASE_URL`). Scheduled pipeline on GitHub Actions cron (state is in Postgres). Dashboard on a Render free web service or Streamlit Community Cloud, connecting as the read-only role. Cloudflare in front for DNS, CDN and WAF. Mandatory nightly `pg_dump` to an off-host location; the provider's history window is not a backup. Raw bytes go to the database (`RAW_STORE_BACKEND=db`) until about 60 percent of the free size, then to S3-compatible storage.
- **Consequences:** Everything is free but limited; the smallest paid upgrade is a paid Postgres plan, with no code change. Free-tier terms change, so `DEPLOYMENT.md` records the date they were checked. Resolves D2.
- **Status:** Accepted (supersedes the data-location part of ADR-014)

## ADR-017: Everything is competition-scoped; least-privilege database roles
- **Context:** Many more requirements are expected, including more competitions, an API and users. Migration 002 closed two gaps in the first schema.
- **Decision:** Runs and snapshots carry `competition_id`; one `daily` snapshot per competition-local day is enforced by a unique index. A `reporting` schema exposes read-only views. Three roles: `sie_owner` (migrations), `sie_pipeline` (read and write `public`), `sie_reader` (`SELECT` on `reporting` only). The dashboard and any future API connect as `sie_reader`.
- **Consequences:** A dashboard bug or leaked dashboard credential cannot modify data or read operational tables. Role grants are tested against the catalogue (`has_table_privilege`) and by attempted writes. Adding a competition needs no schema change.
- **Status:** Accepted

## ADR-018: pandas and openpyxl as the optional `reports` extra
- **Context:** The analytics, report and dashboard modules (`src/sie/analytics/`, `src/sie/dashboard.py`) import `pandas`, and the xlsx reports use pandas' `openpyxl` writer. Neither was declared, so `pytest` failed at collection in CI (exit code 2) on every commit that added them. `AGENTS.md` rule 11 requires a justification for new dependencies.
- **Decision:** Declare both in a new optional extra `reports` in `pyproject.toml`; CI and developers install `.[dev,reports]`. The ingestion pipeline (`sie.db`, parsers, load) does not import them.
- **Why needed:** pandas is the aggregation and pivot tool already named in ADR-001 and `ARCHITECTURE.md`; openpyxl is the only writer pandas needs for `.xlsx`. The standard library has neither a dataframe nor an xlsx writer.
- **Maintenance and licence:** both are actively maintained; pandas is BSD-3-Clause, openpyxl is MIT. Lower bounds are the versions the tests were run with (pandas 3.0, openpyxl 3.1).
- **Alternatives:** write CSV only and drop xlsx (loses a delivered report format); compute everything in SQL (a larger change, to be decided with the analytics-source question).
- **Consequences:** a slim ingestion install stays possible. If analytics later moves entirely to SQL views, this extra can shrink.
- **Status:** Accepted

## Open decisions
| # | Decision | Needed before |
|---|----------|---------------|
| D1 | Primary source (Phase 0 outcome) | Phase 3. Chosen: official results portal API (`back.results.asiangames2026.org`), decodable without a key, event-level data confirmed (`SOURCE_DISCOVERY.md` section 10). **Open: the portal's terms of use (owner).** Automated fetching in Phase 3 waits for it; manual CSV import is the fallback |
| D2 | Where data lives between scheduled runs | Resolved by ADR-016 |
| D3 | Public or private dashboard | Phase 5 |
| D4 | Include LLM explainer | Phase 7 |
