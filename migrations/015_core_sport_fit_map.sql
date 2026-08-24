-- Migration 015: replaces core.sport.fit_sport / fit_sub_sport with core.sport_fit_map.
--
-- THE PROBLEM. Migration 003 (frozen DDL, transcribed as-is from the research document)
-- declares two columns on core.sport, fit_sport smallint NULL and fit_sub_sport smallint
-- NULL, with a table comment claiming they hold "the (sport, sub_sport) pair of the
-- official FIT profile". Neither claim survives contact with the batch that actually
-- implements the mapping (SPORT_CODE_BY_FIT_SPORT, src/garminator/transform/manual.py):
--
--   1. The seed in 003 never populates them. Every row of core.sport has fit_sport IS
--      NULL and fit_sub_sport IS NULL. The columns are dead on arrival.
--   2. The declared type is wrong regardless. It was VERIFIED EMPIRICALLY this session,
--      by running garmin_fit_sdk against a real FIT file, that the decoder resolves the
--      FIT ``sport`` and ``sub_sport`` fields to STRINGS ("cycling", "indoor_cycling", ...),
--      not integers. smallint can never hold what the decoder actually produces -- this
--      is not a seeding gap, it is a type that could not have worked even if seeded.
--
-- WHERE THE MAPPING ACTUALLY LIVES. SPORT_CODE_BY_FIT_SPORT in
-- src/garminator/transform/manual.py already carries this mapping correctly, keyed on
-- (sport, sub_sport) string pairs, with sub_sport = None meaning "fallback for this
-- sport when no more specific sub_sport matches". This migration mirrors that dict into
-- SQL so the correspondence is queryable and FK-checked in the database, not only
-- reachable by importing Python.
--
-- WHY A SEPARATE TABLE RATHER THAN FIXING THE TYPE OF THE TWO COLUMNS IN PLACE.
-- The mapping is MANY-to-one: three distinct (sport, sub_sport) pairs -- ('cycling',
-- 'indoor_cycling'), ('cycling', 'virtual_activity'), ('cycling', 'spin') -- all resolve
-- to the single sport_code 'virtual_cycling'. Two columns on core.sport (one row per
-- sport_code) cannot express a many-to-one correspondence: there is nowhere on the
-- 'virtual_cycling' row to put three different (sport, sub_sport) pairs at once. That is
-- the deeper reason the columns were unusable, not merely unseeded or mistyped -- a
-- retyped smallint pair on core.sport would still be structurally unable to hold this
-- mapping. A dedicated table, one row per (fit_sport, fit_sub_sport) pair referencing
-- sport_code, is the only shape that fits.
--
-- UNIQUENESS OF THE LOOKUP KEY. A plain UNIQUE (fit_sport, fit_sub_sport) does not work:
-- in PostgreSQL, two NULLs are distinct by default under a plain UNIQUE constraint, so
-- nothing would stop two separate fallback rows (fit_sub_sport IS NULL) for the same
-- fit_sport from coexisting, silently making the fallback lookup ambiguous. PostgreSQL
-- 16 adds UNIQUE NULLS NOT DISTINCT for exactly this: it treats NULL as an ordinary
-- value for uniqueness purposes, so at most one fallback row per fit_sport is enforced
-- by the database, not by convention.

CREATE TABLE core.sport_fit_map (
    fit_sport     text NOT NULL,
    fit_sub_sport text NULL,
    sport_code    text NOT NULL REFERENCES core.sport(sport_code),
    CONSTRAINT sport_fit_map_uq UNIQUE NULLS NOT DISTINCT (fit_sport, fit_sub_sport)
);

COMMENT ON TABLE core.sport_fit_map IS
  'Correspondence table between the (sport, sub_sport) string pair produced by the FIT decoder (garmin_fit_sdk resolves both fields to strings, verified empirically, not the smallint originally declared on core.sport in migration 003) and a canonical garminator sport_code. Many-to-one: several (fit_sport, fit_sub_sport) pairs can map to the same sport_code (e.g. cycling/indoor_cycling, cycling/virtual_activity and cycling/spin all map to virtual_cycling), which is exactly why this is a separate table rather than two columns on core.sport. The authoritative copy of this mapping is SPORT_CODE_BY_FIT_SPORT in src/garminator/transform/manual.py; this table is a mirror kept in sync by hand and checked against it in tests/db/test_sport_fit_map.py -- Python remains the source of truth actually executed at ingestion, this table exists so the same correspondence is queryable and FK-constrained in SQL.';
COMMENT ON COLUMN core.sport_fit_map.fit_sub_sport IS
  'NULL means "fallback for this fit_sport when no more specific (fit_sport, fit_sub_sport) row matches" -- mirrors the sub_sport = None fallback entries of SPORT_CODE_BY_FIT_SPORT. UNIQUE NULLS NOT DISTINCT on (fit_sport, fit_sub_sport) guarantees at most one fallback row per fit_sport.';

INSERT INTO core.sport_fit_map (fit_sport, fit_sub_sport, sport_code) VALUES
  ('running',      'treadmill',         'treadmill'),
  ('running',      'trail',             'trail_running'),
  ('running',      NULL,                'running'),
  ('cycling',      'indoor_cycling',    'virtual_cycling'),
  ('cycling',      'virtual_activity',  'virtual_cycling'),
  ('cycling',      'spin',              'virtual_cycling'),
  ('cycling',      NULL,                'cycling'),
  ('swimming',     'lap_swimming',      'lap_swimming'),
  ('swimming',     'open_water',        'open_water'),
  ('hiking',       NULL,                'hiking'),
  ('alpine_skiing',NULL,                'resort_skiing'),
  ('training',     'strength_training', 'strength'),
  ('transition',   NULL,                'transition')
ON CONFLICT DO NOTHING;

-- DROP the dead, mistyped columns and correct the now-false claims in 003's comments.
-- 003 is frozen DDL and already checksummed: it cannot be edited in place, only
-- corrected forward here.

ALTER TABLE core.sport DROP COLUMN fit_sport;
ALTER TABLE core.sport DROP COLUMN fit_sub_sport;

COMMENT ON TABLE core.sport IS
  'Canonical garminator sport, one row per sport_code. This is NOT a simple labels table -- it carries two decision-making pieces of information: discipline (which drives the load fallback matrix, 04 section 0.3) and is_indoor (which forces f(i)=1 in GAP calculation, VERIFIED 04 section 3.3.4: "Treadmill: gradient unreliable"). A simple free text column on core.activity would not allow these rules to be expressed declaratively. The correspondence between the (sport, sub_sport) pair of the official FIT profile and sport_code no longer lives on this table (migration 003 declared fit_sport/fit_sub_sport smallint columns here, both dead -- never seeded, and mistyped: the FIT decoder resolves these fields to strings, not integers): it is many-to-one and cannot be expressed by two columns on this table, see core.sport_fit_map (migration 015).';