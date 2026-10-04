-- Sports Intelligence Engine: initial schema (PostgreSQL 15+)
-- Source of truth for docs/DATABASE.md. Terminology: docs/DOMAIN_MODEL.md.
-- Marker lines "-- [[name]]" delimit sections that DATABASE.md quotes.

-- [[reference]]
CREATE TABLE competitions (
  id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code                 TEXT NOT NULL UNIQUE,            -- e.g. 'asiad-2026'
  name                 TEXT NOT NULL,
  timezone             TEXT NOT NULL,                   -- IANA name; defines the "day" for daily snapshots
  start_date           DATE,
  end_date             DATE,
  official_event_total INTEGER                          -- verified in Phase 0
);

CREATE TABLE countries (
  id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code   TEXT NOT NULL UNIQUE,                          -- official 3-letter NOC code
  name   TEXT NOT NULL,
  region TEXT
);
CREATE TABLE country_aliases (
  alias_norm TEXT PRIMARY KEY,                          -- normalised alias
  country_id BIGINT NOT NULL REFERENCES countries(id)
);

CREATE TABLE sports (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  double_bronze BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE disciplines (
  id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sport_id BIGINT NOT NULL REFERENCES sports(id),
  name     TEXT NOT NULL,
  UNIQUE (sport_id, name)
);
CREATE TABLE sport_aliases (
  alias_norm    TEXT PRIMARY KEY,
  sport_id      BIGINT NOT NULL REFERENCES sports(id),
  discipline_id BIGINT REFERENCES disciplines(id)
);

-- [[events]]
CREATE TABLE events (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id BIGINT NOT NULL REFERENCES competitions(id),
  discipline_id  BIGINT NOT NULL REFERENCES disciplines(id),
  name           TEXT NOT NULL,                         -- cleaned
  name_raw       TEXT NOT NULL,
  gender         TEXT NOT NULL CHECK (gender IN ('Men','Women','Mixed','Open')),
  participation  TEXT NOT NULL CHECK (participation IN ('Individual','Team','Pair')),
  event_date     DATE,
  status         TEXT NOT NULL CHECK (status IN ('scheduled','in_progress','completed','amended')),
  is_disputed    BOOLEAN NOT NULL DEFAULT false,        -- set by the conflict policy, cleared on resolution
  external_key   TEXT,                                  -- source's event id when available
  UNIQUE (competition_id, discipline_id, name, gender)
);

CREATE TABLE entrants (                                 -- the athlete, pair or team on the podium
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  competition_id BIGINT NOT NULL REFERENCES competitions(id),
  country_id     BIGINT NOT NULL REFERENCES countries(id),
  kind           TEXT NOT NULL CHECK (kind IN ('Athlete','Pair','Team')),
  name           TEXT NOT NULL,
  external_key   TEXT,
  UNIQUE (competition_id, country_id, kind, name),
  UNIQUE (id, country_id)                               -- target of the placings composite FK (invariant I1)
);

-- [[operational]]
CREATE TABLE ingest_runs (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at       TIMESTAMPTZ NOT NULL,
  finished_at      TIMESTAMPTZ,
  status           TEXT NOT NULL CHECK (status IN ('running','success','failed')),
  docs_fetched     INTEGER,
  docs_changed     INTEGER,
  rows_loaded      INTEGER,
  rows_quarantined INTEGER,
  error_summary    TEXT
);

CREATE TABLE raw_documents (                            -- one logical page or feed
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source            TEXT NOT NULL,
  url               TEXT NOT NULL,
  first_seen_at     TIMESTAMPTZ NOT NULL,
  latest_version_id BIGINT,                             -- FK added below (circular reference)
  UNIQUE (source, url)
);
CREATE TABLE raw_versions (                             -- one distinct content of a document
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id      BIGINT NOT NULL REFERENCES raw_documents(id),
  version_no       INTEGER NOT NULL CHECK (version_no >= 1),
  sha256           TEXT NOT NULL,
  path             TEXT NOT NULL,
  size_bytes       BIGINT,
  content_type     TEXT,
  first_fetched_at TIMESTAMPTZ NOT NULL,
  UNIQUE (document_id, sha256),
  UNIQUE (document_id, version_no)
);
ALTER TABLE raw_documents
  ADD CONSTRAINT fk_raw_documents_latest
  FOREIGN KEY (latest_version_id) REFERENCES raw_versions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE raw_fetches (                              -- every fetch attempt is logged
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id BIGINT NOT NULL REFERENCES raw_documents(id),
  version_id  BIGINT REFERENCES raw_versions(id),       -- NULL on error
  run_id      BIGINT REFERENCES ingest_runs(id),
  fetched_at  TIMESTAMPTZ NOT NULL,
  http_status INTEGER,
  outcome     TEXT NOT NULL CHECK (outcome IN ('new_version','unchanged','reverted','error')),
  error       TEXT
);

-- [[placings]]
CREATE TABLE placings (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id       BIGINT NOT NULL REFERENCES events(id),
  medal          TEXT NOT NULL CHECK (medal IN ('Gold','Silver','Bronze')),
  slot           INTEGER NOT NULL DEFAULT 1 CHECK (slot >= 1),   -- 1, or 2 for double bronze / ties
  country_id     BIGINT NOT NULL REFERENCES countries(id),
  entrant_id     BIGINT,                                          -- may be unknown
  is_tie         BOOLEAN NOT NULL DEFAULT false,
  valid_from     TIMESTAMPTZ NOT NULL,
  valid_to       TIMESTAMPTZ,
  is_current     BOOLEAN NOT NULL DEFAULT true,
  supersedes_id  BIGINT REFERENCES placings(id),
  raw_version_id BIGINT NOT NULL REFERENCES raw_versions(id),     -- manual CSVs are stored as raw documents too
  source         TEXT NOT NULL,
  CHECK ((is_current AND valid_to IS NULL) OR (NOT is_current AND valid_to IS NOT NULL)),
  -- Invariant I1: if an entrant is set, its country must equal the placing's country.
  -- MATCH SIMPLE: not enforced while entrant_id is NULL.
  FOREIGN KEY (entrant_id, country_id) REFERENCES entrants(id, country_id)
);
-- Uniqueness applies to CURRENT rows only, so closed versions never collide with the new one.
CREATE UNIQUE INDEX ux_placings_current ON placings (event_id, medal, slot) WHERE is_current;

CREATE TABLE placing_history (                          -- append-only audit trail
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

-- [[observations]]
CREATE TABLE source_observations (                      -- what each source claimed (evidence, not truth)
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id          BIGINT NOT NULL REFERENCES events(id),
  medal             TEXT NOT NULL CHECK (medal IN ('Gold','Silver','Bronze')),
  slot              INTEGER NOT NULL CHECK (slot >= 1),
  country_id        BIGINT NOT NULL REFERENCES countries(id),
  entrant_name      TEXT,
  source            TEXT NOT NULL,
  raw_version_id    BIGINT NOT NULL REFERENCES raw_versions(id),
  observed_at       TIMESTAMPTZ NOT NULL,               -- when we fetched it
  source_updated_at TIMESTAMPTZ,                        -- when the source says it last changed, if given
  UNIQUE (event_id, medal, slot, source, raw_version_id)
);
CREATE TABLE source_conflicts (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id         BIGINT NOT NULL REFERENCES events(id),
  medal            TEXT NOT NULL,
  slot             INTEGER NOT NULL,
  observation_a_id BIGINT NOT NULL REFERENCES source_observations(id),
  observation_b_id BIGINT NOT NULL REFERENCES source_observations(id),
  policy_rule      TEXT NOT NULL,                       -- which row of the policy table applied
  status           TEXT NOT NULL CHECK (status IN ('auto_resolved','needs_review','resolved','dismissed')),
  resolution_note  TEXT,
  resolved_by      TEXT,
  created_at       TIMESTAMPTZ NOT NULL,
  resolved_at      TIMESTAMPTZ
);
CREATE TABLE quarantine (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id         BIGINT REFERENCES ingest_runs(id),
  raw_version_id BIGINT REFERENCES raw_versions(id),
  reason         TEXT NOT NULL,
  payload_json   JSONB,
  created_at     TIMESTAMPTZ NOT NULL,
  resolved       BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE reconciliation_results (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        BIGINT REFERENCES ingest_runs(id),
  country_id    BIGINT NOT NULL REFERENCES countries(id),
  gold_db INTEGER, silver_db INTEGER, bronze_db INTEGER,
  gold_official INTEGER, silver_official INTEGER, bronze_official INTEGER,
  matches       BOOLEAN,
  note          TEXT
);

-- [[snapshots]]
CREATE TABLE analytics_snapshots (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at        TIMESTAMPTZ NOT NULL,
  as_of             TIMESTAMPTZ NOT NULL,               -- data time the snapshot describes
  run_id            BIGINT REFERENCES ingest_runs(id),
  kind              TEXT NOT NULL CHECK (kind IN ('change','daily')),
  data_fingerprint  TEXT NOT NULL,                      -- SHA-256 of canonical current state
  analytics_version TEXT NOT NULL,                      -- version of ANALYTICS_SPEC used
  events_completed  INTEGER,
  events_total      INTEGER,
  disputed_events   INTEGER,
  note              TEXT
);
CREATE INDEX ix_snapshots_fingerprint ON analytics_snapshots (data_fingerprint);

CREATE TABLE snapshot_rows (                            -- Country x Sport x Gender grain
  snapshot_id   BIGINT NOT NULL REFERENCES analytics_snapshots(id),
  country_id    BIGINT NOT NULL REFERENCES countries(id),
  sport_id      BIGINT NOT NULL REFERENCES sports(id),
  discipline_id BIGINT NOT NULL REFERENCES disciplines(id),
  gender        TEXT NOT NULL CHECK (gender IN ('Men','Women','Mixed','Open')),
  gold INTEGER NOT NULL, silver INTEGER NOT NULL, bronze INTEGER NOT NULL,
  total INTEGER NOT NULL, points_321 INTEGER NOT NULL,
  PRIMARY KEY (snapshot_id, country_id, sport_id, discipline_id, gender)
);
CREATE TABLE snapshot_changes (
  snapshot_id BIGINT NOT NULL REFERENCES analytics_snapshots(id),
  change_type TEXT NOT NULL,
  country_id  BIGINT REFERENCES countries(id),
  sport_id    BIGINT REFERENCES sports(id),
  gender      TEXT,
  detail      TEXT
);

-- [[view]]
CREATE VIEW v_medal_facts AS                            -- one row per country medal
SELECT p.id AS placing_id, p.event_id, p.medal, p.slot, p.is_tie,
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

-- [[indexes]]
CREATE INDEX ix_placings_event          ON placings (event_id);
CREATE INDEX ix_placings_country_medal  ON placings (country_id, medal) WHERE is_current;
CREATE INDEX ix_events_disc_gender      ON events (discipline_id, gender);
CREATE INDEX ix_events_comp_status      ON events (competition_id, status);
CREATE INDEX ix_raw_versions_sha        ON raw_versions (sha256);
CREATE INDEX ix_raw_fetches_doc_time    ON raw_fetches (document_id, fetched_at);
CREATE INDEX ix_observations_slot       ON source_observations (event_id, medal, slot);
CREATE INDEX ix_snapshot_rows_country   ON snapshot_rows (snapshot_id, country_id);
