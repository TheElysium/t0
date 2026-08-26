-- Migration 014: gives a manual upload a primary key and an explicit provenance (batch L11).
--
-- THE PROBLEM. Migration 012 made raw.fit_file.garmin_activity_id nullable so that a
-- manual upload finally enters the transformation queue. It does enter it -- and stops
-- there: core.activity.activity_id IS the Garmin identifier (bigint PRIMARY KEY, no
-- sequence, no DEFAULT since 004), so a file that never came from Garmin has no key
-- to be inserted under. src/t0/transform/runner.py therefore leaves it in the
-- queue with the outcome "manual upload without summary", indefinitely.
--
-- THE KEY -- a reserved high range rather than a surrogate key.
-- Rejected: turning activity_id into a GENERATED ALWAYS AS IDENTITY surrogate and moving
-- the Garmin identifier to a separate column. It is the cleaner model in the abstract, but
-- it rewrites every join, every mart view and every dashboard query for a case that
-- concerns a handful of rows -- a blast radius out of proportion with the problem.
-- Rejected: negative identifiers (explicit arbitration by the athlete).
-- Adopted: manual activities draw from a dedicated sequence starting at 9 * 10^18.
-- Real Garmin identifiers are of the order of 2 * 10^10 and bigint tops out at
-- 9.223 * 10^18: the reserved range is eight orders of magnitude above anything Garmin
-- has ever emitted, and still leaves 2.2 * 10^17 values. A collision would require Garmin
-- to multiply its identifiers by four hundred million.
--
-- The range is not left as a convention: activity_manual_id_range makes it a CHECK in
-- BOTH directions (equivalence, not implication). A "manual" row outside the range and a
-- "garmin" row inside it are equally refused, so the partition can never drift.
--
-- WHY AN EXPLICIT source COLUMN HERE, WHEN 012 REFUSED ONE ON raw.fit_file.
-- 012's argument was that garmin_activity_id IS NULL already says everything, and that
-- two columns to keep in sync are a defect. That argument does not carry over: core.activity
-- has no separate Garmin identifier column to be NULL -- the identifier IS the primary key,
-- which is NOT NULL by definition. Without this column, provenance would only be readable
-- by comparing the primary key against a magic constant, in every query that needs it.
-- The CHECK above is precisely what removes the "two sources of truth" objection: source
-- and the identifier range cannot diverge, the database refuses it.
--
-- IDEMPOTENCE. A Garmin activity is re-identified by its identifier; a manual one has
-- nothing but the file it came from. activity_manual_file_uq (partial UNIQUE on
-- fit_file_id where source = 'manual') is what forbids a second run of the transformation
-- from creating a duplicate activity for the same archived file. It is partial on purpose:
-- a Garmin row is already unique by its identifier, so constraining its fit_file_id would
-- add nothing and would forbid cases the schema never ruled out.

ALTER TABLE core.activity
    ADD COLUMN source text NOT NULL DEFAULT 'garmin';

ALTER TABLE core.activity
    ADD CONSTRAINT activity_source_ck CHECK (source IN ('garmin', 'manual'));

ALTER TABLE core.activity
    ADD CONSTRAINT activity_manual_id_range
    CHECK ((source = 'manual') = (activity_id >= 9000000000000000000));

CREATE SEQUENCE core.manual_activity_id_seq
    AS bigint
    START WITH 9000000000000000000
    INCREMENT BY 1
    NO CYCLE;

CREATE UNIQUE INDEX activity_manual_file_uq
    ON core.activity (fit_file_id)
    WHERE source = 'manual';

COMMENT ON COLUMN core.activity.source IS
  'Provenance of the activity: ''garmin'' (downloaded from the API, activity_id IS the Garmin identifier) or ''manual'' (uploaded through t0.api.upload, activity_id drawn from core.manual_activity_id_seq). Contrary to raw.fit_file where NULL alone marks a manual upload (migration 012), an explicit column is needed here because the Garmin identifier is the primary key and can never be NULL. The constraint activity_manual_id_range forbids this column from diverging from the identifier range, so the two pieces of information cannot contradict each other.';

COMMENT ON SEQUENCE core.manual_activity_id_seq IS
  'Identifier source for activities with no Garmin identifier (manual upload). Starts at 9e18, eight orders of magnitude above real Garmin identifiers (~2e10) and below the bigint ceiling (9.223e18). Never used for a Garmin activity: for those, activity_id IS the identifier received from the API, and the CHECK activity_manual_id_range refuses it in this range.';
