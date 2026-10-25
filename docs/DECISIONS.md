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

## ADR-019: Open events stay Open; only reconciliation folds them into Mixed

- **Context:** The official table counts the 20 Open events (esports, equestrian, sailing) under its Mixed bucket. `docs/ANALYTICS_SPEC.md` says Mixed and Open are reported separately. The reports and dashboard had collapsed Open into Mixed.
- **Decision:** Gender categories are Men, Women, Mixed and Open everywhere internally and in reports and the dashboard. Reconciliation against the official table compares Mixed + Open with the official Mixed bucket, so the 0-mismatch check is unchanged. Country tables that come straight from the official standings feed (`analytics/standings.py`) cannot split Open out and stay M / W / X.
- **Why:** No information is lost, and questions such as "in how many Open events did India win a medal" can be answered. Folding is a view of the data, not a property of it.
- **Decided by:** the owner, 2026-10-05.

## ADR-020: Official sports group the portal's 59 disciplines

- **Context:** The first loader created one sport per portal discipline (59). The schema models sport > discipline, and `DOMAIN_MODEL.md` and `ANALYTICS_SPEC.md` analyse country x sport with the official sports (Aquatics contains Swimming, Diving, Artistic Swimming and Water Polo; Cycling, Gymnastics and Canoe also group disciplines).
- **Decision:** `data/reference/{sports,disciplines,sport_aliases}.csv` carry the grouping: 49 sports, 59 disciplines, `double_bronze` set per sport from the official data. The loader never creates sports or disciplines; a name that is not in the reference is quarantined. The portal's discipline is kept as the event's discipline, the sport is derived from reference data.
- **Why:** Country x sport analysis needs the official grouping, and the original source discipline is preserved.
- **Decided by:** the owner (2026-10-04). Analytics that still group by `discipline_name` and call it "sport" are a known gap (see ADR-021, consequences).

## ADR-021: One canonical ingestion path

- **Context:** `sie/load.py` (portal captures) and the Phase 2 import code (CSV) were two loaders with different rules.
- **Decision:** Every source supplies bytes and a pure parse function to `pipeline/runner.run_ingest`. The raw bytes are stored and committed first; then parse, normalise, validate, quarantine and load run in one transaction; the run is closed as `success` or `failed` in `ingest_runs`. `sie/load.py` is removed. `sie load-capture` and `sie import-csv` are thin commands over the same code; `sources/bornan/to_parsed.py` and `sources/manual/parser.py` are the only source-specific parts.
- **Behaviour that changed:** unknown country, sport or gender no longer fail the whole load: the row goes to `quarantine` with a reason and the valid rows load (`DATA_PIPELINE.md` section 6). Unchanged raw content is still re-processed (cheap and idempotent), so adding an alias and re-running resolves quarantined rows; the earlier quarantine rows of that raw version are marked `resolved`.
- **Not yet built:** entrants (names are not stored; the portal adapter drops them on purpose), the conflict policy, `reconciliation_results`, and the regeneration of `site/` and `reports/`. The analytics read path was moved to `v_medal_facts` afterwards (`sie.analytics.facts`; only the official tables are still read, as references to check against).

## ADR-022: Raw bytes in the database or on disk (migration 003)

- **Context:** `DATABASE.md` documented `raw_blobs`, `raw_versions.storage_backend` and `placings.source_note`, but migration 001 and 002 did not have them, and the first loader recorded a path without saving any file.
- **Decision:** Option A: implement what the document says. Migration 003 adds them, plus `ingest_runs.source`. `RAW_STORE_BACKEND` selects `fs` (default, `DATA_DIR/raw/<source>/<date>/<time>_<sha12>.<ext>`, write-once) or `db` (gzip in `raw_blobs`). `s3` is accepted by the schema and built in Phase 3. The column that holds the file path or object key is named `path` (the document said `storage_key`; the document was corrected, no rename).
- **Why:** Hosted runs have no persistent disk (ADR-016), and a placing must always trace to bytes that still exist.

## ADR-023: Static HTML dashboard instead of Streamlit

