-- Migration 011: arbitration of TWO concurrent definitions of intensity_factor.
--
-- THE PROBLEM. Since migration 008, the same number is produced in two places:
--
--   (A) mart.activity_load RECALCULATES it in SQL, with its own method resolution and
--       intensity_factor_method column;
--   (B) core.activity_metric STORES it, written by garminator.transform.activity, and
--       mart.activity_wide reads from it (the "IF" column on the load.json dashboard).
--
-- Two numbers, two calculation paths, no constraint keeping them in sync. Nothing prevented
-- two neighboring panels from displaying two different IFs for the same session,
-- without either being marked as wrong.
--
-- THE ARBITRATION, AND WHY. (B) wins, for a factual reason not aesthetic:
-- (A) IS READ BY NO ONE. Exhaustively verified -- no dashboard queries mart.activity_load
-- (load.json reads activity_wide and daily_load, pmc.json reads pmc, power.json
-- power_curve_best, zones.json hr_zone_time), and no other view joins it (the only join
-- between schema views is pmc -> daily_load). The entire actually wired chain -- daily_load,
-- then pmc, then activity_wide -- stems from core.activity_metric. mart.activity_load is a
-- second implementation, complete and careful, but orphaned.
--
-- The cost was not zero regardless: ops-refresh refreshes it on every pass, at the head of
-- MART_VIEWS, for zero readers.
--
-- WHAT IS DONE HERE, AND WHAT IS NOT. The view is not deleted. It recalculates
-- IF from thresholds IN EFFECT ON THE DATE of each session, which the stored path does not
-- redo by itself: correcting a wrong FTP afterwards propagates to the entire history by
-- simple REFRESH here, whereas core.activity_metric rows stay on the old value until
-- re-transformation. This is exactly the principle already claimed by the mart.hr_zone_time
-- comment. This view thus serves as COUNTER-EXPERTISE, and it is this role that the comment
-- below establishes -- so we stop taking it as a read path, and stop "correcting" it
-- thinking we''re fixing the dashboard.
--
-- Also missing from activity_wide was the counterpart of tss_method: IF was displayed
-- without saying which method it came from. But "IF 0.82" doesn''t mean the same thing whether
-- it comes from power or GAP -- especially since run rFTP is NULL by explicit decision,
-- so all running goes through GAP.

-- A materialized view cannot be REPLACE''d: it must be rebuilt. No risk here,
-- no other view reads activity_wide (verified above).
DROP MATERIALIZED VIEW IF EXISTS mart.activity_wide;

CREATE MATERIALIZED VIEW mart.activity_wide AS
SELECT a.activity_id, a.local_date, a.start_time_local, a.sport_code,
       sp.label AS sport, a.name, a.duration_s, a.distance_m,
       a.avg_hr, a.max_hr, a.avg_power, a.np_watts, a.np_reliable,
       a.ascent_m, a.data_quality, a.needs_manual_rpe,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.is_canonical) AS tss,
       max(m.method_code) FILTER (WHERE m.metric = 'tss' AND m.is_canonical) AS tss_method,
       max(m.value) FILTER (WHERE m.metric = 'tss'  AND m.method_code = 'hr_hrtss') AS tss_hr,
       max(m.value) FILTER (WHERE m.metric = 'trimp')                     AS trimp,
       -- AND m.is_canonical is load-bearing on BOTH aggregates below, exactly as on the
       -- tss pair above. core.activity_metric holds one intensity_factor row PER METHOD,
       -- most of them with a NULL value (the method did not apply). Without the filter:
       --   * max(method_code) ignores whether the value is NULL and returns the
       --     alphabetically last code, so a run whose IF actually came from run_gap was
       --     labelled 'run_power' -- the very confusion this column exists to prevent.
       --   * max(value) picks the LARGEST value across methods rather than the canonical
       --     one, which silently overstates IF as soon as two methods both resolve.
       max(m.value) FILTER (WHERE m.metric = 'intensity_factor' AND m.is_canonical)
                                                                          AS intensity_factor,
       -- ADDITION 011: the counterpart of tss_method. Same reason, same necessity.
       max(m.method_code) FILTER (WHERE m.metric = 'intensity_factor' AND m.is_canonical)
                                                                          AS intensity_factor_method,
       max(m.value) FILTER (WHERE m.metric = 'decoupling_pct')            AS decoupling_pct,
       max(m.value) FILTER (WHERE m.metric = 'efficiency_factor')         AS efficiency_factor
  FROM core.activity a
  JOIN core.sport sp ON sp.sport_code = a.sport_code
  LEFT JOIN core.activity_metric m ON m.activity_id = a.activity_id
 WHERE a.is_parent = false
 GROUP BY a.activity_id, sp.label;

-- UNIQUE index recreated identically: without it, no REFRESH ... CONCURRENTLY,
-- so Grafana locks during every refresh.
CREATE UNIQUE INDEX activity_wide_pk ON mart.activity_wide (activity_id);

COMMENT ON MATERIALIZED VIEW mart.activity_wide IS
  'WIDE view of one row per activity, intended for Grafana dashboards, and AUTHORITATIVE READ PATH for per-session metrics. Materializes the "narrow table for writing, wide view for reading" pattern: core.activity_metric remains normalized and extensible, while Grafana consumes a tabular format without pivoting on the dashboard. The _method columns are deliberately exposed -- knowing that a TSS comes from HR not power, or that an IF comes from GAP not a power threshold, changes its interpretation. Migration 011: intensity_factor_method added, and arbitration decided in favor of this path over SQL recalculation in mart.activity_load.';

COMMENT ON COLUMN mart.activity_wide.intensity_factor_method IS
  'Method that produced intensity_factor, vocabulary aligned with core.load_method. NULL when no method is applicable -- notably road bike without power meter, where IF is deliberately not inverted from hrTSS (it would be a number derived from itself). For running, equals GAP as long as rftp_watts is NULL, which is the case by explicit athlete decision.';

COMMENT ON MATERIALIZED VIEW mart.activity_load IS
  'COUNTER-EXPERTISE, NOT A READ PATH. Recalculates load and intensity from thresholds IN EFFECT ON THE DATE of each session, where mart.activity_wide exposes values STORED in core.activity_metric at transformation time. Migration 011: no dashboard nor view reads this one, and arbitration favored the stored path -- do not wire a panel to it without re-reading 011. Its value is where the two diverge: a gap between its intensity_factor and mart.activity_wide''s means a threshold was corrected AFTER ingestion and the relevant core.activity_metric rows need re-transformation. It is a tool for detecting stale values, and the only place this drift is visible. The is_parent = false filter prevents double-counting multisport sequences; the UNIQUE index conditions REFRESH CONCURRENTLY.';
