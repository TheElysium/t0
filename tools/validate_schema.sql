-- Control queries for a schema validation run against the real history.
--
-- WHAT THIS IS FOR
-- Migrations are frozen by checksum the first time they are applied on a real
-- deployment (see src/t0/db/runner.py). Until then, and only until then, a
-- schema defect can still be fixed in the migration file itself rather than by a
-- corrective migration stacked on top. This file is the checklist that turns the
-- athlete's own history into evidence that the schema holds.
--
-- HOW TO RUN
--   psql "$T0_POSTGRES_DSN" -f tools/validate_schema.sql
-- Run it AFTER the import and AFTER `python -m t0.ops refresh`: every mart.*
-- object below is a materialized view and reads as empty until it is refreshed.
--
-- HOW TO READ IT
-- Each section states what the numbers must satisfy. An empty result is not a pass by
-- default: several sections are empty ONLY for a reason stated inline. Anything that
-- contradicts the stated expectation is a schema finding, not an import glitch.

\echo '== 1. core.activity: volume, sports, sources =='
-- Expect one row per usable export line. Every row imported from the bulk export has
-- source = 'manual' and data_quality = 'unknown': no sample stream was read.
SELECT source, data_quality, count(*) AS activities,
       min(local_date) AS first_date, max(local_date) AS last_date
  FROM core.activity
 GROUP BY source, data_quality
 ORDER BY source, data_quality;

SELECT sport_code, count(*) AS activities,
       count(*) FILTER (WHERE distance_m IS NULL) AS without_distance,
       count(*) FILTER (WHERE np_watts IS NULL)   AS without_np,
       count(*) FILTER (WHERE gap_applied)        AS gap_applied
  FROM core.activity
 GROUP BY sport_code
 ORDER BY activities DESC;

\echo '== 2. Identifiers: re-importing the export must be idempotent =='
-- Must return zero rows. A duplicate here means two activities share a start second,
-- which the identifier scheme cannot distinguish.
SELECT activity_id, count(*)
  FROM core.activity
 GROUP BY activity_id HAVING count(*) > 1;

-- Must return zero rows: CHECK activity_synthetic_id_range enforces this, so a row here
-- would mean the constraint is not doing what its name claims. Both synthetic sources
-- (manual, leg) share the range; the boundary is 1e15 since migration 024 (was 9e18).
SELECT activity_id, source
  FROM core.activity
 WHERE (source IN ('manual', 'leg')) <> (activity_id >= 1000000000000000);

\echo '== 3. Time zone handling across the DST boundary =='
-- Both offsets must appear for a history spanning more than a year in Europe/Paris:
-- 3600 in winter (CET) and 7200 in summer (CEST). A single value means the offset was
-- assumed rather than resolved.
SELECT tz_offset_s, count(*) AS activities,
       min(local_date) AS first_date, max(local_date) AS last_date
  FROM core.activity
 GROUP BY tz_offset_s
 ORDER BY tz_offset_s;

-- Must return zero rows: the local wall clock must reconstruct from the UTC instant.
SELECT activity_id, start_time, start_time_local, tz_offset_s
  FROM core.activity
 WHERE start_time_local
       <> (start_time + make_interval(secs => tz_offset_s)) AT TIME ZONE 'UTC'
 LIMIT 20;

\echo '== 4. core.activity_metric: which TSS method actually won =='
-- At most one canonical row per (activity, metric), enforced by the partial unique index
-- activity_metric_canonical_uq. The interesting column is method_code: it shows the
-- fallback cascade of migration 003 resolving itself on real data.
SELECT method_code, count(*) AS canonical_rows,
       round(min(value)::numeric, 1) AS min_tss,
       round(avg(value)::numeric, 1) AS avg_tss,
       round(max(value)::numeric, 1) AS max_tss
  FROM core.activity_metric
 WHERE metric = 'tss' AND is_canonical
 GROUP BY method_code
 ORDER BY canonical_rows DESC;

-- Activities with NO canonical TSS, by sport. For a summary-only import this is
-- EXPECTED, and must be exactly the sports whose only chains need a heart-rate
-- histogram: hiking, resort_skiing, multisport, transition, plus outdoor cycling with
-- no power meter. Any OTHER sport appearing here is a finding.
SELECT a.sport_code, count(*) AS without_canonical_tss
  FROM core.activity a
  LEFT JOIN core.activity_metric m
    ON m.activity_id = a.activity_id AND m.metric = 'tss' AND m.is_canonical
 WHERE m.activity_id IS NULL
 GROUP BY a.sport_code
 ORDER BY without_canonical_tss DESC;