- **Context:** ADR-007 chose Streamlit. The delivered dashboard is a static page (`src/sie/dashboard.py` writes `site/index.html`) served by Cloudflare.
- **Decision:** Keep the static dashboard for v1. It reads an exported file, not the database, and contains no metric logic beyond display.
- **Open:** whether the dashboard stays public is still D3 (owner).
- **Consequence:** the dashboard reads the exported `placings.csv`, which is produced from `reporting.medal_facts`. `site/` and `reports/` were regenerated from the full capture on 2026-10-07 (PR #5).

## ADR-024: Asian Games is the first dataset, not the product boundary

*Points 1 and 3 are superseded by ADR-039 (2026-10-11); points 2, 4 and 5 stand.*

- **Context:** The only data so far is Asian Games 2026, and the dashboard and reports say so. Without a stated rule the core could grow Asian-Games-specific assumptions, and a later research-query or AI layer could blur facts and generated text.
- **Decision:**
  1. The product is a competition-agnostic sports research engine (`PRODUCT_VISION.md`). `competition_id` stays the boundary between competitions. A new competition is added through metadata, an adapter, parser configuration, reference mappings and validation rules, never by changing the core analytics.
  2. Multi-competition support is not claimed until a second-competition fixture passes the proof listed in `PRODUCT_VISION.md` section 3 (Phase 7).
  3. A future research query layer compiles questions into a deterministic, testable plan over the canonical data. It starts with a small enumerated set of questions, not an open agent. The plan shape is decided later, against the Phase 4 analytics API.
  4. Raw source data, normalised data, analytical results, external evidence and generated explanation are separate layers and are never mixed.
  5. AI only explains verified results. It never computes numbers, edits facts or hides conflicts.
- **Consequences:** Documentation only. Nothing is implemented by this ADR. Phase 4 stays the immediate engineering priority. Roadmap phase numbers are unchanged; Phase 7 gains acceptance criteria and a "Research query layer" milestone follows it.
- **Decided by:** the owner (2026-10-05).

## ADR-025: A medal keeps the date it was decided

- **Context:** `events.event_date` holds one date per event, the latest one the source gave. Counted per real event, 53 events are decided on more than one day and 106 medals fall on a different day than their event's latest date. A medal timeline built from the event date moves those medals to later days. (Earlier notes said 55 events and 288 medals: that came from grouping by the portal's event code, which merges different events of different disciplines.) Three Modern Pentathlon medals have no date in the source at all.
- **Decision:** migration 004 adds `placings.result_date` (nullable `DATE`), set by the loader from each row's own date and exposed as `v_medal_facts.result_date`. `events.event_date` stays as the event's latest date. A source that gives no date leaves it `NULL`; `apply_placing` never invents one. A date arriving for a current placing that has none is filled in place (nothing was claimed before); a different date for a placing that has one is a correction and keeps the old version. Timelines count undated medals on a final `undated` row, so the last cumulative row equals the country medal table.
- **Consequence:** a database loaded before migration 004 gets its dates when the capture is loaded again. Tests pin 53 multi-date events, 106 re-dated medals, 3 undated medals and the timeline totals.

## ADR-026: Reference country names in analytics, source names kept as provenance

- **Context:** The portal names countries its own way ("Republic of Korea", "People's Republic of China"); 8 of the 40 differ from the reference names ("South Korea", "China").
- **Decision:** Analytics and the dashboard show the reference name (`countries.name`, already what `v_medal_facts.country` returns). Migration 004 also adds `placings.source_country`, the country label exactly as the source wrote it, set by the loader and never compared or used to match anything. Matching still goes through `country_aliases`.
- **Consequence:** the portal's wording stays available for audit and for debugging aliases; no analytics table contains a source-specific name.

## ADR-020 and ADR-019 applied to analytics (clarification)

- The `sport` dimension of every analytics table is the official sport (49). The source's 59 disciplines stay available as `discipline` (`country_sport(df, "discipline")`, the report's discipline sheets, the `Placings` export). Official per-discipline reconciliation is unchanged because the portal's standings are keyed by discipline.
- `Open` stays its own gender category in all analytics. `reconcile` and `reconcile_official_table` fold Open into Mixed because the official table has no Open column. The dashboard's Open toggle (official or separate) only changes the view.

## ADR-027: Failure category stored in `error_summary`, no migration

- **Context:** Phase 6 needs to tell fetch, parse, validation and load failures apart and say what is safe to retry. `ingest_runs` has only a free-text `error_summary`.
- **Decision:** Store the category as a prefix, `[load_failure] detail`, read back by `parse_error_summary`. Outcome and freshness are derived from existing columns. No schema change, so this work merges independently of any analytics migration.
- **Consequence:** The category is queryable with `LIKE '[load_failure]%'` but not constrained by the database. If scheduling needs indexed filtering by category, add a `failure_category` column in a later migration and backfill from the prefix.
- **Decided by:** the owner's Phase 6 brief (2026-10-06).

## ADR-028: Scheduler applies the failure contract; per-source advisory lock; no portal fetcher yet

- **Context:** Phase 6A needs overlap prevention, retries and stuck-run handling on top of ADR-027, without changing its categories.
- **Decision:** The retry numbers are read from `RETRY_RULES` (fetch 3 attempts, load 1 retry). The advisory lock key is derived from (competition, source), so independent sources are not serialised; a busy source is skipped, not queued. Stuck runs are closed as `failed` with no category. The workflow is manual-only and no automated portal fetcher exists, because D1 (terms of use) is open.
- **Consequences:** An overlap is silent apart from a log line and exit 0; alerting on it belongs to Phase 6B. A non-scheduler run (`import-csv`) does not take the lock. If it runs longer than the stuck threshold while a scheduled run starts, the scheduler would close it; raise `--older-than` or route all runs through `scheduled-run`.
- **Decided by:** the owner's Phase 6A brief (2026-10-06).

## ADR-029: Health composes freshness; thresholds are existing numbers; no provider coupling

