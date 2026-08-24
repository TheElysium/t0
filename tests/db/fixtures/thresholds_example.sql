-- ============================================================================================
-- FIXTURE -- EXAMPLE THRESHOLDS (FICTIONAL VALUES). TO BE LOADED MANUALLY.
-- ============================================================================================
--
-- This file IS NOT a migration: it is neither named "NNN_....sql" nor placed in
-- migrations/, and garminator.db.runner never discovers nor applies it
-- automatically (discover_migrations() ignores all files outside this name pattern).
--
-- Why this file exists outside migrations anyway: core.threshold is structurally
-- NOT a frozen reference like core.sport -- thresholds change over time, with history
-- via validity range (validity column, GiST exclusion constraint threshold_no_overlap).
-- Seeding a threshold value as if it were stable reference data would be misleading;
-- a new threshold must always arrive as an explicit dated INSERT, never an UPDATE.
--
-- EVERY VALUE BELOW IS FICTIONAL, and deliberately so: real thresholds are personal
-- biometric data and are never committed. They belong in deploy/seed/athlete.sql, which
-- .gitignore excludes. Keep it that way when updating this file -- the numbers here exist
-- to exercise the schema, never to describe anyone.
--
-- To execute manually, never via `python -m garminator.db migrate`:
--   psql "$GARMINATOR_TEST_POSTGRES_DSN" -f tests/db/fixtures/thresholds_example.sql
--
-- Idempotent: replaying this file creates no duplicates. Warning, idempotence is NOT
-- achieved the same way everywhere, and that is deliberate:
--   * core.threshold: `ON CONFLICT DO NOTHING` without target. PostgreSQL also treats
--     EXCLUSION constraint violations this way, so threshold_no_overlap arbitrates: a second
--     pass sees `validity` overlap on (athlete_id, discipline) and does nothing.
--   * core.athlete: `WHERE NOT EXISTS`. `ON CONFLICT DO NOTHING` would be a TRAP -- the table has
--     NO unique or exclusion constraint in frozen DDL (only athlete_sex_ck, a CHECK),
--     the clause can never trigger and each execution would insert one more athlete.
--     Threshold INSERTs, which resolve athlete_id by `WHERE display_name = ...`,
--     would then hit multiple rows and create one threshold per duplicate.
--
-- If the athlete changes thresholds, best practice is a new INSERT with a new `validity`
-- range (lower bound = date of new test), never an UPDATE of this row -- threshold
-- history must remain readable, cf. 04 section 2.3: "An activity's TSS uses the threshold
-- in force at its date. Otherwise an FTP update would retroactively rewrite all CTL history."
--
-- NON-NEGOTIABLE POINT -- rFTP (running power threshold) left NULL deliberately:
-- this example carries a threshold PACE (4:30 min/km), not a threshold POWER. The two
-- scales are not interchangeable (Garmin Running Power = undocumented proprietary model,
-- VERIFIED 04 section 3.2) and no reliable pace->power conversion exists without
-- independent measurement. rftp_watts stays NULL here: no default value, no derivation
-- from bike FTP, no estimation. A wrong threshold produces no visible error: it produces
-- plausible and wrong numbers throughout the load history. Any load calculation method
-- that depends on rFTP (Running Power) must remain UNAVAILABLE for this athlete until
-- reliable running power measurement is provided -- that is the role of
-- core.metric_method_priority (fallback to next method, e.g. HR-TRIMP) rather than a
-- fabricated threshold value here.
--
-- Height (1.75 m) and age (30 years): not stored -- core.threshold /
-- core.athlete have no dedicated column in frozen DDL (research/05-postgres-schema.md).
-- Not added here: this file does not redefine the schema, out of scope for batch L1. Noted in
-- the end-of-batch report.
-- ============================================================================================

INSERT INTO core.athlete (display_name, sex, home_timezone)
SELECT 'Main athlete', 'male', 'Europe/Paris'
 WHERE NOT EXISTS (
     SELECT 1 FROM core.athlete WHERE display_name = 'Main athlete'
 );

