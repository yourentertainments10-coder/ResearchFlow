-- Migration 005: evidence ledger (ADR-042, docs/EVIDENCE_LEDGER_DESIGN.md).
-- ADDITIVE ONLY: new tables, indexes and one view. No existing table, column, constraint, index or
-- view is altered, so the sports pipeline and the dashboard behave exactly as before.
--
-- Lock note (measured on the real 001-004 schema, docs/EVIDENCE_LEDGER_DESIGN.md section 10): creating
-- tables whose foreign keys point at raw_documents and raw_versions takes SHARE ROW EXCLUSIVE on those
-- two tables only. Reads continue; writes to them wait. The new tables are empty, so the window is
-- milliseconds. The timeouts below make a collision fail fast and roll back atomically instead of
-- queuing writers behind the DDL. They are SET LOCAL: they end with this migration's transaction.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- [[projects]]
CREATE TABLE projects (                                  -- the ownership boundary for evidence
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name        TEXT NOT NULL,
  owner_ref   TEXT,                                      -- reserved for authentication (Phase 6); unused now
  created_at  TIMESTAMPTZ NOT NULL,
  archived_at TIMESTAMPTZ,                               -- soft stop: no new evidence
  purged_at   TIMESTAMPTZ                                -- set by `sie project purge`; the row stays as a tombstone
);

CREATE TABLE purge_tombstones (                          -- what a purge did, and what it could not undo
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id             BIGINT NOT NULL REFERENCES projects(id),
  purged_at              TIMESTAMPTZ NOT NULL,
  purged_by              TEXT NOT NULL,
  row_counts             JSONB NOT NULL,
  backup_retention_days  INTEGER NOT NULL CHECK (backup_retention_days >= 0),
  db_history_window_hours INTEGER CHECK (db_history_window_hours >= 0),  -- NULL: not checked by the owner
  db_history_verified    BOOLEAN NOT NULL DEFAULT false,
  note                   TEXT NOT NULL
);

-- [[project_documents]]
CREATE TABLE project_documents (                         -- which raw documents belong to which project
  project_id       BIGINT NOT NULL REFERENCES projects(id),
  raw_document_id  BIGINT NOT NULL REFERENCES raw_documents(id),
  role             TEXT NOT NULL CHECK (role IN ('upload','fetched','dataset_input')),
  original_filename TEXT,                                -- as the user named it; display only, never a path
  claimed_media_type TEXT,                               -- what the client said; never trusted
  added_at         TIMESTAMPTZ NOT NULL,
  added_by         TEXT,
  PRIMARY KEY (project_id, raw_document_id)
);

-- [[evidence_sources]]
CREATE TABLE evidence_sources (                          -- one citable source: one distinct content, as seen by one project
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id     BIGINT NOT NULL REFERENCES projects(id),
  kind           TEXT NOT NULL CHECK (kind IN ('upload','web')),
  retention_mode TEXT NOT NULL CHECK (retention_mode IN ('full','excerpt','metadata')),
  raw_version_id BIGINT REFERENCES raw_versions(id),     -- set exactly when the bytes are kept ('full')
  content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),   -- of the bytes as received
  size_bytes     BIGINT NOT NULL CHECK (size_bytes >= 0),
  title          TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
  requested_url  TEXT,
  final_url      TEXT,                                   -- after redirects
  http_status    INTEGER,
  content_type   TEXT,
  tier           INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 5),   -- docs/PRODUCT_VISION.md section 6; metadata, not reliability
  terms_ref      TEXT,                                   -- the recorded policy that permits keeping a web page in full
  retrieved_at   TIMESTAMPTZ NOT NULL,                   -- when this content was first obtained
  retain_until   TIMESTAMPTZ,                            -- full web copies only; NULL means until purge
  pruned_at      TIMESTAMPTZ,                            -- set by `sie evidence prune`, which drops the bytes
  created_at     TIMESTAMPTZ NOT NULL,
  UNIQUE (id, project_id),
  CHECK ((retention_mode = 'full') = (raw_version_id IS NOT NULL)),
  CHECK (kind <> 'upload' OR retention_mode = 'full'),
  CHECK (kind <> 'web' OR requested_url IS NOT NULL),
  CHECK (NOT (kind = 'web' AND retention_mode = 'full') OR (terms_ref IS NOT NULL AND retain_until IS NOT NULL)),
  CHECK (pruned_at IS NULL OR retention_mode = 'metadata')
);
CREATE UNIQUE INDEX ux_evidence_sources_version ON evidence_sources (project_id, raw_version_id)
  WHERE raw_version_id IS NOT NULL;
