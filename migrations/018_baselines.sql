-- Migration 018: rolling HRV / resting-HR baselines and deviations (mart.wellness_baseline).
-- Source: docs/COACH_FEEDBACK.md point 6, arbitrated by docs/COACH_FEEDBACK_3.md
-- sections 2.2 (median, not mean) and 5 point 1 (baseline off-by-one).
-- Batch L1 (coaching dashboards) -- do not modify the DDL manually outside migration.

-- WHY. Absolute HRV values are nearly meaningless; deviation from a personal 28-day
-- rolling baseline is the actionable quantity (Coach 1 point 6). Two method decisions,
-- both arbitrated:
--
-- (1) MEDIAN, not mean (Coach 3 section 2.2): a crash night drags a mean for up to four
--     weeks -- the baseline would chase the very anomaly it exists to detect. The median
--     shrugs off one outlier by construction.
--
-- (2) THE FRAME EXCLUDES TODAY (Coach 3 section 5 point 1): a window that includes the
--     current reading regresses today toward itself and muffles every signal. The frame
--     is exactly the 28 calendar days ENDING YESTERDAY.
--
-- IMPLEMENTATION NOTE -- why this is NOT written `percentile_cont(0.5) WITHIN GROUP
-- (...) OVER (ORDER BY local_date ROWS BETWEEN 27 PRECEDING AND 1 PRECEDING)`, even
-- though that is the literal frame semantics: PostgreSQL rejects ordered-set aggregates
-- as window functions outright ("ERROR: OVER is not supported for ordered-set aggregate
-- percentile_cont", still true on PG 16; only hypothetical-set aggregates like rank()
-- work with OVER). The self-join below is semantically IDENTICAL to that frame over the
-- dense calendar: for each row of `cal` it pairs EXACTLY the 28 rows whose local_date is
-- in [d-27, d-1] -- ROWS BETWEEN 27 PRECEDING AND 1 PRECEDING counted on the dense
-- calendar -- and aggregates them with percentile_cont as a plain GROUP BY aggregate,
-- which PostgreSQL does support. Because the calendar is DENSE (generate_series, same
-- discipline as mart.pmc), each calendar day contributes exactly one paired row, whether
-- or not wellness data exists that day: the frame counts DAYS, not non-NULL readings --
-- identical to ROWS (which counts rows) rather than RANGE semantics. percentile_cont
-- ignores NULL aggregated input (documented PG behavior), so a missing watch day neither
-- shifts nor blocks the median; if all 28 days are missing the baseline is NULL and every
-- deviation downstream stays NULL instead of inventing a number.
--
-- KNOWN LIMITATION, DOCUMENTED NOT SOLVED (Coach 3 sections 2.2 and 5): Garmin firmware
-- updates have historically shifted HRV values wholesale. The right fix -- close the
-- baseline period on device/firmware change and mark it unreliable, exactly like
-- core.threshold validity periods close on power_source change -- needs a device-history
-- source that core.* does not carry yet. Until then, log a core.note(kind='other') row on
-- firmware-change dates and read deviations across such boundaries with suspicion.

