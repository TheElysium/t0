# Task: Fitness dashboard + power-curve window fix

Status: slices 1-2 committed, gates green, reviewer APPROVE | branch feat/fitness-dashboard | next: athlete manual QA after deploy
Started: 2026-09-16T17:06:02Z

## Spec

Problem
- No dashboard shows mart.pmc or mart.ramp_risk (pmc.json dropped in 20810cc). core.plan_week wired nowhere.
- Power curves (sport-bike panel 5, sport-run panel 9) read mart.power_curve_best, an all-time,
  cross-athlete record view, then filter on local_date: "28 days" = records set in 28 days, not best of 28 days.

Solution
- Slice 1 (AFK): the 8 curve targets aggregate max(value) per duration_s from core.activity_power_curve
  (athlete filter, is_parent = false, NOT spans_pause, window on a.local_date). Regression test first.
- Slice 2 (HITL design checkpoint): new fitness.json (uid t0-fitness). Layout validated by the athlete:
  stat row (CTL, ATL, TSB, dCTL/week, CTL reliable) / PMC full width / ramp rate / weekly TSS + weekly hours
  stacked by discipline with plan line. Chart bump 0.1.38.

Decisions
- TSB = mart.pmc.tsb (reference convention, previous day).
- CTL split: "CTL" when ctl_reliable, "CTL (provisional)" dashed otherwise.
- Form bands: > +25 transition, +5..+25 fresh, -10..+5 grey, -30..-10 optimal, < -30 high risk.
- dCTL bands: ≤3 green, 3-5 yellow, 5-8 orange, >8 red (019_signals.sql).
- Plan: sum target per week, NULL without plan row, never COALESCE to 0.
- Week = date_trunc('week', local_date) (Monday, matches plan_week convention).

Out of scope
- monotony/strain (deferred), per-discipline PMC, eFTP/CP/W', forward TSB projection, compliance table,
  calendar view, pace/HR/swim curves, cross-dashboard links.

## Manual QA (athlete, deployed Grafana, after helm upgrade)
1. Cycling > Power duration curve: 28 d curve ≤ 90 d ≤ 365 d ≤ all time at every duration; 28 d covers all durations of rides in the window.
2. Running > Running power curve: same nesting property.
3. Fitness: stat row shows today's CTL/ATL/TSB matching MCP get_pmc; "CTL reliable" = no.
4. PMC: CTL drawn dashed (provisional) over the whole range; TSB on right axis with 5 coloured bands; daily TSS bars.
5. Ramp rate: bars coloured by 3/5/8 bands.
6. Weekly TSS/hours: stacked swim/bike/run/other; plan line absent on weeks without plan row, present for future weeks with a plan row.

## Subagent metrics
subagent | tokens | tool_uses | duration | retries | review_iterations | gate_failures | outcome
bulk-reader | 82108 | 32 | 231s | 0 | 0 | 0 | inventory (W' claim wrong)
spec-critic | 71974 | 25 | 197s | 0 | 0 | 0 | NEEDS_CLARIFICATION → resolved (defer monotony, QA checklist)
implementer (slice 2) | 80927 | 27 | 279s | 0 | 0 | 0 | fitness.json + chart bump, gates green
gate-keeper | 19636 | 13 | 125s | 0 | 0 | 0 | GREEN (sast = documented gap)
reviewer | 85767 | 26 | 265s | 0 | 1 | 0 | APPROVE (nits: CTL reliable stat width to check visually)