CREATE UNIQUE INDEX ux_evidence_sources_content ON evidence_sources (project_id, requested_url, content_sha256)
  WHERE raw_version_id IS NULL;

-- [[claims]]
CREATE TABLE claims (                                    -- one extracted claim; immutable
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id        BIGINT NOT NULL REFERENCES projects(id),
  source_id         BIGINT NOT NULL,
  subject           TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 300),
  metric            TEXT NOT NULL CHECK (char_length(metric) BETWEEN 1 AND 300),
  period            TEXT,
  unit              TEXT,
  value_text        TEXT NOT NULL CHECK (char_length(value_text) BETWEEN 1 AND 1000),
  value_numeric     NUMERIC,
  value_source      TEXT NOT NULL DEFAULT 'literal' CHECK (value_source IN ('literal','cached_formula_value','excerpt')),
  locator_kind      TEXT NOT NULL CHECK (locator_kind IN ('table_cell','json_pointer','xlsx_cell','char_range','page','none')),
  locator           JSONB NOT NULL,
  excerpt           TEXT NOT NULL CHECK (char_length(excerpt) BETWEEN 1 AND 500),
  excerpt_sha256    TEXT NOT NULL CHECK (excerpt_sha256 ~ '^[0-9a-f]{64}$'),
  extraction_method TEXT NOT NULL CHECK (extraction_method IN ('manual','rule','model')),
  extractor_version TEXT NOT NULL,
  claim_hash        TEXT NOT NULL CHECK (claim_hash ~ '^[0-9a-f]{64}$'),
  limitations       TEXT[] NOT NULL DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL,
  created_by        TEXT,
  UNIQUE (id, project_id),
  UNIQUE (project_id, source_id, claim_hash),            -- re-extracting the same claim is a no-op
  FOREIGN KEY (source_id, project_id) REFERENCES evidence_sources(id, project_id)
);

CREATE TABLE claim_reviews (                             -- append-only status history of a claim
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id       BIGINT NOT NULL REFERENCES projects(id),
  claim_id         BIGINT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('unverified','source_located','human_confirmed','disputed','rejected')),
  reason           TEXT NOT NULL,
  actor            TEXT NOT NULL,                        -- 'system:<component>' or a named person
  verifier_version TEXT,
  checks           JSONB,
  at               TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (claim_id, project_id) REFERENCES claims(id, project_id),
  -- Neither status is a truth claim (docs/EVIDENCE_LEDGER_DESIGN.md section 6). source_located is set only
  -- by a verifier, human_confirmed only by a named person.
  CHECK (status <> 'source_located' OR left(actor, 7) = 'system:'),
  CHECK (status <> 'human_confirmed' OR left(actor, 7) <> 'system:')
);

CREATE TABLE claim_conflicts (                           -- two claims in one project that disagree
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  project_id  BIGINT NOT NULL REFERENCES projects(id),
  claim_a_id  BIGINT NOT NULL,
  claim_b_id  BIGINT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('value_differs','period_differs','scope_differs')),
  status      TEXT NOT NULL CHECK (status IN ('open','explained','resolved_a','resolved_b','dismissed')),
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ,
  resolved_by TEXT,
  UNIQUE (claim_a_id, claim_b_id),
  CHECK (claim_a_id < claim_b_id),
  CHECK ((status = 'open') = (resolved_at IS NULL)),
  FOREIGN KEY (claim_a_id, project_id) REFERENCES claims(id, project_id),
  FOREIGN KEY (claim_b_id, project_id) REFERENCES claims(id, project_id)
);

-- The latest review decides the current status; a claim with no review is unverified.
CREATE VIEW evidence_claims_current AS
SELECT c.id AS claim_id, c.project_id, coalesce(r.status, 'unverified') AS status, r.id AS review_id
FROM claims c
LEFT JOIN LATERAL (
  SELECT id, status FROM claim_reviews WHERE claim_id = c.id ORDER BY id DESC LIMIT 1
) r ON true;

-- [[evidence_indexes]]
CREATE INDEX ix_project_documents_doc     ON project_documents (raw_document_id);
CREATE INDEX ix_evidence_sources_project  ON evidence_sources (project_id, kind);
CREATE INDEX ix_evidence_sources_retain   ON evidence_sources (retain_until)
  WHERE retain_until IS NOT NULL AND pruned_at IS NULL;
CREATE INDEX ix_claims_project_source     ON claims (project_id, source_id);
CREATE INDEX ix_claims_subject_metric     ON claims (project_id, subject, metric, period);
CREATE INDEX ix_claim_reviews_claim       ON claim_reviews (claim_id, id DESC);
CREATE INDEX ix_claim_conflicts_open      ON claim_conflicts (project_id) WHERE status = 'open';
