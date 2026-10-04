# Database Design

**PostgreSQL 16+ from day one** (ADR-015). SQLite and MongoDB are not used anywhere, including tests. The DDL below is the logical design; the Alembic migration creates the tables in dependency order. Terminology follows `DOMAIN_MODEL.md`: **event**, **placing**, **entrant**, **country medal**.

Conventions: `BIGINT GENERATED ALWAYS AS IDENTITY` keys, `TIMESTAMPTZ` for every timestamp (stored in UTC), `JSONB` for raw payload fragments, constraints in the database (not only in code), no ORM-specific tricks that block moving between Postgres hosts. Moving host means changing `DATABASE_URL`.

## 1. Entity relationships
```
competitions 1--* events *--1 disciplines *--1 sports
events 1--* placings *--1 countries
placings *--0..1 entrants *--1 countries       (composite FK keeps entrant country = placing country)
placings *--1 raw_versions (traceability)
raw_documents 1--* raw_versions 1--* raw_fetches ;  raw_versions 1--0..1 raw_blobs
events 1--* source_observations 1--* source_conflicts
placings 1--* placing_history
competitions 1--* analytics_snapshots 1--* snapshot_rows
```

## 2. Reference and event tables (logical DDL)
```sql
CREATE TABLE competitions (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code          TEXT UNIQUE NOT NULL,      -- e.g. 'asiad-2026'
  name          TEXT NOT NULL,
  timezone      TEXT NOT NULL,             -- IANA name, used for "one daily snapshot per day"
  start_date    DATE, end_date DATE,
  official_event_total INTEGER             -- verified in Phase 0
);

CREATE TABLE countries (
  id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code      TEXT UNIQUE NOT NULL,          -- official 3-letter NOC code
  name      TEXT NOT NULL,
  region    TEXT
);
CREATE TABLE country_aliases (
  alias_norm TEXT PRIMARY KEY,             -- normalised alias
  country_id BIGINT NOT NULL REFERENCES countries(id)
);

CREATE TABLE sports (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  double_bronze BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE disciplines (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sport_id BIGINT NOT NULL REFERENCES sports(id),
  name TEXT NOT NULL,
  UNIQUE (sport_id, name)
);
CREATE TABLE sport_aliases (
  alias_norm TEXT PRIMARY KEY,
  sport_id BIGINT NOT NULL REFERENCES sports(id),
  discipline_id BIGINT REFERENCES disciplines(id)
);

CREATE TABLE events (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id  BIGINT NOT NULL REFERENCES competitions(id),
  discipline_id   BIGINT NOT NULL REFERENCES disciplines(id),
  name            TEXT NOT NULL,           -- cleaned
  name_raw        TEXT NOT NULL,
  gender          TEXT NOT NULL CHECK (gender IN ('Men','Women','Mixed','Open')),
  participation   TEXT NOT NULL CHECK (participation IN ('Individual','Team','Pair')),
  event_date      DATE,
  status          TEXT NOT NULL CHECK (status IN ('scheduled','in_progress','completed','amended')),
  is_disputed     BOOLEAN NOT NULL DEFAULT FALSE,   -- set by the conflict policy, cleared on resolution
  external_key    TEXT,                    -- source's event id when available
  UNIQUE (competition_id, discipline_id, name, gender)
);

CREATE TABLE entrants (                     -- the athlete, pair or team on the podium
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id BIGINT NOT NULL REFERENCES competitions(id),
  country_id     BIGINT NOT NULL REFERENCES countries(id),
  kind           TEXT NOT NULL CHECK (kind IN ('Athlete','Pair','Team')),
  name           TEXT NOT NULL,
  external_key   TEXT,
  UNIQUE (competition_id, country_id, kind, name),
  UNIQUE (id, country_id)                  -- target of the composite FK from placings
);
```

