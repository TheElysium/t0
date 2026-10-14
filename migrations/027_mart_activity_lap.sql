-- Migration 027: mart.activity_lap and mart.swim_length, per-lap and per-pool-length
-- detail for the MCP tool get_laps (role t0_mcp_ro reads mart.* + core.threshold only).
--
-- Plain views, not materialized: no REFRESH, no MART_VIEWS / ops health change. The
-- views are created by the migration runner as role t0, so migration 020's default
-- privileges grant SELECT to t0_mcp_ro: no GRANT here. No security_invoker: the views
-- run with OWNER rights (t0) over core.activity_lap, core.swim_length and core.activity,
-- which is the deliberate exposure of those rows (and only those columns) to the MCP.
--
-- Typed columns are whitelisted; extra (jsonb) is NEVER exposed: it holds lap start/end
-- GPS positions. Parents are excluded (is_parent = false); parent_activity_id and
-- leg_order are exposed (NULL on a simple activity). lap_index is the raw FIT
-- message_index, so a leg's laps may not start at 0.
--
-- Rollback = a new forward migration with DROP VIEW (migrations are never edited).

CREATE VIEW mart.activity_lap AS
SELECT l.activity_id, l.lap_index, l.start_time, l.duration_s, l.elapsed_s,
       l.distance_m, l.avg_speed_mps, l.avg_hr, l.max_hr, l.avg_power, l.max_power,
       l.np_watts, l.np_convention, l.avg_cadence, l.ascent_m, l.descent_m,
       l.intensity, l.lap_trigger, l.swim_stroke, l.num_lengths,
       a.parent_activity_id, a.leg_order
  FROM core.activity_lap l
  JOIN core.activity a ON a.activity_id = l.activity_id
 WHERE a.is_parent = false;

COMMENT ON VIEW mart.activity_lap IS
  'Laps (splits) of non-parent activities: auto-lap per device setting on free activities, one lap per step on structured workouts, rest laps included as stored (intensity). avg_hr/max_hr are the UNFILTERED sensor values and can be wrong. np_watts follows np_convention (global_window: not recalculable from lap data). lap_index is the FIT message_index: a multisport leg''s laps may not start at 0. core.activity_lap.extra (GPS) is deliberately not exposed.';

CREATE VIEW mart.swim_length AS
SELECT s.activity_id, s.length_index, s.lap_index, s.start_time, s.duration_s,
       s.length_type, s.swim_stroke, s.total_strokes, s.avg_cadence,
       a.parent_activity_id, a.leg_order
  FROM core.swim_length s
  JOIN core.activity a ON a.activity_id = s.activity_id
 WHERE a.is_parent = false;

COMMENT ON VIEW mart.swim_length IS
  'Pool lengths of non-parent activities. length_type idle = rest at the wall (kept as stored, excluded from active swim time); active = swum length.';
