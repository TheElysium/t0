-- Migration 026: expose bike_avg_hr in mart.triathlon_session.
--
-- The Segment breakdown table of the triathlon dashboard shows Avg HR for swim and
-- run but left bike NULL: the column was simply missing from the 025 contract, while
-- the bike leg stores avg_hr like any other leg (data verified on the 2026-09-05
-- event: 159 bpm). Rebuild pattern follows 022/025: a materialized view cannot be
-- ALTERed, so 025's definition is re-emitted with the one added column (after
-- bike_distance_m, mirroring the swim/run column order).

DROP MATERIALIZED VIEW IF EXISTS mart.triathlon_session;

CREATE MATERIALIZED VIEW mart.triathlon_session AS
SELECT p.activity_id, p.local_date, p.start_time_local, p.name, p.sport_code,
       agg.legs_count,
       COALESCE(agg.duration_s, p.duration_s) AS duration_s,
       agg.distance_m, agg.avg_hr, agg.max_hr,
       lm.tss, lm.trimp,
       sw.activity_id   AS swim_activity_id,
       sw.duration_s    AS swim_duration_s,
       sw.distance_m    AS swim_distance_m,
       sw.avg_hr        AS swim_avg_hr,
       t1.activity_id   AS t1_activity_id,
       t1.duration_s    AS t1_duration_s,
       bk.activity_id   AS bike_activity_id,
       bk.duration_s    AS bike_duration_s,
       bk.distance_m    AS bike_distance_m,
       bk.avg_hr        AS bike_avg_hr,
       bk.avg_speed_mps AS bike_avg_speed_mps,
       bk.avg_power     AS bike_avg_power,
       bk.tss           AS bike_tss,
       t2.activity_id   AS t2_activity_id,
       t2.duration_s    AS t2_duration_s,
       rn.activity_id   AS run_activity_id,
       rn.duration_s    AS run_duration_s,
       rn.distance_m    AS run_distance_m,
       rn.avg_hr        AS run_avg_hr,
       rn.tss           AS run_tss
  FROM core.activity p
  LEFT JOIN LATERAL (
      SELECT count(*)  AS legs_count,
             sum(l.duration_s) AS duration_s,
             sum(l.distance_m) AS distance_m,
             sum(l.avg_hr * l.duration_s)
                 / NULLIF(sum(l.duration_s) FILTER (WHERE l.avg_hr IS NOT NULL), 0) AS avg_hr,
             max(l.max_hr) AS max_hr
        FROM core.activity l
       WHERE l.parent_activity_id = p.activity_id
  ) agg ON true
  LEFT JOIN LATERAL (
      SELECT sum(m.value) FILTER (WHERE m.metric = 'tss' AND m.is_canonical) AS tss,
             sum(m.value) FILTER (WHERE m.metric = 'trimp')                 AS trimp
        FROM core.activity_metric m
        JOIN core.activity l ON l.activity_id = m.activity_id
       WHERE l.parent_activity_id = p.activity_id
  ) lm ON true
  LEFT JOIN LATERAL (
      SELECT l.activity_id, l.duration_s, l.distance_m, l.avg_hr
        FROM core.activity l
       WHERE l.parent_activity_id = p.activity_id
         AND l.sport_code IN ('lap_swimming', 'open_water')
       ORDER BY l.leg_order
       LIMIT 1
  ) sw ON true
  LEFT JOIN LATERAL (
      -- leg_order is projected for the T1/T2 laterals below, never exposed by the view.
      SELECT l.activity_id, l.duration_s, l.avg_power, l.avg_speed_mps, l.avg_hr,
             l.distance_m, l.leg_order,
             (SELECT sum(m.value) FROM core.activity_metric m
               WHERE m.activity_id = l.activity_id
                 AND m.metric = 'tss' AND m.is_canonical) AS tss
        FROM core.activity l
       WHERE l.parent_activity_id = p.activity_id
         AND l.sport_code IN ('cycling', 'virtual_cycling')
       ORDER BY l.leg_order
       LIMIT 1
  ) bk ON true
  LEFT JOIN LATERAL (
       -- leg_order is projected for the T1/T2 laterals below, never exposed by the view.
       SELECT l.activity_id, l.duration_s, l.distance_m, l.avg_hr, l.leg_order,
             (SELECT sum(m.value) FROM core.activity_metric m
               WHERE m.activity_id = l.activity_id
                 AND m.metric = 'tss' AND m.is_canonical) AS tss
        FROM core.activity l
       WHERE l.parent_activity_id = p.activity_id
         AND l.sport_code IN ('running', 'trail_running', 'treadmill')
       ORDER BY l.leg_order
       LIMIT 1
  ) rn ON true
   LEFT JOIN LATERAL (
       SELECT l.activity_id, l.duration_s
         FROM core.activity l
        WHERE l.parent_activity_id = p.activity_id
          AND l.sport_code = 'transition'
          AND l.leg_order < COALESCE(bk.leg_order, rn.leg_order)
        ORDER BY l.leg_order DESC
        LIMIT 1
   ) t1 ON true
   LEFT JOIN LATERAL (
       SELECT l.activity_id, l.duration_s
         FROM core.activity l
        WHERE l.parent_activity_id = p.activity_id
          AND l.sport_code = 'transition'
          AND l.leg_order > COALESCE(bk.leg_order, rn.leg_order)
        ORDER BY l.leg_order
        LIMIT 1
   ) t2 ON true
 WHERE p.is_parent = true;