- **Context:** Phase 6B needs health states, alert conditions and a way to deliver alerts, without redefining ADR-027/026 or choosing a provider.
- **Decision:** `SourceHealth` wraps `SourceFreshness` and adds failure history and stuck runs; it never reclassifies freshness. Alert thresholds reuse existing numbers: `FRESHNESS_THRESHOLD_MINUTES`, the fetch attempt limit (3) for repeated failures, the scheduler's stuck age. The one new setting is `SCHEDULED_SOURCES`, because "expected to run" cannot be derived from runs that never happened. Delivery is a `Notifier` protocol with a log channel; `deliver()` never raises.
- **Consequences:** Alerts are level-triggered and stateless, so a channel that sends them must de-duplicate by `key`. A first parse, validation or raw-store failure does not alert until it repeats or the data goes stale. No `failure_category` column was added: nothing here needs to query by category.
- **Decided by:** the owner's Phase 6B brief (2026-10-06).

## ADR-030: Backup, publish bundle, delivery state in a file; first-failure alert for non-auto-retryable categories

- **Context:** Phase 6C/6D and the remaining 6B follow-ups: a backup that is proven to restore, an export bundle, a real delivery channel with de-duplication, and an alert on the first parse, validation or raw-store failure.
- **Decision:** (1) `sie backup` runs `pg_dump` (custom format, no owners) from the same exported snapshot as the row counts in its manifest, then restores into a scratch database and checks schema revision, row counts and a re-hash of every `raw_blobs` value; a backup that fails its restore test exits 1. (2) `sie publish` writes `medals.csv`, `events.csv` and `manifest.json` from the `reporting` schema only; output is deterministic and the manifest is written last. (3) Alert de-duplication state is a JSON file behind a `AlertStateStore` protocol, not a table: this PR was written while Phase 4's migration 004 was unmerged, and a second 004 would have given Alembic two heads. Migration 004 is now on main, so a later migration 005 can move the state into a table. (4) New alert `needs_attention` (critical) fires on the first failure whose category is not auto-retryable (parse, validation, raw store). (5) `WebhookNotifier` posts one JSON batch to `NOTIFY_WEBHOOK_URL` (https; http only for localhost).
- **Consequences:** The state file must live on storage that survives between `sie health` runs (a hosted runner without a persistent disk will re-send every alert; use `ALERT_STATE_PATH` on durable disk, or move the store to a table once migrations are merged). A failed delivery is retried on the next check. Raw bytes kept on disk (`fs` backend) are not in a database dump; the manifest and restore test say so. The portal fetcher and its cron are still not built: they wait for decision D1.
- **Decided by:** the owner's Phase 6 follow-up brief (2026-10-06).

## ADR-031: D1 cleared; portal fetcher and daily refresh, with guards

- **Context:** The portal publishes no terms of use or robots.txt (`SOURCE_DISCOVERY.md` section 12). On 2026-10-07 the owner cleared D1 (recorded in `SOURCE_DISCOVERY.md` section 12).
- **Decision:** Build the fetcher (`sie/sources/bornan/fetch.py`, `sie scheduled-run portal`, `sie fetch-portal`) and schedule `refresh.yml` daily (the Games ended 2026-10-04, so every 30 minutes is not needed). It follows AGENTS.md rule 9: one fixed https host, honest User-Agent with a contact (the placeholder default is refused), at least 2 seconds between requests, no redirects, size cap, only `ALL/disc/data` and `{DISC}/medals/discipline` (never `entries/...`), stop on 429 or 5xx, all or nothing. Two switches stay in the owner's hands: `PORTAL_FETCH_ENABLED` (code and workflow) and the `HTTP_USER_AGENT` variable. The scheduled run also runs `sie health` with the alert state in an Actions cache.
- **Consequences:** If the organisers later restrict automated access, setting `PORTAL_FETCH_ENABLED` to anything but `true` stops it with no code change. The fetcher was tested against a fake portal and the saved fixtures only: the sandbox this was built in cannot reach the portal, so the first live run must be watched (use `sie fetch-portal --out FILE` first). A change in the portal's wire format fails as a fetch failure and is retried 3 times; it needs a code fix. Alert state in an Actions cache can be evicted; durable storage is still the proper fix (ADR-030).
- **Decided by:** the owner (2026-10-07).

## ADR-032: Conflict policy engine: observations on every load, official is trusted only while fresh

- **Context:** `DATA_PIPELINE.md` section 7 defined the freshness-aware conflict policy, but nothing recorded observations or conflicts. Two sources (portal, manual CSV) can now disagree about a slot.
- **Decision:** `pipeline/conflicts.py` is the pure policy (`decide`); `db/conflicts.py` stores observations, conflicts and the disputed flag; `load_placed` calls both when given a policy, and `run_ingest` always passes `conflict_policy(settings)`: the first entry of `SOURCE_PRIORITY` is the official source, `FRESHNESS_THRESHOLD_MINUTES` the freshness window. Every placed row is stored as a `source_observations` row (once per slot, source and raw version). A claim is compared with the latest claim of every other source; a source changing its own claim is never a conflict. `events.is_disputed` is derived from open (`needs_review`) conflicts after each event is loaded. `sie conflicts` lists them; `sie resolve-conflict ID --accept SOURCE --note "..." [--by NAME]` applies that source's claim through `apply_placing` (history row with the reason), stores note, owner and time, and clears the flag. No schema change: migration 001 already has the tables.
- **Consequences:** (1) A non-official claim is always newer than any earlier official fetch, so a manual claim that disagrees with the portal is held (`official_stale`, `needs_review`, flag set, `refetch_official` reported) until the next official fetch decides it, exactly as the policy table says. The "official fresh holds a non-official claim" row only applies to a claim whose observation time is older than the official fetch; it is covered by a unit test. (2) An unchanged re-fetch stores the same raw version, so its observation row (append-only, unique per raw version) keeps its first `observed_at`. Freshness therefore reflects when the content last changed, which is conservative: it can only make the official source look staler. (3) Open question for the owner: if the owner resolves in favour of a non-official source and the official source later re-fetches its old value unchanged, the official value wins again (policy row: official fresh). Making an owner decision sticky would need a new rule; not chosen silently. (4) Cost: one observation row per placing per distinct raw version.
- **Decided by:** the owner's Phase 3 brief (2026-10-07) and `DATA_PIPELINE.md` section 7; item (3) awaits the owner.

