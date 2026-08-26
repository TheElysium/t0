-- Migration 019: coaching signal views -- time-in-zone (mart.power_pace_zone_time),
-- ramp/monotony/strain risk table (mart.ramp_risk), fired amber/red rules
-- (mart.coach_flags).
-- Source: docs/COACH_FEEDBACK.md Q5/Q8/Q3 and point 5 (TRIMP), arbitrated by
-- docs/COACH_FEEDBACK_3.md sections 2.2 (explicit rules, never a composite score),
-- 2.4 (dctl bands), 3 (monotony & strain) and 4 (Phase 0-3 build order).
-- Batch L1 (coaching dashboards) -- do not modify the DDL manually outside migration.
--
-- REFRESH ORDER REQUIREMENT. These three views are NOT mutually independent:
--   mart.power_pace_zone_time reads core.* only;
--   mart.ramp_risk          reads mart.pmc and mart.daily_load;
--   mart.coach_flags        reads mart.wellness_baseline (018), mart.pmc,
--                           mart.ramp_risk and core.sleep_session.
-- src/t0/ops/refresh.py registers them AFTER the six historical views, in the
-- order wellness_baseline -> ramp_risk -> coach_flags; a manual refresh that ignores this
-- order makes coach_flags read stale baselines/ramp inputs for one day -- silently wrong
-- flags are worse than missing flags.

-- ---------------------------------------------------------------------------
-- (a) Time per power / pace zone, per activity, from per-second samples.
--
-- HR zones cannot see interval structure (HR lags 60-90 s behind effort): a 40/20
-- session looks like Zone 3 soup through %LTHR glasses (Coach 1 point 3). This view
-- recomputes time-in-zone FROM SAMPLES against the threshold IN EFFECT ON THE DATE,
-- so correcting a threshold propagates to the whole history by simple REFRESH --
-- same principle as mart.hr_zone_time (007) and the arbitrated read path of 011.
--
-- Conventions, documented because sources contradict each other:
-- - bike, zone_system = ''power_pct_ftp'': Coggan-style percentages of FTP --
--   Z1 <55%, Z2 [55,75), Z3 [75,90), Z4 [90,105), Z5 >=105%. Half-open lower bounds
--   ([lo, hi)) exactly like core.hr_tss_coefficient, so no watt is counted twice or
--   skipped at a boundary.
-- - run, zone_system = ''pace_pct_vth'': thresholds on SPEED as percent of threshold
--   speed v_th = 1000.0 / ftp_pace_s_per_km (s/km inverted to m/s). FASTER than
--   threshold = HIGHER zone: Z1 <70%, Z2 [70,85), Z3 [85,100), Z4 [100,115),
--   Z5 >=115% of v_th. Speed orientation is the reverse of pace orientation -- bounds
--   are stated as % OF SPEED on purpose, matching how the coach reads the histogram
--   ("how much time faster than threshold pace").
--
-- Exclusions, identical in spirit to every mart view: multisport parents excluded
-- (is_parent = false); NULL samples excluded (a second without power/speed belongs to
-- no zone); activities whose threshold row is missing or lacks the relevant value are
-- skipped entirely via LEFT JOIN + WHERE value IS NOT NULL (an unanchored zone split
-- would be fiction -- NULL means "not computable", never 0).