CREATE UNIQUE INDEX triathlon_session_pk ON mart.triathlon_session (activity_id);

COMMENT ON MATERIALIZED VIEW mart.triathlon_session IS
  'ONE ROW PER MULTISPORT (TRIATHLON) EVENT: whole-race aggregates plus one column block per leg (swim, T1, bike, T2, run). The inverse operation of the t0.transform.multisport split (migration 023): the transform fans one FIT file out into leg rows so load views count each discipline with its own method; this view fans them back in so dashboards show the race as a whole. Complements mart.activity_wide (which filters is_parent = false and thus shows the legs but not the event). The parent row of core.activity is NEVER read for metrics (migration 004: its totals are unusable) -- everything is rebuilt from the legs. Migration 025. Migration 026: bike_avg_hr added.';

COMMENT ON COLUMN mart.triathlon_session.duration_s IS
  'Whole-race duration, SUM of the leg durations (timer time per leg). The parent''s own duration_s is only a fallback while legs are absent (migration 024 deletes them until re-transform). Seconds, no conversion.';

COMMENT ON COLUMN mart.triathlon_session.avg_hr IS
  'Duration-weighted average of the leg avg_hr values; legs without HR drop out of both numerator and denominator (NULL-safe), never counted as 0 bpm.';

COMMENT ON COLUMN mart.triathlon_session.swim_activity_id IS
  'Activity_id of the swim leg. Exists for Grafana drill-down links (cast ::text in the dashboard: ids must survive a JSON/float64 round-trip, migration 024). NULL if the event has no swim leg.';

COMMENT ON COLUMN mart.triathlon_session.t1_activity_id IS
  'Activity_id of the FIRST transition (nearest transition leg before the pivot leg: bike, else run for aquathlon). Exists for Grafana drill-down links; NULL if there is no preceding transition.';

COMMENT ON COLUMN mart.triathlon_session.bike_avg_hr IS
  'Average heart rate of the bike leg (core.activity.avg_hr). Migration 026: was missing from the 025 contract while swim and run had theirs.';

COMMENT ON COLUMN mart.triathlon_session.bike_activity_id IS
  'Activity_id of the bike leg. Exists for Grafana drill-down links. NULL if the event has no bike leg.';

COMMENT ON COLUMN mart.triathlon_session.t2_activity_id IS
  'Activity_id of the SECOND transition (nearest transition leg after the pivot leg: bike, else run). Exists for Grafana drill-down links; NULL if there is no following transition.';

COMMENT ON COLUMN mart.triathlon_session.run_activity_id IS
  'Activity_id of the run leg. Exists for Grafana drill-down links. NULL if the event has no run leg.';

COMMENT ON COLUMN mart.triathlon_session.bike_tss IS
  'Canonical TSS of the bike leg alone (core.activity_metric, metric=''tss'' AND is_canonical). Never summed with swim/run TSS into a race TSS here: race-level load lives in the tss column (sum over all legs).';

COMMENT ON COLUMN mart.triathlon_session.run_tss IS
  'Canonical TSS of the run leg alone (core.activity_metric, metric=''tss'' AND is_canonical). Never summed with swim/bike TSS into a race TSS here: race-level load lives in the tss column (sum over all legs).';
