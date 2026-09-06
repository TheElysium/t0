-- Migration 024: shrink the synthetic activity_id range below the float64 exact-integer
-- limit (2^53 = 9 007 199 254 740 992 ~ 9.007e15).
--
-- THE PROBLEM. Migrations 014 and 023 placed synthetic identifiers at 9e18 (manual) and
-- 9.2e18 (leg) -- deliberately eight orders of magnitude above real Garmin ids to make a
-- collision impossible. But activity_id is a bigint, and NO JSON consumer preserves a
-- bigint: JSON has no integer type, JavaScript has only float64, so every id that crosses
-- a JSON boundary (the MCP server, Grafana, any browser) is read as a float64. Integers
-- above 2^53 are NOT all representable in float64 -- 9.2e18 rounds to the nearest multiple
-- of 1024, and two distinct legs can collapse onto the same value. This is not
-- hypothetical: it is the same bug class already patched in Grafana by casting
-- activity_id::text in the drilldown links (commit e2a88f8). That patch treats the
-- symptom at one transport; the disease is the range itself, and it recurs at every new
-- consumer until the ids fit in float64.
--
-- THE FIX. Move both synthetic ranges below 2^53. The collision-avoidance argument of
-- migrations 014/023 only needs the synthetic ids to sit far ABOVE real Garmin ids
-- (~2.4e10 today, VERIFIED as the current max on this athlete's data), not near the
-- bigint ceiling. A threshold of 1e15 keeps ~40 000x headroom above Garmin ids while
-- staying an order of magnitude below 2^53, so every synthetic id is now exactly
-- representable in float64 -- the property the whole consumer chain silently assumed.
--   manual: starts 1e15 (1 000 000 000 000 000)
--   leg:    starts 2e15 (2 000 000 000 000 000)
-- The two populations stay range-separated (manual in [1e15, 2e15), leg in [2e15, 2^53)),
-- mirroring the 9e18/9.2e18 design, and leg keeps ~7e15 values before reaching 2^53.
--
-- THE LOAD-BEARING ASSUMPTION, stated so it is not forgotten: no Garmin activityId will
-- ever reach 1e15. Garmin ids grow by a few million per day globally; 1e15 is ~40 000x
-- the current value. If that assumption ever failed, activity_synthetic_id_range would
-- reject the offending Garmin insert LOUDLY (it binds source to the range in both
-- directions) -- a hard failure, never a silent misclassification.
--
-- WHY THE EXISTING LEGS ARE DELETED HERE. The leg ids already at 9.2e18 (10 rows on this
-- athlete, both multisport events) cannot be renumbered by a re-transform alone:
-- resolve_leg_activity_ids REUSES existing (parent_activity_id, leg_order) rows before
-- drawing from the sequence (t0.transform.runner calls it BEFORE store_activity_bundles
-- deletes the old legs), so a re-pass would re-emit the same 9.2e18 ids. The legs are a
-- regenerable projection of the parent's FIT file (source='leg', ON DELETE CASCADE
-- carries the seven series tables), so we DELETE them here and let a subsequent transform
-- pass rebuild them from the small range. See docs/RUNBOOK.md: after this migration, the
-- parent multisport files must be re-transformed (raw.fit_file.parsed_at reset to NULL,
-- then `ops transform`) to bring the legs back with float64-safe ids. The brief window
-- where the two triathlons show no legs is acceptable for a single-athlete homelab.
--
-- WHY A NEW MIGRATION AND NOT AN EDIT OF 023. The runner is forward-only and refuses
-- drift (a checksum change on an applied migration raises MigrationDriftError): an
-- incident is corrected by the next migration, never by rewriting an applied one. This is
-- the same pattern 023 itself used over 014's constraint.

-- Step 1: widen no constraint, just lower its boundary. Existing rows all satisfy both the
-- old and the new predicate (legs at 9.2e18 >= 1e15 and source='leg'; Garmin ids
-- < 1e15 and source='garmin'; zero manual rows), so the swap validates without a rewrite
-- fight -- and the legs are deleted in step 2 regardless.
ALTER TABLE core.activity
    DROP CONSTRAINT activity_synthetic_id_range;

ALTER TABLE core.activity
    ADD CONSTRAINT activity_synthetic_id_range
    CHECK ((activity_id >= 1000000000000000) = (source IN ('manual', 'leg')));

-- Step 2: drop the out-of-range legs. ON DELETE CASCADE (migrations 004/023) carries
-- core.sample and the six other series tables with each row. The parent rows
-- (source='garmin', is_parent) are untouched: they are the source of truth the
-- re-transform reads from.
DELETE FROM core.activity WHERE source = 'leg';

-- Step 3: restart both sequences in the new range. RESTART, not setval: the sequences
-- were never advanced for manual (0 rows drawn) and the leg rows they numbered are now
-- gone, so there is no live id to step past -- the next nextval() must be the first value
-- of the new range.
ALTER SEQUENCE core.manual_activity_id_seq RESTART WITH 1000000000000000;
ALTER SEQUENCE core.leg_activity_id_seq RESTART WITH 2000000000000000;

COMMENT ON COLUMN core.activity.source IS
  'Provenance of the activity: ''garmin'' (downloaded from the API, activity_id IS the Garmin identifier), ''manual'' (uploaded through t0.api.upload, activity_id drawn from core.manual_activity_id_seq) or ''leg'' (synthetic leg of a multisport event, activity_id drawn from core.leg_activity_id_seq -- see t0.transform.multisport: the split is done by t0 from the parent''s FIT file, because get_activities_by_date does not publish the legs, VERIFIED on the 2026-09-05 triathlon). activity_synthetic_id_range forbids this column from diverging from the identifier range, so the two pieces of information cannot contradict each other. Since migration 024 the synthetic range starts at 1e15 (below the float64 exact-integer limit 2^53) so every id survives a JSON round-trip unrounded.';

COMMENT ON SEQUENCE core.manual_activity_id_seq IS
  'Identifier source for manual uploads (t0.api.upload). Starts at 1e15 since migration 024: above any real Garmin identifier (~2e10) by ~40 000x, below the leg range (2e15+), and below the float64 exact-integer limit (2^53 ~ 9.007e15) so the id is not rounded when read through JSON (MCP, Grafana, browser).';

COMMENT ON SEQUENCE core.leg_activity_id_seq IS
  'Identifier source for the legs the transform derives from a multisport FIT file (one per session: swim, T1, bike, T2, run). Starts at 2e15 since migration 024: above the manual range (1e15+) so the two synthetic populations cannot interleave, above real Garmin identifiers (~2e10), and BELOW the float64 exact-integer limit (2^53 ~ 9.007e15) -- the previous 9.2e18 start rounded when serialized to JSON and could collapse two distinct legs onto one value (the bug commit e2a88f8 patched at the Grafana transport; migration 024 fixes the root cause). Resolution is idempotent: a re-pass reuses the existing (parent_activity_id, leg_order) rows before drawing.';
