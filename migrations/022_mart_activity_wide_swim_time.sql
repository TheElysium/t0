-- Migration 022: expose swim_time_s in mart.activity_wide (MCP audit B6).
--
-- duration_s is ELAPSED time; sTSS/IF are computed on swim_time_s, the active
-- time rebuilt from pool lengths. Exposing only duration_s made swim IF/TSS
-- unverifiable by the client (an IF of 0.94 looked wrong against elapsed time).
-- Rebuild pattern follows 011: a materialized view cannot be ALTERed.

DROP MATERIALIZED VIEW IF EXISTS mart.activity_wide;

CREATE MATERIALIZED VIEW mart.activity_wide AS
SELECT a.activity_id, a.local_date, a.start_time_local, a.sport_code,
       sp.label AS sport, a.name, a.duration_s, a.swim_time_s, a.distance_m,
       a.avg_hr, a.max_hr, a.avg_power, a.np_watts, a.np_reliable,
       a.ascent_m, a.data_quality, a.needs_manual_rpe,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.is_canonical) AS tss,
       max(m.method_code) FILTER (WHERE m.metric = 'tss' AND m.is_canonical) AS tss_method,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.method_code = 'hr_hrtss') AS tss_hr,
       max(m.value) FILTER (WHERE m.metric = 'trimp')                     AS trimp,
       max(m.value) FILTER (WHERE m.metric = 'intensity_factor' AND m.is_canonical)
                                                                          AS intensity_factor,
       max(m.method_code) FILTER (WHERE m.metric = 'intensity_factor' AND m.is_canonical)
                                                                          AS intensity_factor_method,
       max(m.value) FILTER (WHERE m.metric = 'decoupling_pct')            AS decoupling_pct,
       max(m.value) FILTER (WHERE m.metric = 'efficiency_factor')         AS efficiency_factor
  FROM core.activity a
  JOIN core.sport sp ON sp.sport_code = a.sport_code
  LEFT JOIN core.activity_metric m ON m.activity_id = a.activity_id
 WHERE a.is_parent = false
 GROUP BY a.activity_id, sp.label;

CREATE UNIQUE INDEX activity_wide_pk ON mart.activity_wide (activity_id);

COMMENT ON MATERIALIZED VIEW mart.activity_wide IS
  'WIDE view of one row per activity, intended for Grafana dashboards, and AUTHORITATIVE READ PATH for per-session metrics. Materializes the "narrow table for writing, wide view for reading" pattern: core.activity_metric remains normalized and extensible, while Grafana consumes a tabular format without pivoting on the dashboard. The _method columns are deliberately exposed -- knowing that a TSS comes from HR not power, or that an IF comes from GAP not a power threshold, changes its interpretation. Migration 011: intensity_factor_method added. Migration 022: swim_time_s added -- duration_s is elapsed time, sTSS/IF use the active time rebuilt from pool lengths.';

COMMENT ON COLUMN mart.activity_wide.swim_time_s IS
  'Active swim time in seconds, rebuilt from pool lengths (rest-at-wall excluded). NULL for every non-swim sport. Swim sTSS and IF divide by THIS, never by duration_s: elapsed time overstates sTSS by 30-50% on intervals.';