## 3. Placings (current state with full version history)
```sql
CREATE TABLE placings (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id       BIGINT NOT NULL REFERENCES events(id),
  medal          TEXT NOT NULL CHECK (medal IN ('Gold','Silver','Bronze')),
  slot           INTEGER NOT NULL DEFAULT 1 CHECK (slot >= 1),  -- 1, or 2 for double bronze / ties
  country_id     BIGINT NOT NULL REFERENCES countries(id),
  entrant_id     BIGINT,                              -- may be unknown
  is_tie         BOOLEAN NOT NULL DEFAULT FALSE,
  valid_from     TIMESTAMPTZ NOT NULL,
  valid_to       TIMESTAMPTZ,
  is_current     BOOLEAN NOT NULL DEFAULT TRUE,
  supersedes_id  BIGINT REFERENCES placings(id),
  raw_version_id BIGINT REFERENCES raw_versions(id),  -- NULL only for manual rows that carry a source note
  source         TEXT NOT NULL,
  source_note    TEXT,                                -- manual imports: source URL / note
  CHECK ((is_current AND valid_to IS NULL) OR (NOT is_current AND valid_to IS NOT NULL)),
  -- Invariant I1 in the database: if an entrant is set, its country must equal the placing's country.
  -- MATCH SIMPLE skips the check when entrant_id is NULL.
  FOREIGN KEY (entrant_id, country_id) REFERENCES entrants (id, country_id)
);

-- Uniqueness applies to CURRENT rows only, so closed versions never collide with the new one.
CREATE UNIQUE INDEX ux_placings_current ON placings (event_id, medal, slot) WHERE is_current;

CREATE TABLE placing_history (               -- append-only audit trail
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id       BIGINT NOT NULL REFERENCES events(id),
  medal          TEXT NOT NULL,
  slot           INTEGER NOT NULL,
  old_placing_id BIGINT REFERENCES placings(id),
  new_placing_id BIGINT REFERENCES placings(id),
  old_country_id BIGINT REFERENCES countries(id),
  new_country_id BIGINT REFERENCES countries(id),
  change_type    TEXT NOT NULL CHECK (change_type IN ('created','reallocated','corrected','removed','restored')),
  reason         TEXT,
  source         TEXT,
  run_id         BIGINT REFERENCES ingest_runs(id),
  changed_at     TIMESTAMPTZ NOT NULL
);
```

## 4. Raw documents, versions and fetches
```sql
CREATE TABLE ingest_runs (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id BIGINT REFERENCES competitions(id),
  started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
  status TEXT NOT NULL CHECK (status IN ('running','success','failed')),
  docs_fetched INT, docs_changed INT, rows_loaded INT,
  rows_quarantined INT, error_summary TEXT
);

CREATE TABLE raw_documents (                 -- one logical page or feed
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source TEXT NOT NULL, url TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  latest_version_id BIGINT,                  -- FK added after raw_versions exists (deferrable)
  UNIQUE (source, url)
);
CREATE TABLE raw_versions (                  -- one distinct content of a document
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES raw_documents(id),
  version_no INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  storage_backend TEXT NOT NULL CHECK (storage_backend IN ('db','fs','s3')),
  storage_key TEXT,                          -- path or object key for fs / s3; NULL for db
  size_bytes INTEGER, content_type TEXT,
  first_fetched_at TIMESTAMPTZ NOT NULL,
  UNIQUE (document_id, sha256),
  UNIQUE (document_id, version_no)
);
ALTER TABLE raw_documents ADD CONSTRAINT fk_raw_documents_latest
  FOREIGN KEY (latest_version_id) REFERENCES raw_versions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE raw_blobs (                     -- used when storage_backend = 'db' (default for hosted runs)
  version_id BIGINT PRIMARY KEY REFERENCES raw_versions(id),
  content BYTEA NOT NULL,                    -- gzip-compressed bytes exactly as received
  compression TEXT NOT NULL DEFAULT 'gzip'
);
CREATE TABLE raw_fetches (                   -- every fetch attempt is logged
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES raw_documents(id),
  version_id  BIGINT REFERENCES raw_versions(id),   -- NULL on error
  run_id      BIGINT REFERENCES ingest_runs(id),
  fetched_at  TIMESTAMPTZ NOT NULL,
  http_status INT,
  outcome     TEXT NOT NULL CHECK (outcome IN ('new_version','unchanged','reverted','error')),
  error       TEXT
);
-- Insert order for a brand-new document: raw_documents (latest_version_id NULL) -> raw_versions -> update latest_version_id.
```
Raw bytes: the `raw_blobs` table keeps hosted deployments self-contained and transactional. Because identical content is stored once (hash dedup), the volume stays small. If it ever grows past the plan's storage budget, switch `RAW_STORE_BACKEND` to `s3` (any S3-compatible object store) or `fs`. `raw_versions` records which backend holds each version, so mixed history is fine.

