-- Migration 008: corrects two defects in mart.activity_load (migration 007, frozen).
-- No modification of 007: applied migrations are not rewritten.
--
-- DEFECT 1 -- LTHR was never found, so hr_tss was ALWAYS NULL.
-- The 007 `base` CTE joins core.threshold only on `th.discipline = s.discipline`.
-- But LTHR is carried by the row `discipline = 'all'` (HR zones are cross-cutting,
-- see addendum section 5 and core.threshold DDL, where lthr_bpm is NULL on bike/run/swim rows).
-- Consequence: `b.lthr_bpm` was NULL for every activity, the `WHERE b.lthr_bpm IS NOT NULL`
-- in the hrtss CTE let no row through, and hr_tss / hr_seconds columns were
-- empty for the entire history. It was mart.hr_zone_time (007, line 146) that was right:
-- it properly joins `th.discipline = 'all'`.
-- Fix: second LEFT JOIN on the cross-cutting row, with COALESCE giving priority to
-- a possible discipline-specific LTHR (physiologically legitimate: bike threshold HR
-- is typically lower than run threshold HR).
--
-- DEFECT 2 -- intensity_factor was ALWAYS NULL for running.
-- The 007 `run` branch knows only `np_watts / rftp_watts`. But rftp_watts is NULL by
-- explicit decision (addendum section 5: the athlete gave a threshold pace, not a threshold power;
-- never guess a threshold). There was no pace/GAP branch, even though the
-- canonical run TSS calculates itself by the `run_gap` method. Inconsistent result: a TSS right
-- next to an empty IF, for the same session.
-- Fix: fallback to GAP, relative to threshold pace.
--   threshold speed (m/s) = 1000 / ftp_pace_s_per_km   -- 270 s/km -> 3.703704 m/s
--   IF = ngp_speed_mps / threshold speed = ngp_speed_mps * ftp_pace_s_per_km / 1000
-- The order of branches respects core.metric_method_priority: run_power before run_gap. The day
-- a validated rFTP is entered by the athlete, the power branch will take over by itself.
--
-- ADDITION -- intensity_factor_method column.
-- Project rule: never a load value without its method. Two IFs calculated by different methods
-- are not commensurable and must not be blindly aggregated.
--
-- NOT FIXED, DELIBERATELY -- road bike still has no intensity_factor.
-- Without a power meter, the only available load is hrTSS. One could infer an IF
-- by inversion (IF = sqrt(TSS / (100 * hours))), but this would be circular reasoning: a
-- number derived from itself, presented as a measure of intensity. The column stays NULL, which
-- is honest; hr_tss carries the load information for these sessions.

--
-- DEFECT 3 -- the method cascade was hardcoded into the view.
-- 007 discriminated on `discipline` alone, ignoring core.metric_method_priority. But this
-- matrix does NOT grant the same methods to all sports of the same discipline: `treadmill`
-- has no right to `run_gap` (no GPS gradient on a treadmill, GAP would be fiction). A
-- GAP branch applied to the entire `run` discipline would have contradicted, on treadmill, the
-- canonical method retained by core.activity_metric -- two divergent numbers for the same session.
-- The COMMENT on core.metric_method_priority states this risk precisely: "without it, the cascade
-- gets recopied as CASE WHEN in every Grafana view -- unauditable, untestable, divergent on the
-- second copy." Fix: branches are guarded by membership in the matrix, which
-- becomes the single source of truth again, exactly as on the Python side (resolve_canonical).

DROP MATERIALIZED VIEW mart.activity_load;

