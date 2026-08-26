-- Migration 012: makes raw.fit_file.garmin_activity_id NULLABLE for manual upload (batch L7).
--
-- THE PROBLEM. src/t0/api/ingest.py (manual upload endpoint) archives a .FIT/.TCX
-- on the PVC respecting the atomicity rule from raw.fit_file's comment, but
-- would insert no row: a manual upload has no garmin_activity_id by construction (it does not
-- come from the Garmin API), and the column was NOT NULL since 002. The file thus
-- remained archived but outside the transformation queue -- never used.
-- No modification of 002_raw.sql: applied migrations are not rewritten (the runner verifies
-- their checksum, see src/t0/db/runner.py).
--
-- WHAT NOW DISTINGUISHES A "GARMIN" ROW FROM A "MANUAL UPLOAD" ROW.
-- Decision: NULL, and nothing else -- no boolean or provenance enum column added. Two reasons:
--
--   1. A provenance column would duplicate exactly what garmin_activity_id IS NULL
--      already says unambiguously: a row with a Garmin identifier COMES from Garmin,
--      a row without one can only come from manual upload (the Garmin API never produces
--      a row without activityId). Two columns that must stay in sync are a defect
--      with permanent opportunity for divergence; NULL cannot diverge from itself.
--   2. This convention is not an invention of this migration: it is already used by
--      src/t0/ops/reconcile.py (MissingFile.garmin_activity_id: int | None,
--      and format_reconcile_report explicitly displays "manual upload" when the
--      value is NULL) and src/t0/transform/runner.py (FileOutcome exposes
--      garmin_activity_id for the human report). Python code was already typing this
--      column as optional before the DDL permitted it; this migration finally aligns
--      the schema with a choice already made elsewhere, rather than inventing
--      a second competing one.
--
-- IMPACT ON EXISTING CONSTRAINTS -- explicitly verified, not assumed.
--
--   - fit_file_sha_uq UNIQUE (sha256): sha256 remains bytea NOT NULL, UNCHANGED by this
--     migration. It is the only UNIQUE constraint on raw.fit_file, and it does not apply
--     to a column becoming nullable here -- the classic pitfall ("a UNIQUE on a nullable
--     column lets multiple NULLs through") DOES NOT APPLY TO IT. Direct and intended
--     consequence: TWO manual uploads of distinct content each get their own row (different
--     sha256); TWO manual uploads of the SAME binary content (same sha256, both with
--     garmin_activity_id NULL) are still detected as the single already-archived file,
--     exactly like an identical Garmin re-download -- content-based idempotence never
--     depends on presence of garmin_activity_id. It is t0.garmin.raw_store
--     .insert_fit_file (re-read after ON CONFLICT (sha256) DO NOTHING) that carries this
--     application logic; fixed in the same batch to stop wrongly assuming existing_activity_id
--     is re-read non-NULL.
--   - fit_file_activity_idx (garmin_activity_id, downloaded_at DESC): simple B-tree index,
--     not UNIQUE -- natively accepts interspersed NULLs (PostgreSQL places them together,
--     sorted last by default) with no behavior change or index rebuild needed.
--   - fit_file_unparsed_idx (downloaded_at) WHERE parsed_at IS NULL: does not reference
--     garmin_activity_id, not affected.
--
-- WHAT THIS MIGRATION LEFT OPEN, AND WHERE IT WAS CLOSED. The row inserted by
-- src/t0/api/ingest.py is a TRULY pending row (garmin_activity_id NULL,
-- parsed_at NULL, parse_error NULL, just like a freshly downloaded Garmin row):
-- it is necessary for the file to truly enter the transformation queue, which was
-- exactly the issue flagged for this batch. Two consequences were out of scope here
-- (src/t0/transform/runner.py was outside this batch's authorized files):
--   1. the queue selector converted the column by int() with no NULL guard, which would
--      have failed the ENTIRE nightly pass rather than the one file involved. FIXED since,
--      in t0.transform.runner (the conversion is now guarded per row).
--   2. entering the queue is not the same as leaving it: core.activity.activity_id IS the
--      Garmin identifier, so a manual upload had no primary key to be inserted under and
--      stayed pending indefinitely. CLOSED by migration 014, which gives it a key drawn
--      from a reserved range and an explicit provenance.

ALTER TABLE raw.fit_file ALTER COLUMN garmin_activity_id DROP NOT NULL;

-- Index dedicated to manual uploads: partial like fit_file_unparsed_idx (same pattern already
-- used in 002), useful for a future inventory tool ("which files entered through the manual
-- door rather than Garmin?") without scanning the entire table.
CREATE INDEX fit_file_manual_upload_idx
    ON raw.fit_file (downloaded_at DESC)
    WHERE garmin_activity_id IS NULL;

COMMENT ON COLUMN raw.fit_file.garmin_activity_id IS
  'Garmin activity identifier, NULL for manual upload (endpoint t0.api.ingest, batch L7) due to no Garmin identifier by construction. NULL is the ONLY provenance marker: a row without garmin_activity_id can only come from manual upload, the Garmin API never producing one without activityId. Convention already used by t0.ops.reconcile (displays "manual upload") and t0.transform.runner before this column even became nullable here (migration 012). Such a row transforms like any other since migration 014, which gives an activity with no Garmin identifier a primary key drawn from core.manual_activity_id_seq.';

COMMENT ON TABLE raw.fit_file IS
  'Metadata for .FIT/.TCX files archived on the PVC, whether they come from automatic Garmin download OR manual upload (endpoint t0.api.ingest, batch L7; migration 012 made garmin_activity_id nullable for this second case -- NULL = manual upload, see the comment on this column). The binary is NOT in the database (no bytea): multiple MB per file would bloat TOAST and every PostgreSQL dump without contributing anything queryable. MANDATORY ATOMICITY RULE, valid for both sources: the file is written AND fsync''d on the PVC BEFORE the INSERT of this row, never the reverse. An orphaned file (no row) is harmless and recoverable by scanning (`python -m t0.ops reconcile`, see docs/RUNBOOK.md); an orphaned row (no file) makes core reconstructibility false. Backup corollary: the PostgreSQL dump and the PVC snapshot must be taken at the SAME point in time (see section 6.5).';