CREATE MATERIALIZED VIEW mart.wellness_baseline AS
WITH first_day AS (
    SELECT w.athlete_id, min(w.local_date) AS first_local_date
      FROM core.daily_wellness w
     GROUP BY w.athlete_id
),
cal AS (
    SELECT a.athlete_id, generate_series(f.first_local_date, current_date, interval '1 day')::date
                                                                           AS local_date
      FROM core.athlete a
      JOIN first_day f ON f.athlete_id = a.athlete_id
),
paired AS (
    SELECT c.athlete_id, c.local_date,
           w.hrv_last_night_avg AS hrv_value,
           w.resting_hr         AS rhr_value,
           b.hrv_last_night_avg AS window_hrv,
           b.resting_hr         AS window_rhr
      FROM cal c
      LEFT JOIN core.daily_wellness w
             ON w.athlete_id = c.athlete_id AND w.local_date = c.local_date
      LEFT JOIN core.daily_wellness b
             ON b.athlete_id = c.athlete_id
            AND b.local_date >= c.local_date - 27
            AND b.local_date <  c.local_date
),
medians AS (
    SELECT p.athlete_id, p.local_date,
           p.hrv_value, p.rhr_value,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY p.window_hrv) AS hrv_baseline,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY p.window_rhr) AS rhr_baseline
      FROM paired p
     GROUP BY p.athlete_id, p.local_date, p.hrv_value, p.rhr_value
)
SELECT athlete_id,
       local_date,
       hrv_value,
       hrv_baseline,
       CASE WHEN hrv_value IS NOT NULL AND hrv_baseline IS NOT NULL AND hrv_baseline > 0
            THEN hrv_value / hrv_baseline - 1.0
       END AS hrv_dev_ratio,
       rhr_value,
       rhr_baseline,
       CASE WHEN rhr_value IS NOT NULL AND rhr_baseline IS NOT NULL
            THEN rhr_value - rhr_baseline
       END AS rhr_dev_bpm
  FROM medians;

CREATE UNIQUE INDEX wellness_baseline_pk ON mart.wellness_baseline (athlete_id, local_date);

COMMENT ON MATERIALIZED VIEW mart.wellness_baseline IS
  'Per-day HRV and resting-HR readings with their personal 28-day rolling MEDIAN baselines and deviations, for every calendar day since the athlete''s first wellness data (dense calendar via generate_series -- rest/missed days are rows, not absences, same discipline as mart.pmc). Baseline frame = the 28 days ending THE DAY BEFORE (see implementation note above for why the frame excludes today, why median beats mean, and why a self-join stands in for an unsupported percentile_cont OVER). Deviations, not raw values, are what dashboards must plot (COACH_FEEDBACK Q7 column order); core.note(kind IN (''illness'',''injury'',''heat'')) rows mark days that should be excluded when interpreting or recomputing these baselines. Device/firmware changes SHOULD close the baseline period like a threshold validity change -- not yet implementable, documented above. UNIQUE index conditions REFRESH CONCURRENTLY.';

COMMENT ON COLUMN mart.wellness_baseline.hrv_value IS
  'Raw nightly HRV (core.daily_wellness.hrv_last_night_avg), kept alongside the baseline so a panel can draw value + baseline band without rejoining core. NULL on days the watch was not worn or HRV was not published.';

COMMENT ON COLUMN mart.wellness_baseline.hrv_baseline IS
  'Rolling 28-day median of hrv_value over [local_date - 27, local_date - 1]. Median chosen over mean: one crash night drags a mean for weeks (Coach 3 section 2.2). NULL while fewer than one non-NULL reading exists in the window -- deliberately NOT zero-filled.';

COMMENT ON COLUMN mart.wellness_baseline.hrv_dev_ratio IS
  'hrv_value / hrv_baseline - 1: -0.10 means 10% under personal baseline. NULL-safe: NULL whenever value or baseline is missing (never 0 -- 0 is a real reading at exactly baseline). This is the ratio form Coach 1 Q8 flags on ("HRV >1 SD below baseline OR 3 consecutive nights below"); the flag view consumes nights-below-baseline, which needs no SD estimate.';

COMMENT ON COLUMN mart.wellness_baseline.rhr_value IS
  'Raw resting HR (core.daily_wellness.resting_hr), same role as hrv_value.';

COMMENT ON COLUMN mart.wellness_baseline.rhr_baseline IS
  'Rolling 28-day median of rhr_value over [local_date - 27, local_date - 1], same frame and rationale as hrv_baseline.';

COMMENT ON COLUMN mart.wellness_baseline.rhr_dev_bpm IS
  'rhr_value - rhr_baseline in bpm (positive = elevated = worse). Absolute bpm, not ratio: RHR moves in single beats and the coach rule is stated in bpm (+5). NULL-safe like hrv_dev_ratio.';
