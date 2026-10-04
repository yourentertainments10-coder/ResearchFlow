-- Sports Intelligence Engine: migration 002 (PostgreSQL 15+)
-- Competition scoping for runs and snapshots, daily-snapshot uniqueness, and the read-only
-- "reporting" schema. Quoted in docs/DATABASE.md. Marker lines "-- [[name]]" delimit the sections.
--
-- Existing rows (none in a fresh install) are backfilled when there is exactly one competition.
-- With several competitions and existing rows the NOT NULL step fails loudly and the whole
-- migration rolls back: decide the owner of those rows by hand, then re-run.

-- [[competition_scope]]
ALTER TABLE ingest_runs ADD COLUMN competition_id BIGINT REFERENCES competitions(id);
UPDATE ingest_runs SET competition_id = (SELECT id FROM competitions)
 WHERE competition_id IS NULL AND (SELECT count(*) FROM competitions) = 1;
ALTER TABLE ingest_runs ALTER COLUMN competition_id SET NOT NULL;

ALTER TABLE analytics_snapshots
  ADD COLUMN competition_id BIGINT REFERENCES competitions(id),
  ADD COLUMN local_date     DATE;                         -- as_of in the competition's timezone
UPDATE analytics_snapshots SET competition_id = (SELECT id FROM competitions)
 WHERE competition_id IS NULL AND (SELECT count(*) FROM competitions) = 1;
UPDATE analytics_snapshots s SET local_date = (s.as_of AT TIME ZONE c.timezone)::date
  FROM competitions c WHERE c.id = s.competition_id AND s.local_date IS NULL;
ALTER TABLE analytics_snapshots
  ALTER COLUMN competition_id SET NOT NULL,
  ALTER COLUMN local_date     SET NOT NULL;

DROP INDEX ix_snapshots_fingerprint;
CREATE INDEX ix_snapshots_comp_fingerprint ON analytics_snapshots (competition_id, data_fingerprint);
CREATE INDEX ix_snapshots_comp_time        ON analytics_snapshots (competition_id, as_of DESC);
-- One daily snapshot per competition-local day (docs/DATABASE.md section 8).
CREATE UNIQUE INDEX ux_snapshots_daily ON analytics_snapshots (competition_id, local_date) WHERE kind = 'daily';

-- Country-medal view gains competition_id. New columns may only be appended to a replaced view.
CREATE OR REPLACE VIEW v_medal_facts AS
SELECT p.id AS placing_id, p.event_id, p.medal, p.slot, p.is_tie,
       c.code AS country_code, c.name AS country,
       s.name AS sport, d.name AS discipline,
       e.gender, e.participation, e.name AS event, e.event_date, e.is_disputed,
       en.name AS entrant,
       e.competition_id
FROM placings p
JOIN events e      ON e.id = p.event_id
JOIN disciplines d ON d.id = e.discipline_id
JOIN sports s      ON s.id = d.sport_id
JOIN countries c   ON c.id = p.country_id
LEFT JOIN entrants en ON en.id = p.entrant_id
WHERE p.is_current;

-- [[reporting]]
-- Everything a dashboard or API may read. No raw data, quarantine contents, run logs or paths.
-- Views run with their owner's rights, so the read-only role needs no access to public tables.
CREATE SCHEMA reporting;

CREATE VIEW reporting.medal_facts AS
SELECT competition_id, event_id, placing_id, medal, slot, is_tie, country_code, country,
       sport, discipline, gender, participation, event, event_date, is_disputed, entrant
FROM public.v_medal_facts;

CREATE VIEW reporting.events AS
SELECT e.id AS event_id, e.competition_id, s.name AS sport, d.name AS discipline,
       e.name AS event, e.gender, e.participation, e.event_date, e.status, e.is_disputed
FROM public.events e
JOIN public.disciplines d ON d.id = e.discipline_id
JOIN public.sports s      ON s.id = d.sport_id;

CREATE VIEW reporting.snapshots AS
SELECT id AS snapshot_id, competition_id, kind, as_of, local_date, analytics_version,
       events_completed, events_total, disputed_events
FROM public.analytics_snapshots;

CREATE VIEW reporting.snapshot_rows AS
SELECT r.snapshot_id, s.competition_id, c.code AS country_code, c.name AS country,
       sp.name AS sport, d.name AS discipline, r.gender,
       r.gold, r.silver, r.bronze, r.total, r.points_321
FROM public.snapshot_rows r
JOIN public.analytics_snapshots s ON s.id = r.snapshot_id
JOIN public.countries c           ON c.id = r.country_id
JOIN public.sports sp             ON sp.id = r.sport_id
JOIN public.disciplines d         ON d.id = r.discipline_id;
