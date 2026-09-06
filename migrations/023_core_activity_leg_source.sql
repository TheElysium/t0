-- Migration 023: the legs of a multisport event become explicit rows (source 'leg').
--
-- THE PROBLEM. The transform assumed that Garmin publishes the legs of a multisport
-- event (swim / bike / run) as separate activities, each with its own activityId
-- (see FitFileShaConflictError in t0.garmin.raw_store). VERIFIED on this athlete's
-- real data (2026-09-05 triathlon, activity 24243770345): get_activities_by_date
-- NEVER returns the legs -- the list carries only the parent (isParent: true, no
-- children field), the payload staying byte-identical for 24 h. The consequence: the
-- event's FIT file (five sessions: swim, T1, bike, T2, run) is parsed as a single
-- is_parent row with produces_records=false, and EVERY load view filters
-- is_parent = false (migrations/004 comment: anti double-counting) -- the whole
-- triathlon is invisible in core-derived marts, dashboards show nothing for race day.
--
-- THE FIX LIVES IN PYTHON (t0.transform.multisport splits the FIT sessions into leg
-- bundles); this migration only gives those legs a place to exist in core.activity:
--
-- A THIRD SOURCE, because neither existing value fits. A leg is neither a 'garmin'
-- row (its activity_id is NOT a Garmin identifier: inventing one in the Garmin range
-- would race real future activityIds for collision) nor a 'manual' upload (it was not
-- uploaded by hand, and activity_manual_file_uq forbids more than one 'manual' row
-- per fit_file_id -- a split produces several). So: source = 'leg', child of the
-- parent through parent_activity_id (already columned and indexed by migration 004,
-- commented "Filled in for segments (legs) of a multisport sequence" -- the DDL
-- anticipated this batch), which also keeps activity_hierarchy_ck and
-- activity_leg_ck meaningful without touching them.
--
-- THE RANGE. Migration 014 reserved 9e18+ for manual activities and bound source
-- tightly to the range with activity_manual_id_range, in BOTH directions. Legs need
-- the same collision-proof property (a leg id must never be confused with a Garmin
-- activityId), so the constraint widens to cover both synthetic provenances:
--   (activity_id >= 9e18) = (source IN ('manual', 'leg'))
-- The leg sequence starts at 9.2e18: eight orders of magnitude above Garmin
-- identifiers, and ~2.2e16 values -- a manual-upload sequence (starting 9e18) would
-- have to be drawn 2e17 times before reaching the leg range, so the two populations
-- cannot realistically interleave.
--
-- KEEPING fit_file_id ON LEGS. A leg row carries the SAME fit_file_id as its parent
-- (both come from one archived file). activity_manual_file_uq is partial on
-- source = 'manual' and therefore does not forbid this; the parent keeps the unique
-- per-file key it already had, legs add no constraint of their own: re-splitting the
-- same file must replace legs wholesale (DELETE + reinsert, idempotent), which the
-- store does transactionally.

ALTER TABLE core.activity
    DROP CONSTRAINT activity_source_ck;

ALTER TABLE core.activity
    ADD CONSTRAINT activity_source_ck CHECK (source IN ('garmin', 'manual', 'leg'));

ALTER TABLE core.activity
    DROP CONSTRAINT activity_manual_id_range;

ALTER TABLE core.activity
    ADD CONSTRAINT activity_synthetic_id_range
    CHECK ((activity_id >= 9000000000000000000) = (source IN ('manual', 'leg')));

CREATE SEQUENCE core.leg_activity_id_seq
    AS bigint
    START WITH 9200000000000000000
    INCREMENT BY 1
    NO CYCLE;

COMMENT ON COLUMN core.activity.source IS
  'Provenance of the activity: ''garmin'' (downloaded from the API, activity_id IS the Garmin identifier), ''manual'' (uploaded through t0.api.upload, activity_id drawn from core.manual_activity_id_seq) or ''leg'' (synthetic leg of a multisport event, activity_id drawn from core.leg_activity_id_seq -- see t0.transform.multisport: the split is done by t0 from the parent''s FIT file, because get_activities_by_date does not publish the legs, VERIFIED on the 2026-09-05 triathlon). activity_synthetic_id_range forbids this column from diverging from the identifier range, so the two pieces of information cannot contradict each other.';

COMMENT ON SEQUENCE core.leg_activity_id_seq IS
  'Identifier source for the legs the transform derives from a multisport FIT file (one per session: swim, T1, bike, T2, run). Starts at 9.2e18, above the manual range (9e18+) so the two synthetic populations cannot realistically interleave, eight orders of magnitude above real Garmin identifiers (~2e10), below the bigint ceiling (9.223e18). Resolution is idempotent: a re-pass reuses the existing (parent_activity_id, leg_order) rows before drawing.';
