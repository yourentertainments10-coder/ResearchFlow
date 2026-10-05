-- Migration 003: raw bytes inside the database, and a per-placing source note.
-- Brings the schema in line with docs/DATABASE.md sections 3 and 4 and ADR-016 (hosted runs keep
-- raw bytes in the database; the scheduled runner has no persistent disk).

-- [[raw_storage]]
ALTER TABLE raw_versions
  ADD COLUMN storage_backend TEXT NOT NULL DEFAULT 'fs' CHECK (storage_backend IN ('db','fs','s3')),
  ALTER COLUMN path DROP NOT NULL,                      -- the file path or object key; NULL for 'db'
  ADD CONSTRAINT ck_raw_versions_location CHECK (storage_backend = 'db' OR path IS NOT NULL);

CREATE TABLE raw_blobs (                                -- used when storage_backend = 'db'
  version_id  BIGINT PRIMARY KEY REFERENCES raw_versions(id),
  content     BYTEA NOT NULL,                           -- gzip-compressed bytes exactly as received
  compression TEXT NOT NULL DEFAULT 'gzip' CHECK (compression IN ('gzip'))
);

-- [[source_note]]
ALTER TABLE placings ADD COLUMN source_note TEXT;       -- manual imports: source URL or note, per row

ALTER TABLE ingest_runs ADD COLUMN source TEXT;         -- what was run: 'official', 'manual', ...