## 5. Observations, conflicts, quarantine, reconciliation
```sql
CREATE TABLE source_observations (           -- what each source claimed (evidence, not truth)
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES events(id),
  medal TEXT NOT NULL, slot INTEGER NOT NULL,
  country_id BIGINT NOT NULL REFERENCES countries(id),
  entrant_name TEXT,
  source TEXT NOT NULL,
  raw_version_id BIGINT REFERENCES raw_versions(id),
  observed_at TIMESTAMPTZ NOT NULL,          -- when we fetched it
  source_updated_at TIMESTAMPTZ,             -- when the source says it last changed, if provided
  UNIQUE NULLS NOT DISTINCT (event_id, medal, slot, source, raw_version_id)
);
CREATE TABLE source_conflicts (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id BIGINT NOT NULL REFERENCES events(id),
  medal TEXT NOT NULL, slot INTEGER NOT NULL,
  observation_a_id BIGINT NOT NULL REFERENCES source_observations(id),
  observation_b_id BIGINT NOT NULL REFERENCES source_observations(id),
  policy_rule TEXT NOT NULL,                 -- which row of the policy table applied
  status TEXT NOT NULL CHECK (status IN ('auto_resolved','needs_review','resolved','dismissed')),
  resolution_note TEXT, resolved_by TEXT,
  created_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ
);
CREATE TABLE quarantine (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id BIGINT REFERENCES ingest_runs(id),
  raw_version_id BIGINT REFERENCES raw_versions(id),
  reason TEXT NOT NULL, payload JSONB, created_at TIMESTAMPTZ NOT NULL,
  resolved BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE reconciliation_results (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id BIGINT REFERENCES ingest_runs(id), country_id BIGINT REFERENCES countries(id),
  gold_db INT, silver_db INT, bronze_db INT,
  gold_official INT, silver_official INT, bronze_official INT,
  matches BOOLEAN, note TEXT
);
```

## 6. Reallocation and correction procedure (one transaction)
Example: Event X, Gold slot 1 changes from Country A to Country B.
1. Find the current placing `P_old` for (event, medal, slot).
2. Set `P_old.is_current = FALSE` and `P_old.valid_to = now`.
3. Insert `P_new` with `is_current = TRUE`, `valid_from = now`, `supersedes_id = P_old.id`, the new country, entrant, source and raw version.
4. Insert a `placing_history` row: `change_type = 'reallocated'`, old and new country and placing ids, reason, source, run.
5. Re-evaluate the event's `is_disputed` flag and status (`amended`).

Variants: same country but corrected entrant name gives `corrected`. A placing withdrawn with no replacement closes `P_old` and records `removed`. A removed placing that returns inserts a new version and records `restored`.

Why this satisfies uniqueness: steps 2 and 3 run in one transaction and the unique index covers only current rows, so the closed version and the new version never collide. The audit trail is complete: no row is ever deleted or overwritten. Replaying the same input finds `P_new` already current and does nothing (idempotent). Concurrent writers are prevented by the pipeline advisory lock (section 11).

This procedure has explicit tests (`TESTING.md`, cases 5, 21).

## 7. Raw version semantics
| Situation | Result |
|-----------|--------|
| Same URL, same hash as the latest version | No new version. Log fetch `unchanged`. No re-parse |
| Same URL, a hash never seen before | New version (`version_no + 1`), store bytes, update `latest_version_id`, log `new_version`, parse |
| Same URL, hash equals an older version (content reverted) | No new version or bytes. Set `latest_version_id` to that older version, log `reverted`, re-parse so the database follows |
| Fetch error | Log `error`. No version change |
`(document_id, sha256)` is unique, so identical content is never stored twice. The full rules are in `DATA_PIPELINE.md` section 4.

## 8. Analytics tables and snapshot identity
```sql
CREATE TABLE analytics_snapshots (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id BIGINT NOT NULL REFERENCES competitions(id),
  created_at TIMESTAMPTZ NOT NULL,
  as_of TIMESTAMPTZ NOT NULL,                -- data time the snapshot describes
  local_date DATE NOT NULL,                  -- as_of in the competition's timezone
  run_id BIGINT REFERENCES ingest_runs(id),
  kind TEXT NOT NULL CHECK (kind IN ('change','daily')),
  data_fingerprint TEXT NOT NULL,            -- SHA-256, see below
  analytics_version TEXT NOT NULL,           -- version of ANALYTICS_SPEC used
  events_completed INT, events_total INT, disputed_events INT, note TEXT
);
CREATE INDEX ix_snapshots_fingerprint ON analytics_snapshots (competition_id, data_fingerprint);
CREATE UNIQUE INDEX ux_snapshots_daily ON analytics_snapshots (competition_id, local_date) WHERE kind = 'daily';

CREATE TABLE snapshot_rows (                 -- Country x Sport x Gender grain
  snapshot_id BIGINT NOT NULL REFERENCES analytics_snapshots(id),
  country_id BIGINT NOT NULL REFERENCES countries(id),
  sport_id BIGINT NOT NULL REFERENCES sports(id),
  discipline_id BIGINT NOT NULL REFERENCES disciplines(id),
  gender TEXT NOT NULL,
  gold INT NOT NULL, silver INT NOT NULL, bronze INT NOT NULL, total INT NOT NULL, points_321 INT NOT NULL,
  PRIMARY KEY (snapshot_id, country_id, sport_id, discipline_id, gender)
);
CREATE TABLE snapshot_changes (
  snapshot_id BIGINT NOT NULL REFERENCES analytics_snapshots(id),
  change_type TEXT NOT NULL, country_id BIGINT, sport_id BIGINT, gender TEXT, detail TEXT
);
```
Identity rules:
- **Fingerprint** = SHA-256 of a canonical text of all current placings sorted by (event, medal, slot) with their country, plus every event's status and `is_disputed` flag.
- A `change` snapshot is written only when the fingerprint differs from the latest `change` snapshot of the same competition, or `analytics_version` changed.
- One `daily` snapshot per competition-local day, even if nothing changed, so trend charts have a regular series (enforced by `ux_snapshots_daily`).
- Snapshots are immutable. Rebuilding from raw data must reproduce the same fingerprints.
- Change detection compares a snapshot with the previous `change` snapshot.