CREATE MATERIALIZED VIEW mart.power_pace_zone_time AS
WITH base AS (
    SELECT a.activity_id, a.local_date, a.sport_code, s.discipline,
           th.ftp_watts,
           CASE WHEN th.ftp_pace_s_per_km > 0
                THEN 1000.0 / th.ftp_pace_s_per_km
           END AS v_threshold_mps
      FROM core.activity a
      JOIN core.sport s ON s.sport_code = a.sport_code
      LEFT JOIN core.threshold th
             ON th.athlete_id = a.athlete_id
            AND th.discipline = s.discipline
            AND th.validity @> a.local_date
     WHERE a.is_parent = false
),
bike_samples AS (
    SELECT b.activity_id, b.local_date, b.sport_code,
           'power_pct_ftp' AS zone_system,
           CASE
             WHEN sm.power < 0.55 * b.ftp_watts THEN 'Z1'
             WHEN sm.power < 0.75 * b.ftp_watts THEN 'Z2'
             WHEN sm.power < 0.90 * b.ftp_watts THEN 'Z3'
             WHEN sm.power < 1.05 * b.ftp_watts THEN 'Z4'
             ELSE 'Z5'
           END AS zone_code
      FROM base b
      JOIN core.sample sm ON sm.activity_id = b.activity_id
     WHERE b.discipline = 'bike'
       AND b.ftp_watts IS NOT NULL
       AND sm.power IS NOT NULL
),
run_samples AS (
    SELECT b.activity_id, b.local_date, b.sport_code,
           'pace_pct_vth' AS zone_system,
           CASE
             WHEN sm.speed_mps < 0.70 * b.v_threshold_mps THEN 'Z1'
             WHEN sm.speed_mps < 0.85 * b.v_threshold_mps THEN 'Z2'
             WHEN sm.speed_mps < 1.00 * b.v_threshold_mps THEN 'Z3'
             WHEN sm.speed_mps < 1.15 * b.v_threshold_mps THEN 'Z4'
             ELSE 'Z5'
           END AS zone_code
      FROM base b
      JOIN core.sample sm ON sm.activity_id = b.activity_id
     WHERE b.discipline = 'run'
       AND b.v_threshold_mps IS NOT NULL
       AND sm.speed_mps IS NOT NULL
)
SELECT z.activity_id, z.local_date, z.sport_code, z.zone_system, z.zone_code,
       count(*)::integer AS seconds
  FROM (
           SELECT * FROM bike_samples
           UNION ALL
           SELECT * FROM run_samples
       ) z
 GROUP BY z.activity_id, z.local_date, z.sport_code, z.zone_system, z.zone_code;

CREATE UNIQUE INDEX power_pace_zone_time_pk
    ON mart.power_pace_zone_time (activity_id, zone_system, zone_code);

COMMENT ON MATERIALIZED VIEW mart.power_pace_zone_time IS
  'Time per power zone (bike, %FTP, Coggan convention documented above) and per pace zone (run, %threshold SPEED, faster = higher), computed from core.sample per-second data against the threshold IN EFFECT ON THE ACTIVITY DATE. DERIVED not stored: changing zone bounds or correcting a threshold rewrites the whole history by simple REFRESH, no re-ingestion -- the reason this is a view over samples and not a column frozen at parse time (Coach 1 point 3 / Q5). One row per (activity, system, zone); an activity appears under exactly ONE system (its discipline decides). Swim deliberately absent until CSS-relative zones are specified (Q5 priority 3). The UNIQUE index conditions REFRESH CONCURRENTLY.';

COMMENT ON COLUMN mart.power_pace_zone_time.seconds IS
  'Whole seconds spent in the zone (one sample = one second). Sum over an activity''s zones equals its usable-sample seconds, which may be less than duration_s where sensors dropped -- same honesty rule as the HR histogram.';