-- Bike (home-trainer only, cf. athlete profile): example FTP 220 W.
-- LTHR (heart rate threshold, distinct from HR max/rest) not communicated -> left NULL, not derived.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    ftp_watts, hr_max_bpm, hr_rest_bpm, weight_kg,
    power_source, source, note
)
SELECT a.athlete_id, 'bike', daterange('2026-05-19', NULL, '[)'),
       220, 190, 55, 70.0,
       'home_trainer', 'manual',
       'Example values. Bike LTHR not communicated (NULL).'
  FROM core.athlete a
 WHERE a.display_name = 'Main athlete'
ON CONFLICT DO NOTHING;

-- Running: example threshold pace 4:30 min/km = 270 s/km (column in s/km, not m/s).
-- rftp_watts (running power threshold) DELIBERATELY NULL -- see warning at top of
-- file: pace is not power, no reliable conversion is applied.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    ftp_pace_s_per_km, rftp_watts, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'run', daterange('2026-05-19', NULL, '[)'),
       270, NULL, 190, 55, 70.0,
       'manual',
       'Example values. rFTP NULL deliberately: '
       'only a threshold pace (4:30 min/km) was provided, not running power. Do not '
       'derive a value here.'
  FROM core.athlete a
 WHERE a.display_name = 'Main athlete'
ON CONFLICT DO NOTHING;

-- Swimming: example CSS 1:50 min/100 m = 110 s/100 m = 0.909091 m/s.
-- css_estimated = false: value communicated directly by athlete, not from crude fallback
-- "1000 m TT x 0.97" (04 section 4.1) -- derivation method not specified but treated as
-- measured since no indication to the contrary was given.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    css_m_s, css_estimated, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'swim', daterange('2026-05-19', NULL, '[)'),
       0.909091, false, 190, 55, 70.0,
       'manual',
       'Example values (CSS 1:50/100m = 0.909091 m/s).'
  FROM core.athlete a
 WHERE a.display_name = 'Main athlete'
ON CONFLICT DO NOTHING;

-- --------------------------------------------------------------------------------------------
-- ASSUMED BACKDATING: lower bound of validity on 2026-05-19, that is THREE MONTHS before
-- actual threshold communication date (2026-08-19).
--
-- This is NOT a measurement: it is an explicit athlete hypothesis, decided on 2026-08-21,
-- that their thresholds were roughly the same over the elapsed quarter. It was preferred
-- to complete backdating to backfill start (2025-04-03), judged too unreliable.
--
-- Known consequence: activities before 2026-05-19 have NO threshold in force, thus no TSS,
-- thus no CTL/ATL/TSB contribution. PMC starts at zero on 2026-05-19 and takes about
-- six weeks to become representative (CTL is a recursive exponential average over 42 days).
-- Any progression shown before early July 2026 is startup artifact, not fitness gain.
--
-- If older thresholds are found, the correct fix is NOT to widen this period: it is to
-- SPLIT it into multiple disjoint validity rows. The GiST exclusion constraint
-- threshold_no_overlap enforces that anyway.
-- --------------------------------------------------------------------------------------------

-- Transversal ('all'): example threshold HR 170 bpm.
-- This row is the CONDITION FOR EXISTENCE of mart.hr_zone_time and hrTSS: HR zones and
-- HR-based TSS join on discipline = 'all'. Without it, both outputs remain empty forever.
-- It is particularly important for ROAD BIKING, which has no power sensor (addendum section 3):
-- HR is the only possible route to training load there.
-- No per-discipline constraints (threshold_bike_only / _run_only / _swim_only) apply to 'all':
-- they target only the three named disciplines. Power and CSS columns remain NULL here anyway --
-- transversal load has no power threshold.
-- 170/190 = 89.5% of max HR: consistent, but NOT VERIFIED by protocol (average of last
-- 20 minutes of a maximal test). Value taken as communicated.
INSERT INTO core.threshold (
    athlete_id, discipline, validity,
    lthr_bpm, hr_max_bpm, hr_rest_bpm, weight_kg,
    source, note
)
SELECT a.athlete_id, 'all', daterange('2026-05-19', NULL, '[)'),
       170, 190, 55, 70.0,
       'manual',
       'Example threshold HR (LTHR) 170 bpm. Carried by ''all'' row '
       'because HR zones and hrTSS are transversal across three disciplines. Validity backdated '
       'to 2026-05-19 like the other three thresholds -- see warning at top of file.'
  FROM core.athlete a
 WHERE a.display_name = 'Main athlete'
ON CONFLICT DO NOTHING;