## ADR-033: Phase 4 analytics engine: pure metrics, invariants before output, snapshots by fingerprint

- **Context:** `ANALYTICS_SPEC.md` listed twelve output tables, snapshot identity rules and rule-based insights, but only part of them existed (in the report builder). Phase 4 acceptance asks for `sie analyze` to write all of them.
- **Decision:** (1) Every metric is a pure function in `analytics/metrics.py` over the facts-derived frame; the report builder keeps its own functions untouched. (2) `build_tables` checks the invariants of spec section 13 before anything is returned; a violation stops the run (exit 2) and writes nothing. (3) `take_snapshot` follows `DATABASE.md` section 8 with no schema change (migration 002 already added `competition_id`, `local_date` and the daily index): fingerprint over placings by natural key plus event status and disputed flag; `change` snapshot on a new fingerprint or analytics version; one `daily` snapshot per competition-local day. (4) Reconciliation is stored data, a supplied official table (`--official`), or reported as `not_run`; it is never assumed. (5) `sie analyze` takes snapshots by default; `--no-snapshot` is read-only.
- **Consequences:** Snapshots store medals per country, sport, discipline and gender, so per-sport event completion over time is not available; completion is the current state. Changing a formula needs a spec update, a new `ANALYTICS_VERSION` and therefore a new `change` snapshot. Tier and rank conventions are in `ANALYTICS_SPEC.md` section 14.
- **Decided by:** the owner's Phase 4 request (2026-10-07); the formulas are the spec's.

## ADR-034: Backups are encrypted with age before upload; restore is proven in a scratch database

- **Context:** `backup.yml` uploaded the dump as a plain Actions artifact in a public repository, where artifacts are downloadable by any signed-in GitHub user. Audit (2026-10-10): the workflow had never produced an artifact (it called `sie backup --out`, the option is `--out-dir`; runs failed on 8, 9 and 10 Oct), so no dump was exposed. It also restored into the same server as `DATABASE_URL` (production Neon) with an unmatched client version.
- **Decision:** `age` (age-encryption.org) public-key encryption through the `age` binary (no Python dependency; BSD-3 licence, maintained, small audited format; alternatives: GPG, heavier and error-prone; symmetric passphrase, which would put the decrypting secret on the same runner as the data). `sie backup --encrypt-to RECIPIENT --require-encryption` encrypts in a 0700 temporary directory and deletes the plaintext; manifest records plaintext and ciphertext hashes and the recipient. The workflow fails closed without `vars.BACKUP_AGE_RECIPIENT` or `secrets.BACKUP_AGE_IDENTITY`, verifies by decrypt and restore into a `postgres` service container (`sie restore-test --identity-file --scratch-url`), refuses to upload anything but `*.dump.age` and manifests, and uploads with `if-no-files-found: error`. `--scratch-url` equal to the production URL exits 2. A test checks that every `sie <command> --flag` used in workflows exists. CI installs `age` and sets `REQUIRE_AGE=1` so the crypto tests cannot silently skip.
- **Consequences:** The owner must create the key pair and set the variable and secret (`DEPLOYMENT.md` section 3a); until then the nightly backup fails visibly instead of leaking. Losing the private key loses the backups. Someone who can change workflows or read secrets can still obtain the key. `age` is installed from the runner's apt repository. No schema change.

## ADR-035: Alerts must run after a failed refresh; the alert lifecycle is tested end to end

- **Context:** The alert pieces (health, de-duplication, webhook) had unit and component tests, but no test ran them together, and `refresh.yml` did not run `sie health` when `sie scheduled-run` failed (steps after a failed step are skipped), so the one situation alerts exist for, a failing refresh, sent no webhook message.
- **Decision:** The restore-state, health and save-state steps use `if: ${{ !cancelled() }}`; the refresh step stays a hard failure. `tests/integration/test_alert_lifecycle.py` drives the lifecycle through the real CLI against an isolated database and a local webhook receiver; `tests/unit/test_refresh_workflow.py` guards the workflow. No schema, dependency or alert-contract change.
- **Consequences:** After a failed refresh the job still ends red (the health step exits 1 while an alert holds) and now also notifies. A failure before the database is reachable also fails the health step; that is accepted. Delivery to a hosted channel is not tested automatically (see DEPLOYMENT.md 7a).
- **Decided by:** the owner's Phase 6 brief (2026-10-10).