-- Must return zero rows: every sport present in the data needs a declared chain,
-- because resolve_canonical() raises instead of returning None for an unknown pair.
-- This is the query that would have caught the missing multisport chain up front.
SELECT DISTINCT a.sport_code
  FROM core.activity a
 WHERE NOT EXISTS (
       SELECT 1 FROM core.metric_method_priority p
        WHERE p.metric = 'tss' AND p.sport_code = a.sport_code);

\echo '== 5. mart.activity_load vs mart.activity_wide: the drift detector =='
-- These two are NOT redundant. activity_wide exposes what was STORED in
-- core.activity_metric at transformation time; activity_load RECOMPUTES from the
-- thresholds in effect on each date. Migration 011 arbitrated in favour of the stored
-- path, and the whole value of activity_load is the gap between the two.
-- Right after a fresh import both are computed from the same thresholds, so this must
-- return zero rows. Rows appearing later mean a threshold was corrected after ingestion
-- and the affected activities need re-transformation.
SELECT al.activity_id, al.sport_code,
       al.intensity_factor_method AS recomputed_method,
       aw.intensity_factor_method AS stored_method,
       round(al.intensity_factor::numeric, 3) AS recomputed_if,
       round(aw.intensity_factor::numeric, 3) AS stored_if
  FROM mart.activity_load al
  JOIN mart.activity_wide aw ON aw.activity_id = al.activity_id
 WHERE al.intensity_factor_method IS DISTINCT FROM aw.intensity_factor_method
    OR abs(coalesce(al.intensity_factor, 0) - coalesce(aw.intensity_factor, 0)) > 0.01
 LIMIT 20;

-- Both views must hold exactly the non-parent activities: neither may drop an activity
-- just because its load could not be computed.
SELECT (SELECT count(*) FROM mart.activity_load)                    AS activity_load_rows,
       (SELECT count(*) FROM mart.activity_wide)                    AS activity_wide_rows,
       (SELECT count(*) FROM core.activity WHERE is_parent = false) AS non_parent_activities,
       (SELECT count(*) FROM core.activity WHERE is_parent)         AS parent_activities;

-- hr_tss must be NULL for every row after a CSV-only import: it is a dot product over
-- core.activity_hr_histogram, which the bulk export cannot fill. A non-NULL value here
-- would mean the view invented a histogram.
SELECT count(*) AS rows_with_hr_tss FROM mart.activity_load WHERE hr_tss IS NOT NULL;

\echo '== 6. mart.daily_load: no double counting =='
-- Must return zero rows: a multisport parent and its legs would both be counted without
-- the is_parent = false filter, inflating the day.
SELECT dl.local_date, dl.n_activities, dl.tss
  FROM mart.daily_load dl
 WHERE dl.n_activities > (
       SELECT count(*) FROM core.activity a
        WHERE a.local_date = dl.local_date AND a.athlete_id = dl.athlete_id
          AND a.is_parent = false)
 LIMIT 20;

SELECT count(*) AS days_with_load,
       round(sum(tss)::numeric, 0) AS total_tss,
       round(max(tss)::numeric, 0) AS heaviest_day,
       count(*) FILTER (WHERE has_estimated_load) AS days_with_estimated_load
  FROM mart.daily_load;

\echo '== 7. mart.pmc: requires a row in core.pmc_seed =='
-- EMPTY IS EXPECTED when core.pmc_seed has no row: the recursion starts from the seed,
-- so without one there is no calendar to walk. Seed it with
--     python -m t0.ops seed-pmc
-- before reading anything into an empty PMC. The calendar must be DENSE, rest days
-- included, or CTL never decays -- which is what rest_days_present checks.
-- seed_rows > 1 for a single athlete is legitimate (the table keeps the history of
-- seeds); the view takes the most recent one. pmc_rows must stay equal to the number of
-- days since that seed, never a multiple of it.
SELECT (SELECT count(*) FROM core.pmc_seed)                    AS seed_rows,
       (SELECT count(*) FROM mart.pmc)                         AS pmc_rows,
       (SELECT count(*) FROM mart.pmc WHERE tss = 0)           AS rest_days_present,
       (SELECT count(*) FROM mart.pmc WHERE ctl_reliable)      AS days_past_the_180d_warmup;

-- Sanity check on the two time constants: CTL (42 d) must move slowly and ATL (7 d)
-- fast. A CTL reacting as sharply as ATL means the two were swapped.
SELECT local_date, round(tss::numeric, 0) AS tss,
       round(ctl::numeric, 1) AS ctl, round(atl::numeric, 1) AS atl,
       round(tsb::numeric, 1) AS tsb, ctl_reliable
  FROM mart.pmc
 ORDER BY local_date DESC
 LIMIT 14;

