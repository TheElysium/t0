-- Migration 007: materialized views mart.*
-- Source: research/05-postgres-schema.md section 3.6.
-- Batch L1 (garminator) -- do not modify the DDL manually outside migration.

-- CORRECTION APPLIED IN REVIEW (only deviation assumed from section 3.6 literal text):
-- the reference view in the document projects b.distance_m_swim in the swim branch of
-- intensity_factor, whereas the base CTE projects no distance. As written, the
-- migration fails on creation: "column b.distance_m_swim does not exist", and the chain
-- 001..007 is thus not applicable in one pass. The document prescribes the correction itself
-- in its "honesty warning" (section 3.6): "it references b.distance_m_swim which must
-- be added to the base CTE (a.distance_m)". This is exactly what is done here: adding
-- a.distance_m to base, and using b.distance_m in the swim branch. No output column
-- of the view is modified.
CREATE MATERIALIZED VIEW mart.activity_load AS
WITH base AS (
    SELECT a.activity_id, a.athlete_id, a.sport_code, s.discipline,
           a.local_date, a.start_time_local, a.duration_s, a.swim_time_s, a.distance_m,
           a.np_watts, a.np_reliable, a.ngp_speed_mps, a.data_quality,
           a.imputed_seconds, a.needs_manual_rpe,
           th.threshold_id, th.ftp_watts, th.rftp_watts, th.css_m_s,
           th.ftp_pace_s_per_km, th.lthr_bpm
      FROM core.activity a
      JOIN core.sport s ON s.sport_code = a.sport_code
      LEFT JOIN core.threshold th
             ON th.athlete_id = a.athlete_id
            AND th.discipline = s.discipline
            AND th.validity @> a.local_date
     WHERE a.is_parent = false          -- never count the multisport parent
),
-- hrTSS: dot product of histogram x zone coefficients.
hrtss AS (
    SELECT h.activity_id,
           sum(h.seconds / 3600.0 * z.tss_per_hour) AS hr_tss,
           sum(h.seconds)                           AS hr_seconds
      FROM core.activity_hr_histogram h
      JOIN base b ON b.activity_id = h.activity_id
      JOIN core.hr_tss_coefficient z
        ON  h.hr_bpm >= z.lo_pct_lthr * b.lthr_bpm
        AND h.hr_bpm <  z.hi_pct_lthr * b.lthr_bpm
     WHERE b.lthr_bpm IS NOT NULL
     GROUP BY h.activity_id
)
SELECT b.activity_id, b.athlete_id, b.sport_code, b.discipline, b.local_date,
       b.threshold_id, b.data_quality, b.needs_manual_rpe,
       -- IF by discipline, each relative to ITS threshold.
       CASE
         WHEN b.discipline = 'bike' AND b.np_watts IS NOT NULL AND b.ftp_watts  > 0
              THEN b.np_watts / b.ftp_watts
         WHEN b.discipline = 'run'  AND b.np_watts IS NOT NULL AND b.rftp_watts > 0
              THEN b.np_watts / b.rftp_watts
         WHEN b.discipline = 'swim' AND b.css_m_s > 0 AND b.swim_time_s > 0
              THEN (b.distance_m / b.swim_time_s) / b.css_m_s
       END AS intensity_factor,
       h.hr_tss,
       h.hr_seconds
  FROM base b
  LEFT JOIN hrtss h ON h.activity_id = b.activity_id;

