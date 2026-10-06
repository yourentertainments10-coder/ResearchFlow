-- Migration 004: the date a medal was decided and the country label as the source wrote it, both kept
-- on the placing (ADR-025, ADR-026).
-- Medal-timeline analytics need the day each medal was awarded. events.event_date holds one date per
-- event (the latest one), which is wrong for the 55 events whose medals are decided on different days.

-- [[placing_provenance]]
ALTER TABLE placings ADD COLUMN result_date DATE;       -- NULL when the source gives no per-medal date
ALTER TABLE placings ADD COLUMN source_country TEXT;     -- provenance only: 'Republic of Korea', never compared

-- Views may only gain columns at the end.
CREATE OR REPLACE VIEW v_medal_facts AS
SELECT p.id AS placing_id, p.event_id, p.medal, p.slot, p.is_tie,
       c.code AS country_code, c.name AS country,
       s.name AS sport, d.name AS discipline,
       e.gender, e.participation, e.name AS event, e.event_date, e.is_disputed,
       en.name AS entrant,
       e.competition_id,
       p.result_date, p.source_country
FROM placings p
JOIN events e      ON e.id = p.event_id
JOIN disciplines d ON d.id = e.discipline_id
JOIN sports s      ON s.id = d.sport_id
JOIN countries c   ON c.id = p.country_id
LEFT JOIN entrants en ON en.id = p.entrant_id
WHERE p.is_current;

CREATE OR REPLACE VIEW reporting.medal_facts AS
SELECT competition_id, event_id, placing_id, medal, slot, is_tie, country_code, country,
       sport, discipline, gender, participation, event, event_date, is_disputed, entrant,
       result_date, source_country
FROM public.v_medal_facts;