## 9. Core view (the analytical base: country medals)
```sql
CREATE VIEW v_medal_facts AS
SELECT p.id AS placing_id, p.event_id, e.competition_id, p.medal, p.slot, p.is_tie,
       c.code AS country_code, c.name AS country,
       s.name AS sport, d.name AS discipline,
       e.gender, e.participation, e.name AS event, e.event_date, e.is_disputed,
       en.name AS entrant
FROM placings p
JOIN events e      ON e.id = p.event_id
JOIN disciplines d ON d.id = e.discipline_id
JOIN sports s      ON s.id = d.sport_id
JOIN countries c   ON c.id = p.country_id
LEFT JOIN entrants en ON en.id = p.entrant_id
WHERE p.is_current;
```
One row per country medal. All analytics aggregate from `v_medal_facts`; `ANALYTICS_SPEC.md` metrics are implemented on top of it. If a metric gets slow, turn the aggregate into a materialized view refreshed after each load (see `SCALABILITY.md`).

## 10. Indexes
- `placings(event_id)`, `placings(country_id, medal)`, plus `ux_placings_current`
- `events(discipline_id, gender)`, `events(competition_id, status)`
- `raw_versions(sha256)`, `raw_fetches(document_id, fetched_at)`
- `source_observations(event_id, medal, slot)`
- `snapshot_rows(snapshot_id, country_id)`

## 11. Rules
- Foreign keys are always enforced (Postgres default).
- A load runs in a single transaction, and the pipeline holds a session-level advisory lock (`pg_try_advisory_lock`, fixed key) for the whole run. A second run that cannot get the lock exits cleanly. This replaces a lock file and works across machines.
- Ingestion is idempotent: upserts key on the unique constraints and on `ux_placings_current`.
- Rows are never physically deleted. Corrections close a placing version and write `placing_history`.
- Connections: TLS required (`sslmode=require`). Batch jobs use `NullPool` and `pool_pre_ping`, because serverless Postgres can suspend idle compute.
- Migrations: Alembic only, named `NNN_description` (for example `001_initial_schema`). Each migration has a `downgrade` where possible. Never edit the schema by hand.
- Backups: `pg_dump` nightly to a location outside the database host, plus a restore test once per phase (`DEPLOYMENT.md`). Free database plans often have very short point-in-time recovery or none, so our own dumps are mandatory.

## 12. Moving between hosts
Postgres to Postgres: take a `pg_dump`, restore on the new host, change `DATABASE_URL`, run `alembic current` to confirm. No code change. Extensions are not required.

## 13. Roles and schemas
- Schema `public`: all operational and core tables (written only by the pipeline role).
- Schema `reporting`: views the dashboard and any future API may read: `v_medal_facts` (exposed as `reporting.medal_facts`), snapshot tables through views, and reconciliation summaries. No raw data, quarantine contents or run logs.
- Roles: `sie_owner` (migrations), `sie_pipeline` (read and write `public`), `sie_reader` (`SELECT` on `reporting` only). The dashboard connects as `sie_reader`. A dashboard bug or a leaked dashboard credential cannot modify data or read operational tables.
- Tests check these permissions (`TESTING.md`).

## 14. Test databases
Tests run against a real PostgreSQL 16 (CI service container; locally Docker or `pgserver`). A template database is built once per session by running the Alembic migrations (so migrations are tested every time). Every test gets its own fresh copy of that template, so tests are fully isolated and need no cleanup. Migration tests start from an empty database instead.
