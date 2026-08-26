-- Migration 016: coach annotation layer (core.note).
-- Source: docs/COACH_FEEDBACK.md point 8 and Q9 context, arbitrated by
-- docs/COACH_FEEDBACK_3.md sections 2.4 (annotation layer) and 4 (Phase 0 plumbing).
-- Batch L1 (coaching dashboards) -- do not modify the DDL manually outside migration.

-- WHY. The platform is descriptive only: when a CTL dip shows up in March, nothing in
-- the database explains it (race, illness, travel). Coach 1: "Doesn''t need to be fancy:
-- a date + text table rendered as Grafana annotations." Coach 3 makes it Phase 0 and adds
-- a second role: these rows double as EXCLUSION INPUT for HRV/RHR baselines -- contaminated
-- days (illness, heat) should not drag the rolling baseline that deviation is computed
-- against. Deliberately dumb: no link to activities, no structured fields beyond kind;
-- one row per event, maintained by hand through Grafana or psql.

CREATE TABLE core.note (
    note_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    athlete_id smallint    NOT NULL REFERENCES core.athlete(athlete_id),
    local_date date        NOT NULL,
    kind       text        NOT NULL,
    note       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT note_kind_ck CHECK (kind IN ('race','illness','travel','injury','heat','other'))
);

COMMENT ON TABLE core.note IS
  'Hand-maintained coach annotation layer, rendered as Grafana annotations and overlaid on every trend that needs an explanation (CTL dips, EF drops, HRV crashes). Two consumers, both deliberate (COACH_FEEDBACK point 8 / COACH_FEEDBACK_3 sections 2.2 and 2.4): (1) display -- "why did form collapse in March"; (2) exclusion input for mart.wellness_baseline -- illness/heat days must not contaminate the rolling baseline against which deviation is measured. Kept dumb on purpose: date + kind + free text; no planning engine, no activity foreign key.';

COMMENT ON COLUMN core.note.kind IS
  'Closed vocabulary enforced by CHECK rather than free text: dashboards filter and icon-map on it. ''heat'' exists as its own kind because ambient heat confounds EF and decoupling trends (COACH_FEEDBACK_3 section 3: "every July your EF declines ... with zero fitness change") -- logging heat days lets those panels separate seasonal artifact from fatigue, and lets baseline computation exclude confounded days. ''race'' marks A-races and taper-relevant events; ''illness''/''injury'' are the primary baseline-exclusion kinds; ''other'' catches everything without inventing new categories ad hoc.';

COMMENT ON COLUMN core.note.local_date IS
  'Local attachment date, same convention as every daily key in this schema (materialized at ingestion elsewhere, chosen by the athlete here): the day the event applies to, not the day the row was typed. Joins to mart.* views on local_date without any timezone arithmetic.';