## ADR-036: CI runs the dashboard in a real browser

- **Context:** The only browser test skipped itself wherever Chromium was missing, which included CI, so a page-blanking script error (PR #14) reached production while CI was green. The `node --check` test caught syntax errors only.
- **Decision:** CI installs the existing optional `browser` extra (Playwright, already declared in `pyproject.toml`) and Chromium, and sets `REQUIRE_BROWSER=1` so a missing browser fails the build. `tests/dashboard_smoke.py` is a reusable checker (also runnable against a URL); `tests/unit/test_dashboard_smoke.py` proves it fails on broken pages. No runtime dependency and no new package is added to the project.
- **Consequences:** CI takes longer (browser download, a few minutes of page loads). The smoke test compares the page with `reports/placings.csv`; if the dashboard is rebuilt from newer data, the CSV and `site/index.html` must change together. Deployed-site verification is a separate manual run (TESTING.md section 9).

## ADR-037: `sie acceptance` is a read-only check against fixed expected figures

- **Context:** Production acceptance needs a repeatable answer to "does the production database hold the 469 events and 1,568 placings, fresh, with nothing open?" that does not depend on reading workflow logs.
- **Decision:** `sie acceptance` (`ops/acceptance.py`) runs in one `REPEATABLE READ`, read-only transaction and checks: migrated schema, 469 events, the official event total, 1,568 current placings (470 gold, 469 silver, 629 bronze), events with a gold, 40 medal countries, 0 disputed events, 0 open conflicts, 0 unresolved quarantined rows, a successful `official` run within 26 hours, no run stuck in `running`, and at least one analytics snapshot. The expected figures are options (defaults are the Asian Games 2026 totals); exit 1 on any failed check. It never writes, so it can run against production; its tests use only the embedded test database. This replaces the overlapping parts of PR #15, which was consolidated into the encrypted-backup PR (ADR-034) because both rewrote the same backup files; PR #16's design (age binary, scratch server, production-URL refusal) is the canonical one.
- **Consequences:** The check proves the database state, not that the portal still agrees; reconciliation stays in `sie analyze`. Passing it is evidence only when it was actually run against the production database by someone who holds `DATABASE_URL`.
- **Decided by:** the owner's production-acceptance brief (2026-10-10).

## ADR-038: Remote-database commands get bounded waits and visible progress

- **Context:** `sie seed-reference` against Neon printed nothing for minutes and, when interrupted, a psycopg query-cancellation warning. Reproduced on a throwaway PostgreSQL (embedded `pgserver`, never production) behind a TCP proxy that adds latency. Two independent causes, both measured: (1) `seed_reference` issues 361 statements one after another, so the time is 361 x the network round trip: 0.2 s with no latency, 38.8 s at 100 ms, 152.7 s at 400 ms, with no output meanwhile; (2) an upsert waits without limit when another transaction holds the competition row: the blocked statement was the `INSERT INTO competitions ... ON CONFLICT DO UPDATE` (wait event `Lock/transactionid`), the holder an `idle in transaction` session, and the seed finished 0.1 s after the holder rolled back. Which of the two applies to the production database is not known from the sandbox.
- **Decision:** `sie seed-reference` now runs `SET LOCAL lock_timeout` (default 30 s) and `statement_timeout` (default 300 s) in its one transaction (`db/diagnostics.py`; transaction-scoped, so safe behind a transaction pooler), prints a heartbeat to stderr every 10 s naming the statement in progress (first 90 characters, never a parameter), and on a timeout exits 3 with "Nothing was written" and a read-only list of the other open sessions (`pg_stat_activity`, `pg_blocking_pids`; 5 s limit, read-only session). `sie db-activity` prints the same list on demand from a second terminal. No statement, order or data of the seed changed, and there is no retry.
- **Consequences:** A blocked seed now fails in 30 s with the blocker's pid, state, age and statement instead of hanging. A merely slow link still takes 361 round trips; sending each table as one batched statement (executemany, which psycopg pipelines) measured 6 statements and 8.7 s at 400 ms and is the remedy for latency, deliberately not part of this change. `pg_stat_activity` shows other sessions' text only for the same role or a superuser.
- **Decided by:** the owner's request (2026-10-11).

## ADR-039: Widen the product from a sports engine to a general research and analytics engine

- **Status:** Accepted by the owner on 2026-10-11 (reply "to jo abhi krna hai suru kr do" to the question whether to proceed). The owner can still withdraw it; if the reply was not meant as approval, say so and this ADR reverts to Proposed. Nothing is implemented by this ADR; each step needs its own design approval.
- **Context:** The owner's goal (2026-10-11) is one input box for any research or analytics question, with data from uploads, the web, APIs and documents, evidence-backed answers and a conversation. ADR-024 fixed the boundary at a competition-agnostic sports engine. `GENERALISATION_AUDIT.md` lists what is generic, what is sports-specific and what is missing.
- **Proposal:** (1) Replace ADR-024 points 1 and 3 with: the product is a domain-agnostic research engine; sports (Asian Games) is the first domain pack. (2) Keep ADR-024 points 4 and 5 unchanged: layers stay separate, and AI only plans and explains, never computes numbers or edits facts. (3) Web material is stored as cited evidence with its own status, never as verified data. (4) The sports schema, outputs and tests stay as they are; generic capability is added as new modules and new migrations. (5) Build order as in the audit, section 8, starting with generic tabular analytics for uploaded files (no model, no cost). (6) No paid service is adopted without a decision here that states its cost and a spend limit.
- **Alternatives:** keep the sports boundary and add only the planned research query layer (ADR-024 as written); or build the general engine as a separate repository and keep this one sports-only.
- **Consequences if approved:** a larger scope, new tables and a backend, authentication and cost controls become necessary; the rules in `AGENTS.md` stay in force. If rejected, ADR-024 stands.
- **Decided by:** the owner (2026-10-11).

## ADR-040: Library first, command line second, for generic dataset analytics

- **Context:** ADR-039 widened the product; the first generic capability is analytics for an uploaded file (`docs/DATASET_ANALYTICS_DESIGN.md`, approved by the owner on 2026-10-12). The owner required that it can later sit behind a universal research interface (chat, web service, planner) without being rewritten, and that the command line is not the architecture.
- **Decision:** New package `sie.datasets` is a library: no printing, no `sys.exit`, no command-line, database or web assumptions, no model call, no network. Requests are validated data (JSON-able specs and a cleaning plan), never code. The source of a table is a `SourceRef` (upload, fetched or derived), so a future fetcher feeds the same functions through `from_frame`. One orchestration function (`run.run_request`) does raw copy, load, profile, cleaning, operations and atomic publish; `sie profile|clean|calc|calc-spec|operations` (`sie/datasets_cli.py`, registered with one line in `cli.py`) only declare options, read the one input file and map errors to exit codes (0 ok, 1 unusable data, 2 invalid request). Boundaries are enforced by tests: the package imports nothing from the CLI, pipeline, analytics, database or sources code, nothing outside the standard library, pandas, numpy, openpyxl and pydantic (all existing dependencies), nothing imports it except `datasets_cli.py`, and it does not import or get imported by `sie.answer`.
- **Consequences:** No migration, no new dependency, no change to the sports schema, outputs or tests. Output goes only under `--out` and `DATA_DIR/uploads`. Where uploads live in a hosted service, authentication and the final limit values are not decided here (design section 16). Determinism is logical (canonical tables and hashes; CSV and `manifest.json` byte-identical apart from `generated_at` and `tool_environment`); the XLSX is only logically reproducible. Memory is not capped, only rows, columns and cells.
- **Decided by:** the owner (design approved 2026-10-12).

## ADR-041: Answer contract with a number lock (Phase 1 of the general research engine)

- **Context:** ADR-039 widened the product; ADR-024 points 4 and 5 keep AI from computing numbers or editing facts. Without one shared answer shape, every later piece (datasets, evidence ledger, web research, model layer) would invent its own, and "no invented numbers" would be a promise instead of a check.
- **Decision:** New package `sie.answer` (`docs/ANSWER_CONTRACT_DESIGN.md`): layered statements, exact `Figure`s that name their source, `EvidenceRef`, disclosed `Gap`s, `Refusal`. Numbers enter text only through `{F:id}` placeholders; a verifier rejects typed digits, number words, ordinals and other numeric characters unless declared, enforces which layer may cite which source, requires every gap to be disclosed and every answer to carry a limitation, and rendering fails closed. Pure Python, pydantic only, no table, no network, no command. Nothing existing imports it (tested).
- **Consequences:** Production behaviour is unchanged. The lock covers numbers, not names or truth, and English number words only (stated in the design). ADR number 040 is the datasets library (ADR-040). The next phases build on this shape: ledger tables, web evidence, router, model layer.
- **Decided by:** the owner (approved 2026-10-11).

## ADR-042: Evidence ledger and project-scoped upload storage (Phase 2 of the general research engine)

- **Context:** ADR-039 needs user files and web pages to become citable evidence without touching the sports data, and ADR-041's number lock needs figures that name a source. Uploads and web pages must be isolated per project, deletable, and never mistaken for verified truth.
- **Decision:** Migration 005 (additive; 001-004 untouched) adds `projects`, `purge_tombstones`, `project_documents`, `evidence_sources`, `claims`, `claim_reviews`, `claim_conflicts` and the view `evidence_claims_current`; package `sie.evidence` (`docs/EVIDENCE_LEDGER_DESIGN.md`, `docs/EVIDENCE_RETENTION.md`). (1) Every upload is its own per-project copy in the existing raw store under `rf-project/<project_id>/upload/<sha256>`; composite foreign keys `(id, project_id)` and a scoped API make cross-project reads impossible, and a test per scoped function proves it. (2) Web pages default to `excerpt` retention; `full` needs a recorded host policy (`data/evidence/source_policies.csv`, empty = deny) and expires after 90 days (configurable). (3) XLSX claims stay `unverified` until the shared datasets loader exists; this code has no spreadsheet reader. (4) Claim status is the latest append-only review: `unverified`, `source_located` (the quoted value was found at the stated place in the stored bytes: not a statement that it is true), `human_confirmed` (a named person says the claim matches its source: not a statement that it is true), `disputed`, `rejected`. Database checks stop a system actor from setting `human_confirmed` and a person from setting `source_located`. (5) Purge and prune are owner-only, dry-run by default and need explicit confirmation; they delete from the live database only.
- **Exception to raw-bytes immutability (ADR-016):** project uploads and full web copies can be deleted by the owner (purge, prune). The sports raw store is never touched by either command (tested). The `pipeline` role cannot update or delete any ledger table.
- **Consequences:** Backups taken before a purge still contain the data (up to `BACKUP_RETENTION_DAYS`, 14 on the workflow); the provider's point-in-time history window is **not verified** and is reported as such until the owner sets `DB_HISTORY_WINDOW_HOURS`. Nothing here claims complete erasure. Restoring a backup requires re-running purge for projects listed in `purge_tombstones`. The first scheduled refresh after merge applies migration 005 to production; the migration sets 5 s lock and 60 s statement timeouts and locks only `raw_documents` and `raw_versions` (share row exclusive) while the foreign keys are added.
- **Decided by:** the owner (design approved 2026-10-12; implementation under review).

## ADR-043: Project-scoped freshness and health (Phase 3 of the general research engine)

- **Context:** `sie health` and `sie source-status` answer for a competition's source. A research project (ADR-042) needs the same question answered for its own sources, and answers need disclosed gaps when sources are old or gone.
- **Decision:** Read-only module `sie.evidence.health` and command `sie project health` (`docs/EVIDENCE_LEDGER_DESIGN.md` section 8; design `docs/PHASE3_PROJECT_HEALTH_DESIGN.md`). No migration, table or dependency; the sports freshness and health code is unchanged. `pruned` means the bytes were removed, `expired` means the retention deadline passed, `stale` means a web source older than `EVIDENCE_WEB_MAX_AGE_DAYS` (30; potentially outdated, not false). Uploads never become stale by age; an `old_upload` flag is informational and never fails the command. The 90-day full-copy retention is unchanged.
- **Consequences:** Two new settings with defaults. Alerts are not sent by this phase.
- **Decided by:** the owner (design approved 2026-10-15).

## ADR-044: Web research: safe fetching and source discovery (Phase 4 of the general research engine)

- **Context:** ADR-039 point 3 stores web material as cited evidence, never as verified data. The owner (2026-10-23) wants sources found from a question, not from a fixed list, with official sources preferred and secondary sources used for context.
- **Decision:** `sie.evidence.web` fetches one page at a time under rules that apply to every host (AGENTS.md rule 9): https only, no credentials, resolved and connected addresses must be public (SSRF guard), robots.txt per host, honest User-Agent with a contact, at least 2 s between requests to a host, stop on 403, 429 or 5xx, at most one same-host redirect, content-type allow-list, byte cap, no compressed bodies. `sie.evidence.discovery` ranks leads from an injected search provider by source authority tier (`data/evidence/source_authority.csv`), relevance and downloadable data, caps results per host, pre-checks robots.txt, and gives a reason for every rejection. No model is used and no number comes from prose. Retention stays as ADR-042 (excerpt by default).
- **Consequences:** ADR-045 records the owner's decision to use free sources only by default; no paid provider is being considered. Concrete free discovery adapters remain to be implemented. The leads-file provider is a manual fallback, not automatic web discovery. Authority tiers are a transparent heuristic, not a statement that a source is correct. Collection into datasets (4c), analysis and the model layer are separate steps. The real network function is not exercised by CI (no network there); a controlled live test by the owner is required before any automation. No migration.
- **Decided by:** the owner (approved 2026-10-23; implementation under review).

## ADR-045: Free-source-first discovery and collection

- **Context:** The owner requires the general research engine to use free sources and free access paths first: public catalogues, public HTML/CSV/JSON, and open APIs. Paid search APIs or billable services must not appear as a hidden dependency or automatic fallback.
- **Decision:** The default discovery and collection path is free-only. Start with relevant official/government catalogues and documented public dataset APIs, then eligible free search options, then secondary references for context. Catalogue-specific adapters may use documented unauthenticated access or a free key/account only where no payment method or paid plan is needed. Every adapter documents its access requirements, licence/terms, quota and rate limits. Cache results and fetched evidence, avoid repeated calls, use existing libraries/dependencies where possible, and reuse the same normalized dataset for follow-up analytics. When a free quota is exhausted or a provider is unavailable, stop that provider, report the limitation and use cached evidence, another eligible free source, or manual URL/file import. No paid endpoint, automatic paid fallback, billable model call or new dependency is allowed without a separate explicit owner decision. This does not waive source licences, terms, robots.txt or access restrictions; no login/paywall/CAPTCHA/access-control bypass is allowed.
- **Consequences:** The system cannot promise universal coverage from free sources alone. Missing coverage and confidence must be disclosed instead of silently purchasing access or inventing data. The first working end-to-end loop is deterministic: discover, fetch, preserve provenance, normalize, analyze, report and reuse the dataset for follow-ups.
- **Status:** Accepted by the owner on 2026-10-23 (request: stick strictly to free sources). Supersedes any ambiguous reading of ADR-044 that a provider must be chosen based on paid pricing or spend limit; ADR-039's no-paid-service-without-decision rule remains in force.

## ADR-046: Free discovery adapters, collection, cache and report (Phase 4c)

- **Context:** ADR-045 requires free sources only. Phase 4a/4b gave a safe fetcher and lead ranking but no catalogue adapter, no table collection and no reusable dataset.
- **Decision:** New package `sie.research` (no migration, no dependency): (1) a registry `data/evidence/catalogues.csv` whose rows must say `free_no_key`, link the terms read and the date; any other access value, a key in the URL or an IP host is refused at load. Two adapters: a CKAN `package_search` adapter (registered: data.gov.uk, whose guidance states no key and no rate limits) and a keyless OpenAlex works search (whose documentation states a free keyless budget and HTTP 429 on exhaustion). Provider calls can reach only registered hosts. (2) A file cache of fetched pages (one entry per URL, one blob per content, write-once, hash-checked), with a flagged fall-back to an older copy when a live fetch fails. (3) Collection of CSV, JSON and HTML tables through `sie.datasets` (the one HTML table parser is `sie.evidence.collect`, added in parallel by Phase 4c part 1; `sie.research.collect` reuses it and so inherits its refusals: merged cells, nested tables, no header row, duplicate or empty headers and ragged rows are refused with a reason and listed as `table_refused` flags, never repaired; it adds multi-table pages, CSV rows rejected with a count, and unwrapping of a JSON wrapper), keeping URL, retrieval time, page hash and 1-based row ids, with rejects, renamed headers, merged cells, unwrapped JSON, missing values and conflicts flagged and never resolved. (4) A dataset store keyed by content, reused for follow-up questions without network access. (5) Deterministic analysis (`sie.datasets` operations), an answer that passes the Phase 1 number lock, coverage and limitation gaps, and an SVG chart. (6) A guard: a failing, rate-limited or timed-out provider is reported and skipped; nothing replaces it, and the report asks for a URL or file.
- **Amended 2026-10-25 (review of the PR):** (a) *Storage.* The dataset store in item (4) is replaced by `sie.evidence.dataset_store` (built on `sie.datasets.store`, ADR-044): one writer, project-scoped (`--project` label for the database-free research commands, project id for the ledger), hash-addressed, atomic, with an expiry (default 90 days, `EVIDENCE_DATASET_RETENTION_DAYS`; a host policy may shorten it). Only the normalised table and a manifest are kept (URL, retrieval time, page hash, table label, schema, licence as reported and as permitted, terms reference, retention). Rows are saved only if a recorded host policy, a source policy or an open licence (`data/evidence/open_licences.csv`) permits it; otherwise the table is used for the run and reported as not saved. (b) *Fetch cache.* The cache of item (2) is no longer an archive of pages: it keeps a body only for catalogue API answers and hosts with a recorded retention policy, nothing lives past `RESEARCH_CACHE_RETENTION_DAYS` (default 7), a stale fall-back works only inside that window, and `sie research prune` removes expired files. (c) *Reuse.* Saved data is reused only if it is in the project, unexpired, fresh (`EVIDENCE_WEB_MAX_AGE_DAYS`), covers the question and has every column the operation names; otherwise the loop looks for more, uses stale data only as a disclosed last resort, and finally asks for a URL or file. (d) *Conflicts and licence.* With two or more source URLs, `sie research ask` compares shared columns (or `--conflict-key/--conflict-values`) and the report lists every conflicting value, or says what was compared, or says agreement could not be checked. The licence id reported by the catalogue travels from the lead to the manifest and into the report; a missing licence is a disclosed gap.
- **Not done, on purpose:** catalog.data.gov and open.canada.ca (their API terms could not be read on 2026-10-24), data.gov.in (its APIs need a key; terms unchecked), Wikimedia (rate-limit page unreadable; robots.txt rules would also apply), Common Crawl, a general web-search engine (no keyless search with documented permission for automated use was verified). None is enabled.
- **Consequences:** Network behaviour is verified only against a simulated site; no real catalogue was contacted from the build environment (outbound access refused). A controlled live fetch by the owner is still required before any automation. The stored files are internal; exports for spreadsheets go through the datasets export rules.
- **Decided by:** the owner's instruction of 2026-10-23 (free sources only, complete the loop); implementation under review.

## Open decisions
| # | Decision | Needed before |
|---|----------|---------------|
| D1 | Primary source (Phase 0 outcome) | Phase 3. Chosen: official results portal API (`back.results.asiangames2026.org`), decodable without a key, event-level data confirmed (`SOURCE_DISCOVERY.md` section 10). **Cleared by the owner on 2026-10-07** (ADR-031); manual CSV import stays as the fallback |
| D2 | Where data lives between scheduled runs | Resolved by ADR-016 |
| D3 | Public or private dashboard | Phase 5 |
| D4 | Include LLM explainer | Phase 7 (constraints fixed by ADR-024) |