-- ---------------------------------------------------------------------------
-- (b) Ramp-rate / monotony / strain risk table on the dense PMC calendar.
--
-- Three numbers the coach acts on weekly (Ramp & Risk board):
-- - dctl_per_week: CTL minus CTL 7 days earlier, the interpretable ramp rate with
--   bands <=3 sustainable / 3-5 watch / >5 caution / >8 red (Q3, Coach 3 section 2.4
--   keeps Coach 1's version over ATL-spike proxies).
-- - monotony (Foster): mean/stddev of DAILY TRIMP over the trailing 7 days INCLUDING
--   today, rest days as zeros not absences (dense calendar discipline). stddev = 0 or
--   fewer than 7 days of history -> NULL, never a fake number.
-- - strain: weekly TRIMP sum x monotony, entirely within the TRIMP scale (violates
--   nothing; Coach 3 section 3). Monotony >2.0 with rising strain is textbook illness
--   territory.
-- - estimated_share_7d: share of the week''s TSS from days with estimated load --
--   the Q4 discount made queryable ("if >30%, discount CTL movement").

CREATE MATERIALIZED VIEW mart.ramp_risk AS
WITH trimp_day AS (
    SELECT a.athlete_id, a.local_date, sum(m.value) AS trimp
      FROM core.activity a
      JOIN core.activity_metric m
        ON m.activity_id = a.activity_id AND m.metric = 'trimp' AND m.is_canonical
     WHERE a.is_parent = false
     GROUP BY a.athlete_id, a.local_date
),
dense AS (
    SELECT p.athlete_id, p.local_date, p.tss, p.ctl, p.atl, p.tsb, p.ctl_reliable,
           COALESCE(t.trimp, 0.0)::double precision AS trimp,
           CASE WHEN dl.has_estimated_load THEN p.tss ELSE 0.0 END AS estimated_tss
      FROM mart.pmc p
      LEFT JOIN trimp_day t
             ON t.athlete_id = p.athlete_id AND t.local_date = p.local_date
      LEFT JOIN mart.daily_load dl
             ON dl.athlete_id = p.athlete_id AND dl.local_date = p.local_date
),
windowed AS (
    SELECT d.*,
           d.ctl - LAG(d.ctl, 7) OVER w AS dctl_per_week,
           avg(d.trimp)  OVER w7 AS trimp_mean_7d,
           stddev_samp(d.trimp) OVER w7 AS trimp_stddev_7d,
           count(*)     OVER w7 AS days_in_window,
           sum(d.trimp) OVER w7 AS weekly_trimp_sum,
           sum(d.tss)   OVER w7 AS tss_7d,
           sum(d.estimated_tss) OVER w7 AS estimated_tss_7d
      FROM dense d
     WINDOW w  AS (PARTITION BY d.athlete_id ORDER BY d.local_date),
            w7 AS (PARTITION BY d.athlete_id ORDER BY d.local_date
                   ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
)
SELECT athlete_id,
       local_date,
       tss,
       trimp,
       ctl,
       atl,
       tsb,
       ctl_reliable,
       dctl_per_week,
       CASE WHEN days_in_window = 7 AND trimp_stddev_7d > 0
            THEN trimp_mean_7d / trimp_stddev_7d
       END AS monotony,
       weekly_trimp_sum * (CASE WHEN days_in_window = 7 AND trimp_stddev_7d > 0
                                THEN trimp_mean_7d / trimp_stddev_7d
                           END) AS strain,
       CASE WHEN tss_7d > 0
            THEN estimated_tss_7d / tss_7d
       END AS estimated_share_7d
  FROM windowed;

CREATE UNIQUE INDEX ramp_risk_pk ON mart.ramp_risk (athlete_id, local_date);

COMMENT ON MATERIALIZED VIEW mart.ramp_risk IS
  'Per-day ramp-risk table on the DENSE pmc calendar: canonical daily TRIMP (zeros on rest days -- zeros, NOT absences, or mean/stddev and hence monotony lie), delta-CTL-per-week by exact 7-day lag (the calendar is dense, so lag 7 is exactly 7 days), Foster monotony and strain within the TRIMP scale, and the 7-day estimated-load share. Reads mart.pmc/daily_load: refresh AFTER them (see header). All derived business logic lives here, not in Grafana transforms (COACH_FEEDBACK_3 section 5 point 2). UNIQUE index conditions REFRESH CONCURRENTLY.';

COMMENT ON COLUMN mart.ramp_risk.trimp IS
  'Sum of canonical TRIMP for the day (core.activity_metric metric=''trimp'', is_canonical), COALESCEd to 0 when none: a rest day contributes a zero to the trailing-week statistics, it is not a hole in the series. TRIMP stays on ITS OWN scale -- never summed into anything TSS (04 section 5.1, reaffirmed by both coaches).';

COMMENT ON COLUMN mart.ramp_risk.dctl_per_week IS
  'ctl today minus ctl exactly 7 days earlier. NULL for the first 7 days of the calendar. Bands (Grafana draws them, view emits the number): <=3 sustainable, 3-5 productive-but-watch, >5 caution, >8 red.';

COMMENT ON COLUMN mart.ramp_risk.monotony IS
  'Foster monotony: mean / stddev_samp of daily TRIMP over the trailing 7 days INCLUDING today. Frame is ROWS-based over the dense calendar, so it counts DAYS (zeros included). NULL when stddev is 0 (perfectly uniform week -- division undefined) or when fewer than 7 days of history exist: no fake value.';

COMMENT ON COLUMN mart.ramp_risk.strain IS
  'weekly_trimp_sum * monotony (Foster strain). NULL-safe: NULL whenever monotony is NULL. Units are TRIMP-squared-by-construction; interpret relative to the athlete''s own history only.';

COMMENT ON COLUMN mart.ramp_risk.estimated_share_7d IS
  'Share of the trailing 7 days'' TSS contributed by days where mart.daily_load.has_estimated_load. NULL when the window carries no load at all (tss_7d = 0): "no data" must stay distinguishable from "0% estimated". Above ~30%, discount the week''s CTL movement (Coach 1 Q4).';


-- ---------------------------------------------------------------------------
-- (c) Fired-rule feed: one row per (day, rule) that fired. Advisory ONLY.
--
-- Coach 1 Q8 definition, arbitrated EXACTLY by Coach 3 section 2.2 ("Implement Coach
-- 1''s amber/red flag set exactly"): explicit rules, no composite score -- "TSB -34 AND
-- HRV below baseline 3 nights" is actionable, "78/100 GOOD" is pseudo-precision that
-- anchors behavior. Amber rules fire independently; RED fires when any THREE DISTINCT
-- amber rules fire the same day, or on the single HRV-crash override.
--
-- AMBER rules (all five implemented literally):
--   tsb_below_minus30       mart.pmc.tsb < -30
--   hrv_below_baseline_3n   hrv_value below hrv_baseline 3 consecutive nights
--   rhr_above_baseline      rhr_dev_bpm > 5
--   sleep_lt6h_2n           duration_s < 6*3600 two consecutive wake_dates
--   dctl_gt8                ramp_risk.dctl_per_week > 8
-- RED:
--   red_three_ambers        >= 3 distinct amber rules fired the same day
--   hrv_crash               hrv_dev_ratio <= -0.15
--
-- WHY AN ABSOLUTE RATIO FOR THE CRASH, NOT "-2 SD OF RATIOS": computing an SD of
-- deviation ratios would need a second rolling window whose own crash contamination
-- problem recurses (the crash inflates the SD, muting the very flag it triggers). A
-- fixed ratio keeps the view SELF-CONTAINED and explainable months later; -0.15 (~15%
-- below baseline) is retained as the documented ~2 SD proxy for a calibrated n=1 HRV
-- series. After 3-6 months of log-only operation the constant is revisited against
-- fired-rule history (Coach 3 section 3 "calibration of the bands"), not before.
--
-- CONSECUTIVENESS SEMANTICS. hrv_below_baseline_3n uses LAG(1)/LAG(2) over
-- mart.wellness_baseline, whose calendar is DENSE per athlete -- so lag n is exactly
-- n calendar nights back, and NULL readings make the comparison NULL -> rule silent
-- (`IS TRUE` coercion): missing data never fires an alarm. sleep_lt6h_2n requires the
-- two nights to be consecutive CALENDAR wake_dates (prev_wake_date = wake_date - 1):
-- a gap of unworn-watch days resets the pair, which is conservative and intended.
--
-- NOTE: PostgreSQL forbids CHECK constraints on materialized views. level is
-- restricted to (''amber'',''red'') BY CONSTRUCTION -- the only literals ever emitted
-- are written inline below. If this view is ever rebuilt, keep it that way.

CREATE MATERIALIZED VIEW mart.coach_flags AS
WITH hrv_nights AS (
    SELECT b.athlete_id, b.local_date, b.hrv_dev_ratio, b.rhr_dev_bpm,
           (b.hrv_value < b.hrv_baseline) AS below,
           LAG(b.hrv_value < b.hrv_baseline, 1) OVER w AS below_prev1,
           LAG(b.hrv_value < b.hrv_baseline, 2) OVER w AS below_prev2
      FROM mart.wellness_baseline b
     WINDOW w AS (PARTITION BY b.athlete_id ORDER BY b.local_date)
),
sleep_nights AS (
    SELECT s.athlete_id, s.wake_date, s.duration_s,
           LAG(s.duration_s)  OVER sp AS prev_duration_s,
           LAG(s.wake_date)   OVER sp AS prev_wake_date
      FROM core.sleep_session s
     WINDOW sp AS (PARTITION BY s.athlete_id ORDER BY s.wake_date)
),
wide AS (
    SELECT p.athlete_id, p.local_date,
           (p.tsb < -30)                                   AS tsb_below_minus30,
           (n.below IS TRUE AND n.below_prev1 IS TRUE AND n.below_prev2 IS TRUE)
                                                           AS hrv_below_baseline_3n,
           (n.rhr_dev_bpm > 5)                             AS rhr_above_baseline,
           (s.duration_s < 21600 AND s.prev_duration_s < 21600
            AND s.wake_date = s.prev_wake_date + 1)        AS sleep_lt6h_2n,
           (r.dctl_per_week > 8)                           AS dctl_gt8,
           (n.hrv_dev_ratio <= -0.15)                      AS hrv_crash,
           p.tsb, n.rhr_dev_bpm, n.hrv_dev_ratio,
           s.duration_s, r.dctl_per_week
      FROM mart.pmc p
      LEFT JOIN hrv_nights n
             ON n.athlete_id = p.athlete_id AND n.local_date = p.local_date
      LEFT JOIN mart.ramp_risk r
             ON r.athlete_id = p.athlete_id AND r.local_date = p.local_date
      LEFT JOIN sleep_nights s
             ON s.athlete_id = p.athlete_id AND s.wake_date = p.local_date
),
ambers AS (
    SELECT w.athlete_id, w.local_date, v.rule_code, v.detail
      FROM wide w
      CROSS JOIN LATERAL (VALUES
          ('tsb_below_minus30',     w.tsb_below_minus30,
           'TSB ' || round(w.tsb::numeric, 0) || ' < -30'),
          ('hrv_below_baseline_3n', w.hrv_below_baseline_3n,
           'HRV below baseline 3 consecutive nights'),
          ('rhr_above_baseline',    w.rhr_above_baseline,
           'RHR +' || round(w.rhr_dev_bpm::numeric, 0) || ' bpm vs baseline'),
          ('sleep_lt6h_2n',         w.sleep_lt6h_2n,
           'sleep ' || round((w.duration_s / 3600.0)::numeric, 1)
               || ' h (< 6 h two consecutive nights)'),
          ('dctl_gt8',              w.dctl_gt8,
           'dCTL/week ' || round(w.dctl_per_week::numeric, 1) || ' > 8')
      ) AS v(rule_code, fired, detail)
     WHERE v.fired
)
SELECT f.athlete_id, f.local_date, f.rule_code, f.level, f.detail
  FROM (
           SELECT a.athlete_id, a.local_date, a.rule_code,
                  'amber'::text AS level, a.detail
             FROM ambers a
           UNION ALL
           SELECT w.athlete_id, w.local_date, 'hrv_crash',
                  'red'::text AS level,
                  'HRV crash: deviation '
                       || round((w.hrv_dev_ratio * 100)::numeric, 0)
                      || '% <= -15% (~2 SD proxy)'
             FROM wide w
            WHERE w.hrv_crash
           UNION ALL
           SELECT a.athlete_id, a.local_date, 'red_three_ambers',
                  'red'::text AS level,
                  '3 distinct amber rules same day: '
                      || string_agg(a.rule_code, ', ' ORDER BY a.rule_code)
             FROM ambers a
            GROUP BY a.athlete_id, a.local_date
           HAVING count(*) >= 3
       ) f;

CREATE UNIQUE INDEX coach_flags_pk
    ON mart.coach_flags (athlete_id, local_date, rule_code);

COMMENT ON MATERIALIZED VIEW mart.coach_flags IS
  'Fired amber/red rule feed, one row per (athlete, day, rule): the explicit-rules engine Coach 3 section 2.2 arbitrates over any composite readiness index. ADVISORY ONLY -- informs the go/no-go, never decides it (section 5 point 11); run log-only 4-6 weeks before letting it color anything. detail carries the numbers so the feed stays explainable without drill-down. Red derivation pivots ambers per day: >= 3 distinct amber rules the same day, plus the single hrv_crash override (-0.15 absolute dev-ratio proxy for 2 SD, rationale in header). Depends on mart.wellness_baseline (018) and mart.ramp_risk: refresh AFTER them (header of 019). UNIQUE index conditions REFRESH CONCURRENTLY.';

COMMENT ON COLUMN mart.coach_flags.level IS
  '''amber'' or ''red''. No CHECK constraint is possible on a materialized view; the vocabulary is enforced by construction (see header). Red prescription: aerobic-only or rest until two green mornings.';

COMMENT ON COLUMN mart.coach_flags.rule_code IS
  'Stable machine identifier of the fired rule (tsb_below_minus30, hrv_below_baseline_3n, rhr_above_baseline, sleep_lt6h_2n, dctl_gt8, hrv_crash, red_three_ambers). Rename nothing here without migrating dashboards that filter on it.';

COMMENT ON COLUMN mart.coach_flags.detail IS
  'Short human-readable text WITH the triggering numbers (e.g. "TSB -34 < -30") -- the difference between a flag you act on and a flag you mute.';