\echo '== 8. Sample-derived views: zero here means no file was ever ingested =='
-- core.sample, core.swim_length and core.activity_hr_histogram are filled by the FIT/TCX
-- pipeline, never by the bulk export, which carries no per-second data at all.
-- READ THESE FOUR COUNTERS AGAINST WHAT WAS INGESTED, not against zero:
--   * after a CSV-only import they all read 0, which is correct and simply means hrTSS,
--     TRIMP, the HR zones and the power curve remain UNVALIDATED;
--   * once at least one file has gone through the upload endpoint they must be > 0.
--     A file ingested with hr_histogram_rows still at 0 is a real defect.
-- hr_zone_time_rows counts (activity, zone) pairs, so it is naturally far smaller than
-- hr_histogram_rows: only the zones actually visited during a session appear.
SELECT (SELECT count(*) FROM core.activity_hr_histogram) AS hr_histogram_rows,
       (SELECT count(*) FROM mart.hr_zone_time)          AS hr_zone_time_rows,
       (SELECT count(*) FROM core.activity_power_curve)  AS power_curve_rows,
       (SELECT count(*) FROM mart.power_curve_best)      AS power_curve_best_rows;

\echo '== 9. mart.activity_wide: the Grafana-facing view =='
-- This is the view every dashboard panel reads, so a column that is NULL across the
-- whole history is a panel that will render blank. Worth knowing now rather than after
-- deployment. trimp and decoupling_pct depend on samples: they read 0 for a CSV-only
-- import, and must become > 0 as soon as section 8 shows any sample-derived row.
-- with_tss is the one counter that should track the total row count closely; a large
-- gap means activities are landing with no method able to resolve at all.
SELECT count(*)                AS rows,
       count(tss)              AS with_tss,
       count(intensity_factor) AS with_intensity_factor,
       count(distance_m)       AS with_distance,
       count(avg_hr)           AS with_avg_hr,
       count(np_watts)         AS with_np,
       count(trimp)            AS with_trimp,
       count(decoupling_pct)   AS with_decoupling
  FROM mart.activity_wide;

\echo '== 10. Thresholds actually resolved for every activity =='
-- A threshold whose validity range does not cover an activity silently disables every
-- power- and pace-based method for it, and the symptom is only an unexplained fallback
-- to hr_hrtss. Discipline 'other' is exempt: hiking, skiing, multisport and transition
-- have no threshold of their own by design.
--
-- TWO DIFFERENT SITUATIONS, deliberately counted apart. Thresholds are NOT backdated to
-- the beginning of the history (a value measured today says nothing about last year), so
-- every activity older than the earliest threshold is uncovered BY DESIGN and simply
-- carries no TSS -- that is uncovered_before_thresholds, informational only.
-- uncovered_after_thresholds_start MUST BE 0: past that date a gap is a real hole in
-- core.threshold.validity, and the detail query below names the offending activities.
WITH first_threshold AS (SELECT min(lower(validity)) AS d FROM core.threshold),
     uncovered AS (
       SELECT a.local_date, a.sport_code, s.discipline
         FROM core.activity a
         JOIN core.sport s ON s.sport_code = a.sport_code
        WHERE s.discipline <> 'other'
          AND NOT EXISTS (
              SELECT 1 FROM core.threshold th
               WHERE th.athlete_id = a.athlete_id
                 AND th.discipline = s.discipline
                 AND th.validity @> a.local_date))
SELECT (SELECT d FROM first_threshold)                                    AS thresholds_start,
       count(*) FILTER (WHERE local_date <  (SELECT d FROM first_threshold))
                                                        AS uncovered_before_thresholds,
       count(*) FILTER (WHERE local_date >= (SELECT d FROM first_threshold))
                                                  AS uncovered_after_thresholds_start
  FROM uncovered;

-- The detail behind the counter that must stay at 0. Empty is the expected result.
SELECT a.local_date, a.sport_code, s.discipline
  FROM core.activity a
  JOIN core.sport s ON s.sport_code = a.sport_code
 WHERE s.discipline <> 'other'
   AND a.local_date >= (SELECT min(lower(validity)) FROM core.threshold)
   AND NOT EXISTS (
       SELECT 1 FROM core.threshold th
        WHERE th.athlete_id = a.athlete_id
          AND th.discipline = s.discipline
          AND th.validity @> a.local_date)
 ORDER BY a.local_date
 LIMIT 20;

-- The cross-cutting row (discipline = 'all') carries LTHR, which migration 008 made the
-- source for hrTSS. Its absence would silently blank hr_tss for the entire history.
SELECT discipline, lower(validity) AS valid_from, upper(validity) AS valid_to,
       (lthr_bpm IS NOT NULL) AS has_lthr,
       (ftp_watts IS NOT NULL) AS has_ftp,
       (rftp_watts IS NOT NULL) AS has_rftp,
       (ftp_pace_s_per_km IS NOT NULL) AS has_run_pace,
       (css_m_s IS NOT NULL) AS has_css
  FROM core.threshold
 ORDER BY discipline, valid_from;