CREATE UNIQUE INDEX activity_load_pk ON mart.activity_load (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'Load and intensity per activity, recalculated from thresholds IN EFFECT ON THE DATE of each session. The UNIQUE index is not decorative: it is the sine qua non condition for REFRESH MATERIALIZED VIEW CONCURRENTLY, which avoids locking Grafana during refresh. The is_parent = false filter prevents double-counting multisport sequences.';


CREATE MATERIALIZED VIEW mart.daily_load AS
SELECT a.athlete_id,
       a.local_date,
       sum(m.value) FILTER (WHERE lm.scale = 'tss') AS tss,
       count(*) FILTER (WHERE lm.scale = 'tss')     AS n_activities,
       sum(a.duration_s)                            AS duration_s,
       bool_or(lm.is_measured = false)              AS has_estimated_load
  FROM core.activity a
  JOIN core.activity_metric m
    ON m.activity_id = a.activity_id AND m.metric = 'tss' AND m.is_canonical
  JOIN core.load_method lm ON lm.method_code = m.method_code
 WHERE a.is_parent = false
 GROUP BY a.athlete_id, a.local_date;

CREATE UNIQUE INDEX daily_load_pk ON mart.daily_load (athlete_id, local_date);
COMMENT ON MATERIALIZED VIEW mart.daily_load IS
  'Daily load, basis of PMC recursion. The FILTER on scale = ''tss'' is a structural safeguard: it makes it impossible to accidentally add a TRIMP (scale ~170/h at threshold) to TSS (scale 100/h at threshold) -- an error explicitly warned against in 04 section 5.1. has_estimated_load signals to Grafana that at least one session of the day has estimated load (HR or RPE) not measured.';


CREATE MATERIALIZED VIEW mart.pmc AS
WITH RECURSIVE seed AS (
    -- ONE seed per athlete, the most recent one. core.pmc_seed is keyed on
    -- (athlete_id, seed_date), so it deliberately KEEPS the history of seeds: re-seeding
    -- after a backfill adds a row, it does not replace one. Joining the table directly
    -- would then emit one calendar per seed row, duplicate every local_date, and make
    -- REFRESH MATERIALIZED VIEW CONCURRENTLY fail outright on the pmc_pk unique index.
    -- A newer seed supersedes an older one; with a single row this is a no-op.
    -- The casts are load-bearing, not cosmetic. core.pmc_seed.ctl/atl are `real`, while
    -- the recursive term below produces `double precision` (the arithmetic widens). A
    -- recursive CTE requires both terms to agree column by column, so leaving these as
    -- `real` makes PostgreSQL reject the view outright with DatatypeMismatch on
    -- prev_ctl/prev_atl. Widening once here keeps every downstream column consistent.
    SELECT DISTINCT ON (s.athlete_id)
           s.athlete_id, s.seed_date,
           s.ctl::double precision AS ctl,
           s.atl::double precision AS atl
      FROM core.pmc_seed s
     ORDER BY s.athlete_id, s.seed_date DESC
),
cal AS (
    SELECT a.athlete_id,
           generate_series(s.seed_date, current_date, interval '1 day')::date AS local_date,
           s.seed_date, s.ctl AS seed_ctl, s.atl AS seed_atl
      FROM core.athlete a
      JOIN seed s ON s.athlete_id = a.athlete_id
),
d AS (
    SELECT c.athlete_id, c.local_date, c.seed_date, c.seed_ctl, c.seed_atl,
           COALESCE(dl.tss, 0)::double precision AS tss,
           row_number() OVER (PARTITION BY c.athlete_id ORDER BY c.local_date) AS n
      FROM cal c
      LEFT JOIN mart.daily_load dl
             ON dl.athlete_id = c.athlete_id AND dl.local_date = c.local_date
),
rec AS (
    SELECT d.athlete_id, d.local_date, d.seed_date, d.n, d.tss,
           d.seed_ctl + (d.tss - d.seed_ctl) / 42.0 AS ctl,
           d.seed_atl + (d.tss - d.seed_atl) /  7.0 AS atl,
           d.seed_ctl AS prev_ctl, d.seed_atl AS prev_atl
      FROM d WHERE d.n = 1
    UNION ALL
    SELECT d.athlete_id, d.local_date, d.seed_date, d.n, d.tss,
           r.ctl + (d.tss - r.ctl) / 42.0,
           r.atl + (d.tss - r.atl) /  7.0,
           r.ctl, r.atl
      FROM rec r
      JOIN d ON d.athlete_id = r.athlete_id AND d.n = r.n + 1
)
SELECT athlete_id, local_date, tss,
       ctl, atl,
       prev_ctl - prev_atl AS tsb,
       ctl - atl           AS tsb_same_day,
       (local_date >= seed_date + 180) AS ctl_reliable
  FROM rec;

CREATE UNIQUE INDEX pmc_pk ON mart.pmc (athlete_id, local_date);
COMMENT ON MATERIALIZED VIEW mart.pmc IS
  'Performance Management Chart: CTL (42 d), ATL (7 d), TSB. FOUR DECISIONS INSCRIBED IN THIS VIEW, all from 04. (1) Alpha variant = simple 1/42 and 1/7, not exact exponential form: both exist in literature, this one is retained and documented (04 section 6.2). (2) DENSE calendar by generate_series: without it, a rest day would be missing and decay would not occur -- CTL would be mechanically overestimated (04 section 6.4). (3) TSB = previous-day CTL - previous-day ATL, the reference convention; tsb_same_day is exposed IN ADDITION because many tools use the other and the difference puzzles (04 section 6.3). (4) CTE recursion not closed form: closed form involves (1-alpha)^-j, reaching ~1.3e19 in 5 years and losing all precision in double (04 section 6.7).';
COMMENT ON COLUMN mart.pmc.ctl_reliable IS
  'false for the first 180 DAYS following the seed. VERIFIED 04 section 6.5: the seed value still dominates CTL for several months, and CTL displayed as growth over this period is an initialization artifact, not training progression. Grafana should gray out this range rather than hide it.';


CREATE MATERIALIZED VIEW mart.power_curve_best AS
SELECT DISTINCT ON (pc.kind, pc.duration_s)
       pc.kind, pc.duration_s, pc.value, pc.activity_id, a.local_date
  FROM core.activity_power_curve pc
  JOIN core.activity a ON a.activity_id = pc.activity_id
 WHERE NOT pc.spans_pause
 ORDER BY pc.kind, pc.duration_s, pc.value DESC;

CREATE UNIQUE INDEX power_curve_best_pk ON mart.power_curve_best (kind, duration_s);
COMMENT ON MATERIALIZED VIEW mart.power_curve_best IS
  'All-time record curve, by curve type. DISTINCT ON directly exploits the core.activity_power_curve_best_idx index. The activity_id column answers the athlete''s real question: "what day did I set this record?". Windows spanning a pause are excluded (04 section 8.3). A record curve over a sliding period is derived from the same table by adding a local_date filter: it does not need its own materialized view.';


CREATE MATERIALIZED VIEW mart.hr_zone_time AS
SELECT h.activity_id, a.local_date, a.sport_code,
       z.zone_code, z.zone_order,
       sum(h.seconds) AS seconds
  FROM core.activity_hr_histogram h
  JOIN core.activity a ON a.activity_id = h.activity_id
  JOIN core.threshold th
    ON th.athlete_id = a.athlete_id AND th.discipline = 'all'
   AND th.validity @> a.local_date
  JOIN core.hr_tss_coefficient z
    ON  h.hr_bpm >= z.lo_pct_lthr * th.lthr_bpm
    AND h.hr_bpm <  z.hi_pct_lthr * th.lthr_bpm
 GROUP BY h.activity_id, a.local_date, a.sport_code, z.zone_code, z.zone_order;

CREATE UNIQUE INDEX hr_zone_time_pk ON mart.hr_zone_time (activity_id, zone_code);
COMMENT ON MATERIALIZED VIEW mart.hr_zone_time IS
  'Time per HR zone, DERIVED not stored. This is the visible counterpart to the choice of 1 bpm histogram: correcting an LTHR or redefining zone bounds propagates to the entire history by simple REFRESH, without re-ingesting any activity.';


CREATE MATERIALIZED VIEW mart.activity_wide AS
SELECT a.activity_id, a.local_date, a.start_time_local, a.sport_code,
       sp.label AS sport, a.name, a.duration_s, a.distance_m,
       a.avg_hr, a.max_hr, a.avg_power, a.np_watts, a.np_reliable,
       a.ascent_m, a.data_quality, a.needs_manual_rpe,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.is_canonical) AS tss,
       max(m.method_code) FILTER (WHERE m.metric = 'tss' AND m.is_canonical) AS tss_method,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.method_code = 'hr_hrtss') AS tss_hr,
       max(m.value) FILTER (WHERE m.metric = 'trimp')                     AS trimp,
       -- AND m.is_canonical: without it max() returns the largest value across all
       -- methods rather than the canonical one. Same defect and same fix in 011, which
       -- replaces this view and adds the matching intensity_factor_method column.
       max(m.value) FILTER (WHERE m.metric = 'intensity_factor' AND m.is_canonical)
                                                                          AS intensity_factor,
       max(m.value) FILTER (WHERE m.metric = 'decoupling_pct')            AS decoupling_pct,
       max(m.value) FILTER (WHERE m.metric = 'efficiency_factor')         AS efficiency_factor
  FROM core.activity a
  JOIN core.sport sp ON sp.sport_code = a.sport_code
  LEFT JOIN core.activity_metric m ON m.activity_id = a.activity_id
 WHERE a.is_parent = false
 GROUP BY a.activity_id, sp.label;

CREATE UNIQUE INDEX activity_wide_pk ON mart.activity_wide (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_wide IS
  'WIDE view of one row per activity, intended for Grafana tables. Materializes the "narrow table for writing, wide view for reading" pattern: core.activity_metric remains normalized and extensible, while Grafana consumes a tabular format without pivoting on the dashboard. tss_method is deliberately exposed -- knowing a TSS comes from HR not power changes its interpretation.';
