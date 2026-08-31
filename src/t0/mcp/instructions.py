"""The MCP ``instructions`` block: quotes the migration COMMENT ON text verbatim,
pinned against the migration files by tests/mcp/test_server.py."""

from __future__ import annotations

INSTRUCTIONS = """\
Read-only access to the t0 training marts of a single self-coached athlete
(Garmin Connect data: activities, load, PMC, power curve, wellness, coach
signals, thresholds). All quantities are rounded to display precision; a null
means "no data", never zero.

The interpretation flags below are ALWAYS present in the output. Never read a
number without its caveat:

- ctl_reliable (get_pmc): false for the first 180 DAYS following the seed. The
  seed value still dominates CTL for several months, and CTL displayed as
  growth over this period is an initialization artifact, not training
  progression.
- tss_method (list_activities/get_activity): knowing that a TSS comes from HR
  not power changes its interpretation.
- tss_is_canonical: the TSS value is the canonical one, resolved by the
  declarative fallback matrix "power else HR else RPE"; non-canonical method
  rows are never summed or displayed.
- has_estimated_load (get_pmc): signals that at least one session of the day
  has estimated load (HR or RPE) not measured.
- data_quality / needs_manual_rpe (list_activities/get_activity): surface data
  gaps honestly.
- is_parent (list_activities/get_activity): always false here. Multisport
  sequences (e.g. a triathlon) are stored as one parent row plus one row per
  leg, and every load view filters is_parent = false to avoid double-counting.
  The parent row is never returned: count legs, never legs + parent.
- css_estimated (get_thresholds): true when CSS comes from the coarse fallback;
  an sTSS built on an estimated CSS does not have the same status.

Semantics to respect when interpreting:

- PMC (get_pmc): CTL (42 d), ATL (7 d), TSB. TSB = previous-day CTL -
  previous-day ATL (the reference convention); tsb_same_day is exposed IN
  ADDITION because many tools use the other. The calendar is dense: a rest day
  is a row with tss = 0, not an absence. A daily_load with
  has_estimated_load = true days behind it discounts the CTL movement ("if
  >30% estimated share, discount CTL movement").
- Thresholds (get_thresholds): historized by validity period, half-open
  [valid_from, valid_to); "infinity" means the current threshold. An
  activity's TSS uses the threshold in effect on its date -- otherwise an FTP
  update would retroactively rewrite the entire CTL history. rFTP and bike FTP
  are not comparable and must never be mixed.
- Power curve (get_power_curve): all-time record curve by curve type; "date"
  answers "what day did I set this record?". Windows spanning a pause are
  excluded.
- Wellness (get_wellness): per-day HRV and resting-HR readings with their
  personal 28-day rolling MEDIAN baselines and deviations. Baseline frame =
  the 28 days ending THE DAY BEFORE. Deviations, not raw values, are the
  actionable quantity; a missing watch day neither shifts nor blocks the
  median.
- Coach flags (get_coach_flags): explicit amber/red rules, one row per (day,
  rule), detail carries the triggering numbers. ADVISORY ONLY -- informs the
  go/no-go, never decides it. Red prescription: aerobic-only or rest until two
  green mornings. dCTL/week bands: <=3 sustainable, 3-5 productive-but-watch,
  >5 caution, >8 red.
"""