CREATE MATERIALIZED VIEW mart.activity_load AS
WITH allowed AS (
    -- Defect 3: the method cascade is not recopied as CASE WHEN, it is READ.
    SELECT p.sport_code, array_agg(p.method_code) AS methods
      FROM core.metric_method_priority p
     WHERE p.metric = 'tss'
     GROUP BY p.sport_code
),
base AS (
    SELECT a.activity_id, a.athlete_id, a.sport_code, s.discipline,
           a.local_date, a.start_time_local, a.duration_s, a.swim_time_s, a.distance_m,
           a.np_watts, a.np_reliable, a.ngp_speed_mps, a.data_quality,
           a.imputed_seconds, a.needs_manual_rpe,
           th.threshold_id, th.ftp_watts, th.rftp_watts, th.css_m_s,
           th.ftp_pace_s_per_km,
           -- Defect 1: LTHR lives on the cross-cutting row; the discipline row
           -- remains priority if it carries one someday.
           coalesce(th.lthr_bpm, th_all.lthr_bpm) AS lthr_bpm,
           coalesce(al.methods, ARRAY[]::text[])  AS allowed_methods
      FROM core.activity a
      JOIN core.sport s ON s.sport_code = a.sport_code
      LEFT JOIN allowed al ON al.sport_code = a.sport_code
      LEFT JOIN core.threshold th
             ON th.athlete_id = a.athlete_id
            AND th.discipline = s.discipline
            AND th.validity @> a.local_date
      LEFT JOIN core.threshold th_all
             ON th_all.athlete_id = a.athlete_id
            AND th_all.discipline = 'all'
            AND th_all.validity @> a.local_date
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
),
-- The method is resolved ONCE; the value flows from it. Two parallel CASE statements could
-- diverge on first edit: here it is structurally impossible.
resolved AS (
    SELECT b.*,
           CASE
             WHEN 'bike_power' = ANY(b.allowed_methods)
                  AND b.np_watts IS NOT NULL AND b.ftp_watts  > 0  THEN 'bike_power'
             WHEN 'run_power'  = ANY(b.allowed_methods)
                  AND b.np_watts IS NOT NULL AND b.rftp_watts > 0  THEN 'run_power'
             -- Defect 2: fallback pace/GAP, only method available while rFTP is NULL.
             WHEN 'run_gap'    = ANY(b.allowed_methods)
                  AND b.ngp_speed_mps IS NOT NULL
                  AND b.ftp_pace_s_per_km > 0                      THEN 'run_gap'
             WHEN 'swim_css'   = ANY(b.allowed_methods)
                  AND b.css_m_s > 0 AND b.swim_time_s > 0          THEN 'swim_css'
           END AS intensity_factor_method
      FROM base b
)
SELECT r.activity_id, r.athlete_id, r.sport_code, r.discipline, r.local_date,
       r.threshold_id, r.data_quality, r.needs_manual_rpe,
       -- IF by method, each relative to ITS threshold.
       CASE r.intensity_factor_method
         WHEN 'bike_power' THEN r.np_watts / r.ftp_watts
         WHEN 'run_power'  THEN r.np_watts / r.rftp_watts
         -- threshold speed (m/s) = 1000 / ftp_pace_s_per_km; IF = ngp / threshold speed.
         WHEN 'run_gap'    THEN r.ngp_speed_mps * r.ftp_pace_s_per_km / 1000.0
         WHEN 'swim_css'   THEN (r.distance_m / r.swim_time_s) / r.css_m_s
       END AS intensity_factor,
       r.intensity_factor_method,
       h.hr_tss,
       h.hr_seconds
  FROM resolved r
  LEFT JOIN hrtss h ON h.activity_id = r.activity_id;

CREATE UNIQUE INDEX activity_load_pk ON mart.activity_load (activity_id);
COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'Load and intensity per activity, recalculated from thresholds IN EFFECT ON THE DATE of each session. The UNIQUE index is not decorative: it is the sine qua non condition for REFRESH MATERIALIZED VIEW CONCURRENTLY, which avoids locking Grafana during refresh. The is_parent = false filter prevents double-counting multisport sequences. Migration 008: LTHR is now read from the cross-cutting threshold row (discipline = ''all''), else hr_tss remained NULL for the entire history; and run intensity_factor falls back to GAP when rftp_watts is NULL, which is the case by explicit decision.';
COMMENT ON COLUMN mart.activity_load.intensity_factor_method IS
  'Method that produced intensity_factor, vocabulary aligned with core.load_method. NULL when no method is applicable -- notably road bike without power meter, where IF is deliberately not inverted from hrTSS (it would be a number derived from itself).';
